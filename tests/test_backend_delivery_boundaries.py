from __future__ import annotations

import hashlib
import hmac
import json
import subprocess

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATCHPILOT_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("PATCHPILOT_REMOTE_PLANNING", "0")
    monkeypatch.setenv("PATCHPILOT_UNSAFE_LOCAL", "1")
    import importlib
    import patchpilot.api as api

    api = importlib.reload(api)
    return TestClient(api.app), api


def test_artifact_download_and_bundle_check_fail_closed_after_tamper(api_client):
    client, api = api_client
    from patchpilot.domain.models import Run

    run = Run("run-download", "task-download")
    api.store.save_run(run)
    artifact = api.store.write_artifact(run.run_id, "report", b"evidence", "report.md")

    downloaded = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.content == b"evidence"
    assert downloaded.headers["x-artifact-sha256"] == artifact.sha256
    assert 'filename="report.md"' in downloaded.headers["content-disposition"]

    checked = client.get(f"/api/v1/runs/{run.run_id}/bundle-check")
    assert checked.status_code == 200 and checked.json()["valid"] is True

    with open(artifact.path, "wb") as handle:
        handle.write(b"tampered")
    failed = client.get(f"/api/v1/artifacts/{artifact.artifact_id}/download")
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "artifact_integrity_failed"
    assert client.post("/api/v1/bundles/check", json={"run_id": run.run_id}).json()["valid"] is False


def test_github_webhook_verifies_raw_signature_and_deduplicates_delivery(api_client, monkeypatch):
    client, _ = api_client
    secret = "webhook-test-secret"
    monkeypatch.setenv("PATCHPILOT_GITHUB_WEBHOOK_SECRET", secret)
    body = json.dumps({"action": "opened", "repository": {"full_name": "acme/widget"}}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "X-GitHub-Delivery": "delivery-001",
        "X-GitHub-Event": "pull_request",
        "X-Hub-Signature-256": signature,
    }
    first = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert first.status_code == 202
    payload = first.json()
    assert payload["status"] == "accepted"
    replay = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["job_id"] == payload["job_id"]

    changed = b'{"action":"closed"}'
    changed_sig = "sha256=" + hmac.new(secret.encode(), changed, hashlib.sha256).hexdigest()
    conflict = client.post("/api/v1/webhooks/github", content=changed, headers={**headers, "X-Hub-Signature-256": changed_sig})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "webhook_delivery_conflict"

    bad = client.post("/api/v1/webhooks/github", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64, "X-GitHub-Delivery": "delivery-002"})
    assert bad.status_code == 401


def test_evidence_parent_links_are_same_run_and_immutable(tmp_path):
    from patchpilot.domain.models import Evidence
    from patchpilot.evidence.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    root = Evidence("ev-root", "reproduction", "root", "PASSED")
    store.add_evidence("run-a", root)
    child = Evidence("ev-child", "localization", "child", "PASSED", parents=[root.evidence_id])
    store.add_evidence("run-a", child)
    with pytest.raises(ValueError, match="does not exist in run"):
        store.add_evidence("run-b", Evidence("ev-cross", "x", "x", "FAILED", parents=[root.evidence_id]))
    with pytest.raises(ValueError, match="immutable"):
        store.add_evidence("run-a", Evidence("ev-root", "changed", "root", "FAILED"))


def test_archive_member_limit_is_enforced(tmp_path, monkeypatch):
    from patchpilot.application import verification_service as module
    from patchpilot.application.verification_service import VerificationExecutionError, VerificationService
    from patchpilot.evidence.store import EvidenceStore

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "file.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setattr(module, "_ARCHIVE_MAX_MEMBERS", 0)
    with pytest.raises(VerificationExecutionError, match="too many archive entries"):
        VerificationService(EvidenceStore(tmp_path / "artifacts"))._archive_commit(repo, sha, tmp_path / "workspace")
