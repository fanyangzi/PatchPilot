"""Versioned, source-aware HTTP resources for PatchPilot.

The legacy ``/api`` routes are intentionally left in :mod:`patchpilot.api`.
This module adds the durable resource surface described by the final product
specification.  It does not run a repository, call a model, or invent a
successful result: an intake is recorded as pending source resolution, a task
is a draft, and a verification is queued with an explicit ``not_evaluated``
verdict until a worker writes measured checks.

The first local implementation uses the existing ``EvidenceStore`` SQLite
database.  The small ``api_*`` tables are append-only metadata for resources
that do not yet have a worker implementation; domain entities remain stored
through ``EvidenceStore`` so API and CLI share the same immutable records.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .domain.entities import (
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
    normalized_argv_digest,
    utc_now,
)
from .application.verification_service import (
    VerificationExecutionError,
    VerificationService,
    WorkspaceResolutionError,
)
from .application.job_worker import DurableJobStore
from .integrations.github_source import GitHubSourceResolver, SourceResolutionError


router = APIRouter(prefix="/api/v1", tags=["v1"])
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_PATCH_BYTES = 2 * 1024 * 1024


class APIError(Exception):
    """An error with a stable machine code and user-safe details."""

    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class APIModel(BaseModel):
    # Preserve source text byte-for-byte after UTF-8 encoding; trimming unified
    # patches changes their digest and can corrupt line structure.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


def _valid_id(value: str, field_name: str) -> str:
    if not value or not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a non-empty safe identifier")
    return value


def _valid_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    parsed = urlparse(str(value))
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must be an https URL")
    # Prevent an intake from turning the control plane into an SSRF primitive.
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError(f"{field_name} cannot target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise ValueError(f"{field_name} cannot target a private address")
    return str(value)


class IntakeCreate(APIModel):
    mode: Literal["pr", "issue_candidate", "local_patch"]
    repo_id: str
    base_ref: str
    pr_url: str | None = None
    issue_url: str | None = None
    candidate_ref: str | None = None
    patch_text: str | None = None
    issue_title: str | None = None
    issue_body: str | None = None
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("repo_id")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if not _REPO_RE.fullmatch(value):
            raise ValueError("repo_id must be owner/name; local absolute paths are not accepted")
        return value

    @field_validator("base_ref", "candidate_ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or ".." in value or "\x00" in value or len(value) > 256:
            raise ValueError("invalid git ref")
        return value

    @field_validator("pr_url", "issue_url")
    @classmethod
    def validate_url(cls, value: str | None, info) -> str | None:
        return _valid_url(value, info.field_name)

    @field_validator("patch_text")
    @classmethod
    def validate_patch(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("patch_text cannot be empty")
            if len(value.encode("utf-8")) > _MAX_PATCH_BYTES:
                raise ValueError("patch_text exceeds the 2 MiB limit")
        return value

    @model_validator(mode="after")
    def validate_mode_inputs(self):
        if self.mode == "pr" and not self.pr_url:
            raise ValueError("pr_url is required for pr intake")
        if self.mode == "issue_candidate":
            if not self.issue_url:
                raise ValueError("issue_url is required for issue_candidate intake")
            if not self.candidate_ref and not self.patch_text:
                raise ValueError("candidate_ref or patch_text is required for issue_candidate intake")
        if self.mode == "local_patch" and not self.patch_text:
            raise ValueError("patch_text is required for local_patch intake")
        return self


class TaskCreate(APIModel):
    intake_id: str
    mode: Literal["verify", "repair", "review"] = "verify"
    environment_id: str | None = None
    policy_id: str | None = None

    @field_validator("intake_id", "environment_id", "policy_id")
    @classmethod
    def validate_ids(cls, value: str | None, info) -> str | None:
        return _valid_id(value, info.field_name) if value is not None else None


class ConditionInput(APIModel):
    condition_id: str
    kind: Literal["change", "preserve", "constraint", "clarification"]
    statement: str = Field(min_length=1, max_length=10_000)
    source_refs: list[str] = Field(min_length=1)
    required: bool = True
    oracle: dict[str, Any] = Field(default_factory=dict)

    @field_validator("condition_id")
    @classmethod
    def validate_condition_id(cls, value: str) -> str:
        return _valid_id(value, "condition_id")

    @field_validator("statement")
    @classmethod
    def validate_statement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("statement cannot be blank")
        return value


class ContractDraftCreate(APIModel):
    source_ids: list[str] = Field(min_length=1)
    base_snapshot_id: str
    model_profile: str = "manual"
    conditions: list[ConditionInput] = Field(default_factory=list)

    @field_validator("source_ids")
    @classmethod
    def validate_sources(cls, value: list[str]) -> list[str]:
        if any(not x.strip() for x in value):
            raise ValueError("source_ids cannot contain empty values")
        return list(dict.fromkeys(value))


class ContractUpdate(APIModel):
    expected_revision: int = Field(ge=1)
    conditions: list[ConditionInput] = Field(min_length=1)


class ContractFreeze(APIModel):
    expected_revision: int = Field(ge=1)
    confirmed_condition_ids: list[str] = Field(default_factory=list)
    scope_exclusions: list[str] = Field(default_factory=list)
    actor: str = "maintainer"


class CandidateCreate(APIModel):
    source: Literal["pr", "upload", "model_edit", "human"]
    base_sha: str = Field(min_length=1, max_length=200)
    content_ref: str | None = None
    patch_text: str | None = None
    head_sha: str | None = None
    author_type: str | None = None
    parent_candidate_id: str | None = None

    @field_validator("patch_text")
    @classmethod
    def validate_candidate_patch(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > _MAX_PATCH_BYTES:
            raise ValueError("patch_text exceeds the 2 MiB limit")
        return value

    @model_validator(mode="after")
    def validate_content(self):
        if not self.patch_text and not self.content_ref:
            raise ValueError("patch_text or content_ref is required")
        if self.content_ref and (self.content_ref.startswith("/") or ".." in self.content_ref):
            raise ValueError("content_ref cannot be a filesystem path")
        return self


class VerificationCreate(APIModel):
    candidate_id: str
    contract_id: str
    suite_id: str
    environment_id: str
    policy_id: str
    baseline_tests_hash: str = "unavailable"
    command_argv: list[list[str]] = Field(default_factory=lambda: [["pytest", "-q"]])
    verifier_revision: str = "patchpilot-verifier@unversioned"
    probe_plan_hash: str = "none"
    seed: Any = 0

    @field_validator("candidate_id", "contract_id", "suite_id", "environment_id", "policy_id")
    @classmethod
    def validate_verification_ids(cls, value: str, info) -> str:
        return _valid_id(value, info.field_name)


class VerificationExecute(APIModel):
    """Explicit execution inputs; repository paths are allowlisted server-side."""

    repo_id: str | None = None
    repo_path: str | None = None
    suite_id: str | None = None
    command_argv: list[list[str]] | None = None

    @field_validator("repo_id", "suite_id")
    @classmethod
    def validate_optional_ids(cls, value: str | None, info) -> str | None:
        return _valid_id(value, info.field_name) if value is not None else None

    @field_validator("repo_path")
    @classmethod
    def validate_repo_path(cls, value: str | None) -> str | None:
        if value is not None and ("\x00" in value or not value.strip()):
            raise ValueError("repo_path must be a non-empty path without NUL bytes")
        return value


class DecisionCreate(APIModel):
    decision: Literal["accepted", "rejected", "exception_accepted"]
    reason: str = ""
    expected_key: str
    actor: str = "maintainer"


class _ResourceStore:
    """Durable metadata access sharing EvidenceStore's connection and lock."""

    def __init__(self, evidence_store):
        self.store = evidence_store
        self.db = evidence_store.db
        self.lock = getattr(evidence_store, "_lock", threading.RLock())
        with self.lock:
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS api_intakes(
                  intake_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_jobs(
                  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                  resource_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_candidate_contents(
                  candidate_id TEXT PRIMARY KEY, content TEXT, content_ref TEXT,
                  content_sha TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_contract_meta(
                  contract_id TEXT NOT NULL, revision INTEGER NOT NULL,
                  payload TEXT NOT NULL, PRIMARY KEY(contract_id, revision));
                CREATE TABLE IF NOT EXISTS api_reports(
                  report_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                  verification_id TEXT NOT NULL, verification_key TEXT NOT NULL,
                  format TEXT NOT NULL, content_type TEXT NOT NULL,
                  content TEXT NOT NULL, content_sha TEXT NOT NULL,
                  snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_api_reports_update
                  BEFORE UPDATE ON api_reports BEGIN SELECT RAISE(ABORT, 'immutable report'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_api_reports_delete
                  BEFORE DELETE ON api_reports BEGIN SELECT RAISE(ABORT, 'immutable report'); END;
                """
            )
            self.db.commit()
        # Queue columns/events are an additive migration so an existing local
        # artifact database can be resumed after a process restart.
        self.jobs = DurableJobStore(self.db, self.lock, table="api_jobs")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def put_intake(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("INSERT INTO api_intakes VALUES (?,?,?)", (payload["intake_id"], self._json(payload), payload["created_at"]))
            self.db.commit()

    def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT payload FROM api_intakes WHERE intake_id=?", (intake_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_intake(self, payload: dict[str, Any]) -> None:
        """Persist source-resolution state without exposing transport secrets."""
        with self.lock:
            self.db.execute(
                "UPDATE api_intakes SET payload=?, created_at=? WHERE intake_id=?",
                (self._json(payload), payload["created_at"], payload["intake_id"]),
            )
            self.db.commit()

    def put_job(self, payload: dict[str, Any]) -> None:
        # New API jobs are inserted once; state transitions belong to the
        # fenced queue and must not be replaced with an unfenced upsert.
        self.jobs.create(payload)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def put_candidate_content(self, candidate_id: str, patch_text: str | None, content_ref: str | None, content_sha: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO api_candidate_contents VALUES (?,?,?,?,?)", (candidate_id, patch_text, content_ref, content_sha, utc_now()))
            self.db.commit()

    def candidate_content(self, candidate_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM api_candidate_contents WHERE candidate_id=?", (candidate_id,)).fetchone()
        return dict(row) if row else None

    def put_contract_meta(self, contract_id: str, revision: int, payload: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO api_contract_meta VALUES (?,?,?)", (contract_id, revision, self._json(payload)))
            self.db.commit()

    def save_report(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute("INSERT INTO api_reports VALUES (?,?,?,?,?,?,?,?,?,?)", (
                payload["report_id"], payload["task_id"], payload["verification_id"],
                payload["verification_key"], payload["format"], payload["content_type"],
                payload["content"], payload["content_sha"], self._json(payload["snapshot"]), payload["created_at"],
            ))
            self.db.commit()

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM api_reports WHERE report_id=?", (report_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["snapshot"] = json.loads(data["snapshot"])
        return data

    def list_reports(self, task_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM api_reports WHERE task_id=? ORDER BY created_at,report_id", (task_id,)).fetchall()
        values = []
        for row in rows:
            data = dict(row)
            data["snapshot"] = json.loads(data["snapshot"])
            values.append(data)
        return values


def _evidence_store():
    # Delayed import prevents the existing app module's store construction from
    # becoming a circular import and lets tests reload the API with a temp root.
    from . import api as legacy_api
    return legacy_api.store


def _resources() -> _ResourceStore:
    return _ResourceStore(_evidence_store())


def _github_source_resolver() -> GitHubSourceResolver:
    """Build the read-only resolver at request time for safe config reloads.

    Tests and embedding applications may replace this factory with an injected
    resolver.  The default resolver reads ``GITHUB_TOKEN`` but never returns it.
    """
    return GitHubSourceResolver()


def _rid(request: Request) -> str:
    value = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")
    if value and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        return value
    return f"req_{uuid.uuid4().hex}"


def _result(request: Request, payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse({**payload, "request_id": _rid(request)}, status_code=status_code, headers={"X-Request-ID": _rid(request)})


def _request_hash(body: Any) -> str:
    if isinstance(body, BaseModel):
        body = body.model_dump(mode="json")
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _claim_idempotency(request: Request, scope: str, body: Any) -> tuple[str | None, dict[str, Any] | None]:
    """Claim an optional idempotency key and return a prior reference on retry."""
    key = request.headers.get("Idempotency-Key")
    if not key:
        return None, None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise APIError(422, "invalid_idempotency_key", "Idempotency-Key has an invalid format")
    store = _evidence_store()
    try:
        created = store.reserve_idempotency(scope, key, _request_hash(body))
    except ValueError as exc:
        raise APIError(409, "idempotency_conflict", str(exc)) from exc
    if created:
        return key, None
    prior = store.get_idempotency(scope, key)
    return key, prior


def _bind_idempotency(scope: str, key: str | None, response_ref: str) -> None:
    if not key:
        return
    store = _evidence_store()
    with store._lock:
        store.db.execute("UPDATE idempotency_keys SET response_ref=? WHERE scope=? AND idempotency_key=?", (response_ref, scope, key))
        store.db.commit()


def _error_payload(request: Request, exc: APIError) -> JSONResponse:
    body = {"error": {"code": exc.code, "message": exc.message}, "request_id": _rid(request)}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(body, status_code=exc.status_code, headers={"X-Request-ID": _rid(request)})


def _task_dict(task: Task) -> dict[str, Any]:
    return task.to_dict()


def _condition_from_input(condition: ConditionInput, confirmation: str = "unconfirmed") -> AcceptanceCondition:
    return AcceptanceCondition(condition.condition_id, condition.kind, condition.statement, tuple(condition.source_refs), condition.required, confirmation, condition.oracle)


def _contract_dict(contract: ContractVersion) -> dict[str, Any]:
    return contract.to_dict()


def _verification_dict(store, verification: Verification) -> dict[str, Any]:
    data = verification.to_dict()
    decisions = store.list_review_decisions(verification.verification_id)
    if decisions:
        data["review_decision"] = decisions[-1].decision.value
        data["review_decision_id"] = decisions[-1].decision_id
    return data


@router.get("/health")
def v1_health(request: Request):
    return _result(request, {"status": "ok", "service": "patchpilot-api", "api_version": "v1", "python": platform.python_version()})


@router.get("/tasks")
def v1_list_tasks(request: Request, limit: int = 100, cursor: str | None = None):
    if limit < 1 or limit > 500:
        raise APIError(422, "invalid_limit", "limit must be between 1 and 500")
    store = _evidence_store()
    with store._lock:
        rows = store.db.execute(
            "SELECT payload FROM task_versions WHERE revision IN (SELECT MAX(revision) FROM task_versions GROUP BY task_id) ORDER BY created_at DESC LIMIT ?",
            (limit + 1,),
        ).fetchall()
    items = [_task_dict(Task(**json.loads(row["payload"]))) for row in rows[:limit]]
    next_cursor = items[-1]["task_id"] if len(rows) > limit and items else None
    return _result(request, {"items": items, "next_cursor": next_cursor, "total": len(items)})


@router.get("/inbox")
def v1_inbox(request: Request, limit: int = 100, cursor: str | None = None):
    """Return an actionable queue derived only from persisted v1 resources."""
    if limit < 1 or limit > 500:
        raise APIError(422, "invalid_limit", "limit must be between 1 and 500")
    store = _evidence_store()
    with store._lock:
        rows = store.db.execute(
            "SELECT tv.payload FROM task_versions tv JOIN "
            "(SELECT task_id, MAX(revision) revision FROM task_versions GROUP BY task_id) latest "
            "ON latest.task_id=tv.task_id AND latest.revision=tv.revision "
            "ORDER BY tv.created_at DESC, tv.task_id DESC"
        ).fetchall()
    tasks = [Task(**json.loads(row["payload"])) for row in rows]
    if cursor:
        # Cursors are the last seen task ID and are applied after stable ordering.
        found = next((i for i, task in enumerate(tasks) if task.task_id == cursor), None)
        if found is None:
            raise APIError(400, "invalid_cursor", "cursor does not identify a current task")
        tasks = tasks[found + 1:]
    has_more = len(tasks) > limit
    selected = tasks[:limit]
    items = []
    for task in selected:
        candidates = store.list_candidates(task.task_id)
        contracts = store.list_contract_versions(task.task_id)
        verifications = [v for candidate in candidates for v in store.list_verifications(candidate.candidate_id)]
        latest_contract = contracts[-1] if contracts else None
        latest_verification = verifications[-1] if verifications else None
        if not candidates:
            action, reason = "import_candidate", "No candidate has been imported"
        elif latest_contract is None:
            action, reason = "draft_contract", "Acceptance conditions have not been drafted"
        elif latest_contract.state != ContractState.FROZEN:
            action, reason = "review_contract", "Acceptance contract is not frozen"
        elif latest_verification is None:
            action, reason = "run_verification", "No verification exists for the frozen contract"
        elif latest_verification.run_state in (RunState.QUEUED, RunState.RUNNING):
            action, reason = "await_verification", "Verification is not complete"
        elif latest_verification.review_decision == ReviewDecision.PENDING:
            action, reason = "review_result", "Machine result is awaiting a human decision"
        else:
            action, reason = "view_report", "Review the recorded decision and evidence"
        items.append({
            "task_id": task.task_id,
            "title": task.issue_snapshot.get("title", "Untitled task"),
            "repo": task.issue_snapshot.get("repo_id"),
            "mode": task.mode,
            "created_at": task.created_at,
            "source_refs": list(task.source_refs),
            "candidate_count": len(candidates),
            "current_contract": ({"contract_id": latest_contract.contract_id, "revision": latest_contract.revision, "state": latest_contract.state.value} if latest_contract else None),
            "latest_verification": _verification_dict(store, latest_verification) if latest_verification else None,
            "next_action": action,
            "next_action_reason": reason,
        })
    return _result(request, {"items": items, "next_cursor": selected[-1].task_id if has_more and selected else None, "total": len(items)})


@router.post("/intakes", status_code=202)
def create_intake(body: IntakeCreate, request: Request):
    scope = "POST:/api/v1/intakes"
    idem_key, prior = _claim_idempotency(request, scope, body)
    if prior and prior.get("response_ref"):
        existing = _resources().get_intake(prior["response_ref"])
        if existing:
            return _result(request, {"intake_id": existing["intake_id"], "status": existing["status"], "job_id": None, "intake": existing, "idempotent_replay": True}, 202)
    intake_id = f"intake_{uuid.uuid4().hex}"
    created = utc_now()
    payload = body.model_dump(mode="json")
    payload.update({
        "intake_id": intake_id,
        "created_at": created,
        "status": "pending_source_resolution",
        "resolved_refs": None,
        "source_validation": "url_validated_only" if body.mode != "local_patch" else "patch_validated_only",
    })
    resources = _resources()
    resources.put_intake(payload)
    job_id = f"job_{uuid.uuid4().hex}"
    resources.put_job({"job_id": job_id, "kind": "intake", "state": "queued", "resource_id": intake_id, "created_at": created, "updated_at": created})
    _bind_idempotency(scope, idem_key, intake_id)
    return _result(request, {"intake_id": intake_id, "status": payload["status"], "job_id": job_id, "intake": payload}, 202)


@router.get("/intakes/{intake_id}")
def get_intake(intake_id: str, request: Request):
    intake = _resources().get_intake(intake_id)
    if not intake:
        raise APIError(404, "intake_not_found", "intake does not exist or is not visible")
    return _result(request, {"intake": intake})


def _source_url_for_intake(intake: dict[str, Any]) -> str | None:
    if intake.get("mode") == "pr":
        return intake.get("pr_url")
    if intake.get("mode") == "issue_candidate":
        return intake.get("issue_url")
    return None


@router.get("/intakes/{intake_id}/source")
def get_intake_source(intake_id: str, request: Request):
    """Inspect the latest source snapshot without making a network request."""
    intake = _resources().get_intake(intake_id)
    if not intake:
        raise APIError(404, "intake_not_found", "intake does not exist or is not visible")
    resolution = intake.get("source_resolution")
    if resolution is None:
        resolution = {
            "status": "pending",
            "source": {"url": _source_url_for_intake(intake), "kind": intake.get("mode")},
            "code": "source_not_resolved",
            "message": "Source has not been resolved; retry when GitHub read access is configured.",
            "retryable": True,
        }
    return _result(request, {"intake_id": intake_id, "status": intake.get("status"), "source_resolution": resolution})


@router.post("/intakes/{intake_id}/resolve", status_code=200)
def resolve_intake_source(intake_id: str, request: Request):
    """Read a GitHub PR/Issue metadata snapshot, never mutate GitHub."""
    resources = _resources()
    intake = resources.get_intake(intake_id)
    if not intake:
        raise APIError(404, "intake_not_found", "intake does not exist or is not visible")
    source_url = _source_url_for_intake(intake)
    if intake.get("mode") == "local_patch":
        resolution = {
            "status": "resolved",
            "source": {"kind": "local_patch", "repo_id": intake.get("repo_id"), "base_ref": intake.get("base_ref"), "url": None},
            "retryable": False,
        }
    elif not source_url:
        raise APIError(422, "source_missing", "this intake does not contain a GitHub source URL")
    else:
        try:
            resolution = _github_source_resolver().resolve(source_url).to_dict()
        except SourceResolutionError as exc:
            if exc.code in {"invalid_source_url", "ssrf_blocked"}:
                raise APIError(422, exc.code, exc.message) from exc
            resolution = {
                "status": "unavailable",
                "source": {"url": source_url},
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            }
    if resolution.get("status") == "resolved":
        source = resolution.get("source") or {}
        source_repo = str(source.get("repo_id") or "").lower()
        configured_repo = str(intake.get("repo_id") or "").lower()
        if source_repo and configured_repo and source_repo != configured_repo:
            raise APIError(409, "source_repo_mismatch", "GitHub source repository does not match the intake repository", {"expected": intake.get("repo_id"), "actual": source.get("repo_id")})
        intake["resolved_refs"] = source
        intake["source_validation"] = "resolved"
        intake["status"] = "resolved"
    else:
        # Resolution failures remain pending so the caller can repair auth or
        # source visibility and retry; the explicit resolution status explains
        # why a task must not proceed.
        intake["status"] = "pending_source_resolution"
        intake["source_validation"] = "pending"
    intake["source_resolution"] = resolution
    resources.update_intake(intake)
    code = 200 if resolution.get("status") == "resolved" else 202
    return _result(request, {"intake_id": intake_id, "status": intake["status"], "source_resolution": resolution}, code)


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = _resources().get_job(job_id)
    if not job:
        raise APIError(404, "job_not_found", "job does not exist or is not visible")
    return _result(request, {"job": job})


@router.post("/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: str, request: Request):
    """Persist a cancellation request; a running worker confirms cleanup."""
    job = _resources().jobs.request_cancel(job_id)
    if not job:
        raise APIError(404, "job_not_found", "job does not exist or is not visible")
    return _result(request, {"job": job, "cancel_requested": bool(job.get("cancel_requested"))}, 202)


@router.get("/jobs/{job_id}/events")
def get_job_events(job_id: str, request: Request):
    """Replay durable job events for polling/SSE clients."""
    queue = _resources().jobs
    if not queue.get(job_id):
        raise APIError(404, "job_not_found", "job does not exist or is not visible")
    raw_after = request.headers.get("Last-Event-ID") or request.query_params.get("after", "0")
    try:
        after = max(0, int(raw_after))
    except ValueError as exc:
        raise APIError(422, "invalid_event_cursor", "event cursor must be an integer") from exc
    events = queue.events(job_id, after=after)
    # This endpoint is intentionally finite: clients reconnect with the last
    # sequence, while GET /jobs remains the authoritative state snapshot.
    def stream():
        for item in events:
            yield f"id: {item['seq']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False, separators=(',', ':'))}\n\n"
        if not events:
            yield ": heartbeat\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/tasks/{task_id}/events/stream")
def stream_task_events(task_id: str, request: Request):
    """Replay durable events for jobs belonging to a task.

    The stream is finite and reconnectable.  Resource GETs remain authoritative;
    this endpoint only provides refresh hints and historical transition facts.
    """
    store = _evidence_store()
    if not store.get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    queue = _resources().jobs
    with queue.lock:
        rows = queue.db.execute(
            "SELECT DISTINCT j.job_id FROM api_jobs j "
            "JOIN verifications v ON j.resource_id=v.verification_id "
            "JOIN candidates c ON v.candidate_id=c.candidate_id "
            "WHERE c.task_id=?", (task_id,),
        ).fetchall()
    raw_after = request.headers.get("Last-Event-ID") or request.query_params.get("after", "0")
    try:
        after = max(0, int(raw_after))
    except ValueError as exc:
        raise APIError(422, "invalid_event_cursor", "event cursor must be an integer") from exc
    events = [event for row in rows for event in queue.events(row["job_id"], after=after)]
    events.sort(key=lambda item: (item["created_at"], item["job_id"], item["seq"]))

    def stream():
        for item in events:
            yield f"id: {item['job_id']}:{item['seq']}\nevent: {item['event']}\ndata: {json.dumps(item['data'], ensure_ascii=False, separators=(',', ':'))}\n\n"
        if not events:
            yield ": heartbeat\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/tasks", status_code=201)
def create_task(body: TaskCreate, request: Request):
    scope = "POST:/api/v1/tasks"
    idem_key, prior = _claim_idempotency(request, scope, body)
    if prior and prior.get("response_ref"):
        existing = _evidence_store().get_task(prior["response_ref"])
        if existing:
            return _result(request, {"task": _task_dict(existing), "status": "draft", "idempotent_replay": True}, 201)
    resources = _resources()
    intake = resources.get_intake(body.intake_id)
    if not intake:
        raise APIError(404, "intake_not_found", "intake does not exist or is not visible")
    task_id = f"task_{uuid.uuid4().hex}"
    source_refs = tuple(intake.get("source_refs") or [])
    for key in ("pr_url", "issue_url"):
        if intake.get(key):
            source_refs += (str(intake[key]),)
    issue_snapshot = {
        "title": intake.get("issue_title") or f"Imported {intake['mode']} intake",
        "body": intake.get("issue_body") or "",
        "repo_id": intake["repo_id"],
        "base_ref": intake["base_ref"],
        "candidate_ref": intake.get("candidate_ref"),
        "intake_id": body.intake_id,
        "mode": intake["mode"],
        "resolved_refs": intake.get("resolved_refs"),
    }
    task = Task(task_id, issue_snapshot, source_refs, body.mode, 1)
    try:
        _evidence_store().save_task(task)
    except ValueError as exc:
        raise APIError(409, "task_conflict", str(exc)) from exc
    _bind_idempotency(scope, idem_key, task_id)
    return _result(request, {"task": _task_dict(task), "status": "draft"}, 201)


@router.get("/tasks/{task_id}")
def get_task(task_id: str, request: Request):
    task = _evidence_store().get_task(task_id)
    if not task:
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    store = _evidence_store()
    candidates = [item.to_dict() for item in store.list_candidates(task_id)]
    contracts = [_contract_dict(item) for item in store.list_contract_versions(task_id)]
    verifications: list[Verification] = []
    for candidate in store.list_candidates(task_id):
        verifications.extend(store.list_verifications(candidate.candidate_id))
    latest = _verification_dict(store, verifications[-1]) if verifications else None
    return _result(request, {"task": _task_dict(task), "candidates": candidates, "contracts": contracts, "latest_verification": latest})


@router.post("/tasks/{task_id}/contracts/draft", status_code=202)
def draft_contract(task_id: str, body: ContractDraftCreate, request: Request):
    store = _evidence_store()
    task = store.get_task(task_id)
    if not task:
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    # The draft is explicitly source-first.  It never receives a candidate
    # diff and does not pretend that a model has been called.
    contract_id = f"contract_{uuid.uuid4().hex}"
    conditions = tuple(_condition_from_input(item) for item in body.conditions)
    contract = ContractVersion(contract_id, task_id, 1, ContractState.DRAFT, conditions, tuple(body.source_ids))
    try:
        store.save_contract_version(contract)
    except ValueError as exc:
        raise APIError(409, "contract_conflict", str(exc)) from exc
    created = utc_now()
    resources = _resources()
    resources.put_contract_meta(contract_id, 1, {"base_snapshot_id": body.base_snapshot_id, "model_profile": body.model_profile, "source_ids": body.source_ids, "scope_exclusions": []})
    job_id = f"job_{uuid.uuid4().hex}"
    resources.put_job({"job_id": job_id, "kind": "contract_draft", "state": "completed", "resource_id": contract_id, "created_at": created, "updated_at": created})
    return _result(request, {"contract": _contract_dict(contract), "job_id": job_id, "status": "draft", "model_called": False}, 202)


@router.put("/contracts/{contract_id}")
def update_contract(contract_id: str, body: ContractUpdate, request: Request):
    store = _evidence_store()
    current = store.get_contract_version(contract_id)
    if not current:
        raise APIError(404, "contract_not_found", "contract does not exist or is not visible")
    if current.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "contract revision is stale", {"expected_revision": current.revision})
    if current.state == ContractState.FROZEN:
        raise APIError(409, "contract_frozen", "frozen contracts are immutable; create a new revision")
    contract = ContractVersion(contract_id, current.task_id, current.revision + 1, ContractState.REVIEW, tuple(_condition_from_input(item) for item in body.conditions), current.source_refs)
    try:
        store.save_contract_version(contract)
    except ValueError as exc:
        raise APIError(409, "contract_conflict", str(exc)) from exc
    return _result(request, {"contract": _contract_dict(contract), "status": "review"})


@router.post("/contracts/{contract_id}/freeze", status_code=201)
def freeze_contract(contract_id: str, body: ContractFreeze, request: Request):
    store = _evidence_store()
    current = store.get_contract_version(contract_id)
    if not current:
        raise APIError(404, "contract_not_found", "contract does not exist or is not visible")
    if current.revision != body.expected_revision:
        raise APIError(409, "revision_conflict", "contract revision is stale", {"expected_revision": current.revision})
    if current.state == ContractState.FROZEN:
        raise APIError(409, "contract_frozen", "contract is already frozen")
    by_id = {condition.condition_id: condition for condition in current.conditions}
    unknown = sorted(set(body.confirmed_condition_ids) - set(by_id))
    if unknown:
        raise APIError(422, "unknown_condition", "confirmed_condition_ids contains unknown conditions", {"ids": unknown})
    conditions = tuple(
        AcceptanceCondition(c.condition_id, c.kind, c.statement, c.source_refs, c.required, "maintainer_confirmed" if c.condition_id in body.confirmed_condition_ids else c.confirmation, c.oracle)
        for c in current.conditions
    )
    missing_sources = [c.condition_id for c in conditions if c.required and not c.source_refs]
    if missing_sources:
        raise APIError(422, "missing_condition_source", "required conditions need source references", {"condition_ids": missing_sources})
    contract = ContractVersion(contract_id, current.task_id, current.revision + 1, ContractState.FROZEN, conditions, current.source_refs)
    try:
        store.save_contract_version(contract)
    except ValueError as exc:
        raise APIError(409, "contract_conflict", str(exc)) from exc
    _resources().put_contract_meta(contract_id, contract.revision, {"scope_exclusions": body.scope_exclusions, "actor": body.actor, "confirmed_condition_ids": body.confirmed_condition_ids})
    return _result(request, {"contract": _contract_dict(contract), "scope_exclusions": body.scope_exclusions, "status": "frozen"}, 201)


@router.get("/tasks/{task_id}/contracts")
def list_contracts(task_id: str, request: Request):
    if not _evidence_store().get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    return _result(request, {"items": [_contract_dict(item) for item in _evidence_store().list_contract_versions(task_id)]})


@router.get("/tasks/{task_id}/findings")
def list_findings(task_id: str, request: Request, candidate_id: str | None = None, condition_id: str | None = None):
    store = _evidence_store()
    if not store.get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    if candidate_id:
        candidate = store.get_candidate(candidate_id)
        if not candidate or candidate.task_id != task_id:
            raise APIError(404, "candidate_not_found", "candidate does not exist for this task")
    # Findings are written only by the evidence-producing verification worker.
    # There is no worker in this phase, so the API marks collection unavailable
    # rather than silently presenting an unsupported pipeline as a clean result.
    return _result(request, {"items": [], "available": False, "complete": False, "reason": "finding execution pipeline is not implemented"})


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str, request: Request):
    raise APIError(404, "finding_not_found", "finding does not exist or has not been produced by a verification worker")


@router.get("/tasks/{task_id}/reports")
def list_reports(task_id: str, request: Request):
    if not _evidence_store().get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    items = []
    for row in _resources().list_reports(task_id):
        items.append({key: row[key] for key in ("report_id", "task_id", "verification_id", "verification_key", "format", "content_type", "content_sha", "created_at")})
    return _result(request, {"items": items})


@router.post("/verifications/{verification_id}/reports", status_code=201)
def create_report(verification_id: str, body: dict[str, Any], request: Request):
    """Render an immutable report from one persisted verification snapshot."""
    fmt = body.get("format", "markdown")
    if fmt not in {"markdown", "html"}:
        raise APIError(422, "unsupported_report_format", "format must be markdown or html")
    verification = _evidence_store().get_verification(verification_id)
    if not verification:
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    store = _evidence_store()
    candidate = store.get_candidate(verification.candidate_id)
    contract = store.get_contract_version(verification.contract_id, verification.contract_revision)
    checks = store.list_check_executions(verification_id)
    if not candidate or not contract:
        raise APIError(409, "report_snapshot_incomplete", "verification references missing immutable resources")
    task = store.get_task(candidate.task_id)
    if not task:
        raise APIError(409, "report_snapshot_incomplete", "verification task snapshot is unavailable")
    snapshot = {
        "task": task.to_dict(),
        "candidate": candidate.to_dict(),
        "contract": contract.to_dict(),
        "verification": verification.to_dict(),
        "checks": [check.to_dict() for check in checks],
        "scope_statement": "Only evidence recorded under this verification key is represented.",
        "generated_at": utc_now(),
    }
    # The report states what was actually recorded.  Queued/unrun verification
    # remains explicitly unassessed; no score, test result or recommendation is
    # synthesized from missing execution data.
    md = "\n".join([
        "# PatchPilot verification report",
        "",
        f"- Task: `{task.task_id}` — {task.issue_snapshot.get('title', 'Untitled task')}",
        f"- Repository: `{task.issue_snapshot.get('repo_id', 'unknown')}`",
        f"- Candidate: `{candidate.candidate_id}` (`{candidate.patch_hash}`)",
        f"- Contract: `{contract.contract_id}` revision {contract.revision} (`{contract.contract_hash}`)",
        f"- Verification: `{verification.verification_id}`",
        f"- Verification key: `{verification.verification_key}`",
        f"- Run state: `{verification.run_state.value}`",
        f"- Verdict: `{verification.verdict.value}`",
        f"- Validity: `{verification.validity.value}`",
        f"- Human decision: `{verification.review_decision.value}`",
        "",
        "## Recorded scope and gaps",
        "",
        snapshot["scope_statement"],
        "",
        *([f"- {gap}" for gap in verification.gaps] or ["- No gaps were recorded on the verification snapshot."]),
        "",
        "## Checks",
        "",
        *([
            f"- `{check.variant}` / `{check.target}`: **{check.outcome.value}** "
            f"({check.count} test cases, exit {check.return_code if check.return_code is not None else 'n/a'})"
            + (f" — `{ ' '.join(check.command_argv) }`" if check.command_argv else "")
            for check in checks
        ] or ["No check executions are attached to this verification snapshot."]),
        "",
        "This report is bound to the immutable verification key above. A queued or incomplete run is not a pass.",
    ])
    if fmt == "markdown":
        content, content_type = md, "text/markdown; charset=utf-8"
    else:
        import html
        content = "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>PatchPilot verification report</title><body><pre>" + html.escape(md) + "</pre></body></html>"
        content_type = "text/html; charset=utf-8"
    report_id = f"report_{uuid.uuid4().hex}"
    content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    record = {
        "report_id": report_id,
        "task_id": task.task_id,
        "verification_id": verification_id,
        "verification_key": verification.verification_key,
        "format": fmt,
        "content_type": content_type,
        "content": content,
        "content_sha": content_sha,
        "snapshot": snapshot,
        "created_at": utc_now(),
    }
    _resources().save_report(record)
    return _result(request, {"report": {key: record[key] for key in ("report_id", "task_id", "verification_id", "verification_key", "format", "content_type", "content_sha", "created_at")}}, 201)


@router.get("/reports/{report_id}")
def get_report(report_id: str, request: Request):
    report = _resources().get_report(report_id)
    if not report:
        raise APIError(404, "report_not_found", "report does not exist or is not visible")
    return _result(request, {"report": {key: report[key] for key in ("report_id", "task_id", "verification_id", "verification_key", "format", "content_type", "content_sha", "snapshot", "created_at")}})


@router.get("/reports/{report_id}/content")
def get_report_content(report_id: str, request: Request):
    report = _resources().get_report(report_id)
    if not report:
        raise APIError(404, "report_not_found", "report does not exist or is not visible")
    # Keep this JSON for the console's typed API and for clients that need to
    # render the report inline.  The digest and content type still make the
    # bytes suitable for a verified download/export step.
    return _result(request, {
        "report_id": report_id,
        "format": report["format"],
        "content_type": report["content_type"],
        "content": report["content"],
        "content_sha": report["content_sha"],
    }, 200)


@router.get("/contracts/{contract_id}")
def get_contract(contract_id: str, request: Request, revision: int | None = None):
    contract = _evidence_store().get_contract_version(contract_id, revision)
    if not contract:
        raise APIError(404, "contract_not_found", "contract does not exist or is not visible")
    return _result(request, {"contract": _contract_dict(contract)})


@router.post("/tasks/{task_id}/candidates", status_code=201)
def create_candidate(task_id: str, body: CandidateCreate, request: Request):
    store = _evidence_store()
    if not store.get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    if body.parent_candidate_id:
        parent = store.get_candidate(body.parent_candidate_id)
        if not parent or parent.task_id != task_id:
            raise APIError(422, "invalid_parent_candidate", "parent candidate is missing or belongs to another task")
    raw_content = body.patch_text if body.patch_text is not None else body.content_ref
    assert raw_content is not None
    content_bytes = raw_content.encode("utf-8")
    patch_hash = hashlib.sha256(content_bytes).hexdigest()
    # A patch-only intake has no checked-out tree yet.  This digest is therefore
    # explicitly derived from base + patch; it is not reported as a resolved
    # repository tree until the execution worker materializes the candidate.
    tree_digest = hashlib.sha256(f"patch-tree\0{body.base_sha}\0{patch_hash}".encode()).hexdigest()
    candidate_id = f"candidate_{uuid.uuid4().hex}"
    candidate = Candidate(candidate_id, task_id, body.base_sha, body.head_sha, tree_digest, patch_hash, body.author_type or body.source, body.parent_candidate_id)
    try:
        store.save_candidate(candidate)
    except ValueError as exc:
        raise APIError(409, "candidate_conflict", str(exc)) from exc
    _resources().put_candidate_content(candidate_id, body.patch_text, body.content_ref, patch_hash)
    return _result(request, {"candidate": candidate.to_dict(), "content_status": "stored"}, 201)


@router.get("/tasks/{task_id}/candidates")
def list_candidates(task_id: str, request: Request):
    if not _evidence_store().get_task(task_id):
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    return _result(request, {"items": [item.to_dict() for item in _evidence_store().list_candidates(task_id)]})


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str, request: Request):
    candidate = _evidence_store().get_candidate(candidate_id)
    if not candidate:
        raise APIError(404, "candidate_not_found", "candidate does not exist or is not visible")
    return _result(request, {"candidate": candidate.to_dict()})


@router.get("/candidates/{candidate_id}/diff")
def get_candidate_diff(candidate_id: str, request: Request):
    candidate = _evidence_store().get_candidate(candidate_id)
    if not candidate:
        raise APIError(404, "candidate_not_found", "candidate does not exist or is not visible")
    content = _resources().candidate_content(candidate_id)
    return _result(request, {"candidate_id": candidate_id, "patch_hash": candidate.patch_hash, "content": (content or {}).get("content"), "content_ref": (content or {}).get("content_ref"), "available": bool(content and content.get("content"))})


@router.post("/tasks/{task_id}/verifications", status_code=202)
def create_verification(task_id: str, body: VerificationCreate, request: Request):
    store = _evidence_store()
    task = store.get_task(task_id)
    if not task:
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    candidate = store.get_candidate(body.candidate_id)
    if not candidate or candidate.task_id != task_id:
        raise APIError(404, "candidate_not_found", "candidate does not exist for this task")
    contract = store.get_contract_version(body.contract_id)
    if not contract or contract.task_id != task_id:
        raise APIError(404, "contract_not_found", "contract does not exist for this task")
    if contract.state != ContractState.FROZEN:
        raise APIError(409, "contract_not_frozen", "verification requires a frozen contract")
    try:
        command_digest = normalized_argv_digest(body.command_argv)
    except ValueError as exc:
        raise APIError(422, "invalid_command_argv", str(exc)) from exc
    key = compute_verification_key(VerificationKeyInputs(
        base_tree=candidate.base_sha,
        candidate_tree=candidate.tree_digest,
        contract=contract.contract_hash,
        suite=body.suite_id,
        baseline_tests=body.baseline_tests_hash,
        environment=body.environment_id,
        commands=command_digest,
        policy=body.policy_id,
        verifier=body.verifier_revision,
        probe_plan=body.probe_plan_hash,
        seed=body.seed,
    ))
    gaps = ("verification_not_started", "baseline_tests_unavailable") if body.baseline_tests_hash == "unavailable" else ("verification_not_started",)
    verification = Verification(f"verification_{uuid.uuid4().hex}", key, candidate.candidate_id, contract.contract_id, RunState.QUEUED, Verdict.NOT_EVALUATED, contract.revision, Validity.CURRENT, ReviewDecision.PENDING, gaps, ())
    try:
        store.save_verification(verification)
    except ValueError as exc:
        raise APIError(409, "verification_conflict", str(exc)) from exc
    created = utc_now()
    job_id = f"job_{uuid.uuid4().hex}"
    _resources().put_job({"job_id": job_id, "kind": "verification", "state": "queued", "resource_id": verification.verification_id, "command_argv": body.command_argv, "suite_id": body.suite_id, "created_at": created, "updated_at": created})
    return _result(request, {"verification": verification.to_dict(), "verification_key": key, "job_id": job_id, "status": "queued"}, 202)


@router.get("/verifications/{verification_id}")
def get_verification(verification_id: str, request: Request):
    verification = _evidence_store().get_verification(verification_id)
    if not verification:
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    return _result(request, {"verification": _verification_dict(_evidence_store(), verification), "verification_key": verification.verification_key})


@router.post("/verifications/{verification_id}/cancel", status_code=202)
def cancel_verification(verification_id: str, request: Request):
    """Request cancellation of the durable verification job.

    The verification snapshot remains immutable.  A running job is only
    marked ``cancelled`` after its worker observes the request and performs
    cleanup; callers can follow the job event stream for that transition.
    """
    store = _evidence_store()
    if not store.get_verification(verification_id):
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    queue = _resources().jobs
    with queue.lock:
        row = queue.db.execute(
            "SELECT job_id FROM api_jobs WHERE resource_id=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
            (verification_id,),
        ).fetchone()
    if not row:
        raise APIError(409, "verification_job_not_found", "verification has no durable job to cancel")
    job = queue.request_cancel(row["job_id"])
    if not job:
        raise APIError(404, "job_not_found", "job does not exist or is not visible")
    return _result(request, {"verification_id": verification_id, "job": job, "cancel_requested": bool(job.get("cancel_requested"))}, 202)


@router.get("/verifications/{verification_id}/decisions")
def get_decisions(verification_id: str, request: Request):
    store = _evidence_store()
    if not store.get_verification(verification_id):
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    return _result(request, {"items": [item.to_dict() for item in store.list_review_decisions(verification_id)]})


@router.get("/verifications/{verification_id}/key")
def get_verification_key(verification_id: str, request: Request):
    verification = _evidence_store().get_verification(verification_id)
    if not verification:
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    return _result(request, {"verification_id": verification_id, "verification_key": verification.verification_key, "validity": verification.validity.value})


@router.get("/verifications/{verification_id}/checks")
def get_verification_checks(verification_id: str, request: Request):
    store = _evidence_store()
    verification = store.get_verification(verification_id)
    if not verification:
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    items = [item.to_dict() for item in store.list_check_executions(verification_id)]
    incomplete = any(item["outcome"] != "pass" for item in items)
    return _result(request, {
        "verification_id": verification_id,
        "items": items,
        "complete": bool(items) and not incomplete and not verification.gaps,
        "required_gaps": list(verification.gaps) or (["not_run"] if not items else []),
    })


@router.post("/verifications/{verification_id}/execute", status_code=201)
def execute_verification(verification_id: str, body: VerificationExecute, request: Request):
    """Run a persisted candidate in an explicitly allowlisted workspace.

    The queued verification remains immutable.  Execution appends a new
    verification snapshot and check records, then returns the new snapshot.
    """
    store = _evidence_store()
    if not store.get_verification(verification_id):
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    queue = _resources().jobs
    with queue.lock:
        row = queue.db.execute(
            "SELECT job_id FROM api_jobs WHERE resource_id=? ORDER BY created_at DESC,job_id DESC LIMIT 1",
            (verification_id,),
        ).fetchone()
    if not row:
        raise APIError(409, "verification_job_not_found", "verification has no durable job to execute")
    worker_id = f"api-execute:{uuid.uuid4().hex}"
    leased = queue.claim(worker_id, job_id=row["job_id"], lease_seconds=900)
    if not leased:
        raise APIError(409, "verification_job_unavailable", "verification job is already running or unavailable")
    if leased.get("state") == "cancelled":
        raise APIError(409, "verification_execution_cancelled", "verification job was cancelled before execution")
    execution_commands = body.command_argv if body.command_argv is not None else leased.get("command_argv")
    execution_suite = body.suite_id or leased.get("suite_id")
    try:
        result = VerificationService(store).execute(
            verification_id,
            repo_id=body.repo_id,
            repo_path=body.repo_path,
            command_argv=execution_commands,
            suite_id=execution_suite,
        )
    except WorkspaceResolutionError as exc:
        queue.complete(row["job_id"], worker_id, leased["lease_token"], state="error", payload={"error": str(exc)})
        raise APIError(422, "workspace_not_allowed", str(exc)) from exc
    except VerificationExecutionError as exc:
        queue.complete(row["job_id"], worker_id, leased["lease_token"], state="error", payload={"error": str(exc)})
        raise APIError(409, "verification_execution_error", str(exc)) from exc
    except ValueError as exc:
        queue.complete(row["job_id"], worker_id, leased["lease_token"], state="error", payload={"error": str(exc)})
        raise APIError(422, "invalid_execution_request", str(exc)) from exc
    except Exception as exc:
        # Keep the durable job from being left in running after an unexpected
        # adapter/runtime failure.  Do not expose exception details to clients.
        try:
            queue.complete(row["job_id"], worker_id, leased["lease_token"], state="error", payload={"error": "worker_failure"})
        except Exception:
            pass
        raise APIError(500, "verification_worker_error", "verification execution failed") from exc
    # A verification snapshot is created even when infrastructure setup fails,
    # so the HTTP resource still returns 201.  Surface the measured run state
    # instead of claiming that an execution completed when the worker recorded
    # an explicit error snapshot.
    execution_status = result.get("verification", {}).get("run_state", "completed")
    queue_state = "completed" if execution_status == RunState.COMPLETED.value else "error"
    queue.complete(row["job_id"], worker_id, leased["lease_token"], state=queue_state, payload={
        "result_verification_id": result.get("verification", {}).get("verification_id"),
        "execution_id": result.get("execution_id"),
        "execution_status": execution_status,
    })
    return _result(request, {**result, "status": execution_status}, 201)


@router.post("/verifications/{verification_id}/decisions")
def record_decision(verification_id: str, body: DecisionCreate, request: Request):
    store = _evidence_store()
    verification = store.get_verification(verification_id)
    if not verification:
        raise APIError(404, "verification_not_found", "verification does not exist or is not visible")
    record = ReviewDecisionRecord(f"decision_{uuid.uuid4().hex}", verification_id, body.expected_key, ReviewDecision(body.decision), body.actor, body.reason)
    try:
        store.record_review_decision(record)
    except ValueError as exc:
        code = "verification_key_mismatch" if "expected_key" in str(exc) else "decision_conflict"
        raise APIError(409, code, str(exc)) from exc
    return _result(request, {"decision": record.to_dict()}, 201)


def install_api_v1(app: FastAPI) -> None:
    """Install v1 exception/request-id handling on an application instance."""

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID")
        if not request_id or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
            request_id = f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(APIError)
    async def _api_error_handler(request: Request, exc: APIError):
        return _error_payload(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        # Preserve FastAPI's useful field locations, but expose one stable
        # envelope for v1 callers and avoid leaking host paths in messages.
        if not request.url.path.startswith("/api/v1"):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        details = [{"loc": list(item.get("loc", ())), "type": item.get("type", "value_error"), "msg": str(item.get("msg", "invalid value"))} for item in exc.errors()]
        return JSONResponse({"error": {"code": "validation_error", "message": "request validation failed", "details": details}, "request_id": _rid(request)}, status_code=422, headers={"X-Request-ID": _rid(request)})
