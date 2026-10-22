"""CLI additions isolated from controller/probe commands."""
import json
import multiprocessing
from pathlib import Path


def _capture_child(kwargs, queue):
    import signal
    from .vision_pipeline import capture
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    try:
        capture(**kwargs)
        queue.put({'result_path':str(Path(kwargs['out'])/'session.json')})
    except BaseException as exc:
        queue.put({'error':str(exc) or type(exc).__name__})


def bounded_capture(**kwargs):
    # Own short-lived child only; bound native driver stalls and parent interruption.
    import math
    seconds=kwargs['seconds']
    if type(seconds) not in (float,int) or not math.isfinite(seconds) or not 0<seconds<=30:
        raise ValueError('Capture seconds must be >0 and <=30')
    ctx=multiprocessing.get_context('spawn'); queue=ctx.Queue()
    worker=ctx.Process(target=_capture_child,args=(kwargs,queue))
    worker.start()
    try:
        worker.join(seconds+12)
        if worker.is_alive():
            raise ValueError('Camera backend exceeded bounded watchdog; owned capture process will be stopped; inspect partial session')
        result=queue.get(timeout=2)
        if 'error' in result:
            raise ValueError(result['error'])
        return json.loads(Path(result['result_path']).read_text())
    finally:
        if worker.is_alive():
            worker.terminate(); worker.join(2)
            if worker.is_alive():
                worker.kill(); worker.join(2)
        queue.close()


def run(args):
    command=args.sensing_action
    if command in ('prepare','propose','segment','validate-segments'):
        from . import geometry as g
    if command=='cameras':
        from .vision_pipeline import cameras
        result={'cameras':cameras()}
    elif command=='capture':
        result=bounded_capture(device=args.device,config_path=args.config,out=args.out,max_frames=args.frames,seconds=args.seconds,width=args.width,height=args.height,preview_window=args.preview)
    elif command=='replay':
        from .vision_pipeline import replay
        result=replay(args.session,args.out)
    elif command=='export':
        from .vision_pipeline import export_observation
        result=export_observation(args.session,args.out,args.before,args.after)
    elif command=='dictionaries':
        from .vision_pipeline import dictionary_candidates
        result=dictionary_candidates(args.image)
    elif command=='prepare':
        result=g.prepare(args.source,args.out,args.local)
    elif command=='propose':
        result=g.propose(args.source,args.out,args.parts)
    elif command=='segment':
        result=g.segment(args.config,args.out)
    elif command=='validate-segments':
        result=g.validate_segments(args.manifest)
    elif command=='articulate':
        from .articulation import export_articulation
        result=export_articulation(args.config,args.out,json.loads(args.positions.read_text()) if args.positions else None)
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0


def register(sub):
    vision=sub.add_parser('vision',help='Camera-only observations; never invokes controller code')
    vs=vision.add_subparsers(dest='sensing_action',required=True)
    p=vs.add_parser('cameras',help='List V4L2 cameras without opening them'); p.set_defaults(func=run)
    p=vs.add_parser('capture',help='Bounded camera capture with ArUco detection'); p.set_defaults(func=run)
    p.add_argument('--device',required=True); p.add_argument('--config',required=True,type=Path); p.add_argument('--out',required=True,type=Path)
    p.add_argument('--frames',type=int,default=30); p.add_argument('--seconds',type=float,default=5.)
    p.add_argument('--width',type=int,default=1920); p.add_argument('--height',type=int,default=1080); p.add_argument('--preview',action='store_true')
    p=vs.add_parser('replay',help='Re-run detection on a recorded session and check agreement'); p.set_defaults(func=run); p.add_argument('session',type=Path); p.add_argument('--out',required=True,type=Path)
    p=vs.add_parser('export',help='Export a before/after observation for probe/map export'); p.set_defaults(func=run); p.add_argument('session',type=Path); p.add_argument('--out',required=True,type=Path)
    p.add_argument('--before',type=int,default=0); p.add_argument('--after',type=int,default=-1)
    p=vs.add_parser('dictionaries',help='Try common ArUco dictionaries on one image'); p.set_defaults(func=run); p.add_argument('image',type=Path)
    model=sub.add_parser('model',help='Scan segmentation and explicitly configured articulation')
    ms=model.add_subparsers(dest='sensing_action',required=True)
    p=ms.add_parser('prepare',help='Copy and inspect a source OBJ mesh'); p.set_defaults(func=run); p.add_argument('source',type=Path); p.add_argument('--local',required=True,type=Path); p.add_argument('--out',required=True,type=Path)
    p=ms.add_parser('propose',help='Write an editable automatic segmentation proposal'); p.set_defaults(func=run); p.add_argument('source',type=Path); p.add_argument('--out',required=True,type=Path); p.add_argument('--parts',type=int,default=4)
    p=ms.add_parser('segment',help='Apply a segmentation config and export candidate link meshes'); p.set_defaults(func=run); p.add_argument('--config',required=True,type=Path); p.add_argument('--out',required=True,type=Path)
    p=ms.add_parser('validate-segments',help='Re-verify a segmentation output against its source'); p.set_defaults(func=run); p.add_argument('manifest',type=Path)
    p=ms.add_parser('articulate',help='Export a confirmed articulation config to URDF'); p.set_defaults(func=run); p.add_argument('--config',required=True,type=Path); p.add_argument('--out',required=True,type=Path); p.add_argument('--positions',type=Path)
