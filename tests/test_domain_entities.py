from __future__ import annotations

import sqlite3

import pytest

from patchpilot.domain.entities import (
    AcceptanceCondition,
    Candidate,
    ContractState,
    ContractVersion,
    ReviewDecision,
    ReviewDecisionRecord,
    RunState,
    Task,
    Validity,
    Verification,
    VerificationKeyInputs,
    Verdict,
    compute_verification_key,
    aggregate_verdict,
    normalized_argv_digest,
)
from patchpilot.evidence.store import EvidenceStore


def _task() -> Task:
    return Task("task-1", {"title": "Keep colon", "body": "preserve a:b"}, ("issue-1",))


def _contract() -> ContractVersion:
    condition = AcceptanceCondition(
        "AC-01", "preserve", "value keeps a colon", ("issue-1#body",),
        oracle={"type": "example", "expected": ["a:b"]}, confirmation="maintainer_confirmed",
    )
    return ContractVersion("contract-1", "task-1", 1, ContractState.FROZEN, (condition,), ("issue-1#body",))


def test_verification_key_is_canonical_and_changes_for_every_input():
    kwargs = dict(
        base_tree="base", candidate_tree="candidate", contract="contract-hash",
        suite="suite-hash", baseline_tests="baseline-hash", environment="env-hash",
        commands=normalized_argv_digest([["pytest", "-q"], ["python", "-m", "compileall"]]),
        policy="policy-hash", verifier="engine@1", probe_plan="probe-hash", seed=17,
    )
    first = compute_verification_key(**kwargs)
    reordered = compute_verification_key(**{**kwargs, "seed": 17})
    assert first == reordered
    assert len(first) == 64
    assert compute_verification_key(**{**kwargs, "seed": 18}) != first
    assert compute_verification_key(**{**kwargs, "candidate_tree": "other"}) != first


@pytest.mark.parametrize("outcome", ["error", "not_run", "flaky", "unsupported", "skipped", "fail"])
def test_incomplete_or_nonpassing_checks_never_accept(outcome):
    assert aggregate_verdict([outcome], required_oracles_valid=True) == Verdict.INCONCLUSIVE


def test_verdict_precedence_requires_complete_proof():
    assert aggregate_verdict([], required_oracles_valid=True) == Verdict.INCONCLUSIVE
    assert aggregate_verdict(["pass"], required_oracles_valid=False) == Verdict.INCONCLUSIVE
    assert aggregate_verdict(["pass"], required_oracles_valid=True, required_gaps=["baseline unknown"]) == Verdict.INCONCLUSIVE
    assert aggregate_verdict(["fail"], required_oracles_valid=False, confirmed_required_failure=True) == Verdict.REJECTED
    assert aggregate_verdict(["pass"], required_oracles_valid=True, confirmed_required_failure=True,
                             hard_policy_violation=True) == Verdict.BLOCKED
    assert aggregate_verdict(["pass"], required_oracles_valid=True) == Verdict.ACCEPTED_WITHIN_SCOPE


def test_store_appends_immutable_revisions_and_binds_verification(tmp_path):
    store = EvidenceStore(tmp_path)
    task = _task()
    store.save_task(task)
    task_v2 = Task(task.task_id, task.issue_snapshot, task.source_refs, task.mode, 2)
    store.save_task(task_v2)
    assert [item.revision for item in store.list_task_revisions(task.task_id)] == [1, 2]
    with pytest.raises(ValueError, match="append as 3"):
        store.save_task(task_v2)

    contract = _contract()
    store.save_contract_version(contract)
    candidate = Candidate("candidate-1", task.task_id, "base", "head", "tree", "patch", "human")
    store.save_candidate(candidate)
    key = compute_verification_key(VerificationKeyInputs(
        "base-tree", "candidate-tree", contract.contract_hash, "suite", "baseline",
        "environment", "commands", "policy", "verifier", "probe", "seed",
    ))
    verification = Verification("verification-1", key, candidate.candidate_id, contract.contract_id,
                                RunState.COMPLETED, Verdict.ACCEPTED_WITHIN_SCOPE, contract_revision=1)
    store.save_verification(verification)
    assert store.get_verification(verification.verification_id).to_dict() == verification.to_dict()
    row = store.db.execute("SELECT run_state,verdict,validity,review_decision FROM verifications WHERE verification_id=?",
                           (verification.verification_id,)).fetchone()
    assert tuple(row) == ("completed", "accepted_within_scope", "current", "pending")
    with pytest.raises(sqlite3.IntegrityError, match="immutable verification"):
        store.db.execute("UPDATE verifications SET verdict='PASS' WHERE verification_id=?", (verification.verification_id,))

    decision = ReviewDecisionRecord("decision-1", verification.verification_id, key,
                                   ReviewDecision.ACCEPTED, "maintainer")
    store.record_review_decision(decision)
    assert store.list_review_decisions(verification.verification_id)[0].decision == ReviewDecision.ACCEPTED
    with pytest.raises(ValueError, match="expected_key"):
        store.record_review_decision(ReviewDecisionRecord("decision-2", verification.verification_id,
                                                           "wrong", ReviewDecision.REJECTED, "maintainer"))
    with pytest.raises(ValueError, match="requires a reason"):
        ReviewDecisionRecord("decision-3", verification.verification_id, key,
                             ReviewDecision.EXCEPTION_ACCEPTED, "maintainer")


def test_immutable_rows_cannot_be_updated_or_deleted(tmp_path):
    store = EvidenceStore(tmp_path)
    store.save_task(_task())
    with pytest.raises(sqlite3.IntegrityError, match="immutable task version"):
        store.db.execute("UPDATE task_versions SET payload=? WHERE task_id=? AND revision=1", ("{}", "task-1"))
    with pytest.raises(sqlite3.IntegrityError, match="immutable task version"):
        store.db.execute("DELETE FROM task_versions WHERE task_id=? AND revision=1", ("task-1",))


def test_jobs_are_resumable_and_idempotency_is_request_bound(tmp_path):
    store = EvidenceStore(tmp_path)
    store.save_job("job-1", "verify", "running", {"completed": 2})
    store.save_job("job-1", "verify", "completed", {"completed": 4})
    assert store.get_job("job-1")["state"] == "completed"
    assert store.get_job("job-1")["payload"] == {"completed": 4}
    assert store.reserve_idempotency("verify", "request-1", "hash-1", "job-1")
    assert not store.reserve_idempotency("verify", "request-1", "hash-1")
    with pytest.raises(ValueError, match="different request"):
        store.reserve_idempotency("verify", "request-1", "hash-2")


def test_existing_first_draft_database_gets_additive_verification_columns(tmp_path):
    db_path = tmp_path / "patchpilot.sqlite3"
    raw = sqlite3.connect(db_path)
    raw.executescript("""
        CREATE TABLE verifications(
            verification_id TEXT PRIMARY KEY, verification_key TEXT NOT NULL,
            candidate_id TEXT NOT NULL, contract_id TEXT NOT NULL,
            contract_revision INTEGER NOT NULL, payload TEXT NOT NULL,
            created_at TEXT NOT NULL);
    """)
    raw.commit(); raw.close()
    store = EvidenceStore(tmp_path)
    columns = {row["name"] for row in store.db.execute("PRAGMA table_info(verifications)")}
    assert {"run_state", "verdict", "validity", "review_decision"} <= columns
