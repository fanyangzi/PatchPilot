from __future__ import annotations
import argparse, json, os, platform, shutil, sys
from pathlib import Path
from .adapters import RemoteModel,RemoteModelConfig
from .orchestrator import PatchPilot
from .skills import SkillRegistry

def _docker_daemon():
    if not shutil.which('docker'): return False
    import subprocess
    try: return subprocess.run(['docker','info'],capture_output=True,timeout=3).returncode==0
    except Exception: return False

def _tool_version(name):
    """Return a short installed-tool version without exposing environment data."""
    path=shutil.which(name)
    if not path: return None
    import subprocess
    try:
        proc=subprocess.run([path,'--version'],capture_output=True,text=True,timeout=3)
        value=(proc.stdout or proc.stderr).strip().splitlines()[0]
        return value[:120] if value else 'installed'
    except Exception: return 'installed'

def doctor(args):
    c=RemoteModelConfig(); out={'python':platform.python_version(),'platform':platform.platform(),'uv':_tool_version('uv'),'git':bool(shutil.which('git')),'docker':bool(shutil.which('docker')), 'docker_daemon':_docker_daemon(),'remote_model':c.public(),'skills':len(SkillRegistry().all()),'registry_errors':SkillRegistry().validate()}
    if args.remote and c.public()['configured']:
        try: out['remote_smoke']='ok' if 'status' in RemoteModel(c).doctor() else 'response_received'
        except Exception as e: out['remote_smoke']=f'failed:{type(e).__name__}'
    print(json.dumps(out,indent=2,ensure_ascii=False)); return 0

def run(args):
    pp=PatchPilot(args.artifacts); task=pp.load_task(args.task); result=pp.run_task(task); print(json.dumps(result.to_dict(),indent=2,ensure_ascii=False)); return 0 if result.conclusion in ('TRUSTED_DELIVERY','NEEDS_REVIEW') else 1

def bundle(args):
    p=Path(args.artifacts)/args.run_id
    if not p.exists(): print('run not found',file=sys.stderr); return 1
    print(json.dumps({'run_id':args.run_id,'path':str(p.resolve()),'files':[str(x) for x in p.iterdir()]},indent=2)); return 0

def main(argv=None):
    ap=argparse.ArgumentParser(prog='patchpilot'); sub=ap.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('doctor'); d.add_argument('--remote',action='store_true'); d.set_defaults(fn=doctor)
    r=sub.add_parser('run'); r.add_argument('--task',required=True); r.add_argument('--artifacts',default='artifacts'); r.set_defaults(fn=run)
    b=sub.add_parser('bundle'); b.add_argument('--run-id',required=True); b.add_argument('--artifacts',default='artifacts'); b.set_defaults(fn=bundle)
    a=ap.parse_args(argv); return a.fn(a)
if __name__=='__main__': raise SystemExit(main())
