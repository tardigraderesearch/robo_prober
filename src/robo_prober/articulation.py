"""Explicit, confirmed kinematic geometry export; no dynamics or controller integration."""
from __future__ import annotations
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial.transform import Rotation
from .geometry import (digest, save_json, read_mesh, write_mesh, rigid_transform, vector,
                       valid_name, validate_segments, preview)


def validate_config(config):
    if config.get('schema') != 'robo-prober-articulation/1':
        raise ValueError('Unsupported articulation schema')
    manifest_path = Path(config['segmentation_manifest'])
    if digest(manifest_path) != config['segmentation_sha256']:
        raise ValueError('Articulation is stale: segmentation manifest changed')
    validate_segments(manifest_path)
    m = json.loads(manifest_path.read_text())
    kind = config.get('evidence_kind')
    if kind not in ('real_scan','synthetic_fixture') or kind != m['evidence_kind']:
        raise ValueError('Evidence kind must match segmentation provenance')
    scale = config['scale']
    s = scale.get('meters_per_source_unit')
    if isinstance(s,bool) or not isinstance(s,(int,float)) or not np.isfinite(s) or s <= 0 or scale.get('confirmed') is not True or not scale.get('measurement'):
        raise ValueError('Physical scale is unconfirmed; supply measurement and positive meters_per_source_unit')
    links = config['links']; names = [valid_name(x['name']) for x in links]
    if len(names)!=len(set(names)) or set(names)!={x['name'] for x in m['links']}:
        raise ValueError('Every segmented region must appear exactly once, including residuals (merge first if needed)')
    frames = {}
    marker_ids = []
    for link in links:
        if link.get('confirmed') is not True or not link.get('confirmation'):
            raise ValueError(f'Unconfirmed rigid link: {link["name"]}')
        frames[link['name']] = rigid_transform(link['source_frame'])
        ids=link.get('marker_ids',[])
        if any(type(x) is not int or x < 0 for x in ids):
            raise ValueError('Marker IDs must be nonnegative integers')
        marker_ids.extend(ids)
    if len(marker_ids)!=len(set(marker_ids)):
        raise ValueError('Duplicate tag-to-link assignment')
    parents = {}; joint_names = set()
    for j in config['joints']:
        name = valid_name(j['name'])
        if name in joint_names:
            raise ValueError('Duplicate joint name')
        joint_names.add(name)
        p,c = j['parent'], j['child']
        if p not in frames or c not in frames or p==c or c in parents:
            raise ValueError('Joint graph has invalid parent/child or multiple parents')
        parents[c]=p
        if j.get('confirmed') is not True or not j.get('confirmation'):
            raise ValueError('Joint pivot/type/axis must be confirmed explicitly')
        if j['type'] not in ('fixed','revolute','prismatic','continuous'):
            raise ValueError('Unsupported joint type')
        if j['type'] != 'fixed':
            axis = vector(j.get('axis'), 'joint axis')
            if not np.isclose(np.linalg.norm(axis),1,atol=1e-8):
                raise ValueError('Joint axis must be explicit and unit length')
            lim = j.get('limits')
            required = ['effort','velocity'] + ([] if j['type']=='continuous' else ['lower','upper'])
            if not isinstance(lim,dict) or any(type(lim.get(k)) not in (int,float) or not np.isfinite(lim[k]) for k in required):
                raise ValueError('Finite explicit joint limits required; radians/meters, N or N m, per second')
            if lim['effort']<=0 or lim['velocity']<=0 or (j['type']!='continuous' and lim['lower']>=lim['upper']):
                raise ValueError('Invalid joint limits')
    roots = set(names)-set(parents)
    if len(roots)!=1 or len(parents)!=len(names)-1:
        raise ValueError('Joint graph must be one connected tree')
    root=next(iter(roots))
    for name in names:
        seen=set(); node=name
        while node in parents:
            if node in seen:
                raise ValueError('Joint graph contains a cycle')
            seen.add(node); node=parents[node]
        if node!=root:
            raise ValueError('Disconnected graph')
    return m, frames, root, float(s)


def numbers(xs):
    return ' '.join(format(float(x),'.17g') for x in xs)


def export_articulation(config_path, out, positions=None):
    config=json.loads(Path(config_path).read_text())
    m,frames,root,scale=validate_config(config)
    positions = positions or {}
    moving={j['name']:j for j in config['joints'] if j['type']!='fixed'}
    if set(positions)-set(moving):
        raise ValueError('Unknown or fixed joint in preview positions')
    for name,j in moving.items():
        q=positions.get(name,0.)
        if type(q) not in (float,int) or not np.isfinite(q) or (j['type']!='continuous' and not j['limits']['lower']<=q<=j['limits']['upper']):
            raise ValueError('Preview position outside configured limits')
    out=Path(out); out.mkdir(parents=True,exist_ok=True); (out/'meshes').mkdir(exist_ok=True)
    folder=Path(config['segmentation_manifest']).parent
    robot=ET.Element('robot',name='synthetic_fixture' if config['evidence_kind']=='synthetic_fixture' else 'confirmed_scan_geometry')
    for link in m['links']:
        name=link['name']; v,f=read_mesh(folder/link['mesh'])
        source_from_local=rigid_transform(link['source_from_local'])
        link_from_local=np.linalg.inv(frames[name])@source_from_local
        local=(v@link_from_local[:3,:3].T+link_from_local[:3,3])*scale
        write_mesh(out/'meshes'/f'{name}.obj',local,f)
        node=ET.SubElement(robot,'link',name=name)
        visual=ET.SubElement(node,'visual')
        geo=ET.SubElement(visual,'geometry')
        ET.SubElement(geo,'mesh',filename=f'meshes/{name}.obj',scale='1 1 1')
    for j in config['joints']:
        node=ET.SubElement(robot,'joint',name=j['name'],type=j['type'])
        ET.SubElement(node,'parent',link=j['parent']); ET.SubElement(node,'child',link=j['child'])
        origin=np.linalg.inv(frames[j['parent']])@frames[j['child']]
        ET.SubElement(node,'origin',xyz=numbers(origin[:3,3]*scale),rpy=numbers(Rotation.from_matrix(origin[:3,:3]).as_euler('xyz')))
        if j['type']!='fixed':
            ET.SubElement(node,'axis',xyz=numbers(j['axis']))
            keys=['effort','velocity']+([] if j['type']=='continuous' else ['lower','upper'])
            ET.SubElement(node,'limit',**{k:str(j['limits'][k]) for k in keys})
    ET.indent(robot)
    urdf_path=out/'robot.urdf'
    ET.ElementTree(robot).write(urdf_path,encoding='utf-8',xml_declaration=True)
    # Independent parser, actual mesh reload, graph validation and FK.
    from yourdfpy import URDF
    loaded=URDF.load(str(urdf_path),load_meshes=True)
    if not loaded.validate() or not loaded.validate_filenames():
        raise ValueError('Exported URDF failed independent reload validation')
    loaded.update_cfg({k:float(v) for k,v in positions.items()})
    all_v=[]; all_f=[]; all_labels=[]; transforms={}
    for i,link in enumerate(m['links']):
        name=link['name']; t=loaded.get_transform(name)
        transforms[name]=t.tolist()
        v,f=read_mesh(out/'meshes'/f'{name}.obj')
        all_f.append(f+sum(len(x) for x in all_v)); all_v.append(v@t[:3,:3].T+t[:3,3]); all_labels.extend([i]*len(f))
    title=('SYNTHETIC fixture - known dimensions and joints' if config['evidence_kind']=='synthetic_fixture' else 'Operator-confirmed scan kinematics - no collision/dynamics validation')
    preview(np.concatenate(all_v),np.concatenate(all_f),np.array(all_labels),[x['name'] for x in m['links']],out/'articulated.png',title)
    report={'schema':'robo-prober-urdf-validation/1','evidence_kind':config['evidence_kind'],'urdf_sha256':digest(urdf_path),
            'config_sha256':digest(config_path),'independent_loader':'yourdfpy','valid':True,'root':root,'positions':positions,
            'root_from_links':transforms,'source_from_root':frames[root].tolist(),'meters_per_source_unit':scale,
            'scope':'Visual kinematic model only. No inertias, collision safety, actuator control, or physical motion validated.'}
    save_json(out/'validation.json',report); save_json(out/'articulation.json',config)
    return report
