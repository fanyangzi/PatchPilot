import json
from pathlib import Path
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


def test_legacy_api_sets_browser_security_headers_and_does_not_allow_wildcard_origin(client):
    c, _ = client
    response = c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    assert response.headers.get("access-control-allow-origin") is None


def test_artifact_content_rejects_registered_symlink(client, tmp_path):
    c, api = client
    art = api.store.write_artifact("run_symlink", "patch_diff", b"safe", "patch.diff")
    api.store.db.execute("INSERT INTO runs VALUES (?,?,?)", ("run_symlink", json.dumps({"run_id": "run_symlink", "task_id": "t"}), 0.0))
    api.store.db.commit()
    target = Path(art.path)
    target.unlink()
    target.symlink_to(tmp_path / "outside-secret")
    (tmp_path / "outside-secret").write_text("secret", encoding="utf-8")
    response = c.get(f"/api/runs/run_symlink/artifacts/{art.artifact_id}/content")
    assert response.status_code == 404


def test_artifact_metadata_isolated_for_identical_content_and_paths_are_safe(tmp_path):
    from patchpilot.evidence.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    first = store.write_artifact("run_one", "patch_diff", b"same", "patch.diff", {"attempt": 1})
    second = store.write_artifact("run_two", "patch_diff", b"same", "patch.diff", {"attempt": 2})
    assert first.artifact_id != second.artifact_id
    assert first.run_id == "run_one" and second.run_id == "run_two"
    assert store.list_artifacts("run_one")[0]["metadata"]["attempt"] == 1
    assert store.list_artifacts("run_two")[0]["metadata"]["attempt"] == 2

    import pytest
    for bad_name in ("../escape", "/tmp/escape", "nested/escape", r"nested\\escape", ""):
        with pytest.raises(ValueError):
            store.write_artifact("run_three", "patch_diff", b"x", bad_name)


def test_eval_summary_missing_returns_404(client):
    c, _ = client
    assert c.get("/api/eval/summary").status_code == 404
