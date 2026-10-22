"""Bounded camera sensing only. This module never imports a serial transport."""
from __future__ import annotations
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
import cv2
import numpy as np
from .artifacts import digest, save_json


def cameras():
    result=[]
    for p in sorted(Path('/sys/class/video4linux').glob('video*')):
        result.append({'device':'/dev/'+p.name,'name':(p/'name').read_text().strip(),
                       'index':int((p/'index').read_text()),'sysfs':str(p.resolve()),
                       'capture_candidate':(p/'index').read_text().strip()=='0' and 'loopback' not in (p/'name').read_text().lower()})
    return result


def load_config(path):
    cfg=json.loads(Path(path).read_text())
    name=cfg.get('dictionary')
    if not isinstance(name,str) or not name.startswith('DICT_') or not hasattr(cv2.aruco,name):
        raise ValueError('Invalid explicit ArUco dictionary')
    dictionary=cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco,name))
    ref=cfg.get('reference_id')
    markers=cfg.get('markers')
    if not isinstance(markers,list):
        raise ValueError('markers must be a list with unique explicit IDs and nullable links')
    ids=[x.get('id') for x in markers]
    if any(type(i) is not int or i<0 or i>=len(dictionary.bytesList) for i in ids) or len(ids)!=len(set(ids)):
        raise ValueError('Invalid or duplicate marker IDs')
    if ref is not None and (type(ref) is not int or ref not in ids):
        raise ValueError('Reference ID must be listed in markers, or null/unassigned')
    if any(x.get('link') is not None and (not isinstance(x['link'],str) or not x['link']) for x in markers):
        raise ValueError('Link must be a name or null')
    for key in ('pixel_threshold','angle_threshold_deg'):
        n=cfg.get(key)
        if type(n) not in (int,float) or not math.isfinite(n) or n<=0:
            raise ValueError(f'{key} must be a positive finite number')
    if cfg.get('target_model') is not None and not Path(cfg['target_model']).is_file():
        raise ValueError('Missing target model config')
    if cfg.get('calibration') is not None:
        raise ValueError('Metric calibration is not implemented: calibration must remain null; outputs are image-space only')
    return cfg


def process_frame(image, cfg, timestamp, index):
    if image is None or image.ndim not in (2,3) or not math.isfinite(timestamp) or timestamp<0:
        raise ValueError('Invalid image/timestamp')
    gray=cv2.cvtColor(image,cv2.COLOR_BGR2GRAY) if image.ndim==3 else image
    dictionary=cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco,cfg['dictionary']))
    detector=cv2.aruco.ArucoDetector(dictionary,cv2.aruco.DetectorParameters())
    corners,ids,rejected=detector.detectMarkers(gray)
    markers=[]
    for marker_id, corner in zip([] if ids is None else ids.flatten(),corners):
        pts=corner.reshape(4,2).astype(float); center=pts.mean(axis=0); dx,dy=pts[1]-pts[0]
        markers.append({'id':int(marker_id),'x':float(center[0]),'y':float(center[1]),'angle_deg':math.degrees(math.atan2(dy,dx)),
                        'corners':pts.tolist(),'visible':True,'area_px2':float(abs(cv2.contourArea(pts.astype(np.float32))))})
    markers.sort(key=lambda x:(x['id'],x['x'],x['y']))
    ids=[x['id'] for x in markers]; duplicate=sorted({x for x in ids if ids.count(x)>1})
    reference=cfg['reference_id']
    ref_state='unassigned' if reference is None else ('ambiguous' if reference in duplicate else 'visible' if reference in ids else 'missing')
    return {'schema':'robo-prober-vision-frame/1','index':index,'timestamp_s':timestamp,'units':{'position':'pixels','angle':'image_plane_degrees'},
            'markers':markers,'reference_status':ref_state,'visibility':{str(x['id']):('ambiguous' if x['id'] in duplicate else 'visible' if x['id'] in ids else 'missing') for x in cfg['markers']},
            'quality':{'rejected_candidates':len(rejected),'duplicate_ids':duplicate,'mean_gray':float(gray.mean()),'std_gray':float(gray.std()),
                       'diagnosis':'detected' if markers else 'no markers decoded; check dictionary, tag size, focus, occlusion, lighting'},
            'metric_pose':None,'world_compensation':False}


def overlay(image, frame):
    result=image.copy()
    for m in frame['markers']:
        pts=np.asarray(m['corners'],np.int32)
        cv2.polylines(result,[pts],True,(0,220,0),2)
        cv2.putText(result,str(m['id']),tuple(pts[0]),cv2.FONT_HERSHEY_SIMPLEX,.8,(0,220,0),2)
    text=f"frame {frame['index']} | reference {frame['reference_status']} | PIXELS ONLY | IDs {[m['id'] for m in frame['markers']]}"
    cv2.putText(result,text,(12,28),cv2.FONT_HERSHEY_SIMPLEX,.55,(0,190,255),2)
    return result


def capture(device, config_path, out, max_frames=30, seconds=5., width=1920,height=1080, preview_window=False, capture_factory=None, evidence_kind='real_camera'):
    cfg=load_config(config_path)
    if type(max_frames) is not int or not 1<=max_frames<=300 or not math.isfinite(seconds) or not 0<seconds<=30 or type(width) is not int or type(height) is not int or not 64<=width<=4096 or not 64<=height<=2160:
        raise ValueError('Capture requires 1..300 frames, >0..30 seconds, width 64..4096 and height 64..2160')
    inventory=cameras()
    matches=[x for x in inventory if x['device']==device and x['capture_candidate']]
    if capture_factory is None and len(matches)!=1:
        raise ValueError('Select an enumerated physical capture device, excluding loopback and metadata nodes')
    if capture_factory is not None and evidence_kind!='synthetic_fixture':
        raise ValueError('Injected cameras must be labeled synthetic_fixture')
    out=Path(out); out.mkdir(parents=True,exist_ok=False); (out/'frames').mkdir()
    save_json(out/'config.json',cfg)
    cap=None
    start=time.monotonic(); started_utc=datetime.now(timezone.utc).isoformat(); records=[]; assets=[]; reason='bounded'; error=None; resolution=None
    try:
        cap=(capture_factory or (lambda path:cv2.VideoCapture(path,cv2.CAP_V4L2)))(device)
        if not cap.isOpened():
            raise ValueError(f'Could not open camera {device}')
        cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,width); cap.set(cv2.CAP_PROP_FRAME_HEIGHT,height)
        cap.set(cv2.CAP_PROP_FPS,30); cap.set(cv2.CAP_PROP_BUFFERSIZE,1)
        # V4L2/OpenCV has a backend read timeout; CLI adds a process watchdog.
        while len(records)<max_frames and time.monotonic()-start<seconds:
            ok,image=cap.read()
            if not ok or image is None:
                reason='disconnect'; break
            timestamp=time.monotonic()-start
            resolution=list(image.shape[1::-1])
            frame=process_frame(image,cfg,timestamp,len(records))
            file=f'frames/{len(records):06d}.png'
            if not cv2.imwrite(str(out/file),image):
                raise ValueError('Failed to record frame')
            frame['image']=file; frame['image_sha256']=digest(out/file)
            records.append(frame); assets.append({'path':file,'sha256':frame['image_sha256']})
            if len(records)==1:
                cv2.imwrite(str(out/'annotated.png'),overlay(image,frame))
            if preview_window:
                cv2.imshow('Robo Prober - image space only',overlay(image,frame))
                if cv2.waitKey(1)&255==ord('q'):
                    reason='operator_stop'; break
    except KeyboardInterrupt:
        reason='interrupted'
    except Exception as exc:
        reason='error'; error=str(exc)
    finally:
        if cap is not None:
            cap.release()
        if preview_window:
            cv2.destroyAllWindows()
        (out/'detections.jsonl').write_text(''.join(json.dumps(x,allow_nan=False)+'\n' for x in records))
        summary={'schema':'robo-prober-vision-session/1','evidence_kind':evidence_kind,'camera':matches[0] if matches else {'device':device,'name':'synthetic fixture'},
                 'started_utc':started_utc,'finished_utc':datetime.now(timezone.utc).isoformat(),'timestamp_semantics':'host monotonic time after read, UTC start before camera open; not exposure timestamps','duration_s':time.monotonic()-start,'requested_bounds':{'frames':max_frames,'seconds':seconds},
                 'frames':len(records),'resolution':resolution,'dictionary':cfg['dictionary'],'dictionary_confirmed':cfg.get('dictionary_confirmed',False),
                 'config_sha256':digest(out/'config.json'),'calibration':None,'units':'pixels and image-plane degrees','detected_ids':sorted({m['id'] for r in records for m in r['markers']}),
                 'stop_reason':reason,'error':error,'camera_released':True,'assets':assets,'detections_sha256':digest(out/'detections.jsonl')}
        save_json(out/'session.json',summary)
    if error or not records:
        raise ValueError(error or 'No frames captured; see session.json')
    return summary


def read_session(folder):
    folder=Path(folder); s=json.loads((folder/'session.json').read_text()); cfg=load_config(folder/'config.json')
    if digest(folder/'config.json')!=s['config_sha256'] or digest(folder/'detections.jsonl')!=s['detections_sha256']:
        raise ValueError('Session provenance mismatch')
    rows=[json.loads(x) for x in (folder/'detections.jsonl').read_text().splitlines()]
    if not rows or len(rows)!=s['frames'] or len(s['assets'])!=len(rows):
        raise ValueError('Session frame count mismatch')
    previous=-1
    for i,(r,asset) in enumerate(zip(rows,s['assets'])):
        if r['index']!=i or not math.isfinite(r['timestamp_s']) or r['timestamp_s']<previous or r['image']!=asset['path'] or r['image_sha256']!=asset['sha256'] or digest(folder/r['image'])!=asset['sha256']:
            raise ValueError('Invalid frame order/timestamp/asset provenance')
        previous=r['timestamp_s']
        regenerated=process_frame(cv2.imread(str(folder/r['image'])),cfg,r['timestamp_s'],i)
        regenerated.update(image=r['image'],image_sha256=r['image_sha256'])
        if regenerated != r:
            raise ValueError('Detection data does not reproduce recorded frame')
    return s,cfg,rows


def replay(folder,out):
    folder=Path(folder); s,cfg,rows=read_session(folder); output=[]
    out=Path(out); out.mkdir(parents=True,exist_ok=False)
    for row in rows:
        image=cv2.imread(str(folder/row['image']))
        record=process_frame(image,cfg,row['timestamp_s'],row['index'])
        record.update({k:row[k] for k in ('image','image_sha256')})
        output.append(record)
    (out/'detections.jsonl').write_text(''.join(json.dumps(x,allow_nan=False)+'\n' for x in output))
    result={'schema':'robo-prober-vision-replay/1','source_session':str(folder.resolve()),'source_session_sha256':digest(folder/'session.json'),
            'frames':len(output),'exact_detection_agreement':rows==output,'evidence_kind':s['evidence_kind'],'detections_sha256':digest(out/'detections.jsonl')}
    save_json(out/'replay.json',result)
    cv2.imwrite(str(out/'annotated.png'),overlay(cv2.imread(str(folder/rows[0]['image'])),output[0]))
    return result


def export_observation(folder,out,before=0,after=-1):
    s,cfg,rows=read_session(folder)
    b,a=rows[before],rows[after]
    status='ready' if cfg['reference_id'] is not None and all(x['reference_status']=='visible' and not x['quality']['duplicate_ids'] for x in (b,a)) else 'incomplete'
    if not (set(m['id'] for m in b['markers'])&set(m['id'] for m in a['markers'])-{cfg['reference_id']}):
        status='incomplete'
    result={'schema':'robo-prober-observation-export/1','kind':'recorded_camera_image_space' if s['evidence_kind']=='real_camera' else 'synthetic_fixture',
            'status':status,'source_session':str(Path(folder).resolve()),'source_session_sha256':digest(Path(folder)/'session.json'),
            'reference_marker_id':cfg['reference_id'],'marker_link_map':{str(x['id']):x['link'] for x in cfg['markers']},
            'before':b,'after':a,'threshold':cfg['pixel_threshold'],'pixel_threshold':cfg['pixel_threshold'],'angle_threshold_deg':cfg['angle_threshold_deg'],
            'units':{'position':'pixels','angle':'image_plane_degrees'},'metric_pose':None,
            'warning':'Pixel reference subtraction is not world-frame compensation; missing poses are not inferred. Motion is not a joint assignment.'}
    save_json(out,result); return result


def dictionary_candidates(image_path):
    image=cv2.imread(str(image_path)); results={}
    if image is None:
        raise ValueError('Unreadable image')
    for name in ['DICT_4X4_50','DICT_5X5_50','DICT_6X6_50','DICT_7X7_50','DICT_ARUCO_ORIGINAL','DICT_APRILTAG_36h11']:
        d=cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco,name))
        _,ids,_=cv2.aruco.ArucoDetector(d).detectMarkers(image)
        results[name]=[] if ids is None else sorted(int(i) for i in ids.flatten())
    return {'image_sha256':digest(image_path),'candidates':results,'warning':'Diagnostic subset only; overlapping dictionaries can decode the same tag. Operator must choose dictionary and IDs.'}
