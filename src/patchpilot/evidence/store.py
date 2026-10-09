from __future__ import annotations
import json, hashlib, sqlite3, threading, time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Mapping
from ..domain import Event, Evidence, Artifact, Run
from ..domain.models import compute_event_hash
from ..domain.entities import (
    AcceptanceCondition, Candidate, ContractState, ContractVersion, ReviewDecision,
    CheckExecution, Finding, ReviewDecisionRecord, RunState, Task, Validity, Verification, Verdict,
)


# ``PatchPilot`` creates one store for the API and another one for the
# orchestrator.  Both connections can be used by different request threads,
# so a lock attached only to an instance is not enough to serialize writes to
# the same SQLite file.  Keep one re-entrant lock per resolved database path;
# this also makes the read-modify-write event hash chain atomic when multiple
# ``EvidenceStore`` instances share an artifact root in one process.
_STORE_LOCKS: dict[str, threading.RLock] = {}
_STORE_LOCKS_GUARD = threading.Lock()


class EvidenceStore:
    def __init__(self, root: str|Path="artifacts"):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.db_path=self.root/"patchpilot.sqlite3"; self.jsonl=self.root/"events.jsonl"
        lock_key=str(self.db_path.resolve())
        with _STORE_LOCKS_GUARD:
            self._lock=_STORE_LOCKS.setdefault(lock_key, threading.RLock())
        # ``check_same_thread=False`` is intentional: FastAPI may dispatch
        # handlers to different worker threads.  Every operation below is
        # protected by ``self._lock``.
        self.db=sqlite3.connect(self.db_path,check_same_thread=False,timeout=30.0)
        self.db.row_factory=sqlite3.Row
        with self._lock:
            self.db.executescript('''PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS task_versions(
                task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL CHECK(revision > 0),
                payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id, revision));
            CREATE TABLE IF NOT EXISTS candidates(
                candidate_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contract_versions(
                contract_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
                task_id TEXT NOT NULL REFERENCES tasks(task_id), contract_hash TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(contract_id, revision));
            CREATE TABLE IF NOT EXISTS verifications(
                verification_id TEXT PRIMARY KEY, verification_key TEXT NOT NULL,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                contract_id TEXT NOT NULL, contract_revision INTEGER NOT NULL,
                run_state TEXT NOT NULL CHECK(run_state IN ('draft','queued','running','awaiting_input','completed','cancelled','error')),
                verdict TEXT NOT NULL CHECK(verdict IN ('not_evaluated','accepted_within_scope','rejected','inconclusive','blocked')),
                validity TEXT NOT NULL CHECK(validity IN ('current','stale')),
                review_decision TEXT NOT NULL CHECK(review_decision IN ('pending','accepted','rejected','exception_accepted')),
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(contract_id, contract_revision) REFERENCES contract_versions(contract_id, revision));
            CREATE INDEX IF NOT EXISTS ix_verifications_key ON verifications(verification_key);
            CREATE TABLE IF NOT EXISTS review_decisions(
                decision_id TEXT PRIMARY KEY,
                verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
                expected_key TEXT NOT NULL, decision TEXT NOT NULL, actor TEXT NOT NULL,
                reason TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS check_executions(
                check_id TEXT PRIMARY KEY,
                verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
                suite_id TEXT NOT NULL,
                target TEXT NOT NULL,
                variant TEXT NOT NULL CHECK(variant IN ('base','candidate')),
                outcome TEXT NOT NULL CHECK(outcome IN ('pass','fail','error','skipped','not_run','flaky','unsupported')),
                count INTEGER NOT NULL CHECK(count >= 0),
                payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_check_executions_verification ON check_executions(verification_id, created_at, check_id);
            CREATE TABLE IF NOT EXISTS findings(
                finding_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
                verification_key TEXT NOT NULL,
                candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
                contract_id TEXT NOT NULL,
                contract_revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                condition_id TEXT,
                source_variant TEXT CHECK(source_variant IN ('base','candidate') OR source_variant IS NULL),
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_findings_task ON findings(task_id, created_at, finding_id);
            CREATE INDEX IF NOT EXISTS ix_findings_verification ON findings(verification_id, created_at, finding_id);
            CREATE TRIGGER IF NOT EXISTS immutable_findings_update
                BEFORE UPDATE ON findings BEGIN SELECT RAISE(ABORT, 'immutable finding'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_findings_delete
                BEFORE DELETE ON findings BEGIN SELECT RAISE(ABORT, 'immutable finding'); END;
            CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS idempotency_keys(
                scope TEXT NOT NULL, idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                response_ref TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(scope, idempotency_key));
            INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
            CREATE TRIGGER IF NOT EXISTS immutable_task_versions_update
                BEFORE UPDATE ON task_versions BEGIN SELECT RAISE(ABORT, 'immutable task version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_task_versions_delete
                BEFORE DELETE ON task_versions BEGIN SELECT RAISE(ABORT, 'immutable task version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_candidates_update
                BEFORE UPDATE ON candidates BEGIN SELECT RAISE(ABORT, 'immutable candidate'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_candidates_delete
                BEFORE DELETE ON candidates BEGIN SELECT RAISE(ABORT, 'immutable candidate'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_contract_versions_update
                BEFORE UPDATE ON contract_versions BEGIN SELECT RAISE(ABORT, 'immutable contract version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_contract_versions_delete
                BEFORE DELETE ON contract_versions BEGIN SELECT RAISE(ABORT, 'immutable contract version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_verifications_update
                BEFORE UPDATE ON verifications BEGIN SELECT RAISE(ABORT, 'immutable verification'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_verifications_delete
                BEFORE DELETE ON verifications BEGIN SELECT RAISE(ABORT, 'immutable verification'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_review_decisions_update
                BEFORE UPDATE ON review_decisions BEGIN SELECT RAISE(ABORT, 'immutable review decision'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_review_decisions_delete
                BEFORE DELETE ON review_decisions BEGIN SELECT RAISE(ABORT, 'immutable review decision'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_check_executions_update
                BEFORE UPDATE ON check_executions BEGIN SELECT RAISE(ABORT, 'immutable check execution'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_check_executions_delete
                BEFORE DELETE ON check_executions BEGIN SELECT RAISE(ABORT, 'immutable check execution'); END;
            ''')
            self._ensure_domain_schema_unlocked()
            self.db.commit()

    def _ensure_domain_schema_unlocked(self) -> None:
        """Upgrade a database created by the first WP01 draft in place.

        ``CREATE TABLE IF NOT EXISTS`` cannot add columns to an existing local
        database.  The additive migration keeps old evidence readable while
        making the four-dimensional verification state explicit for new rows.
        """
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(verifications)").fetchall()}
        additions = {
            "contract_revision": "INTEGER NOT NULL DEFAULT 1",
            "run_state": "TEXT NOT NULL DEFAULT 'queued'",
            "verdict": "TEXT NOT NULL DEFAULT 'not_evaluated'",
            "validity": "TEXT NOT NULL DEFAULT 'current'",
            "review_decision": "TEXT NOT NULL DEFAULT 'pending'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE verifications ADD COLUMN {name} {definition}")
        self.db.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) "
            "VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
        )

    @staticmethod
    def _json(payload) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def save_task(self, task: Task) -> None:
        """Append an immutable task snapshot revision (revisions start at 1)."""
        payload = self._json(task.to_dict())
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("INSERT OR IGNORE INTO tasks(task_id) VALUES (?)", (task.task_id,))
                row = self.db.execute("SELECT MAX(revision) AS revision FROM task_versions WHERE task_id=?", (task.task_id,)).fetchone()
                expected = (row["revision"] or 0) + 1
                if task.revision != expected:
                    raise ValueError(f"task revision must append as {expected}; got {task.revision}")
                self.db.execute("INSERT INTO task_versions VALUES (?,?,?,?)",
                                 (task.task_id, task.revision, payload, task.created_at))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def get_task(self, task_id: str, revision: int | None = None) -> Task | None:
        with self._lock:
            if revision is None:
                row = self.db.execute("SELECT payload FROM task_versions WHERE task_id=? ORDER BY revision DESC LIMIT 1", (task_id,)).fetchone()
            else:
                row = self.db.execute("SELECT payload FROM task_versions WHERE task_id=? AND revision=?", (task_id, revision)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        return Task(**data)

    def list_task_revisions(self, task_id: str) -> list[Task]:
        with self._lock:
            rows = self.db.execute("SELECT payload FROM task_versions WHERE task_id=? ORDER BY revision", (task_id,)).fetchall()
        return [Task(**json.loads(row["payload"])) for row in rows]

    def save_candidate(self, candidate: Candidate) -> None:
        payload = self._json(candidate.to_dict())
        with self._lock:
            if not self.db.execute("SELECT 1 FROM tasks WHERE task_id=?", (candidate.task_id,)).fetchone():
                raise ValueError(f"task does not exist: {candidate.task_id}")
            if candidate.parent_candidate_id and not self.db.execute(
                    "SELECT 1 FROM candidates WHERE candidate_id=? AND task_id=?",
                    (candidate.parent_candidate_id, candidate.task_id)).fetchone():
                raise ValueError("parent candidate must exist in the same task")
            self.db.execute("INSERT INTO candidates VALUES (?,?,?,?)",
                            (candidate.candidate_id, candidate.task_id, payload, candidate.created_at))
            self.db.commit()

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        with self._lock:
            row = self.db.execute("SELECT payload FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return Candidate(**json.loads(row["payload"])) if row else None

    def list_candidates(self, task_id: str) -> list[Candidate]:
        with self._lock:
            rows = self.db.execute("SELECT payload FROM candidates WHERE task_id=? ORDER BY created_at,candidate_id", (task_id,)).fetchall()
        return [Candidate(**json.loads(row["payload"])) for row in rows]

    def save_contract_version(self, contract: ContractVersion) -> None:
        payload = self._json(contract.to_dict())
        with self._lock:
            if not self.db.execute("SELECT 1 FROM tasks WHERE task_id=?", (contract.task_id,)).fetchone():
                raise ValueError(f"task does not exist: {contract.task_id}")
            row = self.db.execute("SELECT MAX(revision) AS revision FROM contract_versions WHERE contract_id=?",
                                  (contract.contract_id,)).fetchone()
            expected = (row["revision"] or 0) + 1
            if contract.revision != expected:
                raise ValueError(f"contract revision must append as {expected}; got {contract.revision}")
            self.db.execute("INSERT INTO contract_versions VALUES (?,?,?,?,?,?)",
                            (contract.contract_id, contract.revision, contract.task_id,
                             contract.contract_hash, payload, contract.created_at))
            self.db.commit()

    def get_contract_version(self, contract_id: str, revision: int | None = None) -> ContractVersion | None:
        with self._lock:
            if revision is None:
                row = self.db.execute("SELECT payload FROM contract_versions WHERE contract_id=? ORDER BY revision DESC LIMIT 1", (contract_id,)).fetchone()
            else:
                row = self.db.execute("SELECT payload FROM contract_versions WHERE contract_id=? AND revision=?", (contract_id, revision)).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        data["conditions"] = tuple(AcceptanceCondition(**condition) for condition in data["conditions"])
        return ContractVersion(**data)

    def list_contract_versions(self, task_id: str) -> list[ContractVersion]:
        with self._lock:
            rows = self.db.execute("SELECT payload FROM contract_versions WHERE task_id=? ORDER BY contract_id,revision", (task_id,)).fetchall()
        values = []
        for row in rows:
            data = json.loads(row["payload"])
            data["conditions"] = tuple(AcceptanceCondition(**condition) for condition in data["conditions"])
            values.append(ContractVersion(**data))
        return values

    def save_verification(self, verification: Verification) -> None:
        payload = self._json(verification.to_dict())
        with self._lock:
            candidate = self.db.execute("SELECT task_id FROM candidates WHERE candidate_id=?", (verification.candidate_id,)).fetchone()
            contract = self.db.execute("SELECT task_id FROM contract_versions WHERE contract_id=? AND revision=?",
                                       (verification.contract_id, verification.contract_revision)).fetchone()
            if not candidate:
                raise ValueError(f"candidate does not exist: {verification.candidate_id}")
            if not contract:
                raise ValueError(f"contract does not exist: {verification.contract_id}")
            if candidate["task_id"] != contract["task_id"]:
                raise ValueError("candidate and contract must belong to the same task")
            contract_revision = verification.contract_revision
            self.db.execute(
                "INSERT INTO verifications(verification_id,verification_key,candidate_id,contract_id,contract_revision,"
                "run_state,verdict,validity,review_decision,payload,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (verification.verification_id, verification.verification_key,
                             verification.candidate_id, verification.contract_id, contract_revision,
                             verification.run_state.value, verification.verdict.value,
                             verification.validity.value, verification.review_decision.value,
                             payload, verification.created_at))
            self.db.commit()

    def get_verification(self, verification_id: str) -> Verification | None:
        with self._lock:
            row = self.db.execute("SELECT payload FROM verifications WHERE verification_id=?", (verification_id,)).fetchone()
        return Verification(**json.loads(row["payload"])) if row else None

    def list_verifications(self, candidate_id: str | None = None, verification_key: str | None = None) -> list[Verification]:
        clauses, params = [], []
        if candidate_id is not None:
            clauses.append("candidate_id=?"); params.append(candidate_id)
        if verification_key is not None:
            clauses.append("verification_key=?"); params.append(verification_key)
        query = "SELECT payload FROM verifications" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at,verification_id"
        with self._lock:
            rows = self.db.execute(query, params).fetchall()
        return [Verification(**json.loads(row["payload"])) for row in rows]

    def save_check_execution(self, check: CheckExecution) -> None:
        """Append one immutable measured check for an existing verification."""
        payload = self._json(check.to_dict())
        with self._lock:
            if not self.db.execute(
                "SELECT 1 FROM verifications WHERE verification_id=?", (check.verification_id,)
            ).fetchone():
                raise ValueError(f"verification does not exist: {check.verification_id}")
            self.db.execute(
                "INSERT INTO check_executions(check_id,verification_id,suite_id,target,variant,outcome,count,payload,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (check.check_id, check.verification_id, check.suite_id, check.target,
                 check.variant, check.outcome.value, check.count, payload, check.created_at),
            )
            self.db.commit()

    def list_check_executions(self, verification_id: str) -> list[CheckExecution]:
        with self._lock:
            rows = self.db.execute(
                "SELECT payload FROM check_executions WHERE verification_id=? ORDER BY created_at,check_id",
                (verification_id,),
            ).fetchall()
        return [CheckExecution(**json.loads(row["payload"])) for row in rows]

    def save_finding(self, finding: Finding) -> None:
        """Append an immutable finding linked to one measured snapshot.

        ``INSERT OR IGNORE`` makes derivation idempotent while the immutable
        trigger prevents a later run from rewriting an earlier observation.
        A repeated execution receives a different verification snapshot and
        therefore produces a separate set of finding records.
        """
        payload = self._json(finding.to_dict())
        with self._lock:
            task = self.db.execute("SELECT 1 FROM tasks WHERE task_id=?", (finding.task_id,)).fetchone()
            verification = self.db.execute(
                "SELECT candidate_id,contract_id,contract_revision,verification_key FROM verifications WHERE verification_id=?",
                (finding.verification_id,),
            ).fetchone()
            candidate = self.db.execute(
                "SELECT task_id FROM candidates WHERE candidate_id=?", (finding.candidate_id,)
            ).fetchone()
            contract = self.db.execute(
                "SELECT task_id FROM contract_versions WHERE contract_id=? AND revision=?",
                (finding.contract_id, finding.contract_revision),
            ).fetchone()
            if not task or not verification or not candidate or not contract:
                raise ValueError("finding references unavailable immutable resources")
            if candidate["task_id"] != finding.task_id or contract["task_id"] != finding.task_id:
                raise ValueError("finding resources must belong to the same task")
            if verification["candidate_id"] != finding.candidate_id or verification["contract_id"] != finding.contract_id:
                raise ValueError("finding must reference its verification candidate and contract")
            if verification["contract_revision"] != finding.contract_revision:
                raise ValueError("finding contract revision does not match verification")
            if verification["verification_key"] != finding.verification_key:
                raise ValueError("finding verification key does not match verification")
            self.db.execute(
                "INSERT OR IGNORE INTO findings(finding_id,task_id,verification_id,verification_key,candidate_id,"
                "contract_id,contract_revision,kind,status,condition_id,source_variant,payload,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (finding.finding_id, finding.task_id, finding.verification_id,
                 finding.verification_key, finding.candidate_id, finding.contract_id,
                 finding.contract_revision, finding.kind, finding.status,
                 finding.condition_id, finding.source_variant, payload, finding.created_at),
            )
            self.db.commit()

    def get_finding(self, finding_id: str) -> Finding | None:
        with self._lock:
            row = self.db.execute("SELECT payload FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        return Finding(**json.loads(row["payload"])) if row else None

    def list_findings(
        self,
        task_id: str,
        *,
        candidate_id: str | None = None,
        condition_id: str | None = None,
        verification_id: str | None = None,
    ) -> list[Finding]:
        clauses = ["task_id=?"]
        params: list[str] = [task_id]
        if candidate_id is not None:
            clauses.append("candidate_id=?")
            params.append(candidate_id)
        if condition_id is not None:
            clauses.append("condition_id=?")
            params.append(condition_id)
        if verification_id is not None:
            clauses.append("verification_id=?")
            params.append(verification_id)
        query = "SELECT payload FROM findings WHERE " + " AND ".join(clauses) + " ORDER BY created_at,finding_id"
        with self._lock:
            rows = self.db.execute(query, params).fetchall()
        return [Finding(**json.loads(row["payload"])) for row in rows]

    @staticmethod
    def _finding_id(verification_id: str, marker: str) -> str:
        # A deterministic key gives derivation retry/idempotency without
        # conflating observations from distinct verification snapshots.
        digest = hashlib.sha256(f"{verification_id}:{marker}".encode("utf-8")).hexdigest()
        return f"finding_{digest}"

    def derive_findings_for_verification(self, verification_id: str) -> list[Finding]:
        """Materialize actionable findings from persisted checks and gaps.

        This is deliberately a conservative projection: a failed check is an
        observed failure, not a confirmed contract violation, because command
        checks do not carry a valid oracle or repeat evidence by themselves.
        Errors, flaky runs, skipped/unsupported checks and absent checks stay
        inconclusive or environmental findings and are never promoted to pass.
        """
        verification = self.get_verification(verification_id)
        if verification is None:
            raise ValueError(f"verification does not exist: {verification_id}")
        candidate = self.get_candidate(verification.candidate_id)
        if candidate is None:
            raise ValueError(f"candidate does not exist: {verification.candidate_id}")
        checks = self.list_check_executions(verification_id)
        expected_basis = f"contract:{verification.contract_id}@{verification.contract_revision}"
        findings: list[Finding] = []

        def make_finding(
            marker: str,
            *,
            kind: str,
            status: str,
            title: str,
            message: str,
            source_variant: str | None = None,
            check_ids: tuple[str, ...] = (),
            condition_id: str | None = None,
            details: dict[str, Any] | None = None,
            repeats: int = 1,
        ) -> None:
            finding = Finding(
                finding_id=self._finding_id(verification_id, marker),
                task_id=candidate.task_id,
                verification_id=verification.verification_id,
                verification_key=verification.verification_key,
                candidate_id=verification.candidate_id,
                contract_id=verification.contract_id,
                contract_revision=verification.contract_revision,
                kind=kind,
                status=status,
                title=title,
                message=message,
                condition_id=condition_id,
                expected_basis=expected_basis,
                source_variant=source_variant,
                check_ids=check_ids,
                evidence_refs=(f"verification:{verification.verification_id}", *(
                    f"check:{check_id}" for check_id in check_ids
                )),
                repeats=repeats,
                details=details or {},
            )
            self.save_finding(finding)
            findings.append(finding)

        represented_gaps: set[str] = set()
        for check in checks:
            if check.outcome.value == "pass":
                continue
            outcome = check.outcome.value
            condition_id = None
            raw_condition = check.details.get("condition_id") if isinstance(check.details, Mapping) else None
            if raw_condition:
                condition_id = str(raw_condition)
            details = {
                "suite_id": check.suite_id,
                "target": check.target,
                "outcome": outcome,
                "count": check.count,
                "return_code": check.return_code,
                "duration_ms": check.duration_ms,
                "runner_details": dict(check.details),
            }
            if check.variant == "base":
                kind = "baseline_failure" if outcome == "fail" else f"baseline_{outcome}"
                status = "baseline_issue" if outcome == "fail" else (
                    "environment_error" if outcome == "error" else "inconclusive"
                )
                title = "基线检查未通过" if outcome == "fail" else f"基线检查 {outcome}"
                message = f"基线 {check.target} 的 {check.suite_id} 结果为 {outcome}；候选结论不能绕过该基线证据。"
                represented_gaps.add("baseline_check_failed")
            else:
                kind = {
                    "fail": "candidate_failure",
                    "error": "candidate_error",
                    "flaky": "flaky",
                    "not_run": "not_run",
                    "skipped": "skipped",
                    "unsupported": "unsupported",
                }.get(outcome, "candidate_observation")
                status = {
                    "fail": "observed",
                    "error": "environment_error",
                    "flaky": "inconclusive",
                    "not_run": "inconclusive",
                    "skipped": "inconclusive",
                    "unsupported": "inconclusive",
                }.get(outcome, "inconclusive")
                title = {
                    "fail": "候选检查失败",
                    "error": "候选检查发生错误",
                    "flaky": "候选检查结果不稳定",
                    "not_run": "候选检查未运行",
                    "skipped": "候选检查被跳过",
                    "unsupported": "候选检查不受支持",
                }.get(outcome, f"候选检查 {outcome}")
                message = f"候选 {check.target} 的 {check.suite_id} 结果为 {outcome}；该观察尚未被提升为确定性需求违反。"
            make_finding(
                f"check:{check.check_id}", kind=kind, status=status,
                title=title, message=message, source_variant=check.variant,
                check_ids=(check.check_id,), condition_id=condition_id, details=details,
            )

        # Persist gaps that have no individual check row (for example a queued
        # verification, a missing baseline, or an execution that failed before
        # the runner started).  They are evidence-backed, not invented passes.
        candidate_checks = [item for item in checks if item.variant == "candidate"]
        base_checks = [item for item in checks if item.variant == "base"]
        gap_items = set(verification.gaps)
        if not candidate_checks:
            gap_items.add("candidate_checks_not_run")
        if not base_checks:
            gap_items.add("baseline_checks_not_run")
        for gap in sorted(gap_items):
            if gap == "baseline_check_failed" and any(item.variant == "base" and item.outcome.value != "pass" for item in checks):
                continue
            if gap == "candidate_checks_not_run" and candidate_checks:
                continue
            if gap == "baseline_checks_not_run" and base_checks:
                continue
            if gap in represented_gaps:
                continue
            if gap in {"verification_not_started", "candidate_checks_not_run", "zero_test_cases"}:
                kind, status, title = "not_run", "inconclusive", "验证尚未完整运行"
            elif gap in {"baseline_tests_unavailable", "baseline_checks_not_run"}:
                kind, status, title = "baseline_not_run", "inconclusive", "基线检查不可用"
            elif gap == "baseline_check_failed":
                kind, status, title = "baseline_failure", "baseline_issue", "基线检查未通过"
            elif "error" in gap or gap in {"incomplete_check_execution", "verification_worker_error"}:
                kind, status, title = "verification_error", "environment_error", "验证执行存在环境缺口"
            else:
                kind, status, title = "verification_gap", "inconclusive", "验证存在未完成项"
            make_finding(
                f"gap:{gap}", kind=kind, status=status, title=title,
                message=f"验证记录明确报告缺口：{gap}。该状态不能解释为通过。",
                source_variant=None, details={"gap": gap},
                repeats=0 if kind in {"not_run", "baseline_not_run"} else 1,
            )

        # Return the durable projection, including rows written by an earlier
        # retry.  This keeps callers from seeing duplicate in-memory objects.
        return self.list_findings(candidate.task_id, verification_id=verification_id)

    def ensure_findings_for_task(self, task_id: str) -> list[Finding]:
        """Derive findings for every persisted verification of a task."""
        candidates = self.list_candidates(task_id)
        for candidate in candidates:
            for verification in self.list_verifications(candidate_id=candidate.candidate_id):
                # The initial queued verification is a request for work, not
                # an observed execution.  Do not manufacture a ``not_run``
                # finding for it; incomplete state is surfaced by the API's
                # explicit ``complete=false`` response until a worker writes
                # a result snapshot or a measured check.
                if verification.run_state in {RunState.DRAFT, RunState.QUEUED, RunState.RUNNING} and not self.list_check_executions(verification.verification_id):
                    continue
                self.derive_findings_for_verification(verification.verification_id)
        return self.list_findings(task_id)

    def record_review_decision(self, record: ReviewDecisionRecord) -> None:
        """Append a human decision after checking the expected immutable key."""
        with self._lock:
            nested = self.db.in_transaction
            marker = "patchpilot_review"
            try:
                self.db.execute(f"SAVEPOINT {marker}" if nested else "BEGIN IMMEDIATE")
                row = self.db.execute("SELECT verification_key FROM verifications WHERE verification_id=?", (record.verification_id,)).fetchone()
                if not row:
                    raise ValueError(f"verification does not exist: {record.verification_id}")
                if row["verification_key"] != record.expected_key:
                    raise ValueError("expected_key does not match the verification being reviewed")
                self.db.execute("INSERT INTO review_decisions VALUES (?,?,?,?,?,?,?,?)",
                                (record.decision_id, record.verification_id, record.expected_key,
                                 record.decision.value, record.actor, record.reason,
                                 self._json(record.to_dict()), record.created_at))
                self.db.execute(f"RELEASE SAVEPOINT {marker}" if nested else "COMMIT")
            except Exception:
                if nested:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                    self.db.execute(f"RELEASE SAVEPOINT {marker}")
                else:
                    self.db.rollback()
                raise

    def list_review_decisions(self, verification_id: str) -> list[ReviewDecisionRecord]:
        with self._lock:
            rows = self.db.execute("SELECT payload FROM review_decisions WHERE verification_id=? ORDER BY rowid", (verification_id,)).fetchall()
        return [ReviewDecisionRecord(**json.loads(row["payload"])) for row in rows]

    def save_job(self, job_id: str, kind: str, state: str, payload: dict, *, created_at: str | None = None) -> None:
        """Persist a resumable job state; later writes update only its progress row."""
        now = created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        encoded = self._json(payload)
        with self._lock:
            self.db.execute(
                "INSERT INTO jobs(job_id,kind,state,payload,created_at,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET kind=excluded.kind,state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at",
                (job_id, kind, state, encoded, now, now),
            )
            self.db.commit()

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT job_id,kind,state,payload,created_at,updated_at FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        return {"job_id": row["job_id"], "kind": row["kind"], "state": row["state"],
                "payload": json.loads(row["payload"]), "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def reserve_idempotency(self, scope: str, idempotency_key: str, request_hash: str, response_ref: str | None = None) -> bool:
        """Insert an idempotency claim; return false for an existing key.

        A changed request hash for an existing key is rejected to prevent a
        client retry from silently reusing a different operation's result.
        """
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            row = self.db.execute("SELECT request_hash FROM idempotency_keys WHERE scope=? AND idempotency_key=?",
                                  (scope, idempotency_key)).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ValueError("idempotency key was reused with a different request")
                return False
            self.db.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?)",
                            (scope, idempotency_key, request_hash, response_ref, now))
            self.db.commit()
            return True

    def get_idempotency(self, scope: str, idempotency_key: str) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT scope,idempotency_key,request_hash,response_ref,created_at FROM idempotency_keys WHERE scope=? AND idempotency_key=?",
                                  (scope, idempotency_key)).fetchone()
        return dict(row) if row else None

    def save_run(self, run:Run):
        payload=json.dumps(run.to_dict(),ensure_ascii=False)
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)",(run.run_id,payload,time.time()))
            self.db.commit()

    def event(self,event:Event):
        # Reading the previous hash and inserting the new event must happen
        # under one lock/transaction.  Otherwise two concurrent requests can
        # both observe the same predecessor and fork the chain.
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                prev_hash = self._get_last_event_hash_unlocked(event.run_id)
                event.prev_hash = prev_hash
                event.event_hash = compute_event_hash(event, prev_hash)
                payload=json.dumps(event.to_dict(),ensure_ascii=False)
                self.db.execute("INSERT INTO events VALUES (?,?,?,?)",(event.event_id,event.run_id,event.ts,payload))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            # Keep the append serialized with the database commit so the JSONL
            # audit stream follows the same insertion order as SQLite.
            with self.jsonl.open("a",encoding="utf-8") as f:
                f.write(payload+"\n")

    def get_last_event_hash(self, run_id:str) -> str|None:
        """获取指定 run 的最后一个事件的哈希"""
        with self._lock:
            return self._get_last_event_hash_unlocked(run_id)

    def _get_last_event_hash_unlocked(self, run_id:str) -> str|None:
        # SQLite's implicit rowid is the serialized append sequence.  It is
        # the authoritative order for the hash chain: event timestamps are
        # captured before a worker acquires the lock and may therefore be out
        # of order under concurrent requests.
        row = self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)).fetchone()
        if not row:
            return None
        event_data = json.loads(row["payload"])
        return event_data.get('event_hash')

    def add_evidence(self,run_id:str,evidence:Evidence):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?)",(evidence.evidence_id,run_id,json.dumps(evidence.to_dict(),ensure_ascii=False)))
            self.db.commit()

    def add_artifact(self,artifact:Artifact):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?)",(artifact.artifact_id,artifact.run_id,json.dumps(artifact.to_dict(),ensure_ascii=False)))
            self.db.commit()

    def get_run(self,run_id):
        with self._lock:
            row=self.db.execute("SELECT payload FROM runs WHERE run_id=?",(run_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def list_runs(self,limit=100):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM runs ORDER BY updated_at DESC LIMIT ?",(limit,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_events(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM events WHERE run_id=? ORDER BY rowid",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_evidence(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM evidence WHERE run_id=?",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def list_artifacts(self,run_id):
        with self._lock:
            rows=self.db.execute("SELECT payload FROM artifacts WHERE run_id=?",(run_id,)).fetchall()
            return [json.loads(row["payload"]) for row in rows]

    def write_artifact(self,run_id,kind,content:bytes,filename:str,metadata=None):
        """Write one immutable artifact and retain a run-scoped metadata link.

        Artifact bytes may be content-identical across runs.  The old
        ``art_<digest>`` primary key combined with ``INSERT OR REPLACE`` meant
        that a later run silently replaced the earlier run's ``run_id`` and
        metadata.  Evidence delivery needs content-addressable bytes *and*
        independent run links, so the public artifact id now includes a safe
        run component while retaining the full SHA-256 in the record.

        ``filename`` is an internal artifact name, never a path.  Rejecting
        separators, absolute paths, NULs, and dot segments prevents callers
        from escaping ``root/<run_id>`` even if a future API exposes this
        helper to uploads.
        """
        safe_run_id = self._safe_component(run_id, "run_id")
        safe_filename = self._safe_filename(filename)
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError("artifact content must be bytes")
        raw = bytes(content)
        folder=self.root/safe_run_id
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            path=folder/safe_filename
            # Refuse a pre-existing symlink.  ``Path.write_bytes`` follows one
            # and could otherwise redirect evidence outside the artifact root.
            if path.is_symlink():
                raise ValueError("artifact filename resolves through a symbolic link")
            path.write_bytes(raw)
        digest=hashlib.sha256(raw).hexdigest()
        # Keep IDs deterministic for retries within one run, but isolate the
        # metadata row from identical content in another run.
        aid=f"art_{safe_run_id}_{digest[:16]}"
        a=Artifact(aid,kind,str(path),digest,len(raw),safe_run_id,metadata or {})
        self.add_artifact(a)
        return a

    @staticmethod
    def _safe_component(value: str, field: str) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError(f"{field} must be a non-empty safe identifier")
        if len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
            raise ValueError(f"{field} contains an unsafe path component")
        return value

    @classmethod
    def _safe_filename(cls, filename: str) -> str:
        if not isinstance(filename, str) or not filename or "\x00" in filename:
            raise ValueError("artifact filename must be non-empty")
        # Check both POSIX and Windows interpretations so a Linux host does
        # not accept a Windows traversal that becomes dangerous on export.
        posix = PurePosixPath(filename)
        windows = PureWindowsPath(filename)
        if posix.is_absolute() or windows.is_absolute() or windows.drive:
            raise ValueError("artifact filename must be relative")
        if "/" in filename or "\\" in filename or any(part in {"", ".", ".."} for part in posix.parts):
            raise ValueError("artifact filename must not contain path separators or dot segments")
        if len(filename) > 200:
            raise ValueError("artifact filename is too long")
        return filename

    def close(self):
        with self._lock:
            self.db.close()
