#!/usr/bin/env python3
"""Build a deterministic SYNTHETIC two-link arm and export it to URDF.

Usage: python examples/build_synthetic_articulation.py [OUTPUT_DIR]

The geometry is two 1 m boxes joined by a revolute hinge. It exercises the
full scan -> segments -> confirmed articulation -> URDF path without any real
scan data. Nothing here is a physical measurement.
"""
import sys
from pathlib import Path
import numpy as np
import trimesh
from robo_prober.geometry import write_mesh,save_json,digest,segment
from robo_prober.articulation import export_articulation


def build(out=Path('synthetic-arm')):
    out=Path(out); out.mkdir(parents=True,exist_ok=True)
    base=trimesh.creation.box(extents=[1,.2,.2]); base.apply_translation([.5,0,0])
    arm=trimesh.creation.box(extents=[1,.2,.2]); arm.apply_translation([1.5,0,0])
    # Disconnected boxes intentionally meet at x=1; original face indices retained.
    v=np.concatenate([base.vertices,arm.vertices]); f=np.concatenate([base.faces,arm.faces+len(base.vertices)])
    source=out/'synthetic.obj'; write_mesh(source,v,f)
    cfg={'schema':'robo-prober-segmentation/1','source':str(source.resolve()),'source_sha256':digest(source),'evidence_kind':'synthetic_fixture','units':'synthetic_meter',
         'confirmed':False,'initial':'all','operations':[{'kind':'faces','region':'body','face_ids':list(range(12)),'selected':'base','remainder':'arm'}]}
    save_json(out/'split.json',cfg); manifest=out/'segments/manifest.json'; segment(out/'split.json',manifest.parent)
    base_frame=np.eye(4); arm_frame=np.eye(4); arm_frame[:3,3]=[1,0,0]
    articulation={'schema':'robo-prober-articulation/1','evidence_kind':'synthetic_fixture','segmentation_manifest':str(manifest.resolve()),'segmentation_sha256':digest(manifest),
                  'scale':{'meters_per_source_unit':1.,'confirmed':True,'measurement':'SYNTHETIC definition: each box is exactly one meter long; not a physical measurement'},
                  'links':[{'name':name,'confirmed':True,'confirmation':'SYNTHETIC deterministic fixture definition','source_frame':frame.tolist(),'marker_ids':[]} for name,frame in [('base',base_frame),('arm',arm_frame)]],
                  'joints':[{'name':'hinge','parent':'base','child':'arm','type':'revolute','axis':[0,0,1],'limits':{'lower':-np.pi,'upper':np.pi,'effort':1.,'velocity':1.},'confirmed':True,'confirmation':'SYNTHETIC axis, pivot and limits defined by fixture'}]}
    save_json(out/'articulation.json',articulation); save_json(out/'positions.json',{'hinge':float(np.pi/2)})
    export_articulation(out/'articulation.json',out/'zero')
    result=export_articulation(out/'articulation.json',out/'bent',{'hinge':float(np.pi/2)})
    print('SYNTHETIC fixture exported, independently reloaded, previewed at zero and pi/2 radians')
    return out,result

if __name__=='__main__':
    build(Path(sys.argv[1]) if len(sys.argv)>1 else Path('synthetic-arm'))
