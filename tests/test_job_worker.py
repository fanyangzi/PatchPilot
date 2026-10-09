from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
import time

import pytest

from patchpilot.application.job_worker import DurableJobStore, DurableJobWorker, LeaseLost
from patchpilot.evidence.store import EvidenceStore


def _job(job_id: str = "job-1") -> dict:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"job_id": job_id, "kind": "verification", "state": "queued",
            "resource_id": "verification-1", "created_at": now, "updated_at": now,
            "request": {"suite": "unit"}}


def test_lease_token_fences_stale_worker_and_restart_requeues(tmp_path):
    store = EvidenceStore(tmp_path)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    queue.create(_job())
    first = queue.claim("worker-a", lease_seconds=1)
    assert first and first["state"] == "running" and first["lease_token"] == 1
    assert first["fencing_token"] == 1

    # Simulate a crashed worker by moving its lease into the past.  Recovery
    # returns the job to queued and the next owner receives a higher fence.
    expired = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    with queue.lock:
        queue.db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (expired, "job-1"))
        queue.db.commit()
    assert queue.recover_expired() == 1
    second = queue.claim("worker-b", lease_seconds=30)
    assert second and second["lease_token"] == 2 and second["fencing_token"] == 2 and second["attempt"] == 2
    with pytest.raises(LeaseLost):
        queue.complete("job-1", "worker-a", 1, state="completed")
    done = queue.complete("job-1", "worker-b", 2, state="completed", payload={"result": "measured"})
    assert done["state"] == "completed" and done["result"] == "measured"
    events = queue.events("job-1")
    assert events[1]["data"]["fencing_token"] == 1
    assert events[3]["data"]["fencing_token"] == 2
    assert [event["event"] for event in events] == ["queued", "claimed", "requeued", "claimed", "completed"]


def test_cancel_is_durable_and_running_worker_confirms_cleanup(tmp_path):
    store = EvidenceStore(tmp_path)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    queue.create(_job())
    claimed = queue.claim("worker-a", lease_seconds=30)
    assert claimed
    requested = queue.request_cancel("job-1")
    assert requested["state"] == "running" and requested["cancel_requested"] is True
    assert queue.is_cancel_requested("job-1", "worker-a", claimed["lease_token"])
    cancelled = queue.complete("job-1", "worker-a", claimed["lease_token"], state="completed", payload={"cleanup": "done"})
    assert cancelled["state"] == "cancelled"
    events = queue.events("job-1")
    assert [event["event"] for event in events][-2:] == ["cancel_requested", "cancelled"]


def test_worker_handler_error_and_events_are_persisted(tmp_path):
    store = EvidenceStore(tmp_path)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    queue.create(_job())
    worker = DurableJobWorker(queue, "worker-a", lease_seconds=30)
    result = worker.run_once(lambda job: (_ for _ in ()).throw(RuntimeError("runner failed")))
    assert result and result["state"] == "error"
    assert result["error"] == "runner failed"
    assert queue.events("job-1")[-1]["event"] == "error"


def test_event_cursor_replay_is_ordered(tmp_path):
    store = EvidenceStore(tmp_path)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    queue.create(_job())
    queue.append_event("job-1", "diagnostic", {"phase": "prepare"})
    queue.append_event("job-1", "diagnostic", {"phase": "run"})
    replay = queue.events("job-1", after=1)
    assert [item["seq"] for item in replay] == [2, 3]
    assert replay[0]["data"] == {"phase": "prepare"}


def _crash_after_claim(db_root: str) -> None:
    """Crash a real worker process after it has durably claimed a job."""
    store = EvidenceStore(db_root)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    assert queue.claim("worker-crashed", lease_seconds=1)
    os._exit(0)


def test_real_worker_process_restart_reclaims_expired_lease(tmp_path):
    """A fresh process recovers a crashed worker and publishes once."""
    root = str(tmp_path)
    store = EvidenceStore(root)
    queue = DurableJobStore(store.db, store._lock, table="jobs")
    queue.create(_job("job-process-restart"))

    first = multiprocessing.Process(target=_crash_after_claim, args=(root,))
    first.start()
    first.join(timeout=5)
    assert first.exitcode == 0
    # The lease is intentionally short; this is the persisted crash window.
    time.sleep(1.15)

    restarted_store = EvidenceStore(root)
    restarted_queue = DurableJobStore(restarted_store.db, restarted_store._lock, table="jobs")
    worker = DurableJobWorker(restarted_queue, "worker-restarted", lease_seconds=30)
    result = worker.run_once(lambda job: {"result": "recovered"})
    assert result and result["state"] == "completed" and result["result"] == "recovered"
    assert result["attempt"] == 2 and result["fencing_token"] == 2
    assert [event["event"] for event in restarted_queue.events("job-process-restart")] == [
        "queued", "claimed", "requeued", "claimed", "completed"
    ]
