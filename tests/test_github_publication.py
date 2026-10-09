from __future__ import annotations

import hashlib
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from patchpilot.integrations.github_source import GitHubSourceResolver, HTTPResponse


@pytest.fixture()
def publication_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHPILOT_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PATCHPILOT_REMOTE_PLANNING", "0")
    monkeypatch.setenv("PATCHPILOT_UNSAFE_LOCAL", "1")
    import importlib
    import patchpilot.api as api
    api = importlib.reload(api)
    return TestClient(api.app), api


class FakeGitHub:
    def __init__(self, head: str = "head-1"):
        self.head = head
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def get(self, url, *, headers, timeout):
        self.calls.append(("GET", url, dict(headers), None))
        if "/pulls/" in url:
            payload = {
                "number": 7,
                "head": {"sha": self.head},
                "base": {"sha": "base-1"},
                "permissions": {"push": True},
                "state": "open",
            }
        else:
            payload = {"permissions": {"push": True}}
        return HTTPResponse(200, {"content-type": "application/json"}, json.dumps(payload).encode(), url)

    def post(self, url, *, headers, body, timeout):
        self.calls.append(("POST", url, dict(headers), body))
        return HTTPResponse(201, {"content-type": "application/json"}, json.dumps({"id": 99, "html_url": "https://github.com/acme/widget/pull/7#issuecomment-99"}).encode(), url)


def _report(client):
    intake = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "acme/widget", "base_ref": "base-1",
        "patch_text": "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n",
        "issue_title": "Publish me", "issue_body": "Report body",
    })
    assert intake.status_code == 202
    task = client.post("/api/v1/tasks", json={"intake_id": intake.json()["intake_id"]}).json()["task"]
    candidate = client.post(f"/api/v1/tasks/{task['task_id']}/candidates", json={
        "source": "upload", "base_sha": "base-1", "patch_text": "--- a/a\n+++ b/a\n",
    }).json()["candidate"]
    draft = client.post(f"/api/v1/tasks/{task['task_id']}/contracts/draft", json={
        "source_ids": ["issue:7"], "base_snapshot_id": "base-1", "conditions": [{
            "condition_id": "AC-1", "kind": "preserve", "statement": "Preserve behavior",
            "source_refs": ["issue:7"], "oracle": {"type": "example", "expected": "ok"},
        }],
    }).json()["contract"]
    edited = client.put(f"/api/v1/contracts/{draft['contract_id']}", json={
        "expected_revision": 1, "conditions": [{
            "condition_id": "AC-1", "kind": "preserve", "statement": "Preserve behavior",
            "source_refs": ["issue:7"], "oracle": {"type": "example", "expected": "ok"},
        }],
    })
    assert edited.status_code == 200
    frozen = client.post(f"/api/v1/contracts/{draft['contract_id']}/freeze", json={
        "expected_revision": 2, "confirmed_condition_ids": ["AC-1"],
    })
    assert frozen.status_code == 201
    queued = client.post(f"/api/v1/tasks/{task['task_id']}/verifications", json={
        "candidate_id": candidate["candidate_id"], "contract_id": draft["contract_id"],
        "suite_id": "suite", "environment_id": "env", "policy_id": "policy",
    })
    verification = queued.json()["verification"]
    report = client.post(f"/api/v1/verifications/{verification['verification_id']}/reports", json={"format": "markdown"})
    assert report.status_code == 201
    return report.json()["report"]


def test_preview_is_read_only_and_binds_body_and_head(publication_client, monkeypatch):
    client, _ = publication_client
    report = _report(client)
    transport = FakeGitHub()
    import patchpilot.api_v1 as api_v1
    monkeypatch.setattr(api_v1, "_github_source_resolver", lambda: GitHubSourceResolver(transport, token="read-token", allow_write=False))

    response = client.post(f"/api/v1/reports/{report['report_id']}/publish-preview", json={
        "target_repo": "acme/widget", "target_pr": 7, "expected_head": "head-1", "publication_type": "comment",
    })
    assert response.status_code == 200
    preview = response.json()["preview"]
    assert preview["current_head"] == "head-1"
    assert preview["body_digest"] == hashlib.sha256(client.get(f"/api/v1/reports/{report['report_id']}/content").json()["content"].encode()).hexdigest()
    assert preview["auto_merge"] is False
    assert not [call for call in transport.calls if call[0] == "POST"]


def test_publish_rejects_stale_head_without_writing(publication_client, monkeypatch):
    client, _ = publication_client
    report = _report(client)
    transport = FakeGitHub("head-1")
    import patchpilot.api_v1 as api_v1
    monkeypatch.setattr(api_v1, "_github_source_resolver", lambda: GitHubSourceResolver(transport, token="write-token", allow_write=True))
    preview = client.post(f"/api/v1/reports/{report['report_id']}/publish-preview", json={
        "repo_id": "acme/widget", "pr_number": 7, "head_sha": "head-1",
    }).json()["preview"]
    transport.head = "head-2"
    response = client.post(f"/api/v1/reports/{report['report_id']}/publish", json={
        "preview_id": preview["preview_id"], "expected_head": "head-1", "confirmation_digest": preview["confirmation_digest"],
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_head"
    assert not [call for call in transport.calls if call[0] == "POST"]


def test_publish_requires_opt_in_and_deduplicates_confirmed_write(publication_client, monkeypatch):
    client, _ = publication_client
    report = _report(client)
    disabled = FakeGitHub()
    import patchpilot.api_v1 as api_v1
    monkeypatch.setattr(api_v1, "_github_source_resolver", lambda: GitHubSourceResolver(disabled, token="read-token", allow_write=False))
    preview_response = client.post(f"/api/v1/reports/{report['report_id']}/publish-preview", json={
        "target_repo": "acme/widget", "target_pr": 7, "expected_head": "head-1",
    })
    preview = preview_response.json()["preview"]
    blocked = client.post(f"/api/v1/reports/{report['report_id']}/publish", json={
        "preview_id": preview["preview_id"], "expected_head": "head-1", "confirmation_digest": preview["confirmation_digest"],
    })
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "github_write_disabled"
    assert not [call for call in disabled.calls if call[0] == "POST"]

    transport = FakeGitHub()
    monkeypatch.setattr(api_v1, "_github_source_resolver", lambda: GitHubSourceResolver(transport, token="write-token", allow_write=True))
    published = client.post(f"/api/v1/reports/{report['report_id']}/publish", json={
        "preview_id": preview["preview_id"], "expected_head": "head-1", "confirmation_digest": preview["confirmation_digest"],
    })
    assert published.status_code == 202
    assert published.json()["publication"]["auto_merge"] is False
    replay = client.post(f"/api/v1/reports/{report['report_id']}/publish", json={
        "preview_id": preview["preview_id"], "expected_head": "head-1", "confirmation_digest": preview["confirmation_digest"],
    })
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert len([call for call in transport.calls if call[0] == "POST"]) == 1
