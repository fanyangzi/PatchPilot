from __future__ import annotations
import json, shutil, uuid, time, os, subprocess

_USE_REMOTE_PATCH = os.getenv('PATCHPILOT_USE_REMOTE_PATCH', '0').lower() in ('1', 'true', 'yes')
from urllib.parse import urlsplit, urlunsplit
from dataclasses import replace
from pathlib import Path
from ..domain import *
from ..evidence import EvidenceStore
from ..skills import SkillRegistry,GraphRouter
from ..harness import Harness
from ..verifier import Verifier
from ..recovery import actions_for
from ..adapters import RemoteModel, RemoteModelConfig
from ..domain.models import stable_hash
from ..policy import compile_policy
from ..policy.gate import PolicyGate

class PatchPilot:
    """Deterministic fixture runner with the same state/evidence contracts as the remote agent path."""
    def __init__(self,artifact_root='artifacts',repo_root='.'):
        self.store=EvidenceStore(artifact_root); self.repo_root=Path(repo_root); self.registry=SkillRegistry(); self.router=GraphRouter(self.registry); self.verifier=Verifier(); self.remote=RemoteModel(RemoteModelConfig())
    def _event(self,run,msg,status,event_type='step',data=None):
        run.status=RunStatus(status); self.store.save_run(run); self.store.event(Event(run.run_id,event_type,status,msg,data or {}))
    def _evidence(self,run,kind,title,status,payload=None,parents=None):
        ev=Evidence('ev_'+uuid.uuid4().hex[:10],kind,title,status,payload or {},parents or []); self.store.add_evidence(run.run_id,ev); return ev
    def load_task(self,path):
        import yaml
        path=Path(path).resolve()
        data=yaml.safe_load(path.read_text())
        root=self._project_root(path)
        if not self._is_git_url(data['repo']) and not Path(data['repo']).is_absolute():
            data['repo']=str((root/data['repo']).resolve())
        files=list(data.pop('patch_files',[]) or [])
        data['patches']=[self._read_patch(root,f) for f in files]; data['patch_sources']=files
        test_file=data.pop('test_patch_file',None)
        if test_file: data['test_patch']=self._read_patch(root,test_file); data['test_patch_source']=test_file
        return TaskSpec.from_dict(data)
    @staticmethod
    def _project_root(path):
        for parent in Path(path).resolve().parents:
            if (parent/'pyproject.toml').exists(): return parent
        return Path(path).resolve().parents[2]
    @staticmethod
    def _read_patch(root,rel):
        p=(root/rel).resolve()
        if not p.is_relative_to(root.resolve()): raise ValueError(f'patch file outside project root: {rel}')
        text=p.read_text()
        if not text.strip(): raise ValueError(f'patch file is empty: {rel}')
        return text
    @staticmethod
    def _git_apply(root,diff_text,reverse=False):
        cmd=['git','apply','--whitespace=nowarn']+(['-R'] if reverse else [])+['-']
        return subprocess.run(cmd,cwd=root,input=diff_text,capture_output=True,text=True,timeout=60)
    @staticmethod
    def _repo_label(repo):
        if repo.startswith(('http://','https://')):
            u=urlsplit(repo); return urlunsplit((u.scheme,u.hostname+(f':{u.port}' if u.port else ''),u.path,'',''))
        return f'patchpilot/{Path(repo).name}'
    def run_task(self,task:TaskSpec):
        run=Run('run_'+uuid.uuid4().hex[:10],task.task_id); self.store.save_run(run)
        try: exec_task, cleanup, workspace_mode = self._prepare_workspace(task, run.run_id)
        except Exception as exc:
            self._event(run,f'Workspace preparation failed: {exc}','FAILED','infra_error',{'error':str(exc)[-1000:]})
            run.conclusion='INFRA_ERROR'; run.ended_at=time.time(); self.store.save_run(run)
            return run
        try:
            self._event(run,'Task accepted', 'RECEIVED','intake',{'task_id':task.task_id,'scenario':task.scenario})
            self._event(run,f'Pinned snapshot: {task.commit}','SNAPSHOT_READY','snapshot',{'repo':self._repo_label(task.repo),'commit':task.commit,'workspace':workspace_mode})

            # Compile policy from repository's own declarations
            policy = compile_policy(exec_task.repo)
            # Merge task.risk_policy into compiled policy (task overrides for fixtures)
            if policy.source == 'default' and task.risk_policy.get('allowed_paths'):
                policy.allowed_paths = task.risk_policy['allowed_paths']
            if policy.source == 'default' and task.risk_policy.get('sensitive_patterns'):
                policy.sensitive_patterns = task.risk_policy['sensitive_patterns']
            self._event(run, f'Policy compiled from {policy.source}', 'PLAN_READY', 'policy', {
                'source': policy.source,
                'scope': policy.scope,
                'allowed_paths': policy.allowed_paths,
                'sensitive_patterns': policy.sensitive_patterns,
                'required_checks': policy.required_checks,
                'max_attempts': policy.max_attempts
            })
            self._evidence(run, 'policy', 'Repository policy compilation', 'PASSED', {
                'source': policy.source,
                'is_fail_closed': policy.is_fail_closed(),
                'path_count': len(policy.allowed_paths),
                'sensitive_count': len(policy.sensitive_patterns)
            })

            graph=self.router.route(task); self._event(run,'Constraint-aware Skill Graph selected execution path','PLAN_READY','route',graph)
            self._evidence(run,'route_decision','Skill Graph route','PASSED',graph)
            model_plan=self._model_plan(task,exec_task,graph)
            self._event(run,'Remote model planning advisory captured','PLAN_READY','model_plan',model_plan)
            self._evidence(run,'model_plan','Bounded model planning advisory','PASSED',model_plan)
            # diff_paths will be extracted from actual patch later; placeholder for compliance check
            diff_paths = []
            compliance=self._compliance_scan(exec_task)
            self._event(run,'Dependency and license scan completed','COMPLIANCE_CHECKED','compliance',compliance)
            self._evidence(run,'compliance','Dependency and license scan','PASSED' if compliance['ok'] else 'FAILED',compliance)
            if exec_task.test_patch:
                tp=self._git_apply(exec_task.repo,exec_task.test_patch)
                if tp.returncode!=0:
                    self._event(run,f'Test patch does not apply: {tp.stderr[-200:]}','FAILED','infra_error',{'source':exec_task.test_patch_source,'stderr':tp.stderr[-1000:]})
                    run.conclusion='INFRA_ERROR'; run.ended_at=time.time(); self.store.save_run(run)
                    return run
                test_patch_sha=stable_hash(exec_task.test_patch)
                self._event(run,'Test patch applied before reproduction','SNAPSHOT_READY','test_patch',{'source':exec_task.test_patch_source,'sha256':test_patch_sha})
            else: test_patch_sha=None
            install=self._install_deps(exec_task)
            if install is not None and install.returncode!=0:
                self._event(run,f'Dependency install failed: {install.stderr[-200:]}','FAILED','infra_error',{'returncode':install.returncode,'stderr':install.stderr[-1000:],'network':bool(task.risk_policy.get('network',False))})
                run.conclusion='INFRA_ERROR'; run.ended_at=time.time(); self.store.save_run(run)
                return run
            repro=self._run_tests(exec_task,stage='repro')
            # 125/126: harness unavailable; 2-5 and 124: collection/usage error, no tests, timeout -> not a reproduced failure
            if repro.returncode in (124, 125, 126, 2, 3, 4, 5):
                self._event(run, f'Infrastructure failure: {repro.stderr[:200] or repro.stdout[-200:]}', 'FAILED', 'infra_error',
                           {'returncode': repro.returncode, 'stderr': repro.stderr[:1000], 'stdout': repro.stdout[-1000:]})
                run.conclusion = 'INFRA_ERROR'
                run.ended_at = time.time()
                self.store.save_run(run)
                return run

            from ..verifier.checks import parse_pytest_verbose as _ppv
            failed_before=[n for n,ok in _ppv(repro.stdout+'\n'+repro.stderr).items() if not ok]
            declared=list(task.target_tests)
            target_tests=declared or list(failed_before)
            missing=[t for t in declared if t not in failed_before]
            repro_ok = repro.returncode == 1 and bool(target_tests) and not missing  # fail-before: every target test must fail on the pinned commit
            preexisting=[t for t in failed_before if t not in set(target_tests)]
            net=bool(task.risk_policy.get('network',False))
            repro_data={'returncode':repro.returncode,'target_tests':target_tests,'targets_source':'declared' if declared else 'all failing tests','missing_targets':missing,'failed_tests':failed_before,'preexisting_failures':len(preexisting),'network':net,'test_patch_sha256':test_patch_sha}
            self._event(run,'Issue reproduction captured','REPRODUCED','reproduction',{**repro_data,'stdout':repro.stdout[-4000:],'stderr':repro.stderr[-4000:]})
            repro_evidence = self._evidence(run,'reproduction','Original failure reproduction','PASSED' if repro_ok else 'FAILED',{'ok':repro_ok,**repro_data})
            location={'files':sorted({n.split('::')[0] for n in target_tests}),'symbols':sorted({n.split('::')[-1].split('[')[0] for n in target_tests}),'method':'failing-test locator (pytest -v node ids)'}
            self._event(run,'Failing tests located from reproduction output','LOCATED','localization',location)
            self._evidence(run,'localization','Failing test locations','PASSED',location, parents=[repro_evidence.evidence_id])

            candidates=list(exec_task.patches)
            max_attempts=max(1,len(candidates))
            if policy.source!='default': max_attempts=min(max_attempts,policy.max_attempts)
            attempt=0; applied_diff=''; last_candidate=''; patch_applied=False; result=None
            while attempt<max_attempts:
                attempt+=1; run.attempt=attempt; self.store.save_run(run)
                if applied_diff:
                    rb=self._git_apply(exec_task.repo,applied_diff,reverse=True)
                    if rb.returncode!=0:
                        self._event(run,f'Rollback of candidate {attempt-1} failed: {rb.stderr[-200:]}','FAILED','infra_error',{'stderr':rb.stderr[-1000:]})
                        run.conclusion='INFRA_ERROR'; break
                    applied_diff=''
                if _USE_REMOTE_PATCH and self.remote.config.public()['configured'] and attempt==1:
                    try:
                        _rp=Path(exec_task.repo); _rfiles={}
                        for _fp in (list((_rp/'src').rglob('*.py')) if (_rp/'src').exists() else list(_rp.rglob('*.py'))):
                            if len(_rfiles)>=10: break
                            try: _rfiles[str(_fp.relative_to(_rp))]=_fp.read_text()[:5000]
                            except Exception: pass
                        diff_text=self.remote.propose_patch(exec_task,_rfiles,target_tests); source='remote_model'
                    except Exception as _re:
                        self._event(run,f'Remote patch failed: {_re}; falling back to fixture','PATCHING','patch',{'provider':'remote_model_failed','attempt':attempt})
                        diff_text=candidates[attempt-1] if attempt<=len(candidates) else ''; source=exec_task.patch_sources[attempt-1] if attempt<=len(exec_task.patch_sources) else ('inline' if diff_text else 'none')
                else:
                    diff_text=candidates[attempt-1] if attempt<=len(candidates) else ''; source=exec_task.patch_sources[attempt-1] if attempt<=len(exec_task.patch_sources) else ('inline' if diff_text else 'none')
                last_candidate=diff_text; patch_applied=False
                patch_info={'provider':'remote_model' if source=='remote_model' else ('patch_file' if diff_text and source!='inline' else ('inline' if diff_text else 'none')),'attempt':attempt,'source':source,'sha256':stable_hash(diff_text) if diff_text else None,'bytes':len(diff_text.encode())}
                if diff_text:
                    ap=self._git_apply(exec_task.repo,diff_text)
                    if ap.returncode!=0:
                        self._event(run,f'Candidate patch {attempt} does not apply: {ap.stderr[-200:]}','FAILED','patch_conflict',{**patch_info,'stderr':ap.stderr[-1000:],'failure_class':FailureClass.PATCH_CONFLICT.value})
                        if attempt<max_attempts: continue
                        run.conclusion='FAILED'; break
                    applied_diff=diff_text; patch_applied=True
                self._event(run,f'Candidate patch {attempt} applied' if diff_text else f'No candidate patch for attempt {attempt}','PATCHING','patch',patch_info)

                verify=self._run_tests(exec_task,stage='verify')

                # Check for infra failure
                if verify.returncode in (124, 125, 126):
                    self._event(run, f'Infrastructure failure during verification: {verify.stderr[:200]}', 'FAILED', 'infra_error',
                               {'returncode': verify.returncode, 'stderr': verify.stderr[:1000]})
                    run.conclusion = 'INFRA_ERROR'
                    break

                # Pass real data to verifier
                result=self.verifier.verify(
                    before_output=repro.stdout + '\n' + repro.stderr,
                    after_output=verify.stdout + '\n' + verify.stderr,
                    target_tests=target_tests,
                    diff_text=diff_text,
                    task=exec_task,
                    policy=policy
                )

                self._event(run,f'Verification attempt {attempt}: {result.status}', 'VERIFYING' if result.status not in ('PASSED','BLOCKED') else result.status,'verification',result.to_dict())
                verification_evidence = self._evidence(run,'verification',f'Verification attempt {attempt}',result.status,result.to_dict(), parents=[repro_evidence.evidence_id])
                if result.status=='PASSED':
                    run.conclusion='TRUSTED_DELIVERY'; break
                if result.status=='BLOCKED':
                    # A blocked candidate is a safe stop that needs a human
                    # decision.  ``UNSAFE_DELIVERY`` is reserved for a
                    # deliberately unsafe control runner in the benchmark.
                    run.conclusion='NEEDS_REVIEW'; run.status=RunStatus.BLOCKED; break
                if attempt<max_attempts:
                    failure=result.failure; self._event(run,f'Failure classified as {failure.failure_class.value}; recovery started','RECOVERING','recovery',failure.to_dict()); self._evidence(run,'recovery','Trace-driven recovery', 'PASSED', failure.to_dict())
                    self._event(run,'Rollback and rebuild context before retry','RETRYING','retry',{'attempt':attempt+1,'actions':actions_for(failure.failure_class)})
                else: run.conclusion='FAILED'
            run.ended_at=time.time(); run.metrics={'attempts':attempt,'reproduction_rate':int(repro_ok),'final_repair_rate':int(run.conclusion=='TRUSTED_DELIVERY'),'recovery_success_rate':int(attempt>1 and run.conclusion=='TRUSTED_DELIVERY'),'evidence_completeness':min(1.0,round(len(self.store.list_evidence(run.run_id))/max(1, len(task.expected)),2))}
            self.store.save_run(run)

            # Extract diff_paths from the final patch
            final_diff = last_candidate
            from ..verifier.diff import parse_unified_diff
            try:
                entries = parse_unified_diff(final_diff)
                diff_paths = [e.path for e in entries if e.path != '/dev/null']
            except Exception:
                diff_paths = []

            patch_artifact=self._patch_artifact(run,last_candidate,diff_paths,patch_applied)
            pr_artifact=self._pr_draft(run,task,diff_paths,compliance)
            run.artifacts.extend([patch_artifact.artifact_id,pr_artifact.artifact_id]); self.store.save_run(run)

            # Compute verdict_hash before packing evidence
            from ..domain.models import compute_verdict_hash
            evidence_list = self.store.list_evidence(run.run_id)
            evidence_ids = sorted([e['evidence_id'] for e in evidence_list])
            evidence_root_hash = stable_hash(evidence_ids)

            # Get final checks from last verification result or empty if failed before verification
            final_checks = {}
            if 'checks' in run.metrics:
                final_checks = run.metrics['checks']
            elif result:  # result from last verification attempt
                final_checks = result.checks

            run.verdict_hash = compute_verdict_hash(
                run,
                final_checks,
                task.task_id,
                task.commit,
                patch_artifact.sha256,
                evidence_root_hash
            )
            run.metrics['checks'] = final_checks
            run.metrics['evidence_root_hash'] = evidence_root_hash
            run.metrics['commit'] = task.commit or ''
            run.metrics['patch_sha256'] = patch_artifact.sha256 or ''
            self.store.save_run(run)

            bundle=self._bundle(run,task)
            run.artifacts.append(bundle.artifact_id); run.status=RunStatus.EVIDENCE_PACKED; self.store.save_run(run)
            self._event(run,'Evidence bundle packed','EVIDENCE_PACKED','bundle',{'artifact_id':bundle.artifact_id,'conclusion':run.conclusion,'verdict_hash':run.verdict_hash[:16]+'...'})
        finally:
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

    def _patch_artifact(self, run, diff_text, diff_paths, applied):
        return self.store.write_artifact(run.run_id,'patch_diff',diff_text.encode(),
                                         'patch.diff',{'paths':diff_paths,'applied':applied,'conclusion':run.conclusion})

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
        """Materialize the pinned commit in an isolated workspace; raises on failure, returns (task, cleanup, mode)."""
        target=(self.store.root/run_id/'worktree').resolve(); target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists(): shutil.rmtree(target)
        if self._is_git_url(task.repo):
            self._clone_repo(task.repo, task.commit, target)
            return replace(task,repo=str(target)), (lambda: shutil.rmtree(target,ignore_errors=True)), 'clone'
        repo=Path(task.repo).resolve()
        if (repo/'.git').exists() and task.commit not in ('', 'working-tree'):
            subprocess.run(['git','-C',str(repo),'worktree','prune'],capture_output=True)
            proc=subprocess.run(['git','-C',str(repo),'worktree','add','--detach',str(target),task.commit],capture_output=True,text=True)
            if proc.returncode!=0: raise RuntimeError(f'git worktree add {task.commit} failed: {proc.stderr.strip()[-500:]}')
            def cleanup():
                subprocess.run(['git','-C',str(repo),'worktree','remove','--force',str(target)],capture_output=True)
                shutil.rmtree(target,ignore_errors=True)
            return replace(task,repo=str(target)), cleanup, 'worktree'
        shutil.copytree(repo,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('.git','__pycache__'))
        return replace(task,repo=str(target)), (lambda: shutil.rmtree(target,ignore_errors=True)), 'copy'

    def _is_git_url(self, repo: str) -> bool:
        """Check if repo is a git URL rather than a local path."""
        return repo.startswith('http://') or repo.startswith('https://') or repo.startswith('git@')

    def _clone_repo(self, url: str, commit: str, target: Path):
        """Fetch exactly `commit` (shallow) into target; raises RuntimeError with redacted git stderr on failure."""
        label=self._repo_label(url)
        def git(*args, timeout=300):
            p=subprocess.run(['git',*args],capture_output=True,text=True,timeout=timeout)
            if p.returncode!=0:
                raise RuntimeError(f'git {args[0] if args[0]!="-C" else args[2]} failed for {label}: {p.stderr.replace(url,label).strip()[-500:]}')
        try:
            if commit and commit!='working-tree':
                t=str(target)
                git('init','-q',t); git('-C',t,'remote','add','origin',url)
                git('-C',t,'fetch','-q','--depth','1','origin',commit); git('-C',t,'checkout','-q','--detach','FETCH_HEAD')
            else: git('clone','-q','--depth','1',url,str(target))
        except Exception:
            shutil.rmtree(target,ignore_errors=True); raise
    def _install_deps(self,task):
        """Run the task's declared install_command (network allowed only if risk_policy.network); returns None when not declared."""
        cmd=task.constraints.get('install_command')
        if not cmd: return None
        unsafe=os.getenv('PATCHPILOT_UNSAFE_LOCAL','0').lower() in ('1','true','yes')
        h=Harness(task.repo,timeout=int(task.risk_policy.get('install_timeout_sec',300)),unsafe_local=unsafe,network=bool(task.risk_policy.get('network',False)),pythonpath=task.constraints.get('pythonpath',[]))
        return h.run(cmd.split())
    def _run_tests(self,task,stage='verify'):
        unsafe=os.getenv('PATCHPILOT_UNSAFE_LOCAL','0').lower() in ('1','true','yes')
        h=Harness(task.repo,timeout=int(task.risk_policy.get('max_runtime_sec',60)),unsafe_local=unsafe,network=bool(task.risk_policy.get('network',False)),pythonpath=task.constraints.get('pythonpath',[]))
        # Use -v for parseable per-test results, --tb=no to keep output compact
        cmd = task.constraints.get('test_command','pytest -q')
        # Ensure -v is present for parsing
        if 'pytest' in cmd:
            parts = cmd.split()
            # Remove -q (quiet) if present, as it conflicts with -v
            parts = [p for p in parts if p not in ('-q', '--quiet')]
            if '-v' not in parts and '--verbose' not in parts:
                # Insert -v after pytest
                for i, p in enumerate(parts):
                    if 'pytest' in p:
                        parts.insert(i + 1, '-v')
                        break
            if '--tb=no' not in ' '.join(parts):
                parts.append('--tb=no')
            return h.run(parts)
        return h.run(cmd.split())
    def _bundle(self,run,task):
        data={'run':run.to_dict(),'task':task.to_dict(),'events':self.store.list_events(run.run_id),'evidence':self.store.list_evidence(run.run_id),'artifacts':self.store.list_artifacts(run.run_id)}
        # Evidence bundles are portable review artifacts.  Keep the fixture
        # identity while removing host checkout paths before serialization.
        data['task']['repo']=self._repo_label(task.repo)
        for event in data['events']:
            if isinstance(event.get('data'),dict) and 'repo' in event['data']:
                event['data']['repo']=self._repo_label(str(event['data']['repo']))
        for artifact in data['artifacts']:
            artifact['path']=Path(str(artifact.get('path',''))).name
        return self.store.write_artifact(run.run_id,'evidence_bundle',json.dumps(data,indent=2,ensure_ascii=False).encode(),'evidence.json',{'conclusion':run.conclusion})
