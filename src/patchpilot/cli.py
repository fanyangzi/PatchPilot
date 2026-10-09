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
    from .evidence.store import EvidenceStore
    if getattr(args, "bundle_action", None) == "check":
        result = EvidenceStore(args.artifacts).check_artifacts(args.run_id)
        if args.json:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        elif result["valid"]:
            print(f'✓ Evidence bundle for {args.run_id} is valid ({result["artifact_count"]} artifacts)')
        else:
            print(f'✗ Evidence bundle for {args.run_id} is invalid', file=sys.stderr)
            for failure in result["failures"]:
                print(f'  {failure.get("artifact_id", "artifact")}: {failure.get("error", "invalid")}', file=sys.stderr)
        return 0 if result["valid"] else 1
    p=Path(args.artifacts)/args.run_id
    if not p.exists(): print('run not found',file=sys.stderr); return 1
    files = sorted(str(x.relative_to(p)) for x in p.iterdir())
    print(json.dumps({'run_id':args.run_id,'files':files},indent=2,ensure_ascii=False)); return 0

def verify(args):
    """验证指定 run_id 的证据链完整性"""
    from .evidence.store import EvidenceStore
    from .domain.models import compute_event_hash, compute_verdict_hash, Event

    store = EvidenceStore(args.artifacts)
    run_data = store.get_run(args.run_id)
    if not run_data:
        result = {'valid': False, 'error': 'run not found'}
        if args.json:
            print(json.dumps(result))
        else:
            print(f'✗ Run {args.run_id} not found', file=sys.stderr)
        return 1

    # 1. 验证事件哈希链
    events = store.list_events(args.run_id)
    prev_hash = None
    for event in events:
        expected_hash = compute_event_hash(Event(**event), prev_hash)
        actual_hash = event.get('event_hash')
        if expected_hash != actual_hash:
            result = {
                'valid': False,
                'error': 'event_hash_mismatch',
                'event_id': event['event_id'],
                'expected': expected_hash,
                'actual': actual_hash
            }
            if args.json:
                print(json.dumps(result))
            else:
                print(f'✗ Event hash mismatch in {event["event_id"]}', file=sys.stderr)
                print(f'  Expected: {expected_hash[:16]}...', file=sys.stderr)
                print(f'  Actual: {actual_hash[:16] if actual_hash else "None"}...', file=sys.stderr)
            return 1
        prev_hash = expected_hash

    # 2. Verify every artifact byte and path before trusting the report.
    artifact_check = store.check_artifacts(args.run_id)
    if not artifact_check["valid"]:
        result = {'valid': False, 'error': 'artifact_integrity_failed', 'run_id': args.run_id, 'artifacts': artifact_check}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f'✗ Artifact integrity failed for {args.run_id}', file=sys.stderr)
            for failure in artifact_check["failures"]:
                print(f'  {failure.get("artifact_id", "artifact")}: {failure.get("error", "invalid")}', file=sys.stderr)
        return 1

    # 3. 验证 verdict_hash (如果存在)
    verdict_valid = True
    if run_data.get('verdict_hash'):
        # 需要重新计算 verdict_hash
        artifacts = store.list_artifacts(args.run_id)
        evidence_list = store.list_evidence(args.run_id)

        # 找到 patch artifact
        patch_sha256 = ''
        for art in artifacts:
            if art.get('kind') == 'patch_diff':
                patch_sha256 = art.get('sha256', '')
                break

        # 计算 evidence_root_hash
        evidence_ids = sorted([e['evidence_id'] for e in evidence_list])
        from .domain.models import stable_hash
        evidence_root_hash = stable_hash(evidence_ids)

        # 获取最终 checks
        checks = run_data.get('metrics', {}).get('checks', {})

        # 重新计算 verdict_hash
        from .domain.models import Run, compute_verdict_hash
        run_obj = Run(**{k:v for k,v in run_data.items() if k in ['run_id','task_id','status','started_at','ended_at','conclusion','attempt','metrics','artifacts','verdict_hash']})
        commit = run_data.get('metrics', {}).get('commit', '')
        stored_patch_sha256 = run_data.get('metrics', {}).get('patch_sha256', patch_sha256)
        stored_verdict = run_data.get('verdict_hash')
        if stored_verdict:
            expected_verdict = compute_verdict_hash(run_obj, checks, run_obj.task_id, commit, stored_patch_sha256, evidence_root_hash)
            if expected_verdict != stored_verdict:
                result = {'valid': False, 'error': 'verdict_hash_mismatch', 'run_id': args.run_id,
                          'expected': expected_verdict, 'actual': stored_verdict}
                if args.json:
                    print(json.dumps(result))
                else:
                    print(f'FAIL Run {args.run_id}: verdict_hash mismatch')
                return 1

    result = {'valid': True, 'events_checked': len(events), 'run_id': args.run_id,
              'verdict_hash_verified': bool(run_data.get('verdict_hash')),
              'artifacts': artifact_check}
    if args.json:
        print(json.dumps(result))
    else:
        print(f'✓ Run {args.run_id} is valid')
        print(f'  Events checked: {len(events)}')
    if run_data.get('verdict_hash'):
            print(f'  Verdict hash: {run_data["verdict_hash"][:16]}...')
    return 0

def main(argv=None):
    ap=argparse.ArgumentParser(prog='patchpilot'); sub=ap.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('doctor'); d.add_argument('--remote',action='store_true'); d.set_defaults(fn=doctor)
    r=sub.add_parser('run'); r.add_argument('--task',required=True); r.add_argument('--artifacts',default='artifacts'); r.add_argument('--json',action='store_true'); r.set_defaults(fn=run)
    b=sub.add_parser('bundle'); b.add_argument('bundle_action', nargs='?', choices=['check']); b.add_argument('--run-id',required=True); b.add_argument('--artifacts',default='artifacts'); b.add_argument('--json',action='store_true'); b.set_defaults(fn=bundle)
    v=sub.add_parser('verify'); v.add_argument('--run-id',required=True); v.add_argument('--artifacts',default='artifacts'); v.add_argument('--json',action='store_true'); v.set_defaults(fn=verify)
    a=ap.parse_args(argv); return a.fn(a)
if __name__=='__main__': raise SystemExit(main())
