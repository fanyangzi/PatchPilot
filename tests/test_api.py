import json
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHPILOT_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PATCHPILOT_REMOTE_PLANNING", "0")
    monkeypatch.setenv("PATCHPILOT_UNSAFE_LOCAL", "1")
    import importlib, patchpilot.api as api
    api = importlib.reload(api)
    return TestClient(api.app), api


def test_tasks_hide_host_paths(client):
    c, _ = client
    tasks = c.get("/api/tasks").json()
    assert len(tasks) >= 3
    assert all(t["repo"].startswith("patchpilot/") for t in tasks)
    assert "/Users/" not in json.dumps(tasks)


def test_artifact_content_only_serves_registered_files(client, tmp_path):
    c, api = client
    art = api.store.write_artifact("run_x", "patch_diff", b"--- a\n+++ b\n", "patch.diff")
    api.store.db.execute("INSERT INTO runs VALUES (?,?,?)", ("run_x", json.dumps({"run_id": "run_x", "task_id": "t"}), 0.0))
    api.store.db.commit()
    ok = c.get(f"/api/runs/run_x/artifacts/{art.artifact_id}/content")
    assert ok.status_code == 200 and ok.json()["content"].startswith("--- a")
    assert c.get("/api/runs/run_x/artifacts/art_missing/content").status_code == 404


def test_eval_summary_missing_returns_404(client):
    c, _ = client
    assert c.get("/api/eval/summary").status_code == 404
