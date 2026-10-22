"""Source-preserving triangle partitioning. Candidate regions are NOT inferred joints."""
from __future__ import annotations
import json
import re
import shutil
from pathlib import Path
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components


from .artifacts import digest, save_json


def read_mesh(path):
    # Deliberately no welding, repair, simplification, or vertex/face reordering.
    vertices, faces = [], []
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == 'v':
            vertices.append([float(x) for x in fields[1:4]])
        elif fields[0] == 'f':
            if len(fields) != 4:
                raise ValueError('Only triangular OBJ input supported; triangulate explicitly first')
            face = [int(x.split('/')[0]) for x in fields[1:]]
            if 0 in face:
                raise ValueError('OBJ vertex indices cannot be zero')
            faces.append([i - 1 if i > 0 else len(vertices) + i for i in face])
    v, f = np.asarray(vertices, dtype=float), np.asarray(faces, dtype=np.int64)
    if v.ndim != 2 or v.shape[1] != 3 or not np.isfinite(v).all() or f.ndim != 2 or not len(f):
        raise ValueError('Empty or invalid triangle mesh')
    if f.min() < 0 or f.max() >= len(v):
        raise ValueError('Face vertex index out of range')
    return v, f


def write_mesh(path, vertices, faces):
    with Path(path).open('w') as out:
        out.write('# Robo Prober: source triangle partition; units specified by manifest\n')
        for v in vertices:
            out.write('v ' + ' '.join(format(float(x), '.17g') for x in v) + '\n')
        for f in faces:
            out.write('f ' + ' '.join(str(int(x) + 1) for x in f) + '\n')


def components(vertices, faces):
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    graph = coo_matrix((np.ones(len(edges)), (edges[:, 0], edges[:, 1])), shape=(len(vertices), len(vertices)))
    _, labels = connected_components(graph, directed=False)
    raw = labels[faces[:, 0]]
    ids, counts = np.unique(raw, return_counts=True)
    order = sorted(zip(ids, counts), key=lambda x: (-x[1], x[0]))
    remap = {int(old): i for i, (old, _) in enumerate(order)}
    return np.array([remap[int(i)] for i in raw]), [int(c) for _, c in order]


PALETTE = [(233, 121, 66), (67, 168, 230), (113, 194, 91), (200, 105, 195), (220, 188, 75), (110, 119, 220), (80, 193, 184), (160, 160, 160)]


def preview(vertices, faces, labels, names, path, title='Unconfirmed candidate regions — source units unknown'):
    """Full triangle rasterization, orthographic views; exported meshes stay full resolution."""
    import cv2
    width, height = 720, 820
    canvas = np.full((height, width * 3, 3), 247, dtype=np.uint8)
    lo, hi = vertices.min(axis=0), vertices.max(axis=0)
    scale = 610 / max(float((hi-lo).max()), 1e-12)
    centered = vertices - (lo + hi) / 2
    for panel, (a, b, depth) in enumerate([(0, 1, 2), (0, 2, 1), (1, 2, 0)]):
        pts = np.column_stack((centered[:, a]*scale + panel*width + width/2, -centered[:, b]*scale + 395)).astype(np.int32)
        z = vertices[faces, depth].mean(axis=1)
        normal = np.cross(vertices[faces[:,1]]-vertices[faces[:,0]], vertices[faces[:,2]]-vertices[faces[:,0]])
        normal /= np.maximum(np.linalg.norm(normal,axis=1,keepdims=True),1e-15)
        light = .45 + .55 * np.abs(normal[:,depth]*.8+normal[:,a]*.35+normal[:,b]*.2).clip(0,1)
        for i in np.argsort(z, kind='stable'):
            cv2.fillConvexPoly(canvas, pts[faces[i]], tuple(int(c*light[i]) for c in PALETTE[int(labels[i]) % len(PALETTE)]))
        cv2.putText(canvas, f'{"XYZ"[a]} / {"XYZ"[b]}   source bounds {lo[a]:.2f}..{hi[a]:.2f}, {lo[b]:.2f}..{hi[b]:.2f}', (panel*width+15, 45), cv2.FONT_HERSHEY_SIMPLEX, .5, (25,25,25), 1)
    cv2.putText(canvas, title.replace('—', '-'), (15, 20), cv2.FONT_HERSHEY_SIMPLEX, .55, (20,20,20), 1)
    for i, name in enumerate(names[:16]):
        x, y = 20+(i % 4)*530, 735+(i//4)*20
        cv2.rectangle(canvas, (x,y-10), (x+18,y+3), PALETTE[i % len(PALETTE)], -1)
        cv2.putText(canvas, f'{name}: {int((labels == i).sum())} faces', (x+25,y), cv2.FONT_HERSHEY_SIMPLEX, .45, (20,20,20), 1)
    if not cv2.imwrite(str(path), canvas):
        raise ValueError('Could not write preview')


def prepare(source, out, local):
    source, out, local = Path(source), Path(out), Path(local)
    out.mkdir(parents=True, exist_ok=True)
    local.parent.mkdir(parents=True, exist_ok=True)
    source_hash = digest(source)
    if local.exists() and digest(local) != source_hash:
        raise ValueError('Refusing to replace a different preserved source')
    if not local.exists():
        shutil.copyfile(source, local)
    if digest(local) != source_hash:
        raise ValueError('Source copy hash mismatch')
    v, f = read_mesh(local)
    labels, counts = components(v, f)
    twice_area = np.linalg.norm(np.cross(v[f[:,1]]-v[f[:,0]], v[f[:,2]]-v[f[:,0]]), axis=1)
    report = {'schema': 'robo-prober-mesh-inspection/1', 'source_path': str(source.resolve()), 'local_path': str(local.resolve()), 'sha256': source_hash,
              'vertices': len(v), 'faces': len(f), 'bounds': [v.min(axis=0).tolist(), v.max(axis=0).tolist()],
              'units': 'unknown', 'meters_per_unit': None, 'components_face_counts': counts,
              'degenerate_faces': int((twice_area <= 1e-12).sum()), 'surface_area_source_units_squared': float(twice_area.sum()/2),
              'mechanically_confirmed': False, 'warning': 'Connectivity is topological evidence only; no rigid links or joints confirmed.'}
    save_json(out/'report.json', report)
    preview(v, f, np.minimum(labels, 7), [f'component {i}' for i in range(min(len(counts),8))], out/'preview.png')
    return report


def propose(source, out, parts=4, evidence_kind="real_scan"):
    if evidence_kind not in ("real_scan", "synthetic_fixture"):
        raise ValueError("Invalid evidence kind")
    if not 2 <= parts <= 12:
        raise ValueError('parts must be between 2 and 12')
    v, f = read_mesh(source)
    comp, counts = components(v, f)
    c = v[f].mean(axis=1)
    main = c[comp == 0]
    _, vectors = np.linalg.eigh(np.cov(main.T))
    axis = vectors[:, -1]
    if axis[np.argmax(abs(axis))] < 0:
        axis = -axis
    projection = main @ axis
    hist, edges = np.histogram(projection, bins=96)
    smooth = np.convolve(hist, np.ones(5)/5, mode='same')
    cuts = []
    # Search low triangle-density cross sections near equally spaced extents.
    # A suggestion for an operator, not automatic anatomical/joint discovery.
    for i in range(1, parts):
        target = 96*i/parts
        indices = np.arange(max(3,int(target-96/(parts*4))), min(93,int(target+96/(parts*4))+1))
        idx = int(indices[np.argmin(smooth[indices])])
        cuts.append(float((edges[idx]+edges[idx+1])/2))
    ops = []
    region = 'body'
    for i, cut in enumerate(cuts):
        next_region = f'remainder_{i}' if i < len(cuts)-1 else f'candidate_{i+1}'
        ops.append({'kind':'plane', 'region':region, 'normal':axis.tolist(), 'offset':cut, 'negative':f'candidate_{i}', 'positive':next_region})
        region = next_region
    config = {'schema':'robo-prober-segmentation/1', 'source':str(Path(source).resolve()), 'source_sha256':digest(source), 'evidence_kind':evidence_kind,
              'units':'unknown', 'confirmed':False, 'initial':'components', 'min_component_faces':max(100,int(len(f)*.002)),
              'operations':ops, 'proposal':{'method':'vertex connectivity + principal-axis low-density cross sections of largest component', 'principal_axis':axis.tolist(), 'offsets':cuts,
              'histogram':hist.tolist(), 'bin_edges':edges.tolist(), 'warning':'Cross sections can divide a rigid part or combine moving parts. Review all boundaries. Small islands retained as residual.'}}
    save_json(out, config)
    return config


def valid_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name):
        raise ValueError(f'Invalid region/link name: {name!r}')
    return name


def vector(value, name='vector'):
    a = np.asarray(value, dtype=float)
    if a.shape != (3,) or not np.isfinite(a).all():
        raise ValueError(f'{name} must be three finite numbers')
    return a


def partition(v, f, config):
    centers = v[f].mean(axis=1)
    if config.get('initial') == 'components':
        comp, counts = components(v,f)
        minimum = config.get('min_component_faces',100)
        if not isinstance(minimum,int) or minimum < 1:
            raise ValueError('min_component_faces must be positive integer')
        regions = np.array(['body' if i == 0 else (f'island_{i}' if counts[i] >= minimum else 'residual') for i in comp], dtype=object)
    elif config.get('initial') == 'all':
        regions = np.full(len(f), 'body', dtype=object)
    else:
        raise ValueError('initial must be all or components')
    for op in config['operations']:
        kind = op['kind']
        if kind == 'merge':
            sources = op['regions']
            if not sources or not set(sources) <= set(regions):
                raise ValueError('Merge regions must exist')
            target = valid_name(op['name'])
            if target in regions and target not in sources:
                raise ValueError('Merge target already exists')
            regions[np.isin(regions, sources)] = target
            continue
        source = op['region']
        mask = regions == source
        if not mask.any():
            raise ValueError(f'Missing region {source}')
        if kind == 'plane':
            n = vector(op['normal'])
            if np.linalg.norm(n) < 1e-12 or not np.isfinite(op['offset']):
                raise ValueError('Invalid cutting plane')
            selected = centers @ n < float(op['offset'])
            names = [op['negative'], op['positive']]
        elif kind == 'box':
            low, high = vector(op['min']), vector(op['max'])
            if not (low < high).all():
                raise ValueError('Invalid selection box')
            selected = ((centers >= low) & (centers <= high)).all(axis=1)
            names = [op['inside'], op['outside']]
        elif kind == 'faces':
            ids = np.asarray(op['face_ids'])
            if ids.ndim != 1 or ids.dtype.kind not in 'iu' or not len(ids) or ids.min() < 0 or ids.max() >= len(f) or not mask[ids].all():
                raise ValueError('Face selection must belong to source region')
            selected = np.zeros(len(f), dtype=bool)
            selected[ids] = True
            names = [op['selected'],op['remainder']]
        else:
            raise ValueError(f'Unknown operation {kind}')
        for name in names:
            valid_name(name)
            if name in regions and name != source:
                raise ValueError('Region name collision')
        if names[0] == names[1] or not (mask & selected).any() or not (mask & ~selected).any():
            raise ValueError('Split must produce two distinct nonempty regions')
        regions[mask & selected] = names[0]
        regions[mask & ~selected] = names[1]
    names = sorted(set(regions))
    labels = np.array([names.index(x) for x in regions], dtype=np.int32)
    return labels, names


def segment(config_path, out):
    config = json.loads(Path(config_path).read_text())
    if config.get('schema') != 'robo-prober-segmentation/1':
        raise ValueError('Unsupported segmentation schema')
    source = Path(config['source'])
    if digest(source) != config['source_sha256']:
        raise ValueError('Segmentation config source hash mismatch')
    if config.get('confirmed') is not False:
        raise ValueError('Segmentation outputs are candidates; confirmation belongs in articulation config')
    v, f = read_mesh(source)
    labels, names = partition(v, f, config)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    save_json(out/'segmentation.json',config)
    links = []
    for i, name in enumerate(names):
        ids = np.flatnonzero(labels == i)
        vertex_ids, inverse = np.unique(f[ids].ravel(), return_inverse=True)
        origin = v[vertex_ids].mean(axis=0)
        mesh_path = out / (name+'.obj')
        write_mesh(mesh_path, v[vertex_ids]-origin, inverse.reshape(-1,3))
        face_path = out/(name+'.faces.npy')
        np.save(face_path, ids, allow_pickle=False)
        transform = np.eye(4)
        transform[:3,3] = origin
        links.append({'name':name, 'mesh':mesh_path.name, 'sha256':digest(mesh_path), 'face_ids':face_path.name, 'face_ids_sha256':digest(face_path),
                      'faces':len(ids), 'vertices':len(vertex_ids), 'source_from_local':transform.tolist(), 'confirmed':False})
    edges = np.concatenate([f[:,[0,1]], f[:,[1,2]], f[:,[2,0]]])
    owners = np.tile(np.arange(len(f)),3)
    edges.sort(axis=1)
    _, edge_ids, edge_counts = np.unique(edges, axis=0, return_inverse=True, return_counts=True)
    lo = np.full(len(edge_counts),len(names)); hi = np.full(len(edge_counts),-1)
    np.minimum.at(lo,edge_ids,labels[owners]); np.maximum.at(hi,edge_ids,labels[owners])
    manifest = {'schema':'robo-prober-segmented/1','source':str(source.resolve()),'source_sha256':digest(source), 'config_sha256':digest(out/'segmentation.json'),
                'evidence_kind':config.get('evidence_kind','unassigned'), 'units':config.get('units','unknown'), 'mechanically_confirmed':False,
                'source_faces':len(f), 'source_vertices':len(v), 'links':links, 'coverage':{'assigned':len(f),'missing':0,'overlap':0,'boundary_edges_between_regions':int((lo!=hi).sum()),
                'source_open_edges':int((edge_counts==1).sum()),'source_nonmanifold_edges':int((edge_counts>2).sum())},
                'method':'whole triangles classified by centroid; no caps, no new faces, no repair; jagged cut boundaries expected',
                'warning':'Candidate regions may cross physical joints or divide rigid parts. Residual/islands are retained, not discarded or declared robot links.'}
    preview(v,f,labels,names,out/'preview.png')
    manifest['preview'] = {'path':'preview.png','sha256':digest(out/'preview.png'),'views':['XY','XZ','YZ'],'triangles_rendered_per_view':len(f)}
    save_json(out/'manifest.json',manifest)
    scaffold = {'schema':'robo-prober-articulation/1','evidence_kind':config.get('evidence_kind','unassigned'),'segmentation_manifest':str((out/'manifest.json').resolve()),
                'segmentation_sha256':digest(out/'manifest.json'),'scale':{'meters_per_source_unit':None,'confirmed':False,'measurement':None},
                'links':[{'name':name,'confirmed':False,'source_frame':np.eye(4).tolist(),'marker_ids':[]} for name in names],
                'joints':[], 'note':'source_frame is source-from-link at zero pose, in source units. Define link frames at pivots; joint axes in child/joint frame. Joint records need name,parent,child,type,axis,limits,confirmed. No physical parameters inferred.'}
    # Never overwrite an operator's completed articulation config on re-segmentation.
    scaffold_path = out/'articulation.unconfirmed.json'
    existing = json.loads(scaffold_path.read_text()) if scaffold_path.exists() else None
    if existing is None or existing == dict(scaffold, segmentation_sha256=existing.get('segmentation_sha256')):
        save_json(scaffold_path,scaffold)
    validate_segments(out/'manifest.json')
    return manifest


def rigid_transform(value):
    t = np.asarray(value, dtype=float)
    if t.shape != (4,4) or not np.isfinite(t).all() or not np.allclose(t[3],[0,0,0,1]) or not np.allclose(t[:3,:3].T @ t[:3,:3],np.eye(3),rtol=0,atol=1e-8) or not np.isclose(np.linalg.det(t[:3,:3]),1,rtol=0,atol=1e-8):
        raise ValueError('Expected finite proper rigid transform (no shear/reflection/scale)')
    return t


def validate_segments(manifest_path):
    path = Path(manifest_path); m = json.loads(path.read_text()); folder = path.parent
    import cv2
    img = cv2.imread(str(folder/m['preview']['path']))
    if img is None or min(img.shape[:2]) < 100 or img.std() < 1 or digest(folder/m['preview']['path']) != m['preview']['sha256']:
        raise ValueError('Missing, unreadable or changed segmentation preview')
    if digest(m['source']) != m['source_sha256'] or digest(folder/'segmentation.json') != m['config_sha256']:
        raise ValueError('Source/config provenance mismatch')
    v,f = read_mesh(m['source'])
    if (len(v),len(f)) != (m['source_vertices'],m['source_faces']):
        raise ValueError('Source counts mismatch')
    counts = np.zeros(len(f),dtype=int)
    cfg = json.loads((folder/'segmentation.json').read_text())
    labels,names = partition(v,f,cfg)
    if [x['name'] for x in m['links']] != names:
        raise ValueError('Manifest region mismatch')
    for i,link in enumerate(m['links']):
        for field, hash_field in [('mesh','sha256'),('face_ids','face_ids_sha256')]:
            if digest(folder/link[field]) != link[hash_field]:
                raise ValueError('Asset hash mismatch')
        ids = np.load(folder/link['face_ids'],allow_pickle=False)
        if not np.array_equal(ids,np.flatnonzero(labels==i)):
            raise ValueError('Face mapping does not reproduce config')
        np.add.at(counts,ids,1)
        lv,lf = read_mesh(folder/link['mesh']); t = rigid_transform(link['source_from_local'])
        reconstructed = lv @ t[:3,:3].T + t[:3,3]
        if len(lf)!=link['faces'] or len(lv)!=link['vertices'] or not np.allclose(reconstructed[lf],v[f[ids]],rtol=0,atol=1e-9):
            raise ValueError('Mesh or source transform does not reconstruct original triangles')
    if not (counts==1).all() or m['coverage']['assigned']!=len(f) or m['coverage']['missing']!=0 or m['coverage']['overlap']!=0:
        raise ValueError('Face coverage/loss/overlap mismatch')
    return {'faces':len(f),'links':len(m['links']),'missing':0,'overlap':0,'reconstruction':'all triangles checked'}
