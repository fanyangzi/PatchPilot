from __future__ import annotations
import json, os, platform, re, time
from pathlib import Path

from .domain import TaskSpec
from .evidence import EvidenceStore
from .orchestrator import PatchPilot
from .skills import SkillRegistry

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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def _task_for(run_id: str):
    data = store.get_run(run_id)
    if not data: return None
    for p in TASK_ROOT.glob("*.yaml"):
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
    return {**run, "id": run["run_id"], "title": task.issue_title if task else run.get("task_id"), "issue_title": task.issue_title if task else None, "repo": repo_label, "commit": task.commit if task else None, "fixture": f"Fixture · {task.scenario}" if task else "local run", "runtime_sec": round(runtime, 3), "duration": f"{runtime:.1f}s", "retry_count": max(0, int(run.get("attempt", 1)) - 1), "confidence": 94 if run.get("conclusion") == "TRUSTED_DELIVERY" else 35, "updated_at": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated)) if updated else "", "event_count": len(events)}

def _event_payload(e):
    data=e.get("data") or {}
    safe_data = _safe_value(data)
    return {"event_id":e.get("event_id"), "kind":e.get("event_type"), "stage":e.get("event_type"), "title":_safe_text(e.get("message")), "summary":_safe_text(e.get("message")), "detail":json.dumps(safe_data, ensure_ascii=False)[:1000], "status":e.get("status"), "runtime_sec":None, "artifact_ids":data.get("artifact_ids", []), "data":safe_data, "ts":e.get("ts")}

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
        out.append({"id":a["artifact_id"],"artifact_id":a["artifact_id"],"type":a["kind"],"kind":a["kind"],"name":filename,"filename":filename,"sha256":a.get("sha256"),"size":a.get("size"),"run_id":run_id,"status":"sealed","detail":a.get("metadata",{}).get("conclusion","")})
    return out

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
    raise HTTPException(400,"MVP accepts task_id for fixture runs")
