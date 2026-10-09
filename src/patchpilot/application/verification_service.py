"""Controlled, source-aware execution for the acceptance domain.

The legacy orchestrator is a fixture/repair runner with a different contract.
This service is intentionally small and independent: it executes one persisted
candidate against one frozen contract, records the base and candidate results,
and appends a new immutable :class:`Verification` snapshot.  It never mutates
the queued verification that requested the work.

Execution is fail-closed.  A missing snapshot, missing patch, empty test
suite, timeout, parser error, or an unconfigured workspace is represented as
``error``/``not_run``/``inconclusive`` rather than a guessed pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from typing import Any, Mapping, Sequence

from ..domain.entities import (
    CheckExecution,
    CheckOutcome,
    ContractState,
    Verification,
    RunState,
    ReviewDecision,
    Verdict,
    aggregate_verdict,
    utc_now,
)


class WorkspaceResolutionError(ValueError):
    """The requested repository is not in the configured workspace allowlist."""


class VerificationExecutionError(RuntimeError):
    """An execution failed before a candidate result could be measured."""


_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_SAFE_EXECUTABLES = frozenset({
    "pytest", "python", "python3", "uv", "ruff", "node", "npm", "pnpm", "yarn",
})
_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors?|skipped|xfailed|xpassed|deselected)",
    re.IGNORECASE,
)
_CASE_RE = re.compile(r"^.*?::[^ ]+\s+(?P<kind>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s*$", re.MULTILINE)


def _redact(text: str, limit: int = 120_000) -> str:
    """Keep command evidence useful without persisting common secret forms."""
    text = text or ""
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "<redacted-key>", text)
    return text[-limit:]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_results(stdout: str, stderr: str, return_code: int | None, timed_out: bool) -> tuple[CheckOutcome, int, dict[str, Any]]:
    """Parse a pytest-like command conservatively.

    A successful process with no observed test case is ``not_run``.  This
    prevents ``echo ok`` or an empty test collection from becoming a green
    acceptance result.
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    if timed_out:
        return CheckOutcome.ERROR, 0, {"reason": "timeout"}
    if "FLAKY" in combined.upper() or "RERUN" in combined.upper():
        return CheckOutcome.FLAKY, 0, {"reason": "runner reported rerun/flaky output"}

    counts: dict[str, int] = {}
    for match in _SUMMARY_RE.finditer(combined):
        kind = match.group("kind").lower().rstrip("s")
        counts[kind] = counts.get(kind, 0) + int(match.group("count"))
    if not counts:
        for match in _CASE_RE.finditer(combined):
            kind = match.group("kind").lower()
            counts[kind] = counts.get(kind, 0) + 1
    count = sum(counts.values())
    if count == 0:
        if return_code == 0:
            return CheckOutcome.NOT_RUN, 0, {"reason": "no test case result observed"}
        return CheckOutcome.ERROR, 0, {"reason": "command failed without a test result", "return_code": return_code}
    if counts.get("failed", 0) or counts.get("error", 0):
        return CheckOutcome.FAIL, count, {"counts": counts}
    if counts.get("skipped", 0) == count or counts.get("deselected", 0) == count:
        return CheckOutcome.SKIPPED, count, {"counts": counts}
    if return_code not in (0, None):
        return CheckOutcome.ERROR, count, {"counts": counts, "return_code": return_code}
    return CheckOutcome.PASS, count, {"counts": counts}


@dataclass(frozen=True, slots=True)
class CommandMeasurement:
    argv: tuple[str, ...]
    return_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    outcome: CheckOutcome
    count: int
    details: Mapping[str, Any]


class WorkspaceResolver:
    """Resolve only repositories explicitly allowed by configuration.

    ``PATCHPILOT_REPO_MAP`` is a JSON object mapping a public repository id
    (``owner/name``) to a local workspace.  Alternatively, callers may pass a
    path under ``PATCHPILOT_WORKSPACE_ROOTS``/``PATCHPILOT_WORKSPACE_ROOT``.
    An arbitrary host path is rejected even when supplied by an API client.
    """

    def __init__(self, roots: Sequence[str | Path] | None = None, repo_map: Mapping[str, str | Path] | None = None):
        configured_roots = list(roots or [])
        if not configured_roots:
            raw = os.getenv("PATCHPILOT_WORKSPACE_ROOTS") or os.getenv("PATCHPILOT_WORKSPACE_ROOT")
            if raw:
                configured_roots = [item for item in raw.split(os.pathsep) if item]
        self.roots = tuple(Path(item).expanduser().resolve() for item in configured_roots)
        if repo_map is None:
            raw_map = os.getenv("PATCHPILOT_REPO_MAP", "")
            if raw_map:
                try:
                    repo_map = json.loads(raw_map)
                except json.JSONDecodeError as exc:
                    raise WorkspaceResolutionError("PATCHPILOT_REPO_MAP must be valid JSON") from exc
        self.repo_map = {str(key): Path(value).expanduser().resolve() for key, value in (repo_map or {}).items()}

    def _allowed(self, path: Path) -> bool:
        return bool(self.roots) and any(path == root or root in path.parents for root in self.roots)

    def resolve(self, *, repo_id: str | None = None, repo_path: str | Path | None = None) -> Path:
        if repo_path is not None:
            path = Path(repo_path).expanduser().resolve()
            if not self._allowed(path):
                raise WorkspaceResolutionError("repo_path is outside configured PatchPilot workspaces")
        elif repo_id and repo_id in self.repo_map:
            path = self.repo_map[repo_id]
            if self.roots and not self._allowed(path):
                raise WorkspaceResolutionError("mapped repository is outside configured PatchPilot workspaces")
        else:
            raise WorkspaceResolutionError("a repo_path or configured repo mapping is required")
        if not path.is_dir():
            raise WorkspaceResolutionError("configured repository workspace does not exist")
        if not (path / ".git").exists():
            raise WorkspaceResolutionError("configured repository workspace is not a git repository")
        return path


class VerificationService:
    """Execute and persist one verification without rewriting old snapshots."""

    def __init__(self, store, *, resolver: WorkspaceResolver | None = None, timeout: int | float = 120):
        self.store = store
        self.resolver = resolver or WorkspaceResolver()
        self.timeout = max(1, min(int(timeout), 900))
        self._ensure_execution_links()

    def _ensure_execution_links(self) -> None:
        with self.store._lock:
            self.store.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS verification_executions(
                  execution_id TEXT PRIMARY KEY,
                  source_verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
                  result_verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
                  state TEXT NOT NULL,
                  payload TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_verification_executions_source
                  ON verification_executions(source_verification_id, created_at, execution_id);
                CREATE TRIGGER IF NOT EXISTS immutable_verification_executions_update
                  BEFORE UPDATE ON verification_executions BEGIN SELECT RAISE(ABORT, 'immutable verification execution'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_verification_executions_delete
                  BEFORE DELETE ON verification_executions BEGIN SELECT RAISE(ABORT, 'immutable verification execution'); END;
                """
            )
            self.store.db.commit()

    def _candidate_patch(self, candidate_id: str) -> str | None:
        # Candidate content is owned by the v1 resource store.  Keep this
        # lookup read-only and do not accept a path supplied by the caller.
        with self.store._lock:
            try:
                row = self.store.db.execute(
                    "SELECT content FROM api_candidate_contents WHERE candidate_id=?", (candidate_id,)
                ).fetchone()
            except Exception:
                row = None
        return str(row["content"]) if row and row["content"] is not None else None

    @staticmethod
    def _validate_commands(commands: Sequence[Sequence[str]] | None) -> tuple[tuple[str, ...], ...]:
        commands = commands or (("pytest", "-q"),)
        result: list[tuple[str, ...]] = []
        for argv in commands:
            if not argv or not all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv):
                raise ValueError("command_argv entries must be non-empty argv strings")
            executable = Path(argv[0]).name
            if executable not in _SAFE_EXECUTABLES:
                raise ValueError(f"command is not allowed: {executable}")
            if any(arg in {"-c", "--config", "--config-file"} and i + 1 < len(argv) and Path(argv[i + 1]).is_absolute() for i, arg in enumerate(argv)):
                raise ValueError("commands may not read an absolute host config path")
            result.append(tuple(argv))
        return tuple(result)

    @staticmethod
    def _archive_commit(repo: Path, ref: str, destination: Path) -> None:
        if not ref or not _SAFE_REF.fullmatch(ref):
            raise VerificationExecutionError("base snapshot ref is invalid")
        check = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            cwd=repo, capture_output=True, text=True, timeout=15,
        )
        if check.returncode != 0:
            raise VerificationExecutionError("base snapshot could not be resolved")
        archive = subprocess.run(
            ["git", "archive", "--format=tar", ref], cwd=repo,
            capture_output=True, timeout=30,
        )
        if archive.returncode != 0:
            raise VerificationExecutionError("base snapshot could not be materialized")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve()
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as tar:
            # Git archives can contain symlinks/hardlinks.  Never materialize
            # archive entries that could escape the disposable workspace or
            # redirect a test process to an untrusted host path.
            for member in tar.getmembers():
                name = member.name.replace("\\", "/").rstrip("/")
                target = (destination / name).resolve()
                if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                    raise VerificationExecutionError("base snapshot contains an unsafe archive path")
                if target != destination and destination not in target.parents:
                    raise VerificationExecutionError("base snapshot escapes the temporary workspace")
                if member.issym() or member.islnk():
                    raise VerificationExecutionError("base snapshot contains an unsupported link entry")
                if not (member.isfile() or member.isdir()):
                    raise VerificationExecutionError("base snapshot contains an unsupported archive entry")
                tar.extract(member, destination, filter="data")

    @staticmethod
    def _reject_symlinks(workspace: Path) -> None:
        for path in workspace.rglob("*"):
            if path.is_symlink():
                raise VerificationExecutionError("candidate workspace contains an unsupported symbolic link")

    @staticmethod
    def _apply_patch(workspace: Path, patch_text: str) -> None:
        if not patch_text or not patch_text.strip():
            raise VerificationExecutionError("candidate patch content is unavailable")
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=workspace, input=patch_text, capture_output=True, text=True, timeout=30,
        )
        if check.returncode != 0:
            raise VerificationExecutionError("candidate patch does not apply to the pinned base snapshot")
        applied = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=workspace, input=patch_text, capture_output=True, text=True, timeout=30,
        )
        if applied.returncode != 0:
            raise VerificationExecutionError("candidate patch application failed")

    def _measure(self, workspace: Path, argv: tuple[str, ...]) -> CommandMeasurement:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                list(argv), cwd=workspace, capture_output=True, text=True,
                timeout=self.timeout, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            timed_out = False
            return_code = proc.returncode
            stdout, stderr = _redact(proc.stdout), _redact(proc.stderr)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            return_code = 124
            stdout = _redact(exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            stderr = _redact(exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        except OSError as exc:
            # Missing executables and permission errors are measured
            # infrastructure failures, not uncaught API exceptions.
            timed_out = False
            return_code = None
            stdout = ""
            stderr = _redact(str(exc))
            duration_ms = int((time.monotonic() - started) * 1000)
            return CommandMeasurement(
                tuple(argv), return_code, stdout, stderr, duration_ms, timed_out,
                CheckOutcome.ERROR, 0, {"reason": "executable_unavailable", "error": str(exc)},
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        outcome, count, details = _parse_results(stdout, stderr, return_code, timed_out)
        return CommandMeasurement(tuple(argv), return_code, stdout, stderr, duration_ms, timed_out, outcome, count, details)

    def _save_link(self, execution_id: str, source_id: str, result_id: str, state: str, payload: Mapping[str, Any]) -> None:
        with self.store._lock:
            self.store.db.execute(
                "INSERT INTO verification_executions(execution_id,source_verification_id,result_verification_id,state,payload,created_at) VALUES (?,?,?,?,?,?)",
                (execution_id, source_id, result_id, state, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), utc_now()),
            )
            self.store.db.commit()

    def execute(
        self,
        verification_id: str,
        *,
        repo_id: str | None = None,
        repo_path: str | Path | None = None,
        command_argv: Sequence[Sequence[str]] | None = None,
        suite_id: str | None = None,
    ) -> dict[str, Any]:
        source = self.store.get_verification(verification_id)
        if source is None:
            raise VerificationExecutionError("verification does not exist")
        candidate = self.store.get_candidate(source.candidate_id)
        contract = self.store.get_contract_version(source.contract_id, source.contract_revision)
        if candidate is None or contract is None:
            raise VerificationExecutionError("verification references an unavailable candidate or contract")
        if contract.state is not ContractState.FROZEN:
            raise VerificationExecutionError("verification contract is not frozen")
        commands = self._validate_commands(command_argv)
        suite = suite_id or "persisted-suite"
        patch_text = self._candidate_patch(candidate.candidate_id)
        result_id = f"verification_{uuid.uuid4().hex}"
        execution_id = f"execution_{uuid.uuid4().hex}"
        checks: list[CheckExecution] = []
        gaps: list[str] = []
        run_state = RunState.COMPLETED
        verdict = Verdict.INCONCLUSIVE
        link_payload: dict[str, Any] = {"source_verification_id": verification_id, "commands": [list(item) for item in commands]}

        try:
            repository = self.resolver.resolve(repo_id=repo_id, repo_path=repo_path)
            with tempfile.TemporaryDirectory(prefix="patchpilot-verify-") as temp_root:
                root = Path(temp_root)
                base_workspace = root / "base"
                candidate_workspace = root / "candidate"
                self._archive_commit(repository, candidate.base_sha, base_workspace)
                shutil.copytree(base_workspace, candidate_workspace)
                if patch_text is None:
                    raise VerificationExecutionError("candidate patch content is unavailable")
                self._apply_patch(candidate_workspace, patch_text)
                self._reject_symlinks(candidate_workspace)
                for index, argv in enumerate(commands, start=1):
                    target = f"command-{index}"
                    for variant, workspace in (("base", base_workspace), ("candidate", candidate_workspace)):
                        measurement = self._measure(workspace, argv)
                        checks.append(CheckExecution(
                            check_id=f"check_{uuid.uuid4().hex}", verification_id=result_id,
                            suite_id=suite, target=target, variant=variant,
                            outcome=measurement.outcome, count=measurement.count,
                            command_argv=measurement.argv, return_code=measurement.return_code,
                            duration_ms=measurement.duration_ms, stdout=measurement.stdout,
                            stderr=measurement.stderr, details=measurement.details,
                        ))
        except (WorkspaceResolutionError, VerificationExecutionError, ValueError) as exc:
            gaps.append(str(exc))
            run_state = RunState.ERROR
            # Preserve the reason as an explicit check if the repository was
            # unavailable; there is no meaningful base/candidate execution.
            checks.append(CheckExecution(
                check_id=f"check_{uuid.uuid4().hex}", verification_id=result_id,
                suite_id=suite, target="execution", variant="candidate",
                outcome=CheckOutcome.ERROR, count=0, command_argv=tuple(commands[0]) if commands else (),
                details={"reason": str(exc)},
            ))

        required_conditions = [condition for condition in contract.conditions if condition.required]
        valid_oracles = bool(required_conditions) and all(
            condition.oracle and condition.confirmation != "unconfirmed" for condition in required_conditions
        )
        candidate_checks = [item for item in checks if item.variant == "candidate"]
        base_checks = [item for item in checks if item.variant == "base"]
        if not candidate_checks:
            gaps.append("candidate_checks_not_run")
        if any(item.outcome in {CheckOutcome.ERROR, CheckOutcome.NOT_RUN, CheckOutcome.FLAKY, CheckOutcome.UNSUPPORTED, CheckOutcome.SKIPPED} for item in checks):
            gaps.append("incomplete_check_execution")
        if not base_checks:
            gaps.append("baseline_checks_not_run")
        elif any(item.outcome is not CheckOutcome.PASS for item in base_checks):
            # A candidate cannot be credited for a green result when the
            # pinned baseline was not itself measured successfully.  Existing
            # defects may later be handled by an explicit, recorded policy
            # exemption; this worker has no such exemption by default.
            gaps.append("baseline_check_failed")
        if any(item.count == 0 for item in checks):
            gaps.append("zero_test_cases")
        verdict = aggregate_verdict(
            [item.outcome for item in candidate_checks],
            required_oracles_valid=valid_oracles,
            required_gaps=tuple(dict.fromkeys(gaps)),
        )
        result = Verification(
            result_id, source.verification_key, source.candidate_id, source.contract_id,
            run_state, verdict, source.contract_revision, source.validity,
            ReviewDecision.PENDING, tuple(dict.fromkeys(gaps)),
            tuple(item.check_id for item in checks if item.outcome is CheckOutcome.PASS),
        )
        self.store.save_verification(result)
        for item in checks:
            self.store.save_check_execution(item)
        # Materialize a durable, conservative finding projection from the
        # exact immutable snapshot and measured checks.  This call is
        # idempotent: retries reuse deterministic finding IDs and never alter
        # an earlier verification's history.
        findings = self.store.derive_findings_for_verification(result_id)
        link_payload.update({"result_verification_id": result_id, "verdict": verdict.value, "gaps": list(result.gaps)})
        self._save_link(execution_id, verification_id, result_id, run_state.value, link_payload)
        return {
            "execution_id": execution_id,
            "source_verification_id": verification_id,
            "verification": result.to_dict(),
            "checks": [item.to_dict() for item in checks],
            "findings": [item.to_dict() for item in findings],
        }


__all__ = [
    "VerificationService", "WorkspaceResolver", "WorkspaceResolutionError",
    "VerificationExecutionError",
]
