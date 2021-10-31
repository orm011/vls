#from .data_server import *
from .search_loop_models import *
from .search_loop_tools import *

import inspect
from .dataset_tools import *
from .vloop_dataset_loaders import EvDataset, get_class_ev
from .fine_grained_embedding import *
from .multigrain import *
from .cross_modal_db import EmbeddingDB
from .search_loop_models import adjust_vec, adjust_vec2
import numpy as np
import sklearn.metrics
import math
from .util import *

import importlib

# ignore this comment

def vls_init_logger():
    import logging
    logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)
    logging.captureWarnings(True)


import numpy as np

def readjust_interval(x1, x2, max_x):
    left_excess = -np.clip(x1,-np.inf,0)
    right_excess = np.clip(x2 - max_x, 0,np.inf)

    offset = left_excess - right_excess

    x1p = x1 + offset
    x2p = x2 + offset
    x1p = np.clip(x1p,0,max_x) # trim any approx error
    x2p = np.clip(x2p,0,max_x) # same

    assert np.isclose(x2p - x1p, x2 - x1).all()
    assert (x1p >= 0).all(), x1p
    assert (x2p <= max_x).all(), x2p
    return x1p, x2p

def random_seg_start(x1, x2, target_x, max_x, off_center_range, n=1):
    dist = x2 - x1
    # assert (dist <= target_x).all(), dont enforce containment
    center = (x2 + x1)/2

    rel_offset = (np.random.rand(n) - .5)*off_center_range

    ## perturb offset a bit. but do keep center within crop
    if (dist < target_x).all():
        offset = rel_offset * (target_x - dist) * .5
    else:
        assert (dist >= target_x).all() # figure out what to do when some are and some arent.
        offset = rel_offset * target_x * .5
    
    start = center - target_x*.5
    start = start + offset
    end = start + target_x
    start, end = readjust_interval(start, end, max_x)
    assert np.isclose(end - start, target_x).all()
    assert (start <= center).all()
    assert (center <= end).all()
    return start, end

def add_clearance(x1,x2,max_x, clearance_ratio):
    cx = (x1 + x2)*.5
    dx = x2 - x1
    diff = dx*clearance_ratio*.5
    return readjust_interval(cx - diff, cx + diff, max_x)

def add_box_clearance(b, max_x, max_y, clearance_ratio):
    x1,x2 = add_clearance(b.x1, b.x2, max_x, clearance_ratio)
    y1,y2 = add_clearance(b.y1, b.y2, max_y, clearance_ratio)
    return {'x1':x1, 'x2':x2, 'y1':y1, 'y2':y2}

def random_container_box(b, scale_range=3.3, aspect_ratio_range=1.2, off_center_range=1., clearance=1.2, n=1):
    assert clearance >= aspect_ratio_range

    bw = b.x2 - b.x1
    bh = b.y2 - b.y1
    max_d = max(bw,bh)
    max_len = min(b.im_height, b.im_width)
    img_scale = max_len/max_d

    min_scale = min(img_scale, clearance)
    max_scale = min(img_scale, scale_range*clearance) # don't do more than 3x
    assert img_scale >= max_scale >= min_scale

    scale = np.exp(np.random.rand(n)*np.log(max_scale/min_scale))*min_scale
    # assert (scale >= clearance).all()
    # assert (scale <= scale_range).all()

    target_x = scale*max_d
    target_y = target_x
    assert ((bw <= target_x) | (target_x == max_len)).all()
    assert ((bh <= target_y) | (target_y == max_len)).all()
    
    if False:
        lratio = 2*(np.random.rand(n) - .5)*np.log(aspect_ratio_range)
        ratio = np.exp(lratio/2)

        upper = math.sqrt(aspect_ratio_range)
        assert (ratio <= upper).all()
        assert (ratio >= 1/upper).all()

        ## TODO: after adjusting the box, it is possible that we violate prevously valid constraints wrt. 
        ## the object box or wrt the containing image. The ratio limits need to be bound based on these constraints
        ## before applying randomness
    else:
        ratio = 1.


    target_y = target_y*ratio
    target_x = target_x/ratio #np.ones_like(ratio)
    start_x, end_x = random_seg_start(b.x1, b.x2, target_x, b.im_width, off_center_range=off_center_range, n=n)
    start_y, end_y = random_seg_start(b.y1, b.y2, target_y, b.im_height, off_center_range=off_center_range, n=n)
    
    assert ((bw > target_x) | (start_x <= b.x1)).all()
    assert ((bw > target_x) | (end_x >= b.x2)).all()

    return pd.DataFrame({'x1':start_x, 'x2': end_x, 'y1':start_y, 'y2':end_y})

def randomly_extended_crop(im, box, scale_range, aspect_ratio_range, off_center_range, clearance, n):
    rbs = random_container_box(box, scale_range, aspect_ratio_range, off_center_range, clearance, n=n)
    crs = []
    for cb in rbs.itertuples():
        cr = im.crop((cb.x1, cb.y1, cb.x2, cb.y2))
        crs.append(cr)
    return crs

def process_crops(crs, tx, embedding):
    if len(crs) == 0:
        return np.zeros((0,512))

    tensors = []
    for cr in crs:
        cr = cr.resize((224,224), resample=3)
        ts = tx(cr)
        tensors.append(ts)

    allts = torch.stack(tensors)

    emvecs = []
    bs = 20
    for i in range(0,len(allts), bs):
        batch = allts[i:i+bs]
        embs = embedding.from_image(preprocessed_image=batch, pooled='bypass')
        emvecs.append(embs)

    embs = np.concatenate(emvecs)
    return embs

import time

_clip_tx = T.Compose([
                    T.ToTensor(), 
                    T.Normalize((0.48145466, 0.4578275, 0.40821073), (0.26862954, 0.26130258, 0.27577711)),
                    lambda x : x.type(torch.float16)
                    ])


def run_loop(*, ev :EvDataset, category, qstr, interactive, warm_start, n_batches, batch_size, minibatch_size, 
              learning_rate, max_examples, num_epochs, loss_margin, 
              tqdm_disabled:bool, granularity:str,
               positive_vector_type, n_augment, min_box_size=10,
               model_type='logistic', solver_opts={}, 
               **kwargs):         
    assert 'fine_grained' not in kwargs
    assert isinstance(granularity, str)
    assert positive_vector_type in ['image_only', 'image_and_vec', 'vec_only', None]
    ev0 = ev
    frame = inspect.currentframe()
    args, _, _, values = inspect.getargvalues(frame)
    allargs = {k:v for (k,v) in values.items() if k in args and k not in ['ev', 'gvec', 'gvec_meta']} 
    print(allargs, kwargs)
    rtup = {**allargs, **kwargs}       

    catgt = ev0.query_ground_truth[category]
    class_idxs = catgt[~catgt.isna()].index.values
    if class_idxs.shape[0] == 0:
        print(f'No labelled frames for class "{category}" found ')
        return (rtup, None)

    ev, class_idxs = get_class_ev(ev0, category, boxes=True)
    dfds =  DataFrameDataset(ev.box_data[ev.box_data.category == category], index_var='dbidx', max_idx=class_idxs.shape[0]-1)
    gt = ev.query_ground_truth[category].values

    #rsz = resize_to_grid(224)
    # ds = TxDataset(dfds, tx=lambda tup : rsz(im=None, boxes=tup)[1])
    # imds = TxDataset(ev.image_dataset, tx = lambda im : rsz(im=im, boxes=None)[0])

    ds = dfds
    imds = ev.image_dataset

    if granularity == 'fine':
        vec_meta = ev.fine_grained_meta
        vecs = ev.fine_grained_embedding
        vec_meta = vec_meta[vec_meta.zoom_level == 0]
        vecs = vecs[vec_meta.index.values]
        vec_meta.reset_index(drop=True)
        hdb = FineEmbeddingDB(raw_dataset=ev.image_dataset, embedding=ev.embedding, 
            embedded_dataset=vecs, vector_meta=vec_meta)
    elif granularity == 'multi':
        vec_meta = ev.fine_grained_meta
        vecs = ev.fine_grained_embedding
        hdb = AugmentedDB(raw_dataset=ev.image_dataset, embedding=ev.embedding, 
            embedded_dataset=vecs, vector_meta=vec_meta, vec_index=ev.vec_index)
    elif granularity == 'coarse':
        dbidxs = np.arange(len(ev)).astype('int')
        vec_meta = pd.DataFrame({'iis': np.zeros_like(dbidxs), 'jjs':np.zeros_like(dbidxs), 'dbidx':dbidxs})
        vecs = ev.embedded_dataset
        hdb = EmbeddingDB(raw_dataset=ev.image_dataset, embedding=ev.embedding,embedded_dataset=vecs)
    else:
        assert False

    bfq = BoxFeedbackQuery(hdb, batch_size=batch_size, auto_fill_df=None)

    if qstr == 'nolang':
        init_vec=None
        init_mode = 'random'
    else:
        init_vec = ev.embedding.from_string(string=qstr)
        init_mode = 'dot'
    
    acc_pos = []
    acc_neg = []
    acc_indices = []
    acc_results = []
    acc_vecs = [np.zeros((0,512))]
            
    res = {'indices':acc_indices, 'results':acc_results}#, 'gt':gt.values.copy()}
    if init_vec is not None:
        init_vec = init_vec/np.linalg.norm(init_vec)
        tvec = init_vec#/np.linalg.norm(init_vec)
    else:
        tvec = None

    tmode = init_mode


    latency_profile = []
    for i in tqdm(range(n_batches), leave=False, disable=tqdm_disabled):
        lp = {'batch_no':i, 'n_images':None, 'n_posvecs':None, 'n_negvecs':None,
                                    'lookup':None, 'label':None, 'refine':None, }    
        latency_profile.append(lp)

        start_lookup = time.time()
        idxbatch, _ = bfq.query_stateful(mode=tmode, vector=tvec, batch_size=batch_size)
        lp['n_images'] = idxbatch.shape[0]

        start_label = time.time()
        lp['lookup'] = start_label - start_lookup


        if idxbatch.shape[0] > 0: 
            acc_indices.append(idxbatch.copy()) # trying to figure out leak
            acc_results.append(gt[idxbatch].copy())

        if idxbatch.shape[0] == 0 or i == (n_batches - 1):
            break

        box_dict = {}
        for idx in idxbatch:
            box_dict[idx] = ds[idx]

        if interactive != 'plain':
            if granularity in ['fine', 'multi']:
                batchpos, batchneg = get_pos_negs_all_v2(idxbatch, box_dict, vec_meta)
                lp['n_posvecs'] = len(batchpos)#.shape[0]
                lp['n_negvecs'] = len(batchneg)#.shape[0]
                ## we are currently ignoring these positives
                acc_pos.append(batchpos)
                acc_neg.append(batchneg)

                pos = pr.BitMap.union(*acc_pos)
                neg = pr.BitMap.union(*acc_neg)

                if positive_vector_type in ['image_only', 'image_and_vec']:
                    crs = []
                    for idx in idxbatch:
                        boxes = ds[idx]
                        widths = (boxes.x2 - boxes.x1)
                        heights = (boxes.y2 - boxes.y1)
                        boxes = boxes[(widths >= min_box_size) & (heights >= min_box_size)]

                        if boxes.shape[0] == 0:
                            continue
                        # only read image if there is something
                        im = imds[idx]

                        for b in boxes.itertuples():
                            if n_augment > 1:
                                pcrs = randomly_extended_crop(im, b, scale_range=3., aspect_ratio_range=1., off_center_range=1., 
                                        clearance=1.3, n=n_augment)
                                for cr in pcrs:
                                    cr = T.RandomHorizontalFlip()(cr)
                                    crs.append(cr)
                            else:
                                pcrs = randomly_extended_crop(im, b, scale_range=1., aspect_ratio_range=1., off_center_range=0., 
                                        clearance=1.5, n=1)

                                for cr in pcrs:
                                    crs.append(cr)
                                                  
                    tmp = process_crops(crs, _clip_tx, ev.embedding)
                    acc_vecs.append(tmp)
                    impos = np.concatenate(acc_vecs)

                if positive_vector_type == 'image_only':
                    allpos = impos  
                if positive_vector_type == 'vec_only':
                    allpos = vecs[pos]
                elif positive_vector_type == 'image_and_vec':
                    allpos = np.concatenate([impos, vecs[pos]])

                Xt = np.concatenate([allpos, vecs[neg]])
                yt = np.concatenate([np.ones(len(allpos)), np.zeros(len(neg))])

                # not really valid. some boxes are area 0. they should be ignored.but they affect qgt
                # if np.concatenate(acc_results).sum() > 0:
                #    assert len(pos) > 0
            else:
                Xt = vecs[idxbatch]
                yt = gt[idxbatch]
                lp['n_posvecs'] = (yt == 1).sum()#.shape[0]
                lp['n_negvecs'] = (yt != 1).sum()


            start_refine = time.time()
            lp['label'] = start_refine - start_label

            if (yt.shape[0] > 0) and (yt.max() > yt.min()):
                tmode = 'dot'
                if interactive == 'sklearn':
                    lr = sklearn.linear_model.LogisticRegression(class_weight='balanced')
                    lr.fit(Xt, yt)
                    tvec = lr.coef_.reshape(1,-1)        
                elif interactive == 'pytorch':
                    p = yt.sum()/yt.shape[0]
                    w = np.clip((1-p)/p, .1, 10.)

                    if model_type == 'logistic':
                        mod = PTLogisiticRegression(Xt.shape[1], learning_rate=learning_rate, C=0, 
                                                    positive_weight=w)
                        if warm_start == 'warm':
                            iv = torch.from_numpy(init_vec)
                            iv = iv / iv.norm()
                            mod.linear.weight.data = iv.type(mod.linear.weight.dtype)
                        elif warm_start == 'default':
                            pass

                        fit_reg(mod=mod, X=Xt.astype('float32'), y=yt.astype('float'), batch_size=minibatch_size)
                        tvec = mod.linear.weight.detach().numpy().reshape(1,-1)
                    elif model_type in ['cosine', 'multirank']:
                        for i in range(num_epochs):
                            tvec = adjust_vec(tvec, Xt, yt, learning_rate=learning_rate, 
                                                max_examples=max_examples, 
                                                minibatch_size=minibatch_size,
                                                loss_margin=loss_margin)
                    elif model_type == 'solver':
                        tvec = adjust_vec2(tvec, Xt, yt, max_examples=p.max_examples, loss_margin=p.loss_margin, **solver_opts)
                    else:
                        assert False, 'model type'

                else:
                    assert False
                lp['refine'] = time.time() - start_refine
            else:
                # print('missing positives or negatives to do any training', yt.shape, yt.max(), yt.min())
                pass

    res['indices'] = [class_idxs[r] for r in res['indices']]
    tup = {**allargs, **kwargs}
    res['latency_profile'] = latency_profile
    return (tup, res)


from dataclasses import dataclass,field
## used only to make life easier
@dataclass(frozen=True)
class LoopParams:
    interactive : str
    warm_start : str
    batch_size : int
    minibatch_size : int
    learning_rate : float
    max_examples : int
    loss_margin : float
    tqdm_disabled : bool
    granularity : str
    positive_vector_type : str
    num_epochs : int
    n_augment : int
    min_box_size : int = 10
    model_type : int = 'logistic'
    solver_opts : dict = None

@dataclass
class LoopState:
    tvec : np.ndarray = None
    tmod : str = None
    acc_vecs : list = field(default_factory=list) #[np.zeros((0,512))]
    latency_profile : list = field(default_factory=list)
    acc_pos : list = field(default_factory=list)
    acc_neg : list = field(default_factory=list)

class SeesawLoop:
    bfq : BoxFeedbackQuery
    params : LoopParams
    state : LoopState
    vecs : np.ndarray
    vec_meta : pd.DataFrame

    def __init__(self, ev : EvDataset, params : LoopParams):
        self.ev = ev
        self.params = params
        self.state = LoopState()

    def initialize(self, qstr : str):
        """
        sets up initial state. must be called ahead of everything else
        """
        ev = self.ev
        p = self.params
        s = self.state

        if p.granularity == 'fine':
            vec_meta = ev.fine_grained_meta
            vecs = ev.fine_grained_embedding
            vec_meta = vec_meta[vec_meta.zoom_level == 0]
            vecs = vecs[vec_meta.index.values]
            vec_meta.reset_index(drop=True)
            hdb = FineEmbeddingDB(raw_dataset=ev.image_dataset, embedding=ev.embedding, 
                embedded_dataset=vecs, vector_meta=vec_meta)
        elif p.granularity == 'multi':
            vec_meta = ev.fine_grained_meta
            vecs = ev.fine_grained_embedding
            hdb = AugmentedDB(raw_dataset=ev.image_dataset, embedding=ev.embedding, 
                embedded_dataset=vecs, vector_meta=vec_meta, vec_index=ev.vec_index)
        elif p.granularity == 'coarse':
            dbidxs = np.arange(len(ev)).astype('int')
            vec_meta = pd.DataFrame({'iis': np.zeros_like(dbidxs), 'jjs':np.zeros_like(dbidxs), 'dbidx':dbidxs})
            vecs = ev.embedded_dataset
            hdb = EmbeddingDB(raw_dataset=ev.image_dataset, embedding=ev.embedding,embedded_dataset=vecs)
        else:
            assert False

        bfq = BoxFeedbackQuery(hdb, batch_size=p.batch_size, auto_fill_df=None)
        self.vec_meta = vec_meta
        self.vecs = vecs
        self.hdb = hdb
        self.bfq = bfq

        if qstr == 'nolang':
            s.tvec = None
            s.tmode = 'random'
        else:
            init_vec = ev.embedding.from_string(string=qstr)
            init_vec = init_vec/np.linalg.norm(init_vec)
            s.tvec = init_vec
            s.tmode = 'dot'
        
        #res = {'indices':acc_indices, 'results':acc_results}#, 'gt':gt.values.copy()}

    def next_batch(self):
        """
        gets next batch of image indices based on current vector
        """
        start_lookup = time.time()

        s = self.state
        p = self.params
        lp = {'n_images':None, 'n_posvecs':None, 'n_negvecs':None,
                                    'lookup':None, 'label':None, 'refine':None, }    
        s.latency_profile.append(lp)

        idxbatch, _ = self.bfq.query_stateful(mode=s.tmode, vector=s.tvec, batch_size=p.batch_size)
        lp['n_images'] = idxbatch.shape[0]
        lp['lookup'] = time.time() - start_lookup
        return idxbatch

    def refine(self, idxbatch : np.array, box_dict : dict):
        """
        update based on vector. box dict will have every index from idx batch, including empty dfs.
        """
        assert idxbatch.shape[0] == len(box_dict)

        start_refine = time.time()

        p = self.params
        s = self.state
        lp = s.latency_profile[-1]
        
        lp['label'] = start_refine - lp['lookup']

        if p.interactive != 'plain':
            if p.granularity in ['fine', 'multi']:
                batchpos, batchneg = get_pos_negs_all_v2(idxbatch, box_dict, self.vec_meta)
                lp['n_posvecs'] = len(batchpos)#.shape[0]
                lp['n_negvecs'] = len(batchneg)#.shape[0]
                ## we are currently ignoring these positives
                s.acc_pos.append(batchpos)
                s.acc_neg.append(batchneg)

                pos = pr.BitMap.union(*s.acc_pos)
                neg = pr.BitMap.union(*s.acc_neg)


                if p.positive_vector_type == 'vec_only':
                    allpos = self.vecs[pos]
                elif p.positive_vector_type in ['image_only', 'image_and_vec']:
                    crs = []
                    for idx in idxbatch:
                        boxes = box_dict[idx]
                        widths = (boxes.x2 - boxes.x1)
                        heights = (boxes.y2 - boxes.y1)
                        boxes = boxes[(widths >= p.min_box_size) & (heights >= p.min_box_size)]

                        if boxes.shape[0] == 0:
                            continue

                        # only read image if there was something
                        im = self.ev.image_dataset[idx]

                        for b in boxes.itertuples():
                            if p.n_augment > 1:
                                pcrs = randomly_extended_crop(im, b, scale_range=3., aspect_ratio_range=1., off_center_range=1., 
                                        clearance=1.3, n=p.n_augment)
                                for cr in pcrs:
                                    cr = T.RandomHorizontalFlip()(cr)
                                    crs.append(cr)
                            else:
                                pcrs = randomly_extended_crop(im, b, scale_range=1., aspect_ratio_range=1., off_center_range=0., 
                                        clearance=1.5, n=1)

                                for cr in pcrs:
                                    crs.append(cr)
                                                  
                    tmp = process_crops(crs, _clip_tx, self.vecs)
                    s.acc_vecs.append(tmp)
                    impos = np.concatenate(s.acc_vecs)

                    if p.positive_vector_type == 'image_only':
                        allpos = impos  
                    elif p.positive_vector_type == 'image_and_vec':
                        allpos = np.concatenate([impos, self.vecs[pos]])
                    else:
                        assert False
                else:
                    assert False

                Xt = np.concatenate([allpos, self.vecs[neg]])
                yt = np.concatenate([np.ones(len(allpos)), np.zeros(len(neg))])
                # not really valid. some boxes are area 0. they should be ignored.but they affect qgt
                # if np.concatenate(acc_results).sum() > 0:
                #    assert len(pos) > 0
            else:
                Xt = self.vecs[idxbatch]
                yt = np.array([box_dict[idx].shape[0] > 0 for idx in idxbatch])
                # yt = gt[idxbatch]
                lp['n_posvecs'] = (yt == 1).sum()#.shape[0]
                lp['n_negvecs'] = (yt != 1).sum()

            if (yt.shape[0] > 0) and (yt.max() > yt.min()):
                s.tmode = 'dot'
                if p.interactive == 'sklearn':
                    lr = sklearn.linear_model.LogisticRegression(class_weight='balanced')
                    lr.fit(Xt, yt)
                    s.tvec = lr.coef_.reshape(1,-1)        
                elif p.interactive == 'pytorch':
                    prob = yt.sum()/yt.shape[0]
                    w = np.clip((1-prob)/prob, .1, 10.)

                    if p.model_type == 'logistic':
                        mod = PTLogisiticRegression(Xt.shape[1], learning_ratep=p.learning_rate, C=0, 
                                                    positive_weight=w)
                        if p.warm_start == 'warm':
                            iv = torch.from_numpy(s.tvec)
                            iv = iv / iv.norm()
                            mod.linear.weight.data = iv.type(mod.linear.weight.dtype)
                        elif p.warm_start == 'default':
                            pass

                        fit_reg(mod=mod, X=Xt.astype('float32'), y=yt.astype('float'), batch_size=p.minibatch_size)
                        s.tvec = mod.linear.weight.detach().numpy().reshape(1,-1)
                    elif p.model_type in ['cosine', 'multirank']:
                        for i in range(p.num_epochs):
                            s.tvec = adjust_vec(s.tvec, Xt, yt, learning_rate=p.learning_rate, 
                                                max_examples=p.max_examples, 
                                                minibatch_size=p.minibatch_size,
                                                loss_margin=p.loss_margin)
                    elif p.model_type == 'solver':
                        s.tvec = adjust_vec2(s.tvec, Xt, yt, **p.solver_opts)
                    else:
                        assert False, 'model type'
                else:
                    assert False
            else:
                # print('missing positives or negatives to do any training', yt.shape, yt.max(), yt.min())
                pass

            lp['refine'] = time.time() - start_refine


def run_loop2(*, ev :EvDataset, n_batches, tqdm_disabled:bool, category, qstr,
                interactive, warm_start, batch_size, minibatch_size, 
              learning_rate, max_examples, num_epochs, loss_margin, 
               granularity:str, positive_vector_type, n_augment,min_box_size=10,
               model_type='logistic', solver_opts={}, **kwargs):     
    assert positive_vector_type in ['image_only', 'image_and_vec', 'vec_only', None]
    ev0 = ev
    frame = inspect.currentframe()
    args, _, _, values = inspect.getargvalues(frame)
    params = {k:v for (k,v) in values.items() if k in args and k not in ['ev', 'category', 'qstr', 'n_batches']} 
    rtup = {**{'category':category, 'qstr':qstr}, **params, **kwargs}       
    print('run loop 2', rtup)

    catgt = ev0.query_ground_truth[category]
    class_idxs = catgt[~catgt.isna()].index.values
    if class_idxs.shape[0] == 0:
        print(f'No labelled frames for class "{category}" found ')
        return (rtup, None)

    ev, class_idxs = get_class_ev(ev0, category, boxes=True)
    ds =  DataFrameDataset(ev.box_data[ev.box_data.category == category], index_var='dbidx', max_idx=class_idxs.shape[0]-1)
    gt = ev.query_ground_truth[category].values


    params = LoopParams(**params)

    max_results = gt.sum()
    assert max_results > 0
    
    acc_indices = []
    acc_results = []

    total_results = 0
    loop = SeesawLoop(ev, params)
    loop.initialize(qstr)
    for i in tqdm(range(n_batches),leave=False, disable=tqdm_disabled):
        idxbatch = loop.next_batch()
        
        # ran out of batches
        if idxbatch.shape[0] == 0:
            break

        acc_indices.append(idxbatch)
        acc_results.append(gt[idxbatch])
        total_results += gt[idxbatch].sum()

        if total_results == max_results:
            print(f'Found all {total_results} possible results for {category} after {i} batches. stopping...')
            break

        if i + 1 == n_batches:
            print(f'n batches. ending...')
            break 
        
        box_dict = {}
        for idx in idxbatch:
            box_dict[idx] = ds[idx]

        gt2 = np.array([box_dict[idx].shape[0] > 0 for idx in idxbatch]).astype('float')
        if (gt2 != gt[idxbatch]).all():
            print('Warning: gt data and box data seem to disagree. ')

        loop.refine(idxbatch=idxbatch, box_dict=box_dict)



    res = {}
    res['indices'] = [class_idxs[r].copy() for r in acc_indices]
    res['results'] = [acc.copy() for acc in acc_results]
    res['latency_profile'] = loop.state.latency_profile
    return (rtup, res)


def run_on_actor(br, tup):
    return br.run_loop.remote(tup)

from .progress_bar import tqdm_map

#from .dataset_manager import RemoteVectorIndex

class BenchRunner(object):
    def __init__(self, evs):
        print('initing benchrunner env...')
        print(evs)
        revs = {}
        for (k,evref) in evs.items():
            if isinstance(evref, ray.ObjectRef): # remote
                ev = ray.get(evref)
                revs[k] = ev
            else: # local
                revs[k] = evref
        self.evs = revs
        vls_init_logger()
        print('loaded all evs...')
    
    def run_loop(self, tup):
        start = time.time()
        print(f'getting ev for {tup["dataset"]}...')
        ev = self.evs[tup['dataset']]
        print(f'got ev {ev} after {time.time() - start}')
        ## for repeated benchmarking we want to reload without having to recreate these classes
        importlib.reload(importlib.import_module(run_loop.__module__))
        res = run_loop2(ev=ev, **tup)
        print(f'Finished running after {time.time() - start}')
        return res

RemoteBenchRunner = ray.remote(BenchRunner)

def make_bench_actors(evs, num_actors, resources=dict(num_cpus=4, memory=15*(2**30))):
    evref = ray.put(evs)
    actors = []
    try:
        for i in range(num_actors):
            a = RemoteBenchRunner.options(**resources).remote(evs=evref)
            actors.append(a)
    except Exception as e:
        for a in actors:
            ray.kill(a)
        raise e

    return actors

def parallel_run(*, evs, actors, tups, benchresults):
    print('new run')
    if len(actors) > 0:
        tqdm_map(actors, run_on_actor, tups, benchresults)
    else:    
        for tup in tqdm(tups):
            pexp = BenchRunner(evs).run_loop(tup)
            benchresults.append(pexp)