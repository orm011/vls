from pydantic import BaseModel
from typing import Optional, List, Union, Literal


class Box(BaseModel):
    x1: float
    y1: float
    x2: float
    y2: float
    description: Optional[str]
    marked_accepted = False


# TODO switch annotations to this class
# make box only about boundaries
class Annotation(BaseModel):
    box: Box
    description: Optional[str]
    marked_accepted = False


class ActivationData(BaseModel):
    box: Box
    score: float


class Interval(BaseModel):
    start_ms: int
    end_ms: int


class Imdata(BaseModel):
    url: str
    dbidx: int
    boxes: Optional[
        List[Box]
    ]  # None means not labelled (neutral). [] means positively no boxes.
    activations: Optional[List[ActivationData]]
    timing: List[Interval] = []


def is_image_accepted(imdata: Imdata):
    return (
        any(map(lambda box: box.marked_accepted, imdata.boxes))
        if imdata.boxes is not None
        else False
    )

class IndexSpec(BaseModel):
    d_name: str
    i_name: str
    c_name: Optional[
        str
    ]  # ground truth category (needed to specify subset for lvis benchmark)

class MultiscaleParams: #TODO switch session params
    aug_larger: Literal['greater', 'all'] = 'all'
    agg_method: Literal["avg_score", 'avg_vector', 'plain_score'] = 'avg_score'
    shortlist_size: int

class SessionParams(BaseModel):
    index_spec: IndexSpec
    interactive: Literal['pytorch', 'plain', 'knn_greedy', 'textual', 'knn_prop', 'linear_prop', 'log_reg2', 'pseudo_lr']
    interactive_options: Optional[dict] = None
    batch_size: int
    index_options : Optional[dict] = {'use_vec_index':True}
    aug_larger: Literal['greater', 'all'] = 'all'
    agg_method: Optional[Literal["avg_score", 'avg_vector', 'plain_score']] = 'avg_score'
    shortlist_size: Optional[int]
    method_config: Optional[dict] 
    image_vector_strategy: Optional[Literal[ "matched", 'computed']]
    other_params: Optional[dict]

class LogEntry(BaseModel):
    logger: Literal["server", 'client']
    message: str
    time: float
    seen: int
    accepted: int
    other_fields: Optional[dict]


class SessionState(BaseModel):
    params: SessionParams
    gdata: List[List[Imdata]]
    timing: List[float]
    reference_categories: List[str]
    query_string: Optional[str]
    action_log: List[LogEntry] = []


class BenchParams(BaseModel):
    name: str
    ground_truth_category: str
    qstr: str
    provide_textual_feedback: bool = False
    n_batches: int  # max number of batches to run
    max_results: Optional[int] = None  # stop when this numbrer of results is found
    max_feedback: Optional[int] = None
    box_drop_prob: float = 0.0
    query_template: str = "a {}"  # clip needs this


class BenchResult(BaseModel):
    nimages: int
    ntotal: int
    session: SessionState
    run_info: dict
    total_time: float


class BenchSummary(BaseModel):
    bench_params: BenchParams
    session_params: SessionParams
    timestamp: str
    result: Optional[BenchResult]
