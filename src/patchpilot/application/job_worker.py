"""Durable SQLite job queue primitives used by the local PatchPilot worker.

The queue is deliberately small but has the properties that matter for the
acceptance flow: claims are fenced by a monotonically increasing lease token,
expired claims become available after a process restart, cancellation is
persisted, and every state transition has an append-only event.  It is an
at-least-once queue; callers must make external side effects idempotent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import socket
import threading
import uuid
from typing import Any, Callable, Mapping


TERMINAL_STATES = frozenset({"completed", "cancelled", "error"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


class LeaseLost(RuntimeError):
    """Raised when a stale worker attempts a fenced write."""


class DurableJobStore:
    """A lease/fencing aware job store backed by a DB-API SQLite connection.

    ``db`` and ``lock`` are intentionally injected so the queue can share the
    existing :class:`EvidenceStore` connection and transaction lock.  The
    default table is ``api_jobs`` because those are the jobs exposed by the
    versioned HTTP API; the class also works with the legacy ``jobs`` table.
    """

    def __init__(self, db, lock: threading.RLock, *, table: str = "api_jobs"):
        if table not in {"api_jobs", "jobs"}:
            raise ValueError("unsupported job table")
        self.db = db
        self.lock = lock
        self.table = table
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Add queue columns to an existing local database, idempotently."""
        with self.lock:
            columns = {row["name"] for row in self.db.execute(f"PRAGMA table_info({self.table})").fetchall()}
            additions = {
                "resource_id": "TEXT",
                "lease_owner": "TEXT",
                "lease_token": "INTEGER NOT NULL DEFAULT 0",
                "lease_until": "TEXT",
                "heartbeat_at": "TEXT",
                "attempt": "INTEGER NOT NULL DEFAULT 0",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "event_seq": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE {self.table} ADD COLUMN {name} {definition}")
            # A separate event stream permits reconnect/replay without making
            # the mutable job row the source of truth for history.
                cursor = self.db.execute(
                "CREATE TABLE IF NOT EXISTS api_job_events("
                "job_id TEXT NOT NULL, seq INTEGER NOT NULL, event_type TEXT NOT NULL,"
                "payload TEXT NOT NULL, created_at TEXT NOT NULL,"
                "PRIMARY KEY(job_id, seq))"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS ix_api_job_events_job ON api_job_events(job_id, seq)")
            self.db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _payload(row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # Runtime fields are returned alongside the original request payload;
        # this makes GET /jobs useful after a restart without rewriting callers.
        for name in ("job_id", "kind", "state", "resource_id", "created_at", "updated_at",
                     "lease_owner", "lease_token", "lease_until", "heartbeat_at", "attempt",
                     "cancel_requested", "event_seq"):
            if name in row.keys():
                payload[name] = row[name]
        payload["cancel_requested"] = bool(payload.get("cancel_requested"))
        return payload

    def _row(self, job_id: str):
        return self.db.execute(f"SELECT * FROM {self.table} WHERE job_id=?", (job_id,)).fetchone()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self._row(job_id)
            return self._payload(row) if row else None

    def _append_event_unlocked(self, job_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        row = self._row(job_id)
        if not row:
            raise KeyError(job_id)
        seq = int(row["event_seq"] or 0) + 1
        created = utc_now()
        body = dict(payload or {})
        self.db.execute(
            "INSERT INTO api_job_events(job_id,seq,event_type,payload,created_at) VALUES (?,?,?,?,?)",
            (job_id, seq, event_type, self._json(body), created),
        )
        self.db.execute(f"UPDATE {self.table} SET event_seq=?, updated_at=? WHERE job_id=?", (seq, created, job_id))
        return {"job_id": job_id, "seq": seq, "event": event_type, "data": body, "created_at": created}

    def append_event(self, job_id: str, event_type: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                event = self._append_event_unlocked(job_id, event_type, payload)
                self.db.commit()
                return event
            except Exception:
                self.db.rollback()
                raise

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        with self.lock:
            rows = self.db.execute(
                "SELECT job_id,seq,event_type,payload,created_at FROM api_job_events "
                "WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?", (job_id, max(0, int(after)), limit)
            ).fetchall()
        return [{"job_id": row["job_id"], "seq": row["seq"], "event": row["event_type"],
                 "data": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]

    def create(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Insert a queued job, preserving the caller's request payload."""
        required = ("job_id", "kind", "created_at", "updated_at")
        if any(not payload.get(name) for name in required):
            raise ValueError("job_id, kind, created_at and updated_at are required")
        data = dict(payload)
        data.setdefault("state", "queued")
        data.setdefault("lease_owner", None)
        data.setdefault("lease_token", 0)
        data.setdefault("lease_until", None)
        data.setdefault("heartbeat_at", None)
        data.setdefault("attempt", 0)
        data.setdefault("cancel_requested", 0)
        data.setdefault("event_seq", 0)
        encoded = self._json(payload)
        with self.lock:
            try:
                # Insert and its first event share one transaction.  This
                # prevents a worker from claiming a job in the tiny window
                # between the row commit and the initial event append.
                self.db.execute("BEGIN IMMEDIATE")
                cursor = self.db.execute(
                    f"INSERT INTO {self.table}(job_id,kind,state,resource_id,payload,created_at,updated_at,"
                    "lease_owner,lease_token,lease_until,heartbeat_at,attempt,cancel_requested,event_seq) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (data["job_id"], data["kind"], data["state"], data.get("resource_id"), encoded,
                     data["created_at"], data["updated_at"], data["lease_owner"], data["lease_token"],
                     data["lease_until"], data["heartbeat_at"], data["attempt"], int(bool(data["cancel_requested"])), data["event_seq"]),
                )
                self._append_event_unlocked(
                    data["job_id"],
                    data["state"] if data["state"] in TERMINAL_STATES else "queued",
                    {"kind": data["kind"], "resource_id": data.get("resource_id")},
                )
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        return self.get(data["job_id"]) or data

    def claim(self, worker_id: str | None = None, *, job_id: str | None = None, lease_seconds: int = 60) -> dict[str, Any] | None:
        owner = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex}"
        if not owner or len(owner) > 200:
            raise ValueError("worker_id must be non-empty and <= 200 characters")
        duration = max(1, min(int(lease_seconds), 3600))
        now = utc_now()
        until = (datetime.now(timezone.utc) + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z")
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                if job_id:
                    row = self.db.execute(
                        f"SELECT * FROM {self.table} WHERE job_id=? AND (state='queued' OR "
                        "(state='running' AND lease_until IS NOT NULL AND lease_until<=?))", (job_id, now)
                    ).fetchone()
                else:
                    row = self.db.execute(
                        f"SELECT * FROM {self.table} WHERE state='queued' OR "
                        "(state='running' AND lease_until IS NOT NULL AND lease_until<=?) "
                        "ORDER BY created_at,job_id LIMIT 1", (now,)
                    ).fetchone()
                if not row:
                    self.db.rollback()
                    return None
                current = self._payload(row)
                if bool(current.get("cancel_requested")):
                    self.db.execute(
                        f"UPDATE {self.table} SET state='cancelled', lease_owner=NULL, lease_until=NULL, "
                        "heartbeat_at=?, updated_at=? WHERE job_id=?", (now, now, row["job_id"])
                    )
                    self._append_event_unlocked(row["job_id"], "cancelled", {"reason": "cancel_requested_before_claim"})
                    self.db.commit()
                    return self.get(row["job_id"])
                token = int(row["lease_token"] or 0) + 1
                attempt = int(row["attempt"] or 0) + 1
                cursor = self.db.execute(
                    f"UPDATE {self.table} SET state='running', lease_owner=?, lease_token=?, lease_until=?, "
                    "heartbeat_at=?, attempt=?, updated_at=? WHERE job_id=? AND (state='queued' OR "
                    "(state='running' AND lease_until IS NOT NULL AND lease_until<=?))",
                    (owner, token, until, now, attempt, now, row["job_id"], now),
                )
                if cursor.rowcount != 1:
                    self.db.rollback()
                    return None
                self._append_event_unlocked(row["job_id"], "claimed", {"worker_id": owner, "lease_token": token, "attempt": attempt, "lease_until": until})
                self.db.commit()
                return self.get(row["job_id"])
            except Exception:
                self.db.rollback()
                raise

    def heartbeat(self, job_id: str, worker_id: str, lease_token: int, *, lease_seconds: int = 60) -> bool:
        now = utc_now()
        until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, min(int(lease_seconds), 3600)))).isoformat().replace("+00:00", "Z")
        with self.lock:
            cursor = self.db.execute(
                f"UPDATE {self.table} SET lease_until=?,heartbeat_at=?,updated_at=? WHERE job_id=? AND state='running' "
                "AND lease_owner=? AND lease_token=? AND lease_until IS NOT NULL AND lease_until>?",
                (until, now, now, job_id, worker_id, int(lease_token), now),
            )
            ok = cursor.rowcount == 1
            if ok:
                self._append_event_unlocked(job_id, "heartbeat", {"worker_id": worker_id, "lease_token": int(lease_token), "lease_until": until})
            self.db.commit()
            return ok

    def is_cancel_requested(self, job_id: str, worker_id: str | None = None, lease_token: int | None = None) -> bool:
        with self.lock:
            row = self._row(job_id)
            if not row:
                raise KeyError(job_id)
            if worker_id is not None and (row["lease_owner"] != worker_id or int(row["lease_token"] or 0) != int(lease_token or -1)):
                raise LeaseLost(f"job {job_id} is fenced")
            return bool(row["cancel_requested"])

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self._row(job_id)
                if not row:
                    self.db.rollback()
                    return None
                state = row["state"]
                if state in TERMINAL_STATES:
                    self.db.rollback()
                    return self._payload(row)
                if state == "queued":
                    self.db.execute(
                        f"UPDATE {self.table} SET state='cancelled',cancel_requested=1,updated_at=? WHERE job_id=? AND state='queued'",
                        (now, job_id),
                    )
                    self._append_event_unlocked(job_id, "cancelled", {"reason": "cancelled_before_claim"})
                else:
                    self.db.execute(f"UPDATE {self.table} SET cancel_requested=1,updated_at=? WHERE job_id=? AND state='running'", (now, job_id))
                    self._append_event_unlocked(job_id, "cancel_requested", {})
                self.db.commit()
                return self.get(job_id)
            except Exception:
                self.db.rollback()
                raise

    def complete(self, job_id: str, worker_id: str, lease_token: int, *, state: str = "completed", payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if state not in TERMINAL_STATES:
            raise ValueError(f"terminal state required, got {state}")
        now = utc_now()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                row = self._row(job_id)
                lease_until = _parse_time(row["lease_until"]) if row else None
                if (
                    not row
                    or row["state"] != "running"
                    or row["lease_owner"] != worker_id
                    or int(row["lease_token"] or 0) != int(lease_token)
                    or lease_until is None
                    or lease_until <= datetime.now(timezone.utc)
                ):
                    self.db.rollback()
                    raise LeaseLost(f"job {job_id} is fenced")
                cancel_requested = bool(row["cancel_requested"])
                final_state = "cancelled" if cancel_requested and state == "completed" else state
                existing = self._payload(row)
                if payload:
                    existing.update(dict(payload))
                existing.update({"state": final_state, "updated_at": now, "lease_owner": None, "lease_until": None, "heartbeat_at": now})
                cursor = self.db.execute(
                    f"UPDATE {self.table} SET state=?,payload=?,lease_owner=NULL,lease_until=NULL,heartbeat_at=?,updated_at=? "
                    "WHERE job_id=? AND state='running' AND lease_owner=? AND lease_token=? AND lease_until>?",
                    (final_state, self._json({k: v for k, v in existing.items() if k not in {"job_id", "kind", "state", "resource_id", "created_at", "updated_at", "lease_owner", "lease_token", "lease_until", "heartbeat_at", "attempt", "cancel_requested", "event_seq"}}), now, now, job_id, worker_id, int(lease_token), now),
                )
                if cursor.rowcount != 1:
                    self.db.rollback()
                    raise LeaseLost(f"job {job_id} is fenced")
                event_type = final_state if final_state != "cancelled" else "cancelled"
                self._append_event_unlocked(job_id, event_type, {"worker_id": worker_id, "lease_token": int(lease_token), "cancel_requested": cancel_requested})
                self.db.commit()
                result = self.get(job_id)
                if result is None:
                    raise LeaseLost(f"job {job_id} disappeared")
                return result
            except Exception:
                if self.db.in_transaction:
                    self.db.rollback()
                raise

    def recover_expired(self, *, now: str | None = None) -> int:
        """Return expired running jobs to the queue after a worker restart."""
        stamp = now or utc_now()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                rows = self.db.execute(
                    f"SELECT job_id,attempt,cancel_requested FROM {self.table} WHERE state='running' AND lease_until IS NOT NULL AND lease_until<=?",
                    (stamp,),
                ).fetchall()
                for row in rows:
                    if bool(row["cancel_requested"]):
                        state = "cancelled"
                        event = "cancelled"
                    else:
                        state = "queued"
                        event = "requeued"
                    self.db.execute(
                        f"UPDATE {self.table} SET state=?,lease_owner=NULL,lease_until=NULL,heartbeat_at=NULL,updated_at=? WHERE job_id=? AND state='running'",
                        (state, stamp, row["job_id"]),
                    )
                    self._append_event_unlocked(row["job_id"], event, {"reason": "lease_expired", "attempt": int(row["attempt"] or 0)})
                self.db.commit()
                return len(rows)
            except Exception:
                self.db.rollback()
                raise


class DurableJobWorker:
    """Run one leased job at a time; safe to call from a process loop."""

    def __init__(self, queue: DurableJobStore, worker_id: str | None = None, *, lease_seconds: int = 60):
        self.queue = queue
        self.worker_id = worker_id or f"worker:{socket.gethostname()}:{uuid.uuid4().hex}"
        self.lease_seconds = max(1, min(int(lease_seconds), 3600))

    def run_once(self, handler: Callable[[dict[str, Any]], Mapping[str, Any] | None], *, job_id: str | None = None) -> dict[str, Any] | None:
        self.queue.recover_expired()
        job = self.queue.claim(self.worker_id, job_id=job_id, lease_seconds=self.lease_seconds)
        if not job:
            return None
        if job.get("state") == "cancelled":
            return job
        token = int(job["lease_token"])
        try:
            if self.queue.is_cancel_requested(job["job_id"], self.worker_id, token):
                return self.queue.complete(job["job_id"], self.worker_id, token, state="cancelled", payload={"cancelled_before_start": True})
            result = handler(job) or {}
            return self.queue.complete(job["job_id"], self.worker_id, token, state=str(result.get("state", "completed")), payload=result)
        except LeaseLost:
            raise
        except Exception as exc:
            try:
                return self.queue.complete(job["job_id"], self.worker_id, token, state="error", payload={"error": str(exc)[:2000]})
            except LeaseLost:
                raise


__all__ = ["DurableJobStore", "DurableJobWorker", "LeaseLost", "TERMINAL_STATES"]
