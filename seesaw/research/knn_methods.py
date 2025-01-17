import numpy as np
import pyroaring as pr
from seesaw.knn_graph import *
from ..label_propagation import *

from scipy.special import expit as sigmoid

class SimpleKNNRanker:
    def __init__(self, knng, init_scores=None):
        self.knng : KNNGraph = knng

        if init_scores is None:
            self.init_numerators = np.ones(self.knng.nvecs)*.1 # base if nothing is given
        else:
            self.set_base_scores(init_scores)

        self.pscount = 1.
        
        self.numerators = np.zeros_like(self.init_numerators)
        self.denominators = np.zeros_like(self.init_numerators)

        self.labels = np.zeros_like(self.init_numerators)
        self.is_labeled = np.zeros_like(self.init_numerators)
        
        self.all_indices = pr.FrozenBitMap(range(self.knng.nvecs))
        
    def current_scores(self):
        num = self.pscount*self.init_numerators + self.numerators
        denom = self.pscount + self.denominators
        estimates = num/denom
        return self.labels*self.is_labeled + estimates*(1-self.is_labeled)
        
    def set_base_scores(self, scores):
        assert self.knng.nvecs == scores.shape[0]
        self.init_numerators = sigmoid(2*scores)

    def update(self, idxs, labels):
        for idx, label in zip(idxs, labels):
            idx = int(idx)
            label = float(label)
            
            assert np.isclose(label,0) or np.isclose(label,1)
            
            if self.is_labeled[idx] > 0: # if new label for old 
                old_label = self.labels[idx]
                delta_denom = 0
                delta_num = label - old_label # erase old label and add new label
            else:
                delta_num = label
                delta_denom = 1
            
            self.labels[idx] = label
            self.is_labeled[idx] = 1
                    
            ## update scores for all v such that idx \in knn(v)
            rev_neighbors = self.knng.rev_lookup(idx).src_vertex.values
            # rev_weights = 
            self.numerators[rev_neighbors] += delta_num
            self.denominators[rev_neighbors] += delta_denom
        
    def top_k(self, k, unlabeled_only=True):
        if unlabeled_only:
            subset = np.where(self.is_labeled < 1)[0]
        else: 
            subset = np.array(self.all_indices)
            
        raw_scores = self.current_scores()
        
        topk_positions = np.argsort(-raw_scores[subset])[:k]
        topk_indices = subset[topk_positions]
        
        return topk_indices, raw_scores[topk_indices]


def prepare(knng : KNNGraph, *, edist, prior_weight):
    knndf = knng.knn_df 
    symknn = knndf.assign(weight = kernel(knndf.distance, edist=edist))
    n = knng.nvecs

    wmatrix = sp.coo_matrix( (symknn.weight.values, (symknn.src_vertex.values, symknn.dst_vertex.values)), shape=(n, n))
    diagw = sp.coo_matrix((np.ones(n)*prior_weight, (np.arange(n), np.arange(n))))
    wmatrix_tot = wmatrix + diagw
    norm_w = 1./np.array(wmatrix_tot.sum(axis=1)).reshape(-1)
    adj_matrix = wmatrix.tocsr()
    return adj_matrix, norm_w

def normalize_scores(scores, epsilon):
    assert epsilon < .5
    gap = scores.max() - scores.min()
    if gap == 0: # center at .5 is all scores the same
        return scores - scores + .5
    
    x = (scores - scores.min()) / (scores.max() - scores.min())
    x = x*(1-2*epsilon) + epsilon # shift to be between (epislon, 1-epsilon)
    return x

class BaseLabelPropagationRanker:
    def __init__(self, *, knng : KNNGraph, nvecs, normalize_scores, sigmoid_before_propagate, calib_a, calib_b, 
                    prior_weight, normalize_epsilon = None, **other):
        self.knng = knng
        self.nvecs = nvecs
        self.normalize_scores = normalize_scores

        if self.normalize_scores:
            assert normalize_epsilon is not None
            self.epsilon = normalize_epsilon

        self.calib_a = calib_a
        self.calib_b = calib_b
        self.prior_weight = prior_weight
        self.sigmoid_before_propagate = sigmoid_before_propagate

        self.is_labeled = np.zeros(nvecs)
        self.labels = np.zeros(nvecs)

        self.prior_scores = None
        self._current_scores = None

        self.all_indices = pr.FrozenBitMap(range(nvecs))

    def set_base_scores(self, init_scores):
        assert self.nvecs == init_scores.shape[0]
        ## 1. normalize scores to fit between 0.1 and 0.9

        if self.normalize_scores:
            init_scores = normalize_scores(init_scores, epsilon=self.epsilon)

        if self.sigmoid_before_propagate:# affects the size of the scores wrt. 0, 1 labels from user.
            ## also affects the regularization target things are pushed back to.
            self.prior_scores = sigmoid(self.calib_a*(init_scores + self.calib_b))
        else:
            self.prior_scores = init_scores 

        ## when there are no labels at all, do not propagate, just replace.
        ## when there are labels. what do we do?
        if self.is_labeled.sum() == 0:
            self._current_scores = self.prior_scores
        else:
            self._current_scores = self._propagate(self.prior_scores)

    def _propagate(self, scores):
        raise NotImplementedError('implement me')

    def update(self, idxs, labels):
        for idx, label in zip(idxs, labels):
            idx = int(idx)
            label = float(label)
            assert np.isclose(label,0) or np.isclose(label,1)
            self.labels[idx] = label  # make 0 or 1
            self.is_labeled[idx] = 1
                
        num_negatives = sum(self.labels[self.is_labeled > 0] == 0)
        if num_negatives > 0:
            print(' propagating')
            pscores = self._propagate(self.prior_scores)
            self._current_scores = pscores
        else:
            print(' no negatives yet, skipping propagation')

    def current_scores(self):
        return self._current_scores

    def top_k(self, k, unlabeled_only=True):
        if unlabeled_only:
            subset = np.where(self.is_labeled < 1)[0]
        else: 
            subset = np.array(self.all_indices)
            
        raw_scores = self.current_scores()        
        topk_positions = np.argsort(-raw_scores[subset])[:k]
        topk_indices = subset[topk_positions]
        return topk_indices, raw_scores[topk_indices]


    
class LabelPropagationRanker2(BaseLabelPropagationRanker):
    lp : LabelPropagation

    def __init__(self, *, weight_matrix : sp.csr_array, verbose : int = 0, **other):
        nvecs = weight_matrix.shape[0]
        super().__init__(knng=None, nvecs=nvecs, **other)
        self.knng_intra = None #knng_intra

        self.weight_matrix = weight_matrix
        
        common_params = dict(reg_lambda = self.prior_weight, weight_matrix=self.weight_matrix, max_iter=300, verbose=verbose)
        self.lp = LabelPropagation(**common_params)

        # assert knng_intra is None
        # if knng_intra is None:
        # else:
        #     self.weight_matrix_intra = get_weight_matrix(knng_intra, kfun=kfun, self_edges=self_edges, normalized=normalized_weights)
        #     self.lp = LabelPropagationComposite(weight_matrix_intra = self.weight_matrix_intra, **common_params)
    
    def _propagate(self,  scores):
        ids = np.nonzero(self.is_labeled.reshape(-1))
        labels = self.labels.reshape(-1)[ids]
        scores = self.lp.fit_transform(label_ids=ids, label_values=labels, reg_values=self.prior_scores, start_value=scores)
        return scores