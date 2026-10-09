from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture()
def acl_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHPILOT_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PATCHPILOT_REMOTE_PLANNING", "0")
    monkeypatch.setenv("PATCHPILOT_UNSAFE_LOCAL", "1")
    monkeypatch.setenv("PATCHPILOT_ACL_REQUIRED", "1")
    import importlib
    import patchpilot.api as api
    api = importlib.reload(api)
    return TestClient(api.app), api


def _headers(workspace: str) -> dict[str, str]:
    return {"X-PatchPilot-Workspace": workspace}


def _create_task(client: TestClient, workspace: str) -> tuple[dict, dict]:
    headers = _headers(workspace)
    intake_response = client.post(
        "/api/v1/intakes",
        headers=headers,
        json={
            "mode": "local_patch", "repo_id": "owner/repo", "base_ref": "main",
            "patch_text": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n",
            "issue_title": "ACL <img src=x onerror=alert(1)>", "issue_body": "Keep workspaces separate",
        },
    )
    assert intake_response.status_code == 202
    intake = intake_response.json()
    task_response = client.post("/api/v1/tasks", headers=headers, json={"intake_id": intake["intake_id"]})
    assert task_response.status_code == 201
    return intake, task_response.json()["task"]


def test_acl_requires_workspace_and_hides_cross_workspace_task_and_job(acl_client):
    client, _ = acl_client
    intake, task = _create_task(client, "workspace-a")
    job_id = intake["job_id"]

    missing = client.get(f"/api/v1/tasks/{task['task_id']}")
    assert missing.status_code == 401
    own_task = client.get(f"/api/v1/tasks/{task['task_id']}", headers=_headers("workspace-a"))
    assert own_task.status_code == 200
    foreign_task = client.get(f"/api/v1/tasks/{task['task_id']}", headers=_headers("workspace-b"))
    assert foreign_task.status_code == 404
    foreign_job = client.get(f"/api/v1/jobs/{job_id}", headers=_headers("workspace-b"))
    assert foreign_job.status_code == 404
    own_list = client.get("/api/v1/tasks", headers=_headers("workspace-a"))
    foreign_list = client.get("/api/v1/tasks", headers=_headers("workspace-b"))
    assert task["task_id"] in {item["task_id"] for item in own_list.json()["items"]}
    assert task["task_id"] not in {item["task_id"] for item in foreign_list.json()["items"]}


def test_acl_binds_candidate_and_report_to_task_workspace(acl_client):
    client, _ = acl_client
    headers = _headers("workspace-a")
    _, task = _create_task(client, "workspace-a")
    candidate = client.post(
        f"/api/v1/tasks/{task['task_id']}/candidates", headers=headers,
        json={"source": "upload", "base_sha": "base", "patch_text": "--- a/a.py\n+++ b/a.py\n"},
    ).json()["candidate"]
    draft = client.post(
        f"/api/v1/tasks/{task['task_id']}/contracts/draft", headers=headers,
        json={"source_ids": ["issue:acl"], "base_snapshot_id": "base", "conditions": [{
            "condition_id": "AC-ACL", "kind": "preserve", "statement": "Preserve behavior",
            "source_refs": ["issue:acl"], "oracle": {"type": "example", "expected": "ok"},
        }]},
    ).json()["contract"]
    assert client.put(
        f"/api/v1/contracts/{draft['contract_id']}", headers=headers,
        json={"expected_revision": 1, "conditions": [{
            "condition_id": "AC-ACL", "kind": "preserve", "statement": "Preserve behavior",
            "source_refs": ["issue:acl"], "oracle": {"type": "example", "expected": "ok"},
        }]},
    ).status_code == 200
    assert client.post(
        f"/api/v1/contracts/{draft['contract_id']}/freeze", headers=headers,
        json={"expected_revision": 2, "confirmed_condition_ids": ["AC-ACL"]},
    ).status_code == 201
    verification = client.post(
        f"/api/v1/tasks/{task['task_id']}/verifications", headers=headers,
        json={"candidate_id": candidate["candidate_id"], "contract_id": draft["contract_id"],
              "suite_id": "suite", "environment_id": "env", "policy_id": "policy"},
    ).json()["verification"]
    report = client.post(
        f"/api/v1/verifications/{verification['verification_id']}/reports", headers=headers,
        json={"format": "html"},
    )
    assert report.status_code == 201
    report_id = report.json()["report"]["report_id"]
    assert client.get(f"/api/v1/candidates/{candidate['candidate_id']}", headers=_headers("workspace-b")).status_code == 404
    assert client.get(f"/api/v1/reports/{report_id}", headers=_headers("workspace-b")).status_code == 404
    assert client.get(f"/api/v1/reports/{report_id}", headers=headers).status_code == 200
    html = client.get(f"/api/v1/reports/{report_id}/content", headers=headers).json()["content"]
    assert "<img" not in html and "&lt;img" in html
