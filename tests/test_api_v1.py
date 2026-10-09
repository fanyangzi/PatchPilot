from __future__ import annotations

import hashlib
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


def test_v1_health_and_validation_use_request_id_envelope(api_client):
    client, _ = api_client
    health = client.get("/api/v1/health", headers={"X-Request-ID": "caller-42"})
    assert health.status_code == 200
    assert health.json()["request_id"] == "caller-42"
    assert health.headers["X-Request-ID"] == "caller-42"

    invalid = client.post("/api/v1/intakes", json={"mode": "local_patch", "repo_id": "owner/repo", "base_ref": "main"})
    assert invalid.status_code == 422
    body = invalid.json()
    assert body["error"]["code"] == "validation_error"
    assert body["request_id"]


def test_write_idempotency_replays_same_intake_and_rejects_changed_body(api_client):
    client, _ = api_client
    payload = {"mode": "local_patch", "repo_id": "owner/repo", "base_ref": "main", "patch_text": "diff --git a/a b/a"}
    first = client.post("/api/v1/intakes", json=payload, headers={"Idempotency-Key": "intake-retry-1"})
    second = client.post("/api/v1/intakes", json=payload, headers={"Idempotency-Key": "intake-retry-1"})
    assert first.status_code == second.status_code == 202
    assert first.json()["intake_id"] == second.json()["intake_id"]
    assert second.json()["idempotent_replay"] is True
    changed = client.post("/api/v1/intakes", json={**payload, "base_ref": "release"}, headers={"Idempotency-Key": "intake-retry-1"})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"


def _create_task(client) -> dict:
    response = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "fanyangzi/PatchPilot", "base_ref": "main",
        "patch_text": "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n",
        "issue_title": "Preserve local patch source", "issue_body": "The imported change must be reviewable.",
        "source_refs": ["user:local-patch"],
    })
    assert response.status_code == 202
    intake = response.json()
    assert intake["status"] == "pending_source_resolution"
    assert intake["intake"]["resolved_refs"] is None
    created = client.post("/api/v1/tasks", json={"intake_id": intake["intake_id"], "mode": "verify"})
    assert created.status_code == 201
    assert created.json()["status"] == "draft"
    return created.json()["task"]


def test_v1_only_lists_durable_tasks_and_creates_real_local_patch_candidate(api_client):
    client, _ = api_client
    task = _create_task(client)

    rows = client.get("/api/v1/tasks").json()["items"]
    assert [row["task_id"] for row in rows] == [task["task_id"]]
    assert client.get(f"/api/v1/tasks/{task['task_id']}").json()["task"]["issue_snapshot"]["title"] == "Preserve local patch source"

    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"
    candidate_response = client.post(f"/api/v1/tasks/{task['task_id']}/candidates", json={
        "source": "upload", "base_sha": "base-sha-1", "patch_text": patch,
    })
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()["candidate"]
    assert candidate["patch_hash"] == hashlib.sha256(patch.encode()).hexdigest()
    assert client.get(f"/api/v1/candidates/{candidate['candidate_id']}/diff").json()["content"] == patch


def test_contract_versions_freeze_immutably_and_verification_is_not_assumed_pass(api_client):
    client, api = api_client
    task = _create_task(client)
    patch = "--- a.txt\n+++ b.txt\n"
    candidate_response = client.post(f"/api/v1/tasks/{task['task_id']}/candidates", json={
        "source": "upload", "base_sha": "base-sha", "patch_text": patch,
    })
    candidate = candidate_response.json()["candidate"]

    draft_response = client.post(f"/api/v1/tasks/{task['task_id']}/contracts/draft", json={
        "source_ids": ["issue:42#body"], "base_snapshot_id": "snapshot-base",
        "model_profile": "manual",
        "conditions": [{
            "condition_id": "AC-01", "kind": "preserve", "statement": "Keep the public field stable",
            "source_refs": ["issue:42#body"], "required": True,
            "oracle": {"type": "example", "expected": "stable"},
        }],
    })
    assert draft_response.status_code == 202
    draft = draft_response.json()["contract"]
    assert draft["state"] == "draft"
    contract_id = draft["contract_id"]

    edited = client.put(f"/api/v1/contracts/{contract_id}", json={
        "expected_revision": 1,
        "conditions": [{
            "condition_id": "AC-01", "kind": "preserve", "statement": "Keep the public field stable",
            "source_refs": ["issue:42#body"], "required": True,
            "oracle": {"type": "example", "expected": "stable"},
        }],
    })
    assert edited.status_code == 200
    assert edited.json()["contract"]["revision"] == 2
    frozen = client.post(f"/api/v1/contracts/{contract_id}/freeze", json={
        "expected_revision": 2, "confirmed_condition_ids": ["AC-01"],
    })
    assert frozen.status_code == 201
    contract = frozen.json()["contract"]
    assert contract["revision"] == 3
    assert contract["state"] == "frozen"
    assert contract["conditions"][0]["confirmation"] == "maintainer_confirmed"
    stale_edit = client.put(f"/api/v1/contracts/{contract_id}", json={
        "expected_revision": 3, "conditions": [{
            "condition_id": "AC-01", "kind": "preserve", "statement": "changed",
            "source_refs": ["issue:42#body"],
        }],
    })
    assert stale_edit.status_code == 409
    assert stale_edit.json()["error"]["code"] == "contract_frozen"

    queued = client.post(f"/api/v1/tasks/{task['task_id']}/verifications", json={
        "candidate_id": candidate["candidate_id"], "contract_id": contract_id,
        "suite_id": "suite-v1", "environment_id": "env-unresolved", "policy_id": "policy-default",
        "baseline_tests_hash": "unavailable", "command_argv": [["pytest", "-q"]],
        "verifier_revision": "test-verifier@1", "probe_plan_hash": "none", "seed": 0,
    })
    assert queued.status_code == 202
    verification = queued.json()["verification"]
    assert verification["run_state"] == "queued"
    assert verification["verdict"] == "not_evaluated"
    assert verification["validity"] == "current"
    assert verification["review_decision"] == "pending"
    assert len(verification["verification_key"]) == 64
    assert "baseline_tests_unavailable" in verification["gaps"]
    checks = client.get(f"/api/v1/verifications/{verification['verification_id']}/checks").json()
    assert checks["items"] == [] and checks["complete"] is False

    persisted = api.store.get_verification(verification["verification_id"])
    assert persisted.verification_key == verification["verification_key"]
    assert persisted.contract_revision == 3

    report_response = client.post(f"/api/v1/verifications/{verification['verification_id']}/reports", json={"format": "markdown"})
    assert report_response.status_code == 201
    report = report_response.json()["report"]
    assert report["verification_key"] == verification["verification_key"]
    report_meta = client.get(f"/api/v1/reports/{report['report_id']}").json()["report"]
    assert report_meta["snapshot"]["verification"]["verdict"] == "not_evaluated"
    report_content = client.get(f"/api/v1/reports/{report['report_id']}/content")
    assert report_content.status_code == 200
    assert "not_evaluated" in report_content.text


def test_inbox_and_findings_use_explicit_empty_or_actionable_states(api_client):
    client, _ = api_client
    task = _create_task(client)
    inbox = client.get("/api/v1/inbox").json()
    assert inbox["items"][0]["task_id"] == task["task_id"]
    assert inbox["items"][0]["next_action"] == "import_candidate"
    findings = client.get(f"/api/v1/tasks/{task['task_id']}/findings").json()
    assert findings["items"] == []
    assert findings["available"] is True
    assert findings["complete"] is False
    assert "no verification" in findings["reason"]


def test_intake_rejects_local_paths_private_urls_and_missing_source(api_client):
    client, _ = api_client
    bad_repo = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "/Users/me/repo", "base_ref": "main", "patch_text": "diff --git a/a b/a",
    })
    assert bad_repo.status_code == 422
    private_pr = client.post("/api/v1/intakes", json={
        "mode": "pr", "repo_id": "owner/repo", "base_ref": "main", "pr_url": "https://127.0.0.1/owner/repo/pull/1",
    })
    assert private_pr.status_code == 422
    missing_patch = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "owner/repo", "base_ref": "main",
    })
    assert missing_patch.status_code == 422


def test_missing_resource_uses_stable_error_envelope(api_client):
    client, _ = api_client
    response = client.get("/api/v1/tasks/task_missing")
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "task_not_found"
    assert payload["request_id"]


def _temporary_git_repository(root):
    """Create a small real repository and return (path, base_sha, patch)."""
    repo = root / "verification-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "patchpilot@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "PatchPilot tests"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n"
        "def test_add():\n"
        "    assert add(2, 1) == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "calc.py", "test_calc.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    # The candidate makes a real source change while preserving the acceptance
    # contract.  This keeps both base and candidate test runs green.
    (repo / "calc.py").write_text(
        "def add(a, b):\n"
        "    return a - b\n\n"
        "# candidate instrumentation\n",
        encoding="utf-8",
    )
    patch = subprocess.check_output(["git", "diff", "--", "calc.py"], cwd=repo, text=True)
    return repo, base_sha, patch


def _queue_verification_for_execution(client, base_sha, patch):
    intake = client.post("/api/v1/intakes", json={
        "mode": "local_patch", "repo_id": "owner/repo", "base_ref": base_sha,
        "patch_text": patch, "issue_title": "Execute a real candidate",
        "issue_body": "Run base and candidate checks in a temporary repository.",
    })
    assert intake.status_code == 202
    task_response = client.post("/api/v1/tasks", json={"intake_id": intake.json()["intake_id"]})
    assert task_response.status_code == 201
    task = task_response.json()["task"]
    candidate_response = client.post(f"/api/v1/tasks/{task['task_id']}/candidates", json={
        "source": "upload", "base_sha": base_sha, "patch_text": patch,
    })
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()["candidate"]
    draft = client.post(f"/api/v1/tasks/{task['task_id']}/contracts/draft", json={
        "source_ids": ["issue:execution"], "base_snapshot_id": base_sha,
        "conditions": [{
            "condition_id": "AC-EXEC", "kind": "change",
            "statement": "The candidate preserves the arithmetic behavior.",
            "source_refs": ["issue:execution"], "required": True,
            "oracle": {"type": "pytest", "command": ["pytest", "-q"]},
        }],
    })
    assert draft.status_code == 202
    contract_id = draft.json()["contract"]["contract_id"]
    edited = client.put(f"/api/v1/contracts/{contract_id}", json={
        "expected_revision": 1,
        "conditions": [{
            "condition_id": "AC-EXEC", "kind": "change",
            "statement": "The candidate preserves the arithmetic behavior.",
            "source_refs": ["issue:execution"], "required": True,
            "oracle": {"type": "pytest", "command": ["pytest", "-q"]},
        }],
    })
    assert edited.status_code == 200
    frozen = client.post(f"/api/v1/contracts/{contract_id}/freeze", json={
        "expected_revision": 2, "confirmed_condition_ids": ["AC-EXEC"],
    })
    assert frozen.status_code == 201
    queued = client.post(f"/api/v1/tasks/{task['task_id']}/verifications", json={
        "candidate_id": candidate["candidate_id"], "contract_id": contract_id,
        "suite_id": "pytest-default", "environment_id": "local-test",
        "policy_id": "policy-test", "baseline_tests_hash": "sha256:test",
        "command_argv": [["pytest", "-q"]],
    })
    assert queued.status_code == 202
    return queued.json()["verification"], task, candidate, contract_id


def test_execute_runs_base_and_candidate_in_real_temporary_git_repo(api_client, tmp_path, monkeypatch):
    client, api = api_client
    repo, base_sha, patch = _temporary_git_repository(tmp_path)
    monkeypatch.setenv("PATCHPILOT_WORKSPACE_ROOTS", str(tmp_path))
    queued, task, candidate, contract_id = _queue_verification_for_execution(client, base_sha, patch)

    executed = client.post(
        f"/api/v1/verifications/{queued['verification_id']}/execute",
        json={"repo_path": str(repo), "command_argv": [["pytest", "-q"]], "suite_id": "pytest-default"},
    )
    assert executed.status_code == 201
    payload = executed.json()
    assert payload["status"] == "completed"
    result = payload["verification"]
    assert result["run_state"] == "completed"
    assert result["verdict"] == "accepted_within_scope"
    assert result["gaps"] == []
    assert payload["source_verification_id"] == queued["verification_id"]
    assert result["verification_id"] != queued["verification_id"]
    assert result["verification_key"] == queued["verification_key"]
    assert {(check["variant"], check["outcome"], check["count"]) for check in payload["checks"]} == {
        ("base", "pass", 1), ("candidate", "pass", 1),
    }
    checks = client.get(f"/api/v1/verifications/{result['verification_id']}/checks")
    assert checks.status_code == 200
    assert checks.json()["complete"] is True
    assert len(checks.json()["items"]) == 2
    assert api.store.get_verification(queued["verification_id"]).run_state.value == "queued"
    assert api.store.get_verification(result["verification_id"]).verdict.value == "accepted_within_scope"


def test_execution_persists_traceable_candidate_failure_finding(api_client, tmp_path, monkeypatch):
    client, _ = api_client
    repo, base_sha, _ = _temporary_git_repository(tmp_path)
    # Replace the harmless instrumentation patch with a real regression.  The
    # pinned base remains green while the candidate makes the test fail.
    subprocess.run(["git", "checkout", "--", "calc.py"], cwd=repo, check=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    bad_patch = subprocess.check_output(["git", "diff", "--", "calc.py"], cwd=repo, text=True)
    monkeypatch.setenv("PATCHPILOT_WORKSPACE_ROOTS", str(tmp_path))
    queued, task, _, _ = _queue_verification_for_execution(client, base_sha, bad_patch)

    executed = client.post(
        f"/api/v1/verifications/{queued['verification_id']}/execute",
        json={"repo_path": str(repo), "command_argv": [["pytest", "-q"]], "suite_id": "pytest-default"},
    )
    assert executed.status_code == 201
    payload = executed.json()
    # A failed command is persisted as an observed finding; without a
    # condition-mapped oracle/repeat proof it remains inconclusive.
    assert payload["verification"]["verdict"] == "inconclusive"
    assert payload["findings"]
    assert any(item["kind"] == "candidate_failure" for item in payload["findings"])

    listed = client.get(f"/api/v1/tasks/{task['task_id']}/findings")
    assert listed.status_code == 200
    body = listed.json()
    assert body["available"] is True and body["complete"] is True
    candidate_findings = [item for item in body["items"] if item["kind"] == "candidate_failure"]
    assert len(candidate_findings) == 1
    finding = candidate_findings[0]
    assert finding["status"] == "observed"
    assert finding["source_variant"] == "candidate"
    assert finding["verification_id"] == payload["verification"]["verification_id"]
    assert finding["check_ids"] and finding["evidence_refs"]
    detail = client.get(f"/api/v1/findings/{finding['finding_id']}")
    assert detail.status_code == 200
    assert detail.json()["finding"]["finding_id"] == finding["finding_id"]


def test_findings_keep_baseline_failure_and_inconclusive_outcomes(tmp_path):
    """Derivation records each non-pass observation without claiming success."""
    from patchpilot.domain.entities import (
        Candidate, CheckExecution, ContractVersion, Finding, ReviewDecision,
        RunState, Task, Verification, Verdict, Validity, AcceptanceCondition,
        ContractState,
    )
    from patchpilot.evidence.store import EvidenceStore

    store = EvidenceStore(tmp_path)
    task = Task("task-findings", {"title": "finding derivation"})
    store.save_task(task)
    candidate = Candidate("candidate-findings", task.task_id, "base-sha", None, "tree", "patch", "upload")
    store.save_candidate(candidate)
    contract = ContractVersion(
        "contract-findings", task.task_id, 1, ContractState.FROZEN,
        (AcceptanceCondition("AC-1", "preserve", "keep behavior", ("issue:1",), True, "maintainer_confirmed", {"type": "pytest"}),),
        ("issue:1",),
    )
    store.save_contract_version(contract)
    key = "findings-verification-key"
    verification = Verification("verification-findings", key, candidate.candidate_id, contract.contract_id, RunState.COMPLETED, Verdict.INCONCLUSIVE, 1, Validity.CURRENT, ReviewDecision.PENDING, ("incomplete_check_execution",), ())
    store.save_verification(verification)
    for check_id, variant, outcome in (("check-base", "base", "fail"), ("check-candidate-flaky", "candidate", "flaky"), ("check-candidate-not-run", "candidate", "not_run"), ("check-candidate-error", "candidate", "error")):
        store.save_check_execution(CheckExecution(check_id, verification.verification_id, "suite", check_id, variant, outcome, 0, details={"reason": outcome}))

    findings = store.derive_findings_for_verification(verification.verification_id)
    kinds = {item.kind for item in findings}
    assert {"baseline_failure", "flaky", "not_run", "candidate_error"}.issubset(kinds)
    assert all(item.verification_id == verification.verification_id for item in findings)
    assert all(item.repeats == 1 for item in findings)
    assert store.list_findings(task.task_id) == findings


def test_execute_reports_error_state_when_workspace_is_not_allowlisted(api_client):
    client, _ = api_client
    patch = "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"
    queued, _, _, _ = _queue_verification_for_execution(client, "missing-base", patch)
    blocked = client.post(
        f"/api/v1/verifications/{queued['verification_id']}/execute",
        json={"repo_path": "/tmp/not-allowlisted", "command_argv": [["pytest", "-q"]]},
    )
    assert blocked.status_code == 201
    body = blocked.json()
    assert body["status"] == "error"
    assert body["verification"]["run_state"] == "error"
    assert body["verification"]["verdict"] == "inconclusive"
    assert "repo_path is outside configured PatchPilot workspaces" in body["verification"]["gaps"]


def test_intake_source_resolution_is_pending_without_github_auth(api_client, monkeypatch):
    client, _ = api_client
    response = client.post("/api/v1/intakes", json={
        "mode": "pr", "repo_id": "acme/widget", "base_ref": "main",
        "pr_url": "https://github.com/acme/widget/pull/7",
    })
    assert response.status_code == 202
    intake_id = response.json()["intake_id"]
    pending = client.get(f"/api/v1/intakes/{intake_id}/source")
    assert pending.status_code == 200
    assert pending.json()["source_resolution"]["status"] == "pending"
    resolved = client.post(f"/api/v1/intakes/{intake_id}/resolve")
    assert resolved.status_code == 202
    assert resolved.json()["status"] == "pending_source_resolution"
    assert resolved.json()["source_resolution"]["code"] == "github_auth_not_configured"


def test_intake_source_resolution_persists_exact_pr_refs(api_client, monkeypatch):
    client, api = api_client
    from patchpilot.integrations.github_source import GitHubSourceResolver, HTTPResponse
    import json

    class FakeHTTP:
        def get(self, url, *, headers, timeout):
            assert url == "https://api.github.com/repos/acme/widget/pulls/7"
            assert headers["Authorization"] == "Bearer injected-token"
            return HTTPResponse(200, {}, json.dumps({
                "title": "Fix parser", "body": "Need parser compatibility", "state": "open",
                "user": {"login": "maintainer"}, "updated_at": "2026-10-09T10:00:00Z",
                "base": {"ref": "main", "sha": "base-sha"},
                "head": {"ref": "fix/parser", "sha": "head-sha"},
            }).encode(), url)

    monkeypatch.setattr(api_v1_module := __import__("patchpilot.api_v1", fromlist=["x"]), "_github_source_resolver", lambda: GitHubSourceResolver(FakeHTTP(), token="injected-token"))
    response = client.post("/api/v1/intakes", json={
        "mode": "pr", "repo_id": "acme/widget", "base_ref": "main",
        "pr_url": "https://github.com/acme/widget/pull/7",
    })
    intake_id = response.json()["intake_id"]
    resolved = client.post(f"/api/v1/intakes/{intake_id}/resolve")
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["source_resolution"]["source"]["base_sha"] == "base-sha"
    assert body["source_resolution"]["source"]["head_sha"] == "head-sha"
    fetched = client.get(f"/api/v1/intakes/{intake_id}").json()["intake"]
    assert fetched["resolved_refs"]["head_sha"] == "head-sha"
    assert "injected-token" not in json.dumps(body)


def test_intake_source_resolution_rejects_repo_mismatch(api_client, monkeypatch):
    client, _ = api_client
    from patchpilot.integrations.github_source import GitHubSourceResolver, HTTPResponse
    import json

    class FakeHTTP:
        def get(self, url, *, headers, timeout):
            return HTTPResponse(200, {}, json.dumps({"title": "x", "base": {"ref": "main", "sha": "base"}, "head": {"sha": "head"}}).encode(), url)

    api_v1_module = __import__("patchpilot.api_v1", fromlist=["x"])
    monkeypatch.setattr(api_v1_module, "_github_source_resolver", lambda: GitHubSourceResolver(FakeHTTP(), token="token"))
    response = client.post("/api/v1/intakes", json={
        "mode": "pr", "repo_id": "other/repo", "base_ref": "main",
        "pr_url": "https://github.com/acme/widget/pull/7",
    })
    intake_id = response.json()["intake_id"]
    rejected = client.post(f"/api/v1/intakes/{intake_id}/resolve")
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "source_repo_mismatch"
