import ray
from seesaw import GlobalDataManager, SessionParams, BenchParams, BenchRunner, add_routes, SessionReq, ResetReq, SessionInfoReq, IndexSpec
import random, string, os
from fastapi import FastAPI

ray.init('auto', namespace='seesaw')

import math
#TEST_ROOT = '/home/gridsan/omoll/fastai_shared/omoll/seesaw_root/'
TEST_ROOT = '/home/gridsan/omoll/seesaw_root/'
tmp_name = ''.join([random.choice(string.ascii_letters) for _ in range(10)])
TEST_SAVE = f'{os.environ["TMPDIR"]}/test_save/{tmp_name}'

os.makedirs(TEST_SAVE, exist_ok=True)

gdm = GlobalDataManager(TEST_ROOT)
os.chdir(gdm.root)
br = BenchRunner(gdm.root, results_dir=TEST_SAVE)

b = BenchParams(name='seesaw_test', 
  ground_truth_category='aerosol can', qstr='aerosol can', 
  n_batches=4, max_feedback=None, box_drop_prob=0.0, max_results=100000)

p = SessionParams(index_spec=IndexSpec(d_name='data/lvis/', i_name='multiscale', c_name='aerosol can'), 
                  interactive='pytorch', warm_start='warm', batch_size=3, 
  minibatch_size=10, learning_rate=0.005, max_examples=500, 
  loss_margin=0.1, num_epochs=2, model_type='cosine')

bench_path = br.run_loop(b,p)

app = FastAPI()
WebSeesaw = add_routes(app)
webseesaw = WebSeesaw(TEST_ROOT, TEST_SAVE)

# check basic calls work
state = webseesaw.getstate()
assert len(state.session.gdata) == 0

bench_state = webseesaw.session_info(SessionInfoReq(path=bench_path))
assert len(bench_state.session.gdata) == b.n_batches

state = webseesaw.reset(ResetReq(index=p.index_spec))
assert len(state.session.gdata) == 0

state = webseesaw.text('bird')
assert len(state.session.gdata) == 1

for i in range(2, b.n_batches + 1):
    next_req = SessionReq(client_data=state)
    state = webseesaw.next(next_req)
    assert len(state.session.gdata) == i, f'{i}'

assert len(state.session.gdata) == b.n_batches

saved_state = state
r = webseesaw.save(SessionReq(client_data=saved_state))
assert os.path.exists(r.path)

reset_state = webseesaw.reset(ResetReq(index=state.session.params.index_spec))
assert len(reset_state.session.gdata) == 0

restored_state= webseesaw.session_info(SessionInfoReq(path=r.path))
assert len(restored_state.session.gdata) == len(saved_state.session.gdata)

print('test success')