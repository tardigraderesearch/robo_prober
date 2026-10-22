"""Known, explicitly SYNTHETIC geometry exercising scan segmentation and URDF export."""
import json
from pathlib import Path
import importlib.util
import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
pytest.importorskip("cv2")
pytest.importorskip("trimesh")
pytest.importorskip("yourdfpy")
from scipy.spatial.transform import Rotation
from robo_prober.geometry import (read_mesh,write_mesh,digest,save_json,partition,segment,validate_segments,rigid_transform,propose)
from robo_prober.articulation import validate_config,export_articulation


def load_builder():
    spec=importlib.util.spec_from_file_location('synthetic_builder',Path(__file__).parents[1]/'examples/build_synthetic_articulation.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.build


@pytest.fixture
def fixture(tmp_path):
    root,_=load_builder()(tmp_path/'synthetic')
    return root,json.loads((root/'articulation.json').read_text())


def test_synthetic_real_urdf_reload_and_analytic_motion(fixture):
    from yourdfpy import URDF
    root,cfg=fixture
    for folder,q in [('zero',0.),('bent',np.pi/2)]:
        robot=URDF.load(str(root/folder/'robot.urdf'),load_meshes=True)
        assert robot.validate() and robot.validate_filenames()
        robot.update_cfg({'hinge':q})
        transform=robot.get_transform('arm')
        endpoint=transform@np.array([1,0,0,1])
        assert np.allclose(endpoint[:3],[1+np.cos(q),np.sin(q),0],atol=1e-10)
        assert len(robot.scene.geometry)==2
    # Portability: all geometry paths relative and bundle loads after relocation.
    import shutil
    relocated=root/'relocated'; shutil.copytree(root/'bent',relocated)
    assert URDF.load(str(relocated/'robot.urdf'),load_meshes=True).validate_filenames()


def test_deterministic_split_correction_and_reconstruction(fixture):
    root,_=fixture; cfg=json.loads((root/'split.json').read_text())
    cfg['operations']=[{'kind':'plane','region':'body','normal':[1,0,0],'offset':1.,'negative':'low','positive':'high'},
                       {'kind':'box','region':'high','min':[1.2,-1,-1],'max':[3,1,1],'inside':'tip','outside':'seam'},
                       {'kind':'merge','regions':['low','seam'],'name':'base'}]
    save_json(root/'corrected.json',cfg)
    a=segment(root/'corrected.json',root/'one'); b=segment(root/'corrected.json',root/'two')
    assert a==b
    assert len(a['links'])==2
    assert validate_segments(root/'one/manifest.json')['faces']==24
    for link in a['links']:
        assert (root/'one'/link['mesh']).read_bytes()==(root/'two'/link['mesh']).read_bytes()
    p1=propose(root/'synthetic.obj',root/'proposal1.json',2,evidence_kind='synthetic_fixture')
    p2=propose(root/'synthetic.obj',root/'proposal2.json',2,evidence_kind='synthetic_fixture')
    assert p1==p2


@pytest.mark.parametrize('mutation',['scale','scale_confirmation','measurement','axis','axis_zero','link','joint','cycle','duplicate','missing','limits','nan','shear','reflection','stale','kind'])
def test_incomplete_or_invalid_articulation_rejected(fixture,mutation):
    root,cfg=fixture
    if mutation=='scale': cfg['scale']['meters_per_source_unit']=None
    elif mutation=='scale_confirmation': cfg['scale']['confirmed']=False
    elif mutation=='measurement': cfg['scale']['measurement']=None
    elif mutation=='axis': cfg['joints'][0]['axis']=None
    elif mutation=='axis_zero': cfg['joints'][0]['axis']=[0,0,0]
    elif mutation=='link': cfg['links'][0]['confirmed']=False
    elif mutation=='joint': cfg['joints'][0]['confirmed']=False
    elif mutation=='cycle': cfg['joints'].append(dict(cfg['joints'][0],name='return',parent='arm',child='base'))
    elif mutation=='duplicate': cfg['links'].append(cfg['links'][0])
    elif mutation=='missing': cfg['joints']=[]
    elif mutation=='limits': cfg['joints'][0]['limits']['upper']=-5
    elif mutation=='nan': cfg['scale']['meters_per_source_unit']=float('nan')
    elif mutation=='shear': cfg['links'][0]['source_frame'][0][1]=.2
    elif mutation=='reflection': cfg['links'][0]['source_frame'][0][0]=-1
    elif mutation=='stale': cfg['segmentation_sha256']='0'*64
    elif mutation=='kind': cfg['evidence_kind']='real_scan'
    with pytest.raises(ValueError): validate_config(cfg)


def test_nonidentity_link_frames_scale_and_prismatic(fixture):
    from yourdfpy import URDF
    root,cfg=fixture
    rotation=Rotation.from_euler('xyz',[.3,.2,-.4]).as_matrix()
    cfg['links'][1]['source_frame'][:3]=np.column_stack((rotation,[1,0,0])).tolist()
    cfg['scale']['meters_per_source_unit']=.01
    cfg['joints'][0]['type']='prismatic'; cfg['joints'][0]['limits'].update(lower=-1,upper=1)
    save_json(root/'rotated.json',cfg)
    export_articulation(root/'rotated.json',root/'rotated',{'hinge':.2})
    robot=URDF.load(str(root/'rotated/robot.urdf')); robot.update_cfg({'hinge':.2})
    t=robot.get_transform('arm')
    assert np.allclose(t[:3,:3],rotation)
    assert np.allclose(t[:3,3],np.array([.01,0,0])+rotation@np.array([0,0,.2]))
    # Independently reconstruct zero-pose visual geometry in source frame with scale.
    robot.update_cfg({'hinge':0})
    lv,lf=read_mesh(root/'rotated/meshes/arm.obj')
    t=robot.get_transform('arm')
    actual=(lv@t[:3,:3].T+t[:3,3])/.01
    original_v,original_f=read_mesh(root/'synthetic.obj')
    assert np.allclose(actual[lf],original_v[original_f[12:]],atol=1e-9)


@pytest.mark.parametrize('tamper',['faces','mesh','transform','coverage','config'])
def test_assets_and_coverage_tampering_rejected(fixture,tamper):
    root,cfg=fixture; folder=root/'segments'; path=folder/'manifest.json'; m=json.loads(path.read_text()); link=m['links'][0]
    if tamper=='faces':
        p=folder/link['face_ids']; ids=np.load(p); ids[0]=ids[1]; np.save(p,ids); link['face_ids_sha256']=digest(p)
    elif tamper=='mesh':
        p=folder/link['mesh']; v,f=read_mesh(p); v[0]+=.1; write_mesh(p,v,f); link['sha256']=digest(p)
    elif tamper=='transform': link['source_from_local'][0][3]+=1
    elif tamper=='coverage': m['coverage']['missing']=1
    else: (folder/'segmentation.json').write_text('{}')
    save_json(path,m)
    with pytest.raises(ValueError): validate_segments(path)


@pytest.mark.parametrize('op',[
 {'kind':'plane','region':'body','normal':[0,0,0],'offset':0,'negative':'a','positive':'b'},
 {'kind':'plane','region':'body','normal':[1,0,0],'offset':100,'negative':'a','positive':'b'},
 {'kind':'plane','region':'body','normal':[1,0,0],'offset':1,'negative':'../a','positive':'b'},
 {'kind':'faces','region':'body','face_ids':[-1],'selected':'a','remainder':'b'},
 {'kind':'merge','regions':['absent'],'name':'a'},
 {'kind':'box','region':'body','min':[0,0,0],'max':[-1,-1,-1],'inside':'a','outside':'b'}])
def test_invalid_guided_operations(fixture,op):
    root,_=fixture; v,f=read_mesh(root/'synthetic.obj')
    with pytest.raises(ValueError): partition(v,f,{'initial':'all','operations':[op]})
