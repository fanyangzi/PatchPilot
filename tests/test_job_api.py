from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHPILOT_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PATCHPILOT_REMOTE_PLANNING", "0")
    monkeypatch.setenv("PATCHPILOT_UNSAFE_LOCAL", "1")
    import importlib
    import patchpilot.api as api
    api = importlib.reload(api)
    return TestClient(api.app)


def test_job_cancel_and_event_stream_are_durable(client):
    created = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "owner/repo", "base_ref": "main",
        "patch_text": "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n",
    })
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    job = client.get(f"/api/v1/jobs/{job_id}")
    assert job.status_code == 200 and job.json()["job"]["state"] == "queued"
    events = client.get(f"/api/v1/jobs/{job_id}/events")
    assert events.status_code == 200 and events.headers["content-type"].startswith("text/event-stream")
    assert "event: queued" in events.text
    cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert cancelled.status_code == 202
    assert cancelled.json()["job"]["state"] == "cancelled"
    assert "event: cancelled" in client.get(f"/api/v1/jobs/{job_id}/events?after=1").text
