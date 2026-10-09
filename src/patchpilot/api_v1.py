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
import hmac
import ipaddress
import json
import os
import platform
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from .domain.entities import (
    AcceptanceCondition,
    Candidate,
    CheckOutcome,
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
from .domain.entities import CheckExecution
from .verifier.diff import parse_unified_diff
from .integrations.github_source import GitHubSourceResolver, SourceResolutionError


router = APIRouter(prefix="/api/v1", tags=["v1"])
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_PATCH_BYTES = 2 * 1024 * 1024
_MAX_PUBLICATION_BODY_BYTES = 512 * 1024
_PUBLICATION_PREVIEW_TTL_SECONDS = 10 * 60
_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_DEFAULT_WORKSPACE = "local"


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
    model_profile: Literal["manual", "remote"] = "manual"
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


class FindingReproduceCreate(APIModel):
    """Bounded re-execution of one persisted finding's verification evidence."""

    repeats: int = Field(default=3, ge=1, le=3)
    repo_id: str | None = None
    repo_path: str | None = None
    command_argv: list[list[str]] | None = None
    suite_id: str | None = None

    @field_validator("repo_id", "suite_id")
    @classmethod
    def validate_reproduce_ids(cls, value: str | None, info) -> str | None:
        return _valid_id(value, info.field_name) if value is not None else None

    @field_validator("repo_path")
    @classmethod
    def validate_reproduce_path(cls, value: str | None) -> str | None:
        if value is not None and ("\x00" in value or not value.strip()):
            raise ValueError("repo_path must be a non-empty path without NUL bytes")
        return value


class RepairCreate(APIModel):
    """Explicitly approved, bounded candidate repair request.

    Context files are supplied by the caller as read-only model input.  The
    repair provider has no write access to the contract, suite, or workspace.
    """

    parent_candidate_id: str
    finding_ids: list[str] = Field(min_length=1, max_length=16)
    max_budget: int = Field(ge=1, le=3)
    approved: bool = False
    actor: str = Field(min_length=1, max_length=200)
    provider: Literal["remote"] = "remote"
    repo_files: dict[str, str] = Field(default_factory=dict)

    @field_validator("parent_candidate_id")
    @classmethod
    def validate_parent_candidate(cls, value: str) -> str:
        return _valid_id(value, "parent_candidate_id")

    @field_validator("finding_ids")
    @classmethod
    def validate_finding_ids(cls, value: list[str]) -> list[str]:
        result = [_valid_id(item, "finding_id") for item in value]
        return list(dict.fromkeys(result))

    @field_validator("repo_files")
    @classmethod
    def validate_repo_files(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32:
            raise ValueError("repo_files may contain at most 32 entries")
        normalized: dict[str, str] = {}
        for path, content in value.items():
            if not isinstance(path, str) or not path or "\x00" in path:
                raise ValueError("repo_files contains an invalid path")
            parts = path.replace("\\", "/").split("/")
            if path.startswith(("/", "\\")) or ".." in parts:
                raise ValueError("repo_files paths must stay inside the workspace")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 50_000:
                raise ValueError("repo_files entries must be UTF-8 text under 50 KiB")
            normalized[path] = content
        return normalized


class DecisionCreate(APIModel):
    decision: Literal["accepted", "rejected", "exception_accepted"]
    reason: str = ""
    expected_key: str
    actor: str = "maintainer"


class PublishPreviewCreate(APIModel):
    """Inputs for the side-effect preview shown before a GitHub write.

    ``repo_id``/``pr_number``/``head_sha`` are accepted as ergonomic aliases
    for clients that use the same names as intake/candidate resources.  The
    normalized response always uses ``target_repo``, ``target_pr`` and
    ``expected_head``.
    """

    target_repo: str | None = None
    repo_id: str | None = None
    target_pr: int | None = Field(default=None, ge=1)
    pr_number: int | None = Field(default=None, ge=1)
    expected_head: str | None = None
    head_sha: str | None = None
    publication_type: Literal["comment"] = "comment"

    @model_validator(mode="after")
    def normalize_target(self):
        repo = self.target_repo or self.repo_id
        number = self.target_pr or self.pr_number
        head = self.expected_head or self.head_sha
        if not repo:
            raise ValueError("target_repo is required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}", repo):
            raise ValueError("target_repo must be owner/name")
        if number is None:
            raise ValueError("target_pr is required")
        if not head or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", head):
            raise ValueError("expected_head must be a non-empty commit identifier")
        self.target_repo = repo
        self.target_pr = number
        self.expected_head = head
        return self


class PublishCreate(APIModel):
    preview_id: str
    expected_head: str | None = None
    head_sha: str | None = None
    confirmation_digest: str

    @field_validator("preview_id")
    @classmethod
    def validate_preview_id(cls, value: str) -> str:
        return _valid_id(value, "preview_id")

    @field_validator("confirmation_digest")
    @classmethod
    def validate_confirmation_digest(cls, value: str) -> str:
        if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
            raise ValueError("confirmation_digest must be a SHA-256 digest")
        return value.lower()

    @model_validator(mode="after")
    def normalize_head(self):
        head = self.expected_head or self.head_sha
        if not head or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", head):
            raise ValueError("expected_head is required")
        self.expected_head = head
        return self


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
                CREATE TABLE IF NOT EXISTS api_publication_previews(
                  preview_id TEXT PRIMARY KEY, report_id TEXT NOT NULL,
                  target_repo TEXT NOT NULL, target_pr INTEGER NOT NULL,
                  expected_head TEXT NOT NULL, publication_type TEXT NOT NULL,
                  body TEXT NOT NULL, body_digest TEXT NOT NULL,
                  confirmation_digest TEXT NOT NULL, current_head TEXT NOT NULL,
                  expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                  status TEXT NOT NULL, target_snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_publications(
                  publication_id TEXT PRIMARY KEY, preview_id TEXT NOT NULL UNIQUE,
                  report_id TEXT NOT NULL, target_repo TEXT NOT NULL,
                  target_pr INTEGER NOT NULL, expected_head TEXT NOT NULL,
                  body_digest TEXT NOT NULL, status TEXT NOT NULL,
                  external_id TEXT, external_url TEXT, response TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS api_resource_acl(
                  resource_kind TEXT NOT NULL,
                  resource_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(resource_kind, resource_id));
                CREATE INDEX IF NOT EXISTS ix_api_resource_acl_workspace
                  ON api_resource_acl(workspace_id, resource_kind, resource_id);
                CREATE TRIGGER IF NOT EXISTS immutable_api_reports_update
                  BEFORE UPDATE ON api_reports BEGIN SELECT RAISE(ABORT, 'immutable report'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_api_reports_delete
                  BEFORE DELETE ON api_reports BEGIN SELECT RAISE(ABORT, 'immutable report'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_api_publication_previews_update
                  BEFORE UPDATE ON api_publication_previews BEGIN SELECT RAISE(ABORT, 'immutable publication preview'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_api_publication_previews_delete
                  BEFORE DELETE ON api_publication_previews BEGIN SELECT RAISE(ABORT, 'immutable publication preview'); END;
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

    def save_publication_preview(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO api_publication_previews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["preview_id"], payload["report_id"], payload["target_repo"],
                    payload["target_pr"], payload["expected_head"], payload["publication_type"],
                    payload["body"], payload["body_digest"], payload["confirmation_digest"],
                    payload["current_head"], payload["expires_at"], payload["created_at"],
                    payload["status"], self._json(payload["target_snapshot"]),
                ),
            )
            self.db.commit()

    def get_publication_preview(self, preview_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM api_publication_previews WHERE preview_id=?", (preview_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["target_snapshot"] = json.loads(data["target_snapshot"])
        return data

    def save_publication(self, payload: dict[str, Any]) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO api_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["publication_id"], payload["preview_id"], payload["report_id"],
                    payload["target_repo"], payload["target_pr"], payload["expected_head"],
                    payload["body_digest"], payload["status"], payload.get("external_id"),
                    payload.get("external_url"), self._json(payload.get("response") or {}), payload["created_at"],
                ),
            )
            self.db.commit()

    def get_publication(self, preview_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM api_publications WHERE preview_id=?", (preview_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["response"] = json.loads(data["response"])
        return data

    def get_publication_by_id(self, publication_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM api_publications WHERE publication_id=?", (publication_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["response"] = json.loads(data["response"])
        return data

    def bind_acl(self, resource_kind: str, resource_id: str, workspace_id: str) -> None:
        """Bind an immutable API resource to one workspace namespace."""
        if not _WORKSPACE_RE.fullmatch(workspace_id):
            raise ValueError("workspace_id contains unsafe characters")
        with self.lock:
            row = self.db.execute(
                "SELECT workspace_id FROM api_resource_acl WHERE resource_kind=? AND resource_id=?",
                (resource_kind, resource_id),
            ).fetchone()
            if row:
                if row["workspace_id"] != workspace_id:
                    raise ValueError("resource is already bound to another workspace")
                return
            self.db.execute(
                "INSERT INTO api_resource_acl(resource_kind,resource_id,workspace_id,created_at) VALUES (?,?,?,?)",
                (resource_kind, resource_id, workspace_id, utc_now()),
            )
            self.db.commit()

    def acl_workspace(self, resource_kind: str, resource_id: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT workspace_id FROM api_resource_acl WHERE resource_kind=? AND resource_id=?",
                (resource_kind, resource_id),
            ).fetchone()
        return str(row["workspace_id"]) if row else None

    def list_acl_resources(self, workspace_id: str, resource_kind: str | None = None) -> list[str]:
        query = "SELECT resource_id FROM api_resource_acl WHERE workspace_id=?"
        params: list[str] = [workspace_id]
        if resource_kind:
            query += " AND resource_kind=?"
            params.append(resource_kind)
        query += " ORDER BY resource_id"
        with self.lock:
            return [str(row["resource_id"]) for row in self.db.execute(query, params).fetchall()]


def _evidence_store():
    # Delayed import prevents the existing app module's store construction from
    # becoming a circular import and lets tests reload the API with a temp root.
    from . import api as legacy_api
    return legacy_api.store


def _resources() -> _ResourceStore:
    return _ResourceStore(_evidence_store())


def _acl_enabled() -> bool:
    return os.getenv("PATCHPILOT_ACL_REQUIRED", "0") == "1"


def _workspace_for_request(request: Request) -> str:
    workspace = request.headers.get("X-PatchPilot-Workspace") or os.getenv("PATCHPILOT_WORKSPACE_ID") or _DEFAULT_WORKSPACE
    if not _WORKSPACE_RE.fullmatch(workspace):
        raise APIError(422, "invalid_workspace", "X-PatchPilot-Workspace has an invalid format")
    if _acl_enabled() and not request.headers.get("X-PatchPilot-Workspace") and not os.getenv("PATCHPILOT_WORKSPACE_ID"):
        raise APIError(401, "workspace_required", "X-PatchPilot-Workspace is required")
    return workspace


def _acl_resource_from_path(path: str) -> tuple[str, str] | None:
    """Extract an explicit resource id for middleware-level ACL checks."""
    parts = [part for part in path.split("/") if part]
    if len(parts) < 4 or parts[:2] != ["api", "v1"]:
        return None
    mapping = {
        "tasks": "task", "intakes": "intake", "jobs": "job", "candidates": "candidate",
        "contracts": "contract", "verifications": "verification", "findings": "finding",
        "reports": "report", "publications": "publication",
    }
    kind = mapping.get(parts[2])
    if kind:
        return kind, parts[3]
    return None


def _acl_allows(request: Request, resource_kind: str, resource_id: str) -> bool:
    workspace = _workspace_for_request(request)
    owner = _resources().acl_workspace(resource_kind, resource_id)
    # Existing databases predate ACL metadata.  They remain visible in the
    # compatibility mode, while an explicitly enabled ACL deployment fails
    # closed instead of guessing an owner for historical records.
    if owner is None:
        return not _acl_enabled()
    return hmac.compare_digest(owner, workspace)


def _acl_denial(request: Request, code: str = "resource_not_visible") -> JSONResponse:
    status = 401 if code == "workspace_required" else 404
    return JSONResponse(
        {"error": {"code": code, "message": "resource does not exist or is not visible"}, "request_id": _rid(request)},
        status_code=status,
        headers={"X-Request-ID": _rid(request)},
    )


def _bind_acl(request: Request, resources: _ResourceStore, resource_kind: str, resource_id: str) -> str:
    """Bind one newly-created resource and return its workspace namespace."""
    workspace = _workspace_for_request(request)
    try:
        resources.bind_acl(resource_kind, resource_id, workspace)
    except ValueError as exc:
        raise APIError(409, "acl_binding_conflict", str(exc)) from exc
    return workspace


def _github_source_resolver() -> GitHubSourceResolver:
    """Build the read-only resolver at request time for safe config reloads.

    Tests and embedding applications may replace this factory with an injected
    resolver.  The default resolver reads ``GITHUB_TOKEN`` but never returns it.
    """
    return GitHubSourceResolver(allow_write=os.getenv("PATCHPILOT_GITHUB_ALLOW_WRITE", "0") == "1")


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
    if _acl_enabled():
        workspace = _workspace_for_request(request)
        visible = set(_resources().list_acl_resources(workspace, "task"))
        items = [item for item in items if item["task_id"] in visible]
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
    if _acl_enabled():
        workspace = _workspace_for_request(request)
        visible = set(_resources().list_acl_resources(workspace, "task"))
        tasks = [task for task in tasks if task.task_id in visible]
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
    workspace = _bind_acl(request, resources, "intake", intake_id)
    job_id = f"job_{uuid.uuid4().hex}"
    resources.put_job({"job_id": job_id, "kind": "intake", "state": "queued", "resource_id": intake_id, "created_at": created, "updated_at": created})
    resources.bind_acl("job", job_id, workspace)
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
    workspace = _workspace_for_request(request)
    intake_workspace = resources.acl_workspace("intake", body.intake_id)
    if intake_workspace and not hmac.compare_digest(intake_workspace, workspace):
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
    _bind_acl(request, resources, "task", task_id)
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
    # The draft is explicitly source-first.  A remote planner, when selected,
    # receives only the immutable task/source snapshot and is validated below;
    # it never receives a candidate diff and never freezes acceptance criteria.
    contract_id = f"contract_{uuid.uuid4().hex}"
    planned_inputs = list(body.conditions)
    model_called = False
    model_status = "not_requested"
    model_error = None
    ambiguities: list[str] = []
    fallback = None
    if body.model_profile == "remote":
        model_called = True
        try:
            from .adapters.remote import RemoteModel

            issue = task.issue_snapshot
            planned = RemoteModel().plan_contract(
                task_id=task.task_id,
                issue_title=str(issue.get("title", "")),
                issue_body=str(issue.get("body", "")),
                source_ids=list(body.source_ids),
                base_snapshot_id=body.base_snapshot_id,
            )
            raw_conditions = planned.get("conditions", [])
            ambiguities = list(planned.get("ambiguities", []))
            if not raw_conditions:
                raise ValueError("remote planner returned no acceptance conditions")
            parsed: list[ConditionInput] = []
            source_set = set(body.source_ids)
            for raw_condition in raw_conditions:
                condition = ConditionInput.model_validate(raw_condition)
                unknown_sources = sorted(set(condition.source_refs) - source_set)
                if unknown_sources:
                    raise ValueError("remote planner referenced an unknown source")
                parsed.append(condition)
            planned_inputs = parsed
            model_status = "awaiting_input" if ambiguities else "completed"
        except Exception as exc:
            # A model outage/protocol error is explicit and recoverable: the
            # maintainer can fill the draft manually, but we never synthesize
            # a successful contract or hide the provider failure.
            model_status = "error"
            fallback = "manual_review"
            planned_inputs = list(body.conditions)
            model_error = f"{type(exc).__name__}: {str(exc)[:240]}"
    conditions = tuple(_condition_from_input(item) for item in planned_inputs)
    contract = ContractVersion(contract_id, task_id, 1, ContractState.DRAFT, conditions, tuple(body.source_ids))
    try:
        store.save_contract_version(contract)
    except ValueError as exc:
        raise APIError(409, "contract_conflict", str(exc)) from exc
    created = utc_now()
    resources = _resources()
    resources.put_contract_meta(contract_id, 1, {
        "base_snapshot_id": body.base_snapshot_id,
        "model_profile": body.model_profile,
        "source_ids": body.source_ids,
        "scope_exclusions": [],
        "model_status": model_status,
        "ambiguities": ambiguities,
        **({"fallback": fallback} if fallback else {}),
        **({"model_error": model_error} if model_error else {}),
    })
    workspace = _bind_acl(request, resources, "contract", contract_id)
    job_id = f"job_{uuid.uuid4().hex}"
    job_state = "awaiting_input" if model_status in {"error", "awaiting_input"} else "completed"
    resources.put_job({
        "job_id": job_id, "kind": "contract_draft", "state": job_state,
        "resource_id": contract_id, "created_at": created, "updated_at": created,
        "model_profile": body.model_profile, "model_status": model_status,
        "ambiguities": ambiguities,
        **({"model_error": model_error} if model_error else {}),
    })
    resources.bind_acl("job", job_id, workspace)
    response = {
        "contract": _contract_dict(contract), "job_id": job_id, "status": "draft",
        "model_called": model_called, "model_status": model_status,
        "ambiguities": ambiguities,
    }
    if fallback:
        response["fallback"] = fallback
    if model_error:
        response["model_error"] = model_error
    return _result(request, response, 202)


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
    # The projection is rebuilt from immutable verifications/checks on every
    # read.  Derivation is idempotent and therefore also repairs a database
    # created before the findings table was introduced, without inventing a
    # result or turning an empty queue into a successful run.
    store.ensure_findings_for_task(task_id)
    items = store.list_findings(task_id, candidate_id=candidate_id, condition_id=condition_id)
    candidates = store.list_candidates(task_id)
    verifications = [verification for candidate in candidates for verification in store.list_verifications(candidate.candidate_id)]
    check_count = sum(len(store.list_check_executions(verification.verification_id)) for verification in verifications)
    complete = bool(verifications and check_count)
    reason = None
    if not verifications:
        reason = "no verification has produced evidence yet"
    elif not check_count:
        reason = "verification is queued but has no persisted check execution yet"
    return _result(request, {
        "items": [item.to_dict() for item in items],
        "available": True,
        "complete": complete,
        **({"reason": reason} if reason else {}),
    })


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str, request: Request):
    finding = _evidence_store().get_finding(finding_id)
    if not finding:
        raise APIError(404, "finding_not_found", "finding does not exist or is not visible")
    return _result(request, {"finding": finding.to_dict()})


@router.post("/findings/{finding_id}/reproduce", status_code=202)
def reproduce_finding(finding_id: str, body: FindingReproduceCreate, request: Request):
    """Repeat one finding under the same immutable candidate/contract.

    Every repetition creates a new verification snapshot.  If observations
    differ, a separate flaky summary snapshot is appended with a ``flaky``
    check; no passing repetition is selected as the answer.
    """
    store = _evidence_store()
    finding = store.get_finding(finding_id)
    if not finding:
        raise APIError(404, "finding_not_found", "finding does not exist or is not visible")
    source = store.get_verification(finding.verification_id)
    candidate = store.get_candidate(finding.candidate_id)
    contract = store.get_contract_version(finding.contract_id, finding.contract_revision)
    if not source or not candidate or not contract:
        raise APIError(409, "finding_snapshot_incomplete", "finding references unavailable immutable resources")
    if source.candidate_id != finding.candidate_id or source.contract_id != finding.contract_id:
        raise APIError(409, "finding_snapshot_mismatch", "finding references do not match its verification")

    related_checks = {
        check.check_id: check
        for check in store.list_check_executions(finding.verification_id)
    }
    source_checks = [related_checks[item] for item in finding.check_ids if item in related_checks]
    if body.command_argv is None:
        argv = [list(item.command_argv) for item in source_checks if item.command_argv]
        command_argv = argv or [["pytest", "-q"]]
    else:
        command_argv = body.command_argv
    suite_id = body.suite_id or (source_checks[0].suite_id if source_checks else "reproduce-suite")

    created = utc_now()
    job_id = f"job_{uuid.uuid4().hex}"
    resources = _resources()
    workspace = resources.acl_workspace("finding", finding_id) or _workspace_for_request(request)
    queue = resources.jobs
    queue.create({
        "job_id": job_id, "kind": "finding_reproduce", "state": "queued",
        "resource_id": finding.verification_id, "created_at": created, "updated_at": created,
        "finding_id": finding_id, "repeats": body.repeats,
    })
    resources.bind_acl("job", job_id, workspace)
    worker_id = f"api-reproduce:{uuid.uuid4().hex}"
    leased = queue.claim(worker_id, job_id=job_id, lease_seconds=900)
    if not leased:
        raise APIError(409, "reproduction_job_unavailable", "reproduction job could not be claimed")
    repetitions: list[dict[str, Any]] = []
    try:
        service = VerificationService(store)
        for _ in range(body.repeats):
            repetitions.append(service.execute(
                finding.verification_id,
                repo_id=body.repo_id,
                repo_path=body.repo_path,
                command_argv=command_argv,
                suite_id=suite_id,
                cancel_checker=lambda: queue.is_cancel_requested(job_id, worker_id, leased["lease_token"]),
            ))
    except WorkspaceResolutionError as exc:
        queue.complete(job_id, worker_id, leased["lease_token"], state="error", payload={"error": str(exc)})
        raise APIError(422, "workspace_not_allowed", str(exc)) from exc
    except VerificationExecutionError as exc:
        queue.complete(job_id, worker_id, leased["lease_token"], state="error", payload={"error": str(exc)})
        raise APIError(409, "reproduction_execution_error", str(exc)) from exc
    except Exception as exc:
        try:
            queue.complete(job_id, worker_id, leased["lease_token"], state="error", payload={"error": "worker_failure"})
        except Exception:
            pass
        raise APIError(500, "reproduction_worker_error", "finding reproduction failed") from exc

    def signature(item: dict[str, Any]) -> tuple[tuple[str, str, str, int], ...]:
        return tuple(sorted(
            (str(check.get("variant")), str(check.get("target")), str(check.get("outcome")), int(check.get("count", 0)))
            for check in item.get("checks", [])
        ))

    signatures = [signature(item) for item in repetitions]
    flaky = len(set(signatures)) > 1
    final_result = repetitions[-1]
    if flaky:
        summary_id = f"verification_{uuid.uuid4().hex}"
        summary = Verification(
            summary_id, source.verification_key, source.candidate_id, source.contract_id,
            RunState.COMPLETED, Verdict.INCONCLUSIVE, source.contract_revision,
            source.validity, ReviewDecision.PENDING, ("flaky_repeats",), (),
        )
        store.save_verification(summary)
        summary_check = CheckExecution(
            f"check_{uuid.uuid4().hex}", summary_id, suite_id, "reproduce", "candidate",
            CheckOutcome.FLAKY, 0, tuple(command_argv[0]) if command_argv else (),
            details={
                "reason": "repeat outcomes differ",
                "repeat_verification_ids": [item["verification"]["verification_id"] for item in repetitions],
                "observed_signatures": [list(item) for item in signatures],
            },
        )
        store.save_check_execution(summary_check)
        findings = store.derive_findings_for_verification(summary_id)
        resources.bind_acl("verification", summary_id, workspace)
        for item in findings:
            resources.bind_acl("finding", item.finding_id, workspace)
        final_result = {
            "execution_id": f"reproduction_{uuid.uuid4().hex}",
            "source_verification_id": finding.verification_id,
            "verification": summary.to_dict(),
            "checks": [summary_check.to_dict()],
            "findings": [item.to_dict() for item in findings],
        }

    # Each repetition is a new immutable verification snapshot.  Keep all
    # snapshots and derived findings in the same workspace as the source.
    for repetition in repetitions:
        repetition_id = repetition.get("verification", {}).get("verification_id")
        if repetition_id:
            resources.bind_acl("verification", str(repetition_id), workspace)
        for item in repetition.get("findings", []):
            if item.get("finding_id"):
                resources.bind_acl("finding", str(item["finding_id"]), workspace)

    completed = queue.complete(job_id, worker_id, leased["lease_token"], state="completed", payload={
        "result_verification_id": final_result["verification"]["verification_id"],
        "repetitions": len(repetitions), "flaky": flaky,
    })
    return _result(request, {
        "status": "completed", "job_id": job_id, "finding_id": finding_id,
        "repetitions": [item["verification"] for item in repetitions],
        "flaky": flaky, "result": final_result, "job": completed,
    }, 202)


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
    resources = _resources()
    resources.save_report(record)
    _bind_acl(request, resources, "report", report_id)
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


def _publication_error(exc: SourceResolutionError) -> APIError:
    """Map GitHub transport errors to stable, secret-free API errors."""
    if exc.code in {"invalid_publication_target", "invalid_source_url"}:
        return APIError(422, exc.code, exc.message)
    if exc.code == "stale_head":
        return APIError(409, "stale_head", exc.message)
    if exc.code in {"github_forbidden", "github_write_forbidden", "github_auth_not_configured", "github_write_disabled"}:
        return APIError(403, exc.code, exc.message)
    if exc.code in {"github_not_found"}:
        return APIError(404, exc.code, exc.message)
    if exc.code in {"github_unavailable", "source_unavailable", "github_permission_unavailable"}:
        return APIError(503, exc.code, exc.message, {"retryable": exc.retryable})
    return APIError(502, exc.code, exc.message, {"retryable": exc.retryable})


def _publication_confirmation_digest(*, preview_id: str, report_id: str, target_repo: str, target_pr: int, expected_head: str, body_digest: str) -> str:
    value = {
        "preview_id": preview_id,
        "report_id": report_id,
        "target_repo": target_repo,
        "target_pr": target_pr,
        "expected_head": expected_head,
        "body_digest": body_digest,
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _publication_public_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "publication_id": row["publication_id"],
        "preview_id": row["preview_id"],
        "report_id": row["report_id"],
        "target_repo": row["target_repo"],
        "target_pr": row["target_pr"],
        "expected_head": row["expected_head"],
        "body_digest": row["body_digest"],
        "status": row["status"],
        "external_id": row.get("external_id"),
        "external_url": row.get("external_url"),
        "auto_merge": False,
        "created_at": row["created_at"],
    }


@router.post("/reports/{report_id}/publish-preview")
def create_publication_preview(report_id: str, body: PublishPreviewCreate, request: Request):
    """Create a short-lived, side-effect-free GitHub publication preview.

    The target PR head is read immediately and must equal the caller's
    expected head.  The resulting digest binds the exact report body and
    target; a later publish request cannot substitute either value.
    """
    report = _resources().get_report(report_id)
    if not report:
        raise APIError(404, "report_not_found", "report does not exist or is not visible")
    snapshot_verification = (report.get("snapshot") or {}).get("verification") or {}
    if snapshot_verification.get("validity") == Validity.STALE.value:
        raise APIError(409, "report_stale", "stale reports cannot be published; create a new verification")
    report_body = str(report.get("content") or "")
    if not report_body:
        raise APIError(409, "report_content_unavailable", "report has no publishable content")
    if len(report_body.encode("utf-8")) > _MAX_PUBLICATION_BODY_BYTES:
        raise APIError(413, "publication_body_too_large", "report content exceeds the GitHub publication limit")
    resolver = _github_source_resolver()
    try:
        target = resolver.current_pull_request(body.target_repo or "", body.target_pr or 0)
    except SourceResolutionError as exc:
        raise _publication_error(exc) from exc
    if target["head_sha"] != body.expected_head:
        raise APIError(409, "stale_head", "GitHub pull request head does not match the requested preview", {"expected_head": body.expected_head, "current_head": target["head_sha"]})
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=_PUBLICATION_PREVIEW_TTL_SECONDS)
    body_digest = hashlib.sha256(report_body.encode("utf-8")).hexdigest()
    preview_id = f"preview_{uuid.uuid4().hex}"
    confirmation_digest = _publication_confirmation_digest(
        preview_id=preview_id,
        report_id=report_id,
        target_repo=body.target_repo or "",
        target_pr=body.target_pr or 0,
        expected_head=body.expected_head or "",
        body_digest=body_digest,
    )
    created_at = now.isoformat().replace("+00:00", "Z")
    expires = expires_at.isoformat().replace("+00:00", "Z")
    preview = {
        "preview_id": preview_id,
        "report_id": report_id,
        "target_repo": body.target_repo,
        "target_pr": body.target_pr,
        "expected_head": body.expected_head,
        "publication_type": body.publication_type,
        "body": report_body,
        "body_digest": body_digest,
        "confirmation_digest": confirmation_digest,
        "current_head": target["head_sha"],
        "expires_at": expires,
        "created_at": created_at,
        "status": "previewed",
        "target_snapshot": target,
    }
    resources = _resources()
    resources.save_publication_preview(preview)
    workspace = resources.acl_workspace("report", report_id) or _workspace_for_request(request)
    resources.bind_acl("preview", preview_id, workspace)
    return _result(request, {
        "preview": {
            "preview_id": preview_id,
            "report_id": report_id,
            "target_repo": body.target_repo,
            "target_pr": body.target_pr,
            "expected_head": body.expected_head,
            "current_head": target["head_sha"],
            "publication_type": body.publication_type,
            "body": report_body,
            "body_digest": body_digest,
            "confirmation_digest": confirmation_digest,
            "created_at": created_at,
            "expires_at": expires,
            "status": "previewed",
            "side_effect": "create_pull_request_comment",
            "auto_merge": False,
        },
    })


@router.post("/reports/{report_id}/publish", status_code=202)
def publish_report(report_id: str, body: PublishCreate, request: Request):
    """Publish one explicitly confirmed report comment, never merge code."""
    preview = _resources().get_publication_preview(body.preview_id)
    if not preview or preview["report_id"] != report_id:
        raise APIError(404, "publication_preview_not_found", "publication preview does not exist or is not visible")
    existing = _resources().get_publication(body.preview_id)
    if existing:
        # A repeat of the same preview is safe and idempotent.  A different
        # confirmation must never be allowed to alias an already published
        # side effect.
        if not hmac.compare_digest(existing["expected_head"], body.expected_head or "") or not hmac.compare_digest(existing["body_digest"], preview["body_digest"]):
            raise APIError(409, "publication_conflict", "publication preview has already been used with different content")
        return _result(request, {"publication": _publication_public_dict(existing), "idempotent_replay": True}, 202)
    expires_at = datetime.fromisoformat(str(preview["expires_at"]).replace("Z", "+00:00"))
    if datetime.now(timezone.utc) >= expires_at:
        raise APIError(409, "publication_preview_expired", "publication preview has expired; create a new preview")
    if not hmac.compare_digest(str(preview["expected_head"]), body.expected_head or ""):
        raise APIError(409, "stale_head", "confirmation head does not match the preview head", {"expected_head": preview["expected_head"]})
    if not hmac.compare_digest(str(preview["confirmation_digest"]), body.confirmation_digest):
        raise APIError(409, "confirmation_digest_mismatch", "confirmation digest does not match the immutable preview")
    report = _resources().get_report(report_id)
    if not report:
        raise APIError(404, "report_not_found", "report does not exist or is not visible")
    snapshot_verification = (report.get("snapshot") or {}).get("verification") or {}
    if snapshot_verification.get("validity") == Validity.STALE.value:
        raise APIError(409, "report_stale", "stale reports cannot be published; create a new verification")
    resolver = _github_source_resolver()
    try:
        result = resolver.publish_report_comment(
            repo_id=str(preview["target_repo"]), number=int(preview["target_pr"]),
            expected_head=str(preview["expected_head"]), body=str(preview["body"]),
        )
    except SourceResolutionError as exc:
        raise _publication_error(exc) from exc
    created_at = utc_now()
    publication = {
        "publication_id": f"publication_{uuid.uuid4().hex}",
        "preview_id": preview["preview_id"],
        "report_id": report_id,
        "target_repo": preview["target_repo"],
        "target_pr": preview["target_pr"],
        "expected_head": preview["expected_head"],
        "body_digest": preview["body_digest"],
        "status": "published",
        "external_id": result.get("external_id"),
        "external_url": result.get("url"),
        "response": result,
        "created_at": created_at,
    }
    try:
        resources = _resources()
        resources.save_publication(publication)
        workspace = resources.acl_workspace("report", report_id) or _workspace_for_request(request)
        resources.bind_acl("publication", publication["publication_id"], workspace)
    except Exception as exc:
        # A successful remote write with a local persistence failure must not
        # be retried automatically: callers need an operator-visible error to
        # reconcile the external_id, avoiding duplicate comments.
        raise APIError(500, "publication_persistence_failed", "publication completed remotely but could not be recorded locally") from exc
    return _result(request, {"publication": _publication_public_dict(publication), "status": "published"}, 202)


@router.get("/publications/{publication_id}")
def get_publication(publication_id: str, request: Request):
    publication = _resources().get_publication_by_id(publication_id)
    if not publication:
        raise APIError(404, "publication_not_found", "publication does not exist or is not visible")
    return _result(request, {"publication": _publication_public_dict(publication)})


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
    resources = _resources()
    _bind_acl(request, resources, "candidate", candidate_id)
    resources.put_candidate_content(candidate_id, body.patch_text, body.content_ref, patch_hash)
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


def _validate_repair_patch(patch_text: str) -> list[str]:
    """Validate a provider patch before it becomes a new candidate."""
    if not patch_text or not patch_text.strip() or len(patch_text.encode("utf-8")) > _MAX_PATCH_BYTES:
        raise ValueError("repair provider returned an empty or oversized patch")
    try:
        entries = parse_unified_diff(patch_text)
    except (TypeError, ValueError) as exc:
        raise ValueError("repair provider returned an invalid unified diff") from exc
    if not entries:
        raise ValueError("repair provider returned no changed files")
    protected = ("test", "tests/", ".github/", "contract", "policy", "fixture")
    paths: list[str] = []
    for entry in entries:
        path = entry.path.replace("\\", "/")
        lower = path.lower()
        if entry.is_binary or entry.is_mode_change:
            raise ValueError("repair provider may not modify binary files or file modes")
        if path.startswith("/") or ".." in path.split("/"):
            raise ValueError("repair provider returned an unsafe path")
        if any(lower == item or lower.startswith(item) for item in protected):
            raise ValueError("repair provider may not modify tests, contracts, policies, fixtures, or CI files")
        paths.append(path)
    return paths


@router.post("/tasks/{task_id}/repairs", status_code=202)
def create_repair(task_id: str, body: RepairCreate, request: Request):
    """Generate one real child candidate under an explicit budget approval."""
    if not body.approved:
        raise APIError(409, "repair_approval_required", "repair requires explicit approval and a bounded budget")
    store = _evidence_store()
    task = store.get_task(task_id)
    if not task:
        raise APIError(404, "task_not_found", "task does not exist or is not visible")
    parent = store.get_candidate(body.parent_candidate_id)
    if not parent or parent.task_id != task_id:
        raise APIError(404, "parent_candidate_not_found", "parent candidate does not exist for this task")
    findings = []
    for finding_id in body.finding_ids:
        finding = store.get_finding(finding_id)
        if not finding or finding.task_id != task_id or finding.candidate_id != parent.candidate_id:
            raise APIError(404, "finding_not_found", "finding does not belong to the parent candidate")
        findings.append(finding)
    created = utc_now()
    job_id = f"job_{uuid.uuid4().hex}"
    queue = _resources().jobs
    queue.create({
        "job_id": job_id, "kind": "candidate_repair", "state": "queued",
        "resource_id": parent.candidate_id, "created_at": created, "updated_at": created,
        "parent_candidate_id": parent.candidate_id, "finding_ids": body.finding_ids,
        "max_budget": body.max_budget, "actor": body.actor, "provider": body.provider,
    })
    worker_id = f"api-repair:{uuid.uuid4().hex}"
    leased = queue.claim(worker_id, job_id=job_id, lease_seconds=900)
    if not leased:
        raise APIError(409, "repair_job_unavailable", "repair job could not be claimed")
    try:
        from .adapters.remote import RemoteModel

        issue = task.issue_snapshot
        repair_task = SimpleNamespace(
            issue_title=str(issue.get("title", "")),
            issue_body=str(issue.get("body", "")),
        )
        failed_tests = [f"{item.kind}: {item.message[:500]}" for item in findings]
        patch_text = RemoteModel().propose_patch(repair_task, dict(body.repo_files), failed_tests)
        changed_paths = _validate_repair_patch(patch_text)
        patch_hash = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        tree_digest = hashlib.sha256(f"repair-tree\0{parent.base_sha}\0{patch_hash}".encode("utf-8")).hexdigest()
        child = Candidate(
            f"candidate_{uuid.uuid4().hex}", task_id, parent.base_sha, None,
            tree_digest, patch_hash, "remote_model", parent.candidate_id,
        )
        store.save_candidate(child)
        resources = _resources()
        resources.put_candidate_content(child.candidate_id, patch_text, None, patch_hash)
        workspace = resources.acl_workspace("task", task_id) or _workspace_for_request(request)
        resources.bind_acl("candidate", child.candidate_id, workspace)
        resources.bind_acl("job", job_id, workspace)
        completed = queue.complete(job_id, worker_id, leased["lease_token"], state="completed", payload={
            "candidate_id": child.candidate_id, "changed_paths": changed_paths,
            "budget_used": 1, "approved_budget": body.max_budget,
        })
        return _result(request, {
            "status": "completed", "job_id": job_id, "candidate": child.to_dict(),
            "parent_candidate_id": parent.candidate_id, "contract_unchanged": True,
            "changed_paths": changed_paths, "job": completed,
        }, 202)
    except ValueError as exc:
        queue.complete(job_id, worker_id, leased["lease_token"], state="error", payload={"error": str(exc), "provider": body.provider})
        return _result(request, {
            "status": "error", "job_id": job_id,
            "error": {"code": "repair_protocol_error", "message": str(exc)},
            "parent_candidate_id": parent.candidate_id, "contract_unchanged": True,
        }, 202)
    except Exception as exc:
        try:
            queue.complete(job_id, worker_id, leased["lease_token"], state="error", payload={"error": "repair_provider_error", "provider": body.provider})
        except Exception:
            pass
        return _result(request, {
            "status": "error", "job_id": job_id,
            "error": {"code": "repair_provider_error", "message": "repair provider unavailable"},
            "parent_candidate_id": parent.candidate_id, "contract_unchanged": True,
        }, 202)


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
    resources = _resources()
    workspace = _bind_acl(request, resources, "verification", verification.verification_id)
    resources.put_job({"job_id": job_id, "kind": "verification", "state": "queued", "resource_id": verification.verification_id, "command_argv": body.command_argv, "suite_id": body.suite_id, "created_at": created, "updated_at": created})
    resources.bind_acl("job", job_id, workspace)
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
            cancel_checker=lambda: queue.is_cancel_requested(row["job_id"], worker_id, leased["lease_token"]),
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
    queue_state = (
        "completed" if execution_status == RunState.COMPLETED.value
        else "cancelled" if execution_status == RunState.CANCELLED.value
        else "error"
    )
    queue.complete(row["job_id"], worker_id, leased["lease_token"], state=queue_state, payload={
        "result_verification_id": result.get("verification", {}).get("verification_id"),
        "execution_id": result.get("execution_id"),
        "execution_status": execution_status,
    })
    resources = _resources()
    workspace = resources.acl_workspace("verification", verification_id) or _workspace_for_request(request)
    result_verification_id = result.get("verification", {}).get("verification_id")
    if result_verification_id:
        resources.bind_acl("verification", str(result_verification_id), workspace)
    for finding in result.get("findings", []):
        finding_id = finding.get("finding_id") if isinstance(finding, dict) else None
        if finding_id:
            resources.bind_acl("finding", str(finding_id), workspace)
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

    @app.middleware("http")
    async def _acl_middleware(request: Request, call_next):
        # ACL is opt-in for backwards-compatible local development.  Once
        # enabled, explicit resource routes fail closed before a handler can
        # reveal a cross-workspace row or perform a side effect.
        if request.url.path.startswith("/api/v1"):
            try:
                if _acl_enabled() and request.url.path != "/api/v1/health":
                    # A workspace namespace is required for collection routes
                    # as well as explicit IDs; otherwise list endpoints could
                    # reveal rows even when detail routes are protected.
                    _workspace_for_request(request)
                resource = _acl_resource_from_path(request.url.path)
                if resource and not _acl_allows(request, *resource):
                    return _acl_denial(request)
            except APIError as exc:
                if exc.code == "workspace_required":
                    return _acl_denial(request, "workspace_required")
                return _error_payload(request, exc)
        return await call_next(request)

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
