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
import signal
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

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
from ..verification.adapters import adapter_for_command


class WorkspaceResolutionError(ValueError):
    """The requested repository is not in the configured workspace allowlist."""


class VerificationExecutionError(RuntimeError):
    """An execution failed before a candidate result could be measured."""


class VerificationCancelled(VerificationExecutionError):
    """The caller cancelled a running command before it produced a result.

    The partial stdout/stderr is retained by the caller as diagnostic context,
    while the check itself is recorded as ``not_run``.  A cancellation is a
    control-plane outcome, never a test failure or a successful result.
    """

    def __init__(self, message: str = "verification cancelled", *, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,255}$")
_SAFE_EXECUTABLES = frozenset({
    "pytest", "python", "python3", "uv", "ruff", "node", "npm", "pnpm", "yarn",
})


def _bounded_limit(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a defensive archive limit without allowing unsafe configuration."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


# Git archives are untrusted input when a candidate references a remote or
# shared repository.  Keep both the number of entries and their expanded byte
# size bounded so a tiny tarball cannot exhaust the temporary workspace.
_ARCHIVE_MAX_MEMBERS = _bounded_limit("PATCHPILOT_ARCHIVE_MAX_MEMBERS", 50_000, 1, 1_000_000)
_ARCHIVE_MAX_EXPANDED_BYTES = _bounded_limit(
    "PATCHPILOT_ARCHIVE_MAX_EXPANDED_BYTES", 256 * 1024 * 1024, 1, 4 * 1024 * 1024 * 1024
)
_ARCHIVE_MAX_TAR_BYTES = _bounded_limit(
    "PATCHPILOT_ARCHIVE_MAX_TAR_BYTES", 256 * 1024 * 1024, 1, 4 * 1024 * 1024 * 1024
)
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


_TEST_SURFACE_BASENAMES = frozenset({
    "conftest.py", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg",
    "pyproject.toml", "package.json", "package-lock.json", "pnpm-lock.yaml",
    "yarn.lock",
})


def _is_test_surface_path(relative: str) -> bool:
    """Return whether a repository path can influence the executed tests.

    This intentionally errs on the side of inclusion.  The manifest is only
    used for an integrity comparison, so including a broad but deterministic
    set of test/config files is safer than attempting to infer intent from a
    patch supplied by an untrusted candidate.
    """
    path = Path(relative)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if parts & {"test", "tests", "__tests__", "spec", "specs"}:
        return True
    if name in _TEST_SURFACE_BASENAMES:
        return True
    if name.startswith(("vitest.config.", "vite.config.")):
        return True
    if name.startswith("test_") or name.endswith(("_test.py", ".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx")):
        return True
    return False


def _test_surface_manifest(workspace: Path) -> dict[str, str]:
    """Hash the candidate-visible test/config surface from the workspace.

    Hashes are computed from the materialized base and candidate snapshots,
    never from a caller-provided baseline string.  Symlinks and non-files are
    excluded because the workspace resolver rejects symlinked candidates.
    """
    manifest: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(workspace).as_posix()
        if not _is_test_surface_path(relative):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest[relative] = digest.hexdigest()
    return manifest


def _test_surface_diff(base: Mapping[str, str], candidate: Mapping[str, str]) -> list[dict[str, Any]]:
    """Describe added, removed, and modified test/config files."""
    changed: list[dict[str, Any]] = []
    for path in sorted(set(base) | set(candidate)):
        old, new = base.get(path), candidate.get(path)
        if old == new:
            continue
        changed.append({
            "path": path,
            "change": "added" if old is None else "removed" if new is None else "modified",
            "base_sha256": old,
            "candidate_sha256": new,
        })
    return changed


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
    # Pytest exits with code 5 for an empty collection.  Treat that explicit
    # signal as not_run rather than a generic environment error; both remain
    # incomplete and therefore cannot satisfy an acceptance gate.
    if re.search(r"\bno tests? ran\b|collected\s+0\s+items?", combined, re.IGNORECASE):
        return CheckOutcome.NOT_RUN, 0, {"reason": "zero_test_cases"}

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
        # ``git archive`` is captured in memory.  Reject an oversized tar
        # before opening it, then enforce the expanded limit while walking
        # members so sparse/deflated archives cannot become workspace bombs.
        if len(archive.stdout) > _ARCHIVE_MAX_TAR_BYTES:
            raise VerificationExecutionError("base snapshot archive exceeds the compressed size limit")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve()
        with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:") as tar:
            # Git archives can contain symlinks/hardlinks.  Never materialize
            # archive entries that could escape the disposable workspace or
            # redirect a test process to an untrusted host path.
            members = tar.getmembers()
            if len(members) > _ARCHIVE_MAX_MEMBERS:
                raise VerificationExecutionError("base snapshot contains too many archive entries")
            expanded_bytes = 0
            for member in members:
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
                if member.size < 0:
                    raise VerificationExecutionError("base snapshot contains an invalid archive size")
                expanded_bytes += int(member.size)
                if expanded_bytes > _ARCHIVE_MAX_EXPANDED_BYTES:
                    raise VerificationExecutionError("base snapshot exceeds the expanded size limit")
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

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> tuple[str, str]:
        """Terminate a command and its process group, returning collected output."""
        if proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            stdout, stderr = proc.communicate()
        return stdout or "", stderr or ""

    def _measure(
        self,
        workspace: Path,
        argv: tuple[str, ...],
        *,
        cancel_checker: Callable[[], bool] | None = None,
        probe_input_json: str | None = None,
    ) -> CommandMeasurement:
        """Run one argv without a shell and observe cancellation while running.

        ``subprocess.run(timeout=...)`` cannot reliably terminate descendants.
        A new process group lets cancellation and timeout clean up the complete
        command tree, which is required before a job may be marked cancelled.
        """
        started = time.monotonic()
        adapter = adapter_for_command(argv)
        executable = Path(argv[0]).name.lower() if argv else ""
        # Registered test runners must be interpreted by an adapter.  Utility
        # commands such as ``python -c`` remain executable for controlled
        # lifecycle probes, but node/package-manager test commands with no
        # adapter are explicit unsupported observations, never guessed passes.
        adapter_required = executable in {"pytest", "node", "npm", "pnpm", "yarn", "vitest", "jest", "mocha"}
        if adapter is None and adapter_required:
            return CommandMeasurement(
                tuple(argv), None, "", "", 0, False, CheckOutcome.UNSUPPORTED, 0,
                {"reason": "no_registered_adapter", "command": list(argv)},
            )
        try:
            if cancel_checker and cancel_checker():
                raise VerificationCancelled()
            command_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
            if probe_input_json is not None:
                # Both base and candidate receive the identical JSON through
                # an explicit environment channel; argv remains shell-free.
                command_env["PATCHPILOT_PROBE_INPUT_JSON"] = probe_input_json
            proc = subprocess.Popen(
                list(argv), cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=command_env,
                start_new_session=(os.name != "nt"),
            )
            while True:
                try:
                    stdout, stderr = proc.communicate(timeout=0.1)
                    timed_out = False
                    return_code = proc.returncode
                    break
                except subprocess.TimeoutExpired:
                    if cancel_checker and cancel_checker():
                        out, err = self._terminate_process(proc)
                        raise VerificationCancelled(stdout=out, stderr=err)
                    if time.monotonic() - started >= self.timeout:
                        out, err = self._terminate_process(proc)
                        stdout, stderr = out, err
                        timed_out = True
                        return_code = 124
                        break
        except VerificationCancelled:
            raise
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
        stdout, stderr = _redact(stdout), _redact(stderr)
        duration_ms = int((time.monotonic() - started) * 1000)
        if adapter is not None:
            # Keep the explicit empty-collection semantics independent of an
            # adapter's process-exit interpretation (pytest uses exit code 5
            # for this case).  Zero collected tests remain not_run, never pass.
            zero_collection = bool(re.search(r"\bno tests? ran\b|collected\s+0\s+items?", stdout + "\n" + stderr, re.IGNORECASE))
            normalized = adapter.parse(stdout, stderr, return_code, timed_out=timed_out)
            try:
                outcome = CheckOutcome(normalized.outcome)
            except ValueError:
                outcome = CheckOutcome.UNSUPPORTED
            count = int(normalized.count)
            details = {
                "framework": normalized.framework,
                "adapter_supported": normalized.supported,
                **dict(normalized.details),
            }
            if zero_collection:
                outcome = CheckOutcome.NOT_RUN
                count = 0
                details["reason"] = "zero_test_cases"
        else:
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
        cancel_checker: Callable[[], bool] | None = None,
        probe_input: Any | None = None,
        claimed_baseline_tests_hash: str | None = None,
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
        if claimed_baseline_tests_hash is not None:
            # This is retained as an untrusted request annotation only.  The
            # actual baseline hash is computed below from the materialized
            # snapshot and is the sole integrity basis.
            link_payload["claimed_baseline_tests_hash"] = claimed_baseline_tests_hash
        if probe_input is not None:
            link_payload["probe_input"] = probe_input

        probe_json = None if probe_input is None else json.dumps(
            probe_input, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        probe_hash = _sha256_bytes(probe_json.encode("utf-8")) if probe_json is not None else None

        def append_not_run(target: str, variant: str, argv: tuple[str, ...], reason: str) -> None:
            checks.append(CheckExecution(
                check_id=f"check_{uuid.uuid4().hex}", verification_id=result_id,
                suite_id=suite, target=target, variant=variant,
                outcome=CheckOutcome.NOT_RUN, count=0, command_argv=argv,
                details={"reason": reason},
            ))

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
                base_test_surface = _test_surface_manifest(base_workspace)
                candidate_test_surface = _test_surface_manifest(candidate_workspace)
                base_test_surface_hash = _sha256_bytes(
                    json.dumps(base_test_surface, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
                candidate_test_surface_hash = _sha256_bytes(
                    json.dumps(candidate_test_surface, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )
                changed_test_surface = _test_surface_diff(base_test_surface, candidate_test_surface)
                integrity_outcome = CheckOutcome.FAIL if changed_test_surface else (
                    CheckOutcome.NOT_RUN if not base_test_surface else CheckOutcome.PASS
                )
                integrity_details: dict[str, Any] = {
                    "integrity_check": "test_surface",
                    "integrity_violation": bool(changed_test_surface),
                    "base_test_surface_sha256": base_test_surface_hash,
                    "candidate_test_surface_sha256": candidate_test_surface_hash,
                    "base_test_surface_files": sorted(base_test_surface),
                    "candidate_test_surface_files": sorted(candidate_test_surface),
                    "changed_files": changed_test_surface,
                    "claimed_baseline_tests_hash": claimed_baseline_tests_hash,
                    "baseline_hash_source": "materialized_base_snapshot",
                }
                checks.append(CheckExecution(
                    check_id=f"check_{uuid.uuid4().hex}", verification_id=result_id,
                    suite_id=suite, target="test-surface-integrity", variant="candidate",
                    outcome=integrity_outcome, count=len(changed_test_surface) if changed_test_surface else len(base_test_surface),
                    details=integrity_details,
                ))
                if changed_test_surface:
                    gaps.append("test_surface_tampered")
                elif not base_test_surface:
                    gaps.append("test_surface_unavailable")
                link_payload.update({
                    "base_test_surface_sha256": base_test_surface_hash,
                    "candidate_test_surface_sha256": candidate_test_surface_hash,
                    "test_surface_changed_files": changed_test_surface,
                    "baseline_hash_source": "materialized_base_snapshot",
                })
                cancelled = False
                for index, argv in enumerate(commands, start=1):
                    target = f"command-{index}"
                    for variant, workspace in (("base", base_workspace), ("candidate", candidate_workspace)):
                        if cancelled:
                            append_not_run(target, variant, argv, "cancelled_before_check")
                            continue
                        try:
                            measurement = self._measure(
                                workspace, argv, cancel_checker=cancel_checker,
                                probe_input_json=probe_json,
                            )
                        except VerificationCancelled as exc:
                            cancelled = True
                            append_not_run(target, variant, argv, "cancelled_during_check")
                            # The current check and every remaining variant are
                            # represented explicitly; completed checks remain
                            # untouched in the immutable result.
                            continue
                        measurement_details = dict(measurement.details)
                        if probe_hash:
                            measurement_details.update({
                                "probe_input_hash": probe_hash,
                                "probe_input_channel": "PATCHPILOT_PROBE_INPUT_JSON",
                            })
                        checks.append(CheckExecution(
                            check_id=f"check_{uuid.uuid4().hex}", verification_id=result_id,
                            suite_id=suite, target=target, variant=variant,
                            outcome=measurement.outcome, count=measurement.count,
                            command_argv=measurement.argv, return_code=measurement.return_code,
                            duration_ms=measurement.duration_ms, stdout=measurement.stdout,
                            stderr=measurement.stderr, details=measurement_details,
                        ))
                    if cancelled:
                        for remaining_index in range(index + 1, len(commands) + 1):
                            remaining_target = f"command-{remaining_index}"
                            remaining_argv = commands[remaining_index - 1]
                            append_not_run(remaining_target, "base", remaining_argv, "cancelled_before_check")
                            append_not_run(remaining_target, "candidate", remaining_argv, "cancelled_before_check")
                        gaps.append("cancelled")
                        run_state = RunState.CANCELLED
                        break
        except (WorkspaceResolutionError, VerificationExecutionError, ValueError) as exc:
            if isinstance(exc, VerificationCancelled):
                gaps.append("cancelled")
                run_state = RunState.CANCELLED
            else:
                gaps.append(str(exc))
                run_state = RunState.ERROR
            # Preserve the reason as an explicit check if the repository was
            # unavailable; there is no meaningful base/candidate execution.
            if not isinstance(exc, VerificationCancelled):
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
    "VerificationExecutionError", "VerificationCancelled",
]
