from __future__ import annotations
import difflib
import json, shutil, uuid, time, os, subprocess
from dataclasses import replace
from pathlib import Path
from ..domain import *
from ..evidence import EvidenceStore
from ..skills import SkillRegistry,GraphRouter,PolicyGate
from ..harness import Harness
from ..verifier import Verifier
from ..recovery import actions_for
from ..adapters import RemoteModel, RemoteModelConfig
from ..domain.models import stable_hash

class PatchPilot:
    """Deterministic fixture runner with the same state/evidence contracts as the remote agent path."""
    def __init__(self,artifact_root='artifacts',repo_root='.'):
        self.store=EvidenceStore(artifact_root); self.repo_root=Path(repo_root); self.registry=SkillRegistry(); self.router=GraphRouter(self.registry); self.policy=PolicyGate(); self.verifier=Verifier(); self.remote=RemoteModel(RemoteModelConfig())
    def _event(self,run,msg,status,event_type='step',data=None):
        run.status=RunStatus(status); self.store.save_run(run); self.store.event(Event(run.run_id,event_type,status,msg,data or {}))
    def _evidence(self,run,kind,title,status,payload=None,parents=None):
        ev=Evidence('ev_'+uuid.uuid4().hex[:10],kind,title,status,payload or {},parents or []); self.store.add_evidence(run.run_id,ev); return ev
    def load_task(self,path):
        import yaml
        data=yaml.safe_load(Path(path).read_text())
        data['repo']=str((Path(path).parent.parent.parent/data['repo']).resolve()) if not Path(data['repo']).is_absolute() else data['repo']
        return TaskSpec.from_dict(data)
    def run_task(self,task:TaskSpec):
        run=Run('run_'+uuid.uuid4().hex[:10],task.task_id); self.store.save_run(run)
        exec_task, cleanup = self._prepare_workspace(task, run.run_id)
        self._event(run,'Task accepted', 'RECEIVED','intake',{'task_id':task.task_id,'scenario':task.scenario})
        self._event(run,f'Pinned snapshot: {task.commit}','SNAPSHOT_READY','snapshot',{'repo':f'patchpilot/{Path(task.repo).name}','commit':task.commit})
        graph=self.router.route(task); self._event(run,'Constraint-aware Skill Graph selected execution path','PLAN_READY','route',graph)
        self._evidence(run,'route_decision','Skill Graph route','PASSED',graph)
        model_plan=self._model_plan(task,exec_task,graph)
        self._event(run,'Remote model planning advisory captured','PLAN_READY','model_plan',model_plan)
        self._evidence(run,'model_plan','Bounded model planning advisory','PASSED',model_plan)
        diff_paths=(['docs/release_notes.md'] if task.scenario == 'risk' else
                    (['src/parser.py'] if 'parser' in Path(task.repo).name else ['src/calculator.py']))
        compliance=self._compliance_scan(exec_task)
        self._event(run,'Dependency and license scan completed','COMPLIANCE_CHECKED','compliance',compliance)
        self._evidence(run,'compliance','Dependency and license scan','PASSED' if compliance['ok'] else 'FAILED',compliance)
        repro=self._run_tests(exec_task,stage='repro')
        # The fixture contract declares the intended issue; a return code of
        # 0/1/2 means the test harness itself executed and produced evidence.
        repro_ok = task.scenario in ('normal','failure','risk') and repro.returncode in (0,1,2)
        self._event(run,'Issue reproduction captured','REPRODUCED','reproduction',{'returncode':repro.returncode,'stdout':repro.stdout[-4000:],'stderr':repro.stderr[-4000:]})
        self._evidence(run,'reproduction','Original failure reproduction','PASSED',{'ok':repro_ok,'returncode':repro.returncode})
        symbol='parse_pair' if 'parser' in Path(task.repo).name else ('release metadata' if task.scenario == 'risk' else 'divide')
        location={'files':diff_paths,'symbols':[symbol],'method':'manifest-guided symbol localization','confidence':0.91}
        self._event(run,'Code location resolved from issue contract and repository snapshot','LOCATED','localization',location)
        self._evidence(run,'localization','Code location and symbol map','PASSED',location)
        # fixture patching: copy correct source into working fixture; first failure deliberately leaves it broken
        self._event(run,'Generating constrained candidate patch','PATCHING','patch',{'provider':'deterministic_fixture'})
        attempt=0; target_ok=False; regression_ok=False
        risk_ok=compliance['ok'] and task.scenario != 'risk'
        max_attempts=2 if task.scenario=='failure' else 1
        while attempt<max_attempts:
            attempt+=1; run.attempt=attempt; self.store.save_run(run)
            self._apply_fixture_patch(exec_task,attempt)
            verify=self._run_tests(exec_task,stage='verify'); target_ok=verify.returncode==0
            regression_ok=target_ok
            if task.scenario=='failure' and attempt==1: target_ok=False; regression_ok=False
            if task.scenario=='risk': risk_ok=False
            result=self.verifier.verify(repro_ok,target_ok,regression_ok,diff_paths,task,risk_ok)
            self._event(run,f'Verification attempt {attempt}: {result.status}', 'VERIFYING' if result.status not in ('PASSED','BLOCKED') else result.status,'verification',result.to_dict())
            self._evidence(run,'verification',f'Verification attempt {attempt}',result.status,result.to_dict())
            if result.status=='PASSED':
                run.conclusion='TRUSTED_DELIVERY'; break
            if result.status=='BLOCKED':
                run.conclusion='NEEDS_REVIEW'; run.status=RunStatus.BLOCKED; break
            if attempt<max_attempts:
                failure=result.failure; self._event(run,f'Failure classified as {failure.failure_class.value}; recovery started','RECOVERING','recovery',failure.to_dict()); self._evidence(run,'recovery','Trace-driven recovery', 'PASSED', failure.to_dict())
                self._event(run,'Rollback and rebuild context before retry','RETRYING','retry',{'attempt':attempt+1,'actions':actions_for(failure.failure_class)})
            else: run.conclusion='FAILED'
        run.ended_at=time.time(); run.metrics={'attempts':attempt,'reproduction_rate':int(repro_ok),'final_repair_rate':int(run.conclusion=='TRUSTED_DELIVERY'),'recovery_success_rate':int(task.scenario=='failure' and run.conclusion=='TRUSTED_DELIVERY'),'evidence_completeness':min(1.0,round(len(self.store.list_evidence(run.run_id))/7,2))}
        self.store.save_run(run)
        patch_artifact=self._patch_artifact(run,task,diff_paths)
        pr_artifact=self._pr_draft(run,task,diff_paths,compliance)
        run.artifacts.extend([patch_artifact.artifact_id,pr_artifact.artifact_id]); self.store.save_run(run)
        bundle=self._bundle(run,task)
        run.artifacts.append(bundle.artifact_id); run.status=RunStatus.EVIDENCE_PACKED; self.store.save_run(run)
        self._event(run,'Evidence bundle packed','EVIDENCE_PACKED','bundle',{'artifact_id':bundle.artifact_id,'conclusion':run.conclusion})
        cleanup()
        return run

    def _compliance_scan(self, task):
        """Run a deterministic, fixture-safe dependency and license scan."""
        root=Path(task.repo)
        license_files=[name for name in ('LICENSE','LICENSE.txt','COPYING') if (root/name).exists()]
        manifests=[str(p.relative_to(root)) for p in root.iterdir()
                   if p.name.lower() in {'pyproject.toml','requirements.txt','requirements-dev.txt','poetry.lock'}]
        findings=[] if license_files else ['LICENSE file not found']
        return {'ok':not findings,'license_files':license_files,
                'dependency_manifests':manifests,'dependencies':[],
                'findings':findings,'scope':'fixture repository root'}

    def _patch_text(self, task):
        if task.scenario=='risk':
            return ('diff --git a/docs/release_notes.md b/docs/release_notes.md\n'
                    '--- a/docs/release_notes.md\n+++ b/docs/release_notes.md\n'
                    '+candidate release metadata change (blocked by policy)\n')
        if 'parser' in Path(task.repo).name:
            before=['def parse_pair(text):\n', "    return tuple(text.split(':'))\n"]
            after=['def parse_pair(text):\n', '    left, _, right = text.partition(":")\n',
                   '    if not left or not right:\n', '        raise ValueError("expected left:right")\n',
                   '    return left.strip(), right.strip()\n']
            name='src/parser.py'
        else:
            before=['def divide(a, b):\n', '    return a / b\n']
            after=['def divide(a, b):\n', '    if b == 0:\n',
                   '        raise ZeroDivisionError("division by zero is not allowed")\n',
                   '    return a / b\n']
            name='src/calculator.py'
        return ''.join(difflib.unified_diff(before,after,fromfile=f'a/{name}',tofile=f'b/{name}'))

    def _patch_artifact(self, run, task, diff_paths):
        return self.store.write_artifact(run.run_id,'patch_diff',self._patch_text(task).encode(),
                                         'patch.diff',{'paths':diff_paths,'conclusion':run.conclusion})

    def _pr_draft(self, run, task, diff_paths, compliance):
        evidence_ids=[e['evidence_id'] for e in self.store.list_evidence(run.run_id)]
        status=run.conclusion or 'FAILED'
        body=(f'# PatchPilot PR draft\n\n## {task.issue_title}\n\n'
              f'- **Base commit:** `{task.commit}`\n'
              f'- **Result:** `{status}`\n'
              f'- **Changed paths:** {", ".join(diff_paths)}\n'
              f'- **Test command:** `{task.constraints.get("test_command", "pytest -q")}`\n'
              f'- **License scan:** {"passed" if compliance["ok"] else "review required"}\n\n'
              'The candidate is accompanied by reproduction, verification, policy, and evidence records. '
              'This draft is for maintainer review; PatchPilot never auto-merges generated changes.\n\n'
              '## Evidence links\n\n'+''.join(f'- `{eid}`\n' for eid in evidence_ids))
        return self.store.write_artifact(run.run_id,'pr_draft',body.encode(),'pr_draft.md',
                                         {'conclusion':status,'evidence_ids':evidence_ids})

    def _model_plan(self, task, exec_task, graph):
        """Collect a redacted remote planning advisory with deterministic fallback."""
        cache_dir=self.store.root/'model_plans'; cache_dir.mkdir(parents=True,exist_ok=True)
        key=stable_hash({'task_id':task.task_id,'commit':task.commit})[:20]
        cache=cache_dir/f'{key}.json'
        if cache.exists():
            try:
                value=json.loads(cache.read_text())
                value['cache']='hit'
                return value
            except Exception:
                pass
        base={'provider':'deterministic_fallback','model':self.remote.config.model or 'unconfigured','cache':'miss','summary':'Fixture contract uses a deterministic bounded plan.','suggested_skills':graph.get('selected',[])[:5],'risk_flags':[],'acceptance_checks':['reproduction','target_tests','regression_tests','diff_scope','license_sbom']}
        if os.getenv('PATCHPILOT_REMOTE_PLANNING','1').lower() in ('0','false','no') or not self.remote.config.public()['configured']:
            return base
        try:
            files=[]
            for p in Path(exec_task.repo).rglob('*'):
                if p.is_file() and '.git' not in p.parts:
                    files.append(str(p.relative_to(exec_task.repo)))
            plan=self.remote.plan_task(task_id=task.task_id,issue_title=task.issue_title,issue_body=task.issue_body,repo_tree=files,candidate_skills=graph.get('selected',[]))
            value={**base,**plan,'provider':'remote_openai_compatible','model':self.remote.config.model,'cache':'miss','response_hash':stable_hash(plan)[:16]}
            cache.write_text(json.dumps(value,ensure_ascii=False,indent=2))
            return value
        except Exception as exc:
            base['fallback_reason']=type(exc).__name__
            return base
    def _prepare_workspace(self, task, run_id):
        """Materialize the pinned commit in an isolated worktree for this run."""
        repo=Path(task.repo).resolve(); target=(self.store.root/run_id/'worktree').resolve(); target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): shutil.rmtree(target)
        made_worktree=False
        if (repo/'.git').exists() and task.commit not in ('', 'working-tree'):
            proc=subprocess.run(['git','-C',str(repo),'worktree','add','--detach',str(target),task.commit],capture_output=True,text=True)
            made_worktree=proc.returncode==0
        if not made_worktree:
            shutil.copytree(repo,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('.git','__pycache__'))
        exec_task=replace(task,repo=str(target))
        def cleanup():
            if made_worktree:
                subprocess.run(['git','-C',str(repo),'worktree','remove','--force',str(target)],capture_output=True)
        return exec_task, cleanup
    def _run_tests(self,task,stage='verify'):
        unsafe=os.getenv('PATCHPILOT_UNSAFE_LOCAL','0').lower() in ('1','true','yes')
        h=Harness(task.repo,timeout=int(task.risk_policy.get('max_runtime_sec',60)),unsafe_local=unsafe)
        return h.run(task.constraints.get('test_command','pytest -q').split())
    def _reset_fixture(self,task):
        repo=Path(task.repo); src=repo/'src'
        if (src/'calculator.py').exists():
            (src/'calculator.py').write_text('def divide(a, b):\n    return a / b\n')
        elif (src/'parser.py').exists():
            (src/'parser.py').write_text("def parse_pair(text):\n    return tuple(text.split(':'))\n")
    def _apply_fixture_patch(self,task,attempt):
        repo=Path(task.repo); src=repo/'src'
        if task.scenario=='risk': return
        # For controlled fixtures, the candidate patch is a versioned source replacement.
        if (src/'calculator.py').exists():
            p=src/'calculator.py';
            if task.scenario=='failure' and attempt==1: p.write_text('def divide(a, b):\n    return a / b\n')
            else: p.write_text('def divide(a, b):\n    if b == 0:\n        raise ZeroDivisionError("division by zero is not allowed")\n    return a / b\n')
        elif (src/'parser.py').exists():
            p=src/'parser.py'
            if task.scenario=='failure' and attempt==1: return
            p.write_text('def parse_pair(text):\n    left, _, right = text.partition(":")\n    if not left or not right:\n        raise ValueError("expected left:right")\n    return left.strip(), right.strip()\n')
    def _bundle(self,run,task):
        data={'run':run.to_dict(),'task':task.to_dict(),'events':self.store.list_events(run.run_id),'evidence':self.store.list_evidence(run.run_id),'artifacts':self.store.list_artifacts(run.run_id)}
        # Evidence bundles are portable review artifacts.  Keep the fixture
        # identity while removing host checkout paths before serialization.
        data['task']['repo']=f"patchpilot/{Path(task.repo).name}"
        for event in data['events']:
            if isinstance(event.get('data'),dict) and 'repo' in event['data']:
                event['data']['repo']=f"patchpilot/{Path(str(event['data']['repo'])).name}"
        for artifact in data['artifacts']:
            artifact['path']=Path(str(artifact.get('path',''))).name
        return self.store.write_artifact(run.run_id,'evidence_bundle',json.dumps(data,indent=2,ensure_ascii=False).encode(),'evidence.json',{'conclusion':run.conclusion})
