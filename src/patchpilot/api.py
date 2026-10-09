from __future__ import annotations
import json, os, platform, re, time, uuid
from pathlib import Path
import errno

from .domain import TaskSpec
from .domain.models import Event, Run, compute_event_hash, compute_verdict_hash, stable_hash
from .evidence import EvidenceStore
from .orchestrator import PatchPilot
from .skills import SkillRegistry
from .api_v1 import install_api_v1, router as api_v1_router

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
except ImportError as exc:  # optional API dependency, keeps CLI usable
    raise RuntimeError("FastAPI is required for the API. Install with: python -m pip install -e '.[api]'") from exc

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = Path(os.getenv("PATCHPILOT_ARTIFACT_ROOT", ROOT / "artifacts"))
TASK_ROOT = Path(os.getenv("PATCHPILOT_TASK_ROOT", ROOT / "fixtures" / "tasks"))
store = EvidenceStore(ARTIFACT_ROOT)
engine = PatchPilot(ARTIFACT_ROOT)
app = FastAPI(title="PatchPilot API", version="0.1.0")


def _allowed_origins() -> list[str]:
    """Return explicit browser origins for the local console/API pair.

    The previous wildcard CORS policy allowed any website to issue browser
    requests to a running local API.  A comma-separated override keeps hosted
    deployments configurable while the default only permits the two local
    Vite origins used by the product.
    """
    raw = os.getenv("PATCHPILOT_ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    return [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "Last-Event-ID", "X-Request-ID", "X-PatchPilot-Workspace"],
    allow_credentials=False,
)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Apply browser hardening without changing JSON error semantics."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
    return response
app.include_router(api_v1_router)
install_api_v1(app)

def _task_for(run_id: str):
    data = store.get_run(run_id)
    if not data: return None
    for p in list(TASK_ROOT.glob("*.yaml")) + list(TASK_ROOT.glob("**/*.yaml")):
        try:
            task = engine.load_task(p)
            if task.task_id == data.get("task_id"): return task
        except Exception: continue
    return None


_ABSOLUTE_PATH_RE = re.compile(r"(?:/Users/[^\s\"']+|/private/[^\s\"']+|/tmp/[^\s\"']+|[A-Za-z]:\\[^\s\"']+)")


def _repo_label(value: object, task=None) -> str:
    """Return a review-safe repository label.

    Run records predate the API decoration layer and may contain an absolute
    checkout path.  Never send that path to the console: the UI only needs a
    stable, human-readable repository identity.
    """
    raw = str(value or "").replace("\\", "/").rstrip("/")
    if task is not None:
        name = Path(task.repo).name
        return f"patchpilot/{name}" if name else "patchpilot/fixture"
    if not raw or raw in {".", "local run"}:
        return "local run"
    name = raw.rsplit("/", 1)[-1] or "repository"
    if raw.startswith(("/", "~", "file:")) or "/" in raw:
        return f"patchpilot/{name}"
    return raw if raw.startswith("patchpilot/") else f"patchpilot/{name}"


def _safe_text(value: object) -> str:
    """Redact host filesystem paths from event detail shown in the UI."""
    return _ABSOLUTE_PATH_RE.sub("<local-path>", str(value or ""))


def _safe_value(value: object, key: str = "") -> object:
    """Recursively redact path-like event data without changing its shape."""
    if key in {"repo", "repository"}:
        return _repo_label(value)
    if isinstance(value, dict):
        return {str(k): _safe_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_value(v, key) for v in value]
    if isinstance(value, str):
        return _safe_text(value)
    return value

def _decorate(run):
    if not run: return None
    task = _task_for(run["run_id"])
    started, ended = run.get("started_at"), run.get("ended_at")
    events = store.list_events(run["run_id"])
    runtime = (ended or time.time()) - started if started else 0
    repo_label = _repo_label(run.get("repo"), task)
    updated = ended or started
    # Legacy consumers may still receive this projection, but confidence is
    # intentionally absent unless a measured calibration record exists. A
    # conclusion or process exit must never be turned into a made-up score.
    return {**run, "id": run["run_id"], "title": task.issue_title if task else run.get("task_id"), "issue_title": task.issue_title if task else None, "repo": repo_label, "commit": task.commit if task else None, "fixture": f"Fixture · {task.scenario}" if task else "local run", "runtime_sec": round(runtime, 3), "duration": f"{runtime:.1f}s", "retry_count": max(0, int(run.get("attempt", 1)) - 1), "confidence": None, "confidence_basis": "not_recorded", "updated_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated)) if updated else "", "event_count": len(events), "task": _task_payload(task)}

def _task_payload(task):
    """Review-safe subset of a TaskSpec (no host paths)."""
    if task is None: return None
    return {"task_id": task.task_id, "repo": _repo_label(task.repo, task), "issue_title": task.issue_title, "issue_body": task.issue_body, "commit": task.commit, "scenario": task.scenario, "test_command": task.constraints.get("test_command"), "risk_policy": task.risk_policy}

ARTIFACT_PREVIEW_BYTES = 200_000

def _event_payload(e):
    data=e.get("data") or {}
    safe_data = _safe_value(data)
    return {"event_id":e.get("event_id"), "kind":e.get("event_type"), "stage":e.get("event_type"), "title":_safe_text(e.get("message")), "summary":_safe_text(e.get("message")), "detail":json.dumps(safe_data, ensure_ascii=False)[:1000], "status":e.get("status"), "runtime_sec":None, "artifact_ids":data.get("artifact_ids", []), "data":safe_data, "ts":e.get("ts")}


def _policy_payload(run_id: str, run: dict, task=None) -> dict:
    """Return the policy snapshot recorded for a run.

    A run executes in a disposable worktree, so compiling the policy again from
    ``task.repo`` after completion is both inaccurate and unsafe.  The policy
    event is the durable source of truth; the fallback only serves old runs
    created before policy snapshots were emitted.
    """
    events = store.list_events(run_id)
    event = next((e for e in reversed(events) if e.get("event_type") == "policy"), None)
    data = (event or {}).get("data") or {}
    if not data and task is not None:
        from .policy import compile_policy
        try:
            policy = compile_policy(Path(task.repo))
        except Exception:
            from .policy.schema import Policy
            policy = Policy()
        data = {
            "source": policy.source,
            "scope": list(policy.scope),
            "allowed_paths": list(policy.allowed_paths),
            "sensitive_patterns": list(policy.sensitive_patterns),
            "required_checks": list(policy.required_checks),
            "max_attempts": policy.max_attempts,
        }
    source = str(data.get("source") or "default")
    scope = list(data.get("scope") or ["**/*"])
    allowed = list(data.get("allowed_paths") or [])
    sensitive = list(data.get("sensitive_patterns") or [])
    required = list(data.get("required_checks") or [])
    max_attempts = int(data.get("max_attempts") or 3)
    yaml_lines = [
        f"source: {source}",
        "scope:", *[f"  - {x}" for x in scope],
        "allowed_paths:", *([f"  - {x}" for x in allowed] or ["  - <none>"]),
        "sensitive_patterns:", *([f"  - {x}" for x in sensitive] or ["  - <none>"]),
        "required_checks:", *([f"  - {x}" for x in required] or ["  - <none>"]),
        f"max_attempts: {max_attempts}",
    ]
    return {
        "run_id": run_id,
        "source": source,
        "scope": scope,
        "allowed_paths": allowed,
        "sensitive_patterns": sensitive,
        "required_checks": required,
        "max_attempts": max_attempts,
        "compiled_at": (event or {}).get("ts") or run.get("started_at"),
        "commit": run.get("metrics", {}).get("commit") or (task.commit if task else None),
        "repository": _repo_label(task.repo, task) if task else _repo_label(run.get("repo")),
        "yaml": "\n".join(yaml_lines) + "\n",
    }

@app.get("/api/health")
def health(): return {"status":"ok", "service":"patchpilot-api", "version":"0.1.0", "python":platform.python_version(), "skills":len(SkillRegistry().all())}

@app.get("/api/runs")
def list_runs(): return [_decorate(x) for x in store.list_runs()]

@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run=store.get_run(run_id)
    if not run: raise HTTPException(404,"run not found")
    return _decorate(run)

@app.get("/api/runs/{run_id}/events")
def get_events(run_id: str):
    if not store.get_run(run_id): raise HTTPException(404,"run not found")
    return [_event_payload(x) for x in store.list_events(run_id)]

@app.get("/api/runs/{run_id}/graph")
def get_graph(run_id: str):
    if not store.get_run(run_id): raise HTTPException(404,"run not found")
    route=next((x for x in store.list_evidence(run_id) if x.get("kind")=="route_decision"),None)
    nodes=[]
    for i,node in enumerate((route or {}).get("payload",{}).get("nodes",[])):
        nodes.append({"id":node["skill"],"name":node["skill"],"label":node["skill"].replace("_"," ").title(),"subtitle":f"score {node['score']}","sub":f"score {node['score']}","kind":"verify" if "verify" in node["skill"] or "evidence" in node["skill"] else "skill","status":"done","score":node["score"],"tools":node.get("tools",[])})
    return {"nodes":nodes,"edges":[],"selected":(route or {}).get("payload",{}).get("selected",[])}

@app.get("/api/runs/{run_id}/artifacts")
def get_artifacts(run_id: str):
    if not store.get_run(run_id): raise HTTPException(404,"run not found")
    out=[]
    for a in store.list_artifacts(run_id):
        # Keep host checkout paths out of the browser/API payload.  The
        # console only needs the artifact filename and its digest metadata.
        filename = Path(a["path"]).name
        out.append({"id":a["artifact_id"],"artifact_id":a["artifact_id"],"type":a["kind"],"kind":a["kind"],"name":filename,"filename":filename,"size":a.get("size"),"run_id":run_id,"status":"sealed","detail":a.get("metadata",{}).get("conclusion","")})
    return out

@app.get("/api/runs/{run_id}/artifacts/{artifact_id}/content")
def get_artifact_content(run_id: str, artifact_id: str):
    """Text preview of a registered artifact. Only paths recorded in the
    evidence store and located under ARTIFACT_ROOT are readable."""
    if not store.get_run(run_id): raise HTTPException(404,"run not found")
    record = next((a for a in store.list_artifacts(run_id) if a.get("artifact_id") == artifact_id), None)
    if not record: raise HTTPException(404,"artifact not found")
    recorded_path = Path(record["path"])
    # Registered artifact paths are trusted metadata, but the filesystem can
    # change after registration.  Reject symlink components and re-check the
    # resolved path immediately before reading to prevent an artifact link
    # from becoming an arbitrary file download.
    root = ARTIFACT_ROOT.resolve()
    try:
        path = recorded_path.resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(404, "artifact not readable")
    if recorded_path.is_symlink() or any(part.is_symlink() for part in recorded_path.parents if part.exists()):
        raise HTTPException(404, "artifact not readable")
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404,"artifact not readable")
    try:
        # O_NOFOLLOW closes the common final-component symlink race on POSIX.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            raw = os.read(fd, ARTIFACT_PREVIEW_BYTES)
            stat = os.fstat(fd)
        finally:
            os.close(fd)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno not in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM}:
            raise
        raise HTTPException(404, "artifact not readable") from exc
    return {"artifact_id": artifact_id, "name": path.name, "kind": record.get("kind"), "truncated": stat.st_size > ARTIFACT_PREVIEW_BYTES, "content": _safe_text(raw.decode("utf-8", errors="replace"))}

@app.get("/api/tasks")
def list_tasks():
    out=[]
    for p in sorted(TASK_ROOT.glob("*.yaml")):
        try: out.append(_task_payload(engine.load_task(p)))
        except Exception: continue
    return out

@app.get("/api/eval/summary")
def eval_summary():
    folder = ARTIFACT_ROOT / "eval"
    summary_path, rows_path = folder / "summary.json", folder / "results.jsonl"
    if not summary_path.is_file(): raise HTTPException(404,"evaluation has not been run")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    keep = ("task_id","scenario","method","status","first_pass","functional_repair","trusted_delivery","recovery_success","policy_gate_pass","evidence_completeness","attempts")
    rows = [{k: r.get(k) for k in keep} for r in (json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip())] if rows_path.is_file() else []
    return {**summary, "rows": rows}

@app.post("/api/demo/run")
def demo_run(body: dict | None = None):
    task_id=(body or {}).get("task_id","issue-001-normal")
    paths=list(TASK_ROOT.glob(f"{task_id}.yaml")) or list(TASK_ROOT.glob(f"*{task_id}*.yaml"))
    if not paths: raise HTTPException(404,f"fixture task not found: {task_id}")
    run=engine.run_task(engine.load_task(paths[0])); return _decorate(run.to_dict())

@app.post("/api/runs")
def create_run(body: dict):
    task_id=body.get("task_id")
    if task_id: return demo_run({"task_id":task_id})
    repo=body.get("repo")
    if not repo: raise HTTPException(400,"provide task_id or repo")
    task=TaskSpec.from_dict({"task_id":f"adhoc_{uuid.uuid4().hex[:8]}","repo":repo,"commit":body.get("commit","HEAD"),"issue_title":body.get("issue_title","Ad-hoc run"),"issue_body":body.get("issue_body",""),"patches":[],"patch_sources":[],"constraints":{"test_command":body.get("test_command","pytest -x -q")},"scenario":"normal"})
    run=engine.run_task(task); return _decorate(run.to_dict())

@app.get("/api/runs/{run_id}/verify")
def verify_run(run_id: str):
    run=store.get_run(run_id)
    if not run: raise HTTPException(404,"run not found")
    events=store.list_events(run_id)
    chain=[]; prev_hash=None; chain_valid=True
    for e in events:
        ev=Event(run_id=e.get('run_id',run_id),event_type=e.get('event_type',''),status=e.get('status',''),message=e.get('message',''),data=e.get('data',{}),event_id=e.get('event_id',''),ts=e.get('ts',0.0))
        expected=compute_event_hash(ev,prev_hash); actual=e.get('event_hash')
        if e.get('prev_hash') != prev_hash or not actual or expected != actual: chain_valid=False
        used=actual or expected
        chain.append({'event_id':e.get('event_id'),'kind':e.get('event_type'),'prev_hash':e.get('prev_hash'),'event_hash':used})
        prev_hash=used
    task = _task_for(run_id)
    metrics = run.get('metrics') or {}
    stored_verdict = run.get('verdict_hash')
    verdict_expected = None
    verdict_valid = None
    if stored_verdict:
        checks = metrics.get('checks') or {}
        patch_sha = metrics.get('patch_sha256') or next(
            (a.get('sha256', '') for a in store.list_artifacts(run_id) if a.get('kind') == 'patch_diff'),
            '',
        )
        evidence_root = metrics.get('evidence_root_hash') or stable_hash(sorted(e.get('evidence_id') for e in store.list_evidence(run_id)))
        commit = metrics.get('commit') or (task.commit if task else '')
        run_obj = Run(run_id=run_id, task_id=run.get('task_id',''), conclusion=run.get('conclusion'), metrics=metrics, attempt=int(run.get('attempt',0)))
        verdict_expected = compute_verdict_hash(run_obj, checks, run_obj.task_id, commit, patch_sha, evidence_root)
        verdict_valid = verdict_expected == stored_verdict
    valid = chain_valid and (verdict_valid is not False)
    return {
        'run_id':run_id, 'valid':valid, 'chain_valid':chain_valid,
        'verdict_hash':stored_verdict, 'verdict_hash_expected':verdict_expected,
        'verdict_hash_valid':verdict_valid, 'event_count':len(events), 'chain':chain,
    }

@app.get("/api/runs/{run_id}/policy")
def get_run_policy(run_id: str):
    run=store.get_run(run_id)
    if not run: raise HTTPException(404,"run not found")
    task=_task_for(run_id)
    return _policy_payload(run_id, run, task)
