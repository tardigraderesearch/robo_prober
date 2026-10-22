"""SYNTHETIC fixtures exercise the nested vision CLI pipeline and recording schema."""
import json
import subprocess
import sys
from pathlib import Path
import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
from robo_prober import vision_pipeline as v
from robo_prober.geometry import save_json
from robo_prober.observations import load_observation_fixture,ObservationError

EXAMPLE_CONFIG=Path(__file__).resolve().parents[1]/'examples'/'vision.json'


def image(ids=(0,1),rotation=0):
    canvas=np.full((300,500,3),255,np.uint8)
    d=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for n,i in enumerate(ids):
        marker=np.rot90(cv2.aruco.generateImageMarker(d,i,100),rotation if n else 0)
        canvas[100:200,50+n*200:150+n*200]=cv2.cvtColor(marker,cv2.COLOR_GRAY2BGR)
    return canvas

class Camera:
    def __init__(self,frames):self.frames=iter(frames);self.released=False
    def set(self,*_):return True
    def isOpened(self):return True
    def read(self):
        frame=next(self.frames,None)
        if isinstance(frame,BaseException): raise frame
        return frame is not None,frame
    def release(self):self.released=True


def record(tmp_path,frames=None):
    config=tmp_path/'config.json'
    save_json(config,{'dictionary':'DICT_4X4_50','dictionary_confirmed':False,'reference_id':0,'markers':[{'id':0,'link':None},{'id':1,'link':None}],'pixel_threshold':3.,'angle_threshold_deg':4.,'calibration':None})
    camera=Camera(frames if frames is not None else [image(),image(rotation=1)])
    folder=tmp_path/'capture'
    result=v.capture('synthetic',config,folder,max_frames=2,seconds=3,capture_factory=lambda _:camera,evidence_kind='synthetic_fixture')
    assert camera.released
    return folder,result


def test_replay_export_real_processing(tmp_path,monkeypatch):
    import serial
    monkeypatch.setattr(serial,'Serial',lambda *_,**__:pytest.fail('Serial opened by vision'))
    folder,m=record(tmp_path)
    assert m['evidence_kind']=='synthetic_fixture' and m['detected_ids']==[0,1]
    assert v.replay(folder,tmp_path/'replay')['exact_detection_agreement']
    export=v.export_observation(folder,tmp_path/'export.json')
    assert export['status']=='ready' and export['metric_pose'] is None
    loaded=load_observation_fixture(tmp_path/'export.json')
    assert loaded['motion'][1]['d_angle_deg']==pytest.approx(-90,abs=1)


def test_missing_reference_no_stale_pose(tmp_path):
    folder,_=record(tmp_path,[image(),image(ids=(1,))])
    exported=v.export_observation(folder,tmp_path/'export.json')
    assert exported['status']=='incomplete'
    assert exported['after']['reference_status']=='missing'
    assert [m['id'] for m in exported['after']['markers']]==[1]
    with pytest.raises(ObservationError):load_observation_fixture(tmp_path/'export.json')

@pytest.mark.parametrize('failure',[None,KeyboardInterrupt(),RuntimeError('camera failure')])
def test_cleanup(tmp_path,failure):
    if isinstance(failure,RuntimeError):
        with pytest.raises(ValueError):record(tmp_path,[image(),failure])
    else:
        _,result=record(tmp_path,[image(),failure]); assert result['camera_released']
    assert json.loads((tmp_path/'capture/session.json').read_text())['camera_released']

@pytest.mark.parametrize('kind',['timestamp','frame','config','detections'])
def test_integrity(tmp_path,kind):
    folder,_=record(tmp_path)
    if kind=='frame':(folder/'frames/000000.png').write_bytes(b'corrupt')
    elif kind=='config':(folder/'config.json').write_text('{}')
    else:
        path=folder/'detections.jsonl'; rows=[json.loads(x) for x in path.read_text().splitlines()]
        rows[0]['timestamp_s']=-1 if kind=='timestamp' else 0
        rows[0]['markers']=[]
        path.write_text('\n'.join(json.dumps(x) for x in rows))
    with pytest.raises(ValueError):v.read_session(folder)

@pytest.mark.parametrize('args',[['vision','capture','--device','/dev/no-camera','--config',str(EXAMPLE_CONFIG),'--out','unused'],['vision','replay','/missing','--out','unused'],['model','segment','--config','/missing','--out','unused']])
def test_cli_errors(args):
    r=subprocess.run([sys.executable,'-m','robo_prober.cli',*args],capture_output=True,text=True)
    assert r.returncode==1 and 'error:' in r.stderr


def test_watchdog_parent_interruption_cleans_owned_worker(monkeypatch):
    from robo_prober import sensing_cli
    class Worker:
        alive=True
        terminated=False
        def start(self): pass
        def join(self,timeout):
            if not self.terminated: raise KeyboardInterrupt
        def is_alive(self): return self.alive
        def terminate(self): self.terminated=True; self.alive=False
    class Queue:
        closed=False
        def close(self): self.closed=True
    worker=Worker(); queue=Queue()
    class Context:
        def Queue(self): return queue
        def Process(self,**_): return worker
    monkeypatch.setattr(sensing_cli.multiprocessing,'get_context',lambda _:Context())
    with pytest.raises(KeyboardInterrupt):sensing_cli.bounded_capture(seconds=1)
    assert worker.terminated and queue.closed


def test_watchdog_rejects_invalid_bound_before_spawn(monkeypatch):
    from robo_prober import sensing_cli
    monkeypatch.setattr(sensing_cli.multiprocessing,'get_context',lambda _:pytest.fail('Invalid capture spawned'))
    with pytest.raises(ValueError):sensing_cli.bounded_capture(seconds=float('nan'))
