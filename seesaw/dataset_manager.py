import clip
import pytorch_lightning as pl
import os
import multiprocessing as mp

import clip, torch
import PIL
from operator import itemgetter

from .multigrain import * #SlidingWindow,PyramidTx,non_resized_transform
from .embeddings import * 
import glob
import pandas as pd
from tqdm.auto import tqdm
 
import torch
import torch.nn as nn
from torch.utils.data import Subset

class ExplicitPathDataset(object):
    def __init__(self, root_dir, relative_path_list):
        '''
        Reads images in a directory according to an explicit list.
        '''
        self.root = root_dir
        self.paths = relative_path_list

    def __len__(self):
        return self.paths.shape[0]

    def __getitem__(self, idx):
        relpath = self.paths[idx].lstrip('./')
        image = PIL.Image.open('{}/{}'.format(self.root, relpath))
        return {'file_path':relpath, 'dbidx':idx, 'image':image}


def list_image_paths(basedir, extensions=['jpg', 'jpeg', 'png']):
    acc = []
    for ext in extensions:
        imgs = glob.glob(f'{basedir}/**/*.{ext}', recursive=True)
        acc.extend(imgs)
        
    relative_paths = [ f[len(basedir):].lstrip('./') for f in acc]
    return relative_paths


def preprocess(tup):
    ptx = PyramidTx(tx=non_resized_transform(224), factor=.5, min_size=224)
    ims, sfs = ptx(tup['image'])
    acc = []
    for zoom_level,(im,sf) in enumerate(zip(ims,sfs), start=1):
        acc.append({'file_path':tup['file_path'],'dbidx':tup['dbidx'], 
                    'image':im, 'scale_factor':sf, 'zoom_level':zoom_level})
        
    return acc

class L2Normalize(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, X):
        with torch.cuda.amp.autocast():
            return F.normalize(X, dim=self.dim)

class ImageEmbedding(nn.Module):
    def __init__(self, device, jit):
        super().__init__()
        self.device = device            
        model, _ = clip.load("ViT-B/32", device=device,  jit=jit)
        kernel_size = 224 # changes with variant
        
        ker = nn.Sequential(model.visual, L2Normalize(dim=1))
        self.model = SlidingWindow(ker, kernel_size=kernel_size, stride=kernel_size//2,center=True)

    def forward(self, *, preprocessed_image):
        return self.model(preprocessed_image)    
    
def postprocess_results(acc):
    flat_acc = {'iis':[], 'jjs':[], 'dbidx':[], 'vecs':[], 'zoom_factor':[], 'zoom_level':[],
               'file_path':[]}
    flat_vecs = []

    #{'accs':accs, 'sf':sf, 'dbidx':dbidx, 'zoom_level':zoom_level}
    for item in acc:
        acc0,sf,dbidx,zl,fp = itemgetter('accs', 'scale_factor', 'dbidx', 'zoom_level', 'file_path')(item)
        acc0 = acc0.squeeze(0)
        acc0 = acc0.transpose((1,2,0))

        iis, jjs = np.meshgrid(np.arange(acc0.shape[0], dtype=np.int16), np.arange(acc0.shape[1], dtype=np.int16), indexing='ij')
        #iis = iis.reshape(-1, acc0)
        iis = iis.reshape(-1)
        jjs = jjs.reshape(-1)
        acc0 = acc0.reshape(-1,acc0.shape[-1])
        imids = np.ones_like(iis)*dbidx
        zf = np.ones_like(iis)*(1./sf)
        zl = np.ones_like(iis)*zl

        flat_acc['iis'].append(iis)
        flat_acc['jjs'].append(jjs)
        flat_acc['dbidx'].append(imids)
        flat_acc['vecs'].append(acc0)
        flat_acc['zoom_factor'].append(zf.astype('float32'))
        flat_acc['zoom_level'].append(zl.astype('int16'))
        flat_acc['file_path'].append([fp]*iis.shape[0])

    flat = {}
    for k,v in flat_acc.items():
        flat[k] = np.concatenate(v)

    vecs = flat['vecs']
    del flat['vecs']

    vec_meta = pd.DataFrame(flat)
    # vecs = vecs.astype('float32')
    # vecs = vecs/(np.linalg.norm(vecs, axis=-1, keepdims=True) + 1e-6)
    vec_meta = vec_meta.assign(**get_boxes(vec_meta), vectors=TensorArray(vecs))
    return vec_meta.drop(['iis', 'jjs'],axis=1)
    
class BatchInferModel:
    def __init__(self, model, device):
        self.device = device
        self.model = model 
        
    def __call__(self, batch):
        with torch.no_grad():
            res = []
            for tup in batch:
                im = tup['image']
                del tup['image']
                vectors = self.model(preprocessed_image=im.unsqueeze(0).to(self.device)).to('cpu').numpy()
                tup['accs'] = vectors
                res.append(tup)
        return postprocess_results(res)


#mcc = mini_coco(None)
def worker_function(wid, dataset,slice_size,indices, cpus_per_proc, vector_root):
    wslice = indices[wid*slice_size:(wid+1)*slice_size]
    dataset = Subset(dataset, indices=wslice)
    device = f'cuda:{wid}'
    extract_seesaw_meta(dataset, output_dir=vector_root, output_name=f'part_{wid:03d}', num_workers=cpus_per_proc, 
                    batch_size=3,device=device)
    print(f'Worker {wid} finished.')

class SeesawDatasetManager:
    def __init__(self, root, dataset_name):
        ''' Assumes layout created by create_dataset
        '''
        self.dataset_name = dataset_name
        self.dataset_root = f'{root}/{dataset_name}'
        file_meta = pd.read_parquet(f'{self.dataset_root}/file_meta.parquet')
        self.file_meta = file_meta
        self.paths = file_meta['file_path'].values
        self.image_root = f'{self.dataset_root}/images'
        
        
    def get_pytorch_dataset(self):
        return ExplicitPathDataset(root_dir=self.image_root, relative_path_list=self.paths)
        
    def preprocess(self, num_subproc=-1):
        ''' Will run a preprocess pipeline and store the output
            -1: use all gpus
            0: use one gpu process and preprocessing all in the local process 
        '''
        dataset = self.get_pytorch_dataset()

        vector_root= self.vector_path()
        os.makedirs(vector_root, exist_ok=True)

        if num_subproc == -1:
            num_subproc = torch.cuda.device_count()
            
        jobs = []
        if num_subproc == 0:
            worker_function(0, dataset, slice_size=len(dataset), 
                indices=np.arange(len(dataset)), cpus_per_proc=0, vector_root=vector_root)
        else:
            indices = np.random.permutation(len(dataset))
            slice_size = int(math.ceil(indices.shape[0]/num_subproc))
            cpus_per_proc = os.cpu_count()//num_subproc if num_subproc > 0 else 0

            for i in range(num_subproc):
                p = mp.Process(target=worker_function, args=(i,dataset, slice_size, indices, cpus_per_proc, vector_root))
                jobs.append(p)
                p.start()

            for p in jobs:
                p.join()

    def vector_path(self):
        meta_root = f'{self.dataset_root}/meta'
        vector_root = f'{meta_root}/vectors'
        return vector_root


"""
Expected data layout for a seesaw root with a few datasets:
/workdir/seesaw_data
├── coco  ### not yet preproceseed, just added
│   ├── file_meta.parquet
│   ├── images -> /workdir/datasets/coco
├── coco5 ### preprocessed
│   ├── file_meta.parquet
│   ├── images -> /workdir/datasets/coco
│   └── meta
│       └── vectors
│           ├── part_000.parquet
│           └── part_001.parquet
├── mini_coco
│   ├── file_meta.parquet
│   ├── images -> /workdir/datasets/mini_coco/images
│   └── meta
│       └── vectors
│           ├── part_000.parquet
│           └── part_001.parquet
"""

class GlobalDataManager:
    def __init__(self, root):
        if not os.path.exists(root):
            print('creating new data folder')
            os.makedirs(root)
        else:
            assert os.path.isdir(root)
            
        self.root = root
        
    def list_datasets(self):
        return os.listdir(self.root)
    
    def create_dataset(self, image_src, dataset_name, paths=[]):
        '''
            if not given explicit paths, it assumes every jpg, jpeg and png is wanted
        '''
        assert os.path.isdir(image_src)
        dspath = f'{self.root}/{dataset_name}'
        assert not os.path.exists(dspath), 'name already used'
        os.mkdir(dspath)
        image_path = f'{dspath}/images'
        os.symlink(os.path.abspath(image_src), image_path)
        if len(paths) == 0:
            paths = list_image_paths(image_src)
            
        df = pd.DataFrame({'file_path':paths})
        df.to_parquet(f'{dspath}/file_meta.parquet')
            
    def get_dataset(self, dataset_name):
        assert dataset_name in self.list_datasets(), 'must create it first'
        ## TODO: cache this representation
        return SeesawDatasetManager(self.root, dataset_name)


def iden(x):
    return x

def extract_seesaw_meta(dataset, output_dir, output_name, num_workers, batch_size, device):
    emb = ImageEmbedding(device=device, jit=False)    
    bim = BatchInferModel(emb, device) 
    assert os.path.isdir(output_dir)
    
    txds = TxDataset(dataset, tx=preprocess)
    dl = DataLoader(txds, num_workers=num_workers, shuffle=False,
                    batch_size=batch_size, collate_fn=iden)
    res = []
    for batch in tqdm(dl):
        flat_batch = sum(batch,[])
        batch_res = bim(flat_batch)
        res.append(batch_res)

    merged_res = pd.concat(res, ignore_index=True)
    merged_res.to_parquet(f'{output_dir}/{output_name}.parquet')

def preprocess_dataset(*, image_src, seesaw_root, dataset_name):
    mp.set_start_method('spawn')
    gdm = GlobalDataManager(seesaw_root)
    gdm.create_dataset(image_src=image_src, dataset_name=dataset_name)
    mc = gdm.get_dataset(dataset_name)
    mc.preprocess()