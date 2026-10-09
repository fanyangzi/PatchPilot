"""Immutable, source-aware entities for the PatchPilot acceptance domain.

These records are intentionally separate from the legacy orchestration DTOs in
``models.py``.  They describe durable product facts; they do not imply that a
verification passed or that a human approved it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class StrEnum(str, Enum):
    """String enum with a JSON-friendly value representation."""


class RunState(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class Verdict(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    ACCEPTED_WITHIN_SCOPE = "accepted_within_scope"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    BLOCKED = "blocked"


class Validity(StrEnum):
    CURRENT = "current"
    STALE = "stale"


class ReviewDecision(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXCEPTION_ACCEPTED = "exception_accepted"


class CheckOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"
    FLAKY = "flaky"
    UNSUPPORTED = "unsupported"


class ContractState(StrEnum):
    DRAFT = "draft"
    REVIEW = "review"
    FROZEN = "frozen"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _freeze_json(value: Any) -> Any:
    """Recursively freeze JSON-compatible values stored in frozen entities."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): _freeze_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(v) for v in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"value is not canonical JSON data: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): thaw_json(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(v) for v in value]
    return value


def canonical_json(value: Any) -> str:
    """RFC-8259 JSON with deterministic key order and no insignificant space."""
    return json.dumps(
        thaw_json(value), sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Task:
    """An immutable task revision with the original request snapshot."""

    task_id: str
    issue_snapshot: Mapping[str, Any]
    source_refs: tuple[str, ...] = ()
    mode: str = "verify"
    revision: int = 1
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.task_id or self.revision < 1:
            raise ValueError("task_id and positive revision are required")
        object.__setattr__(self, "issue_snapshot", _freeze_json(self.issue_snapshot))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "issue_snapshot": thaw_json(self.issue_snapshot),
                "source_refs": list(self.source_refs), "mode": self.mode,
                "revision": self.revision, "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A content-addressed proposal; repairs always create a new candidate."""

    candidate_id: str
    task_id: str
    base_sha: str
    head_sha: str | None
    tree_digest: str
    patch_hash: str
    author_type: str
    parent_candidate_id: str | None = None
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("candidate_id", "task_id", "base_sha", "tree_digest", "patch_hash", "author_type"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class AcceptanceCondition:
    condition_id: str
    kind: str
    statement: str
    source_refs: tuple[str, ...]
    required: bool = True
    confirmation: str = "unconfirmed"
    oracle: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.condition_id or not self.statement:
            raise ValueError("condition_id and statement are required")
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        object.__setattr__(self, "oracle", _freeze_json(self.oracle))

    def to_dict(self) -> dict[str, Any]:
        return {"condition_id": self.condition_id, "kind": self.kind,
                "statement": self.statement, "source_refs": list(self.source_refs),
                "required": self.required, "confirmation": self.confirmation,
                "oracle": thaw_json(self.oracle)}


@dataclass(frozen=True, slots=True)
class ContractVersion:
    """An immutable contract revision, with a hash over its complete content."""

    contract_id: str
    task_id: str
    revision: int
    state: ContractState
    conditions: tuple[AcceptanceCondition, ...]
    source_refs: tuple[str, ...] = ()
    contract_hash: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.contract_id or not self.task_id or self.revision < 1:
            raise ValueError("contract/task ids and positive revision are required")
        object.__setattr__(self, "state", ContractState(self.state))
        object.__setattr__(self, "conditions", tuple(self.conditions))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))
        expected = canonical_hash(self.content_dict())
        if self.contract_hash and self.contract_hash != expected:
            raise ValueError("contract_hash does not match immutable contract content")
        object.__setattr__(self, "contract_hash", expected)

    def content_dict(self) -> dict[str, Any]:
        return {"contract_id": self.contract_id, "task_id": self.task_id,
                "revision": self.revision, "state": self.state.value,
                "conditions": [condition.to_dict() for condition in self.conditions],
                "source_refs": list(self.source_refs), "created_at": self.created_at}

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_dict(), "contract_hash": self.contract_hash}


@dataclass(frozen=True, slots=True)
class VerificationKeyInputs:
    base_tree: str
    candidate_tree: str
    contract: str
    suite: str
    baseline_tests: str
    environment: str
    commands: str
    policy: str
    verifier: str
    probe_plan: str
    seed: Any

    def __post_init__(self) -> None:
        for name in ("base_tree", "candidate_tree", "contract", "suite", "baseline_tests",
                     "environment", "commands", "policy", "verifier", "probe_plan"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required for a verification key")
        # Validate the seed and all values under the same canonical JSON rules.
        object.__setattr__(self, "seed", _freeze_json(self.seed))

    def to_dict(self) -> dict[str, Any]:
        return {"base_tree": self.base_tree, "candidate_tree": self.candidate_tree,
                "contract": self.contract, "suite": self.suite,
                "baseline_tests": self.baseline_tests, "environment": self.environment,
                "commands": self.commands, "policy": self.policy,
                "verifier": self.verifier, "probe_plan": self.probe_plan,
                "seed": self.seed}


def normalized_argv_digest(commands: Sequence[Sequence[str]]) -> str:
    """Hash commands as argv arrays (never shell-joined command strings)."""
    normalized: list[list[str]] = []
    for argv in commands:
        if not argv or any(not isinstance(arg, str) for arg in argv):
            raise ValueError("each command must be a non-empty argv sequence of strings")
        normalized.append(list(argv))
    return canonical_hash(normalized)


def compute_verification_key(inputs: VerificationKeyInputs | None = None, **fields: Any) -> str:
    """Return SHA-256 over the spec's exact canonical verification input object.

    Callers may pass a ``VerificationKeyInputs`` or its named fields. The
    ``commands`` field must already be the normalized argv digest.
    """
    if inputs is not None and fields:
        raise TypeError("pass either VerificationKeyInputs or named fields")
    if inputs is None:
        inputs = VerificationKeyInputs(**fields)
    return canonical_hash(inputs.to_dict())


def aggregate_verdict(
    required_check_outcomes: Sequence[CheckOutcome | str],
    *,
    required_oracles_valid: bool,
    hard_policy_violation: bool = False,
    confirmed_required_failure: bool = False,
    required_gaps: Sequence[str] = (),
    unresolved_required_condition: bool = False,
) -> Verdict:
    """Apply the deterministic verdict precedence from specification §13.

    A failing check is not by itself proof of a requirement violation: callers
    must separately establish ``confirmed_required_failure`` with a valid
    oracle. Incomplete, unsupported, unstable, or infrastructure outcomes
    remain inconclusive and can never be promoted to accepted.
    """
    if hard_policy_violation:
        return Verdict.BLOCKED
    if confirmed_required_failure:
        return Verdict.REJECTED
    if unresolved_required_condition or required_gaps:
        return Verdict.INCONCLUSIVE
    outcomes = tuple(CheckOutcome(outcome) for outcome in required_check_outcomes)
    if not outcomes or not required_oracles_valid:
        return Verdict.INCONCLUSIVE
    if any(outcome is not CheckOutcome.PASS for outcome in outcomes):
        return Verdict.INCONCLUSIVE
    return Verdict.ACCEPTED_WITHIN_SCOPE


@dataclass(frozen=True, slots=True)
class Verification:
    verification_id: str
    verification_key: str
    candidate_id: str
    contract_id: str
    run_state: RunState
    verdict: Verdict
    contract_revision: int = 1
    validity: Validity = Validity.CURRENT
    review_decision: ReviewDecision = ReviewDecision.PENDING
    gaps: tuple[str, ...] = ()
    completed_checks: tuple[str, ...] = ()
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("verification_id", "verification_key", "candidate_id", "contract_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.contract_revision < 1:
            raise ValueError("contract_revision must be positive")
        object.__setattr__(self, "run_state", RunState(self.run_state))
        object.__setattr__(self, "verdict", Verdict(self.verdict))
        object.__setattr__(self, "validity", Validity(self.validity))
        object.__setattr__(self, "review_decision", ReviewDecision(self.review_decision))
        object.__setattr__(self, "gaps", tuple(self.gaps))
        object.__setattr__(self, "completed_checks", tuple(self.completed_checks))

    def to_dict(self) -> dict[str, Any]:
        return {"verification_id": self.verification_id, "verification_key": self.verification_key,
                "candidate_id": self.candidate_id, "contract_id": self.contract_id,
                "contract_revision": self.contract_revision,
                "run_state": self.run_state.value, "verdict": self.verdict.value,
                "validity": self.validity.value, "review_decision": self.review_decision.value,
                "gaps": list(self.gaps), "completed_checks": list(self.completed_checks),
                "created_at": self.created_at}


@dataclass(frozen=True, slots=True)
class CheckExecution:
    """One measured check result attached to an immutable verification run.

    ``variant`` distinguishes the base and candidate executions.  A check is
    deliberately a record rather than a boolean: a zero-test invocation,
    timeout, parser error, or unsupported runner must remain visible and can
    never be silently promoted to ``pass``.
    """

    check_id: str
    verification_id: str
    suite_id: str
    target: str
    variant: str
    outcome: CheckOutcome
    count: int
    command_argv: tuple[str, ...] = ()
    return_code: int | None = None
    duration_ms: int | None = None
    stdout: str = ""
    stderr: str = ""
    artifact_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("check_id", "verification_id", "suite_id", "target", "variant"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.count < 0:
            raise ValueError("count must be non-negative")
        if self.variant not in {"base", "candidate"}:
            raise ValueError("variant must be base or candidate")
        object.__setattr__(self, "outcome", CheckOutcome(self.outcome))
        object.__setattr__(self, "command_argv", tuple(self.command_argv))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        object.__setattr__(self, "details", _freeze_json(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "verification_id": self.verification_id,
            "suite_id": self.suite_id,
            "target": self.target,
            "variant": self.variant,
            "outcome": self.outcome.value,
            "count": self.count,
            "command_argv": list(self.command_argv),
            "return_code": self.return_code,
            "duration_ms": self.duration_ms,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifact_refs": list(self.artifact_refs),
            "details": thaw_json(self.details),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class Finding:
    """A durable observation derived from measured verification evidence.

    Findings intentionally describe what the verifier observed.  They do not
    claim that a requirement was violated unless the contract oracle and the
    required repeat evidence support that conclusion.  In particular, a
    ``candidate_failure`` is an actionable observation, while ``flaky`` and
    ``not_run`` remain inconclusive.  Every finding points back to the exact
    verification snapshot and (when available) check execution that produced
    it, so a later execution can never rewrite its history.
    """

    finding_id: str
    task_id: str
    verification_id: str
    verification_key: str
    candidate_id: str
    contract_id: str
    contract_revision: int
    kind: str
    status: str
    title: str
    message: str
    condition_id: str | None = None
    input_hash: str | None = None
    expected_basis: str = ""
    source_variant: str | None = None
    check_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    repeats: int = 1
    details: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in (
            "finding_id", "task_id", "verification_id", "verification_key",
            "candidate_id", "contract_id", "kind", "status", "title", "message",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.contract_revision < 1:
            raise ValueError("contract_revision must be positive")
        if self.repeats < 1:
            raise ValueError("repeats must be positive")
        if self.source_variant is not None and self.source_variant not in {"base", "candidate"}:
            raise ValueError("source_variant must be base or candidate")
        object.__setattr__(self, "check_ids", tuple(self.check_ids))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "details", _freeze_json(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "task_id": self.task_id,
            "verification_id": self.verification_id,
            "verification_key": self.verification_key,
            "candidate_id": self.candidate_id,
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "kind": self.kind,
            "status": self.status,
            "title": self.title,
            "message": self.message,
            "condition_id": self.condition_id,
            "input_hash": self.input_hash,
            "expected_basis": self.expected_basis,
            "source_variant": self.source_variant,
            "check_ids": list(self.check_ids),
            "evidence_refs": list(self.evidence_refs),
            "repeats": self.repeats,
            "details": thaw_json(self.details),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ReviewDecisionRecord:
    decision_id: str
    verification_id: str
    expected_key: str
    decision: ReviewDecision
    actor: str
    reason: str = ""
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision", ReviewDecision(self.decision))
        for name in ("decision_id", "verification_id", "expected_key", "actor"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.decision == ReviewDecision.PENDING:
            raise ValueError("pending is a verification default, not a review decision")
        if self.decision == ReviewDecision.EXCEPTION_ACCEPTED and not self.reason.strip():
            raise ValueError("exception_accepted requires a reason")

    def to_dict(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id, "verification_id": self.verification_id,
                "expected_key": self.expected_key, "decision": self.decision.value,
                "actor": self.actor, "reason": self.reason, "created_at": self.created_at}
