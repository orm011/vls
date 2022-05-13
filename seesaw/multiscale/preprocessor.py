from seesaw.definitions import resolve_path
from ..models.model import ImageEmbedding
import pandas as pd
from ray.data.extensions import TensorArray
import ray
import io
import torch
import PIL
import os
import PIL.Image
from .multiscale_index import PyramidTx, non_resized_transform, get_boxes
from operator import itemgetter
import numpy as np


def preprocess(tup, factor):
    """meant to preprocess dict with {path, dbidx,image}"""
    ptx = PyramidTx(tx=non_resized_transform(224), factor=factor, min_size=224)
    ims, sfs = ptx(tup["image"])
    acc = []
    for zoom_level, (im, sf) in enumerate(zip(ims, sfs), start=1):
        acc.append(
            {
                "file_path": tup["file_path"],
                "dbidx": tup["dbidx"],
                "image": im,
                "scale_factor": sf,
                "zoom_level": zoom_level,
            }
        )

    return acc


def postprocess_results(acc):
    flat_acc = {
        "iis": [],
        "jjs": [],
        "dbidx": [],
        "vecs": [],
        "zoom_factor": [],
        "zoom_level": [],
        "file_path": [],
    }
    flat_vecs = []

    # {'accs':accs, 'sf':sf, 'dbidx':dbidx, 'zoom_level':zoom_level}
    for item in acc:
        acc0, sf, dbidx, zl, fp = itemgetter(
            "accs", "scale_factor", "dbidx", "zoom_level", "file_path"
        )(item)
        acc0 = acc0.squeeze(0)
        acc0 = acc0.transpose((1, 2, 0))

        iis, jjs = np.meshgrid(
            np.arange(acc0.shape[0], dtype=np.int16),
            np.arange(acc0.shape[1], dtype=np.int16),
            indexing="ij",
        )
        # iis = iis.reshape(-1, acc0)
        iis = iis.reshape(-1)
        jjs = jjs.reshape(-1)
        acc0 = acc0.reshape(-1, acc0.shape[-1])
        imids = np.ones_like(iis) * dbidx
        zf = np.ones_like(iis) * (1.0 / sf)
        zl = np.ones_like(iis) * zl

        flat_acc["iis"].append(iis)
        flat_acc["jjs"].append(jjs)
        flat_acc["dbidx"].append(imids)
        flat_acc["vecs"].append(acc0)
        flat_acc["zoom_factor"].append(zf.astype("float32"))
        flat_acc["zoom_level"].append(zl.astype("int16"))
        flat_acc["file_path"].append([fp] * iis.shape[0])

    flat = {}
    for k, v in flat_acc.items():
        flat[k] = np.concatenate(v)

    vecs = flat["vecs"]
    del flat["vecs"]

    vec_meta = pd.DataFrame(flat)
    # vecs = vecs.astype('float32')
    # vecs = vecs/(np.linalg.norm(vecs, axis=-1, keepdims=True) + 1e-6)
    vec_meta = vec_meta.assign(**get_boxes(vec_meta), vectors=TensorArray(vecs))
    return vec_meta.drop(["iis", "jjs"], axis=1)


class BatchInferModel:
    def __init__(self, model, device):
        self.device = device
        self.model = model

    def __call__(self, batch):
        with torch.no_grad():
            res = []
            for tup in batch:
                im = tup["image"]
                del tup["image"]
                vectors = (
                    self.model(preprocessed_image=im.unsqueeze(0).to(self.device))
                    .to("cpu")
                    .numpy()
                )
                tup["accs"] = vectors
                res.append(tup)

        if len(res) == 0:
            return []
        else:
            return [postprocess_results(res)]


from ..util import reset_num_cpus


class Preprocessor:
    def __init__(self, jit_path, output_dir, meta_dict):
        print(
            f"Init preproc. Avail gpus: {ray.get_gpu_ids()}. cuda avail: {torch.cuda.is_available()}"
        )

        self.num_cpus = int(os.environ.get("OMP_NUM_THREADS"))
        self.device = "cuda:0" if len(ray.get_gpu_ids()) > 0 else "cpu"

        reset_num_cpus(self.num_cpus)
        emb = ImageEmbedding(device=self.device, jit_path=jit_path)
        self.bim = BatchInferModel(emb, self.device)
        self.output_dir = output_dir
        self.meta_dict = meta_dict

    # def extract_meta(self, dataset, indices):
    def extract_meta(self, ray_dataset, pyramid_factor, part_id):
        # dataset = Subset(dataset, indices=indices)
        # txds = TxDataset(dataset, tx=preprocess)

        meta_dict = self.meta_dict

        def fix_meta(ray_tup):
            fullpath, binary = ray_tup
            p = os.path.realpath(fullpath)
            file_path, dbidx = meta_dict[p]
            return {"file_path": file_path, "dbidx": dbidx, "binary": binary}

        def full_preproc(tup):
            ray_tup = fix_meta(tup)
            try:
                image = PIL.Image.open(io.BytesIO(ray_tup["binary"]))
            except PIL.UnidentifiedImageError:
                print(f'error parsing binary {ray_tup["file_path"]}')
                ## some images are corrupted / not copied properly
                ## it is easier to handle that softly
                image = None

            del ray_tup["binary"]
            if image is None:
                return []  # empty list ok?
            else:
                ray_tup["image"] = image
                return preprocess(ray_tup, factor=pyramid_factor)

        def preproc_batch(b):
            return [full_preproc(tup) for tup in b]

        dl = ray_dataset.window(blocks_per_window=20).map_batches(
            preproc_batch, batch_size=20
        )
        res = []
        for batch in dl.iter_rows():
            batch_res = self.bim(batch)
            res.extend(batch_res)
        # dl = DataLoader(txds, num_workers=1, shuffle=False,
        #                 batch_size=1, collate_fn=iden)
        # res = []
        # for batch in dl:
        #     flat_batch = sum(batch,[])
        #     batch_res = self.bim(flat_batch)
        #     res.append(batch_res)

        merged_res = pd.concat(res, ignore_index=True)
        ofile = f"{self.output_dir}/part_{part_id:04d}.parquet"

        ### TMP: parquet does not allow half prec.
        x = merged_res
        x = x.assign(vectors=TensorArray(x["vectors"].to_numpy().astype("single")))
        x.to_parquet(ofile)
        return ofile


import ray

from ..dataset import SeesawDatasetManager
import math
import shutil


def preprocess_dataset(
    sds: SeesawDatasetManager,
    model_path,
    output_path,
    cpu=False,
    pyramid_factor=0.5,
):
    dataset = sds.get_pytorch_dataset()
    output_path = resolve_path(output_path)
    assert not os.path.exists(output_path), "output path already exists"
    model_path = resolve_path(model_path)
    assert os.path.exists(model_path), "model path doesnt exist"

    dirname = os.path.basename(output_path)
    dirpath = os.path.dirname(output_path)
    output_path = f"{dirpath}/.tmp.{dirname}"
    final_output_path = f"{dirpath}/{dirname}"

    os.makedirs(dirpath, exist_ok=True)

    if os.path.exists(output_path):  # remove old tmpfile
        shutil.rmtree(output_path)

    vector_path = f"{output_path}/vectors"
    os.makedirs(vector_path)

    model_link = f"{output_path}/model"
    os.symlink(model_path, model_link)

    dataset_link = f"{output_path}/dataset"
    os.symlink(dataset.root, dataset_link)

    real_prefix = f"{os.path.realpath(sds.image_root)}/"
    read_paths = ((real_prefix + sds.paths)).tolist()
    read_paths = [os.path.normpath(p) for p in read_paths]
    meta_dict = dict(zip(read_paths, zip(sds.paths, np.arange(len(sds.paths)))))

    actors = []
    try:
        print("starting actors...")
        ngpus = ray.available_resources().get("GPU", 0)
        ngpus = math.floor(ngpus)

        nactors = ngpus if ngpus > 0 else 1
        actors = [
            ray.remote(Preprocessor)
            .options(num_cpus=5, num_gpus=(1 if ngpus > 0 else 0))
            .remote(jit_path=model_link, output_dir=vector_path, meta_dict=meta_dict)
            for i in range(nactors)
        ]

        rds = ray.data.read_binary_files(
            paths=read_paths, include_paths=True, parallelism=400
        ).split(nactors, locality_hints=actors)

        res_iter = []
        for part_id, (actor, shard) in enumerate(zip(actors, rds)):
            of = actor.extract_meta.remote(shard, pyramid_factor, part_id)
            res_iter.append(of)
        ray.get(res_iter)
        print(f"finished, renaming to {final_output_path}")
        os.rename(output_path, final_output_path)
    finally:
        print("shutting down actors...")
        for a in actors:
            ray.kill(a)
