"""Tests for evidence hash chain and verify command."""
from __future__ import annotations
import json
from pathlib import Path
from patchpilot.orchestrator import PatchPilot
from patchpilot.evidence.store import EvidenceStore
from patchpilot.domain.models import compute_event_hash, Event

def test_event_hash_chain():
    """Events form a valid hash chain with prev_hash and event_hash."""
    artifacts = Path('artifacts_test_verify')
    artifacts.mkdir(exist_ok=True)
    try:
        pp = PatchPilot(str(artifacts))
        task = pp.load_task('fixtures/tasks/issue-001-normal.yaml')
        run = pp.run_task(task)

        # Verify hash chain
        store = EvidenceStore(str(artifacts))
        events = store.list_events(run.run_id)

        assert len(events) > 0, "Should have events"

        prev_hash = None
        for event in events:
            # Check prev_hash matches previous event's hash
            assert event.get('prev_hash') == prev_hash, \
                f"Event {event['event_id']} prev_hash mismatch"

            # Recompute and verify event_hash
            expected_hash = compute_event_hash(Event(**event), prev_hash)
            actual_hash = event.get('event_hash')
            assert actual_hash == expected_hash, \
                f"Event {event['event_id']} hash mismatch"

            prev_hash = actual_hash

    finally:
        import shutil
        shutil.rmtree(artifacts, ignore_errors=True)

def test_verdict_hash_computed():
    """Run includes verdict_hash and all inputs needed for re-verification."""
    artifacts = Path('artifacts_test_verify2')
    artifacts.mkdir(exist_ok=True)
    try:
        pp = PatchPilot(str(artifacts))
        task = pp.load_task('fixtures/tasks/issue-001-normal.yaml')
        run = pp.run_task(task)

        assert run.verdict_hash is not None, "verdict_hash should be computed"
        assert len(run.verdict_hash) == 64, "verdict_hash should be SHA-256 hex"
        assert 'checks' in run.metrics, "metrics should contain checks"
        assert 'evidence_root_hash' in run.metrics, \
            "metrics should contain evidence_root_hash"
        assert 'commit' in run.metrics, "metrics should contain commit for re-verification"
        assert 'patch_sha256' in run.metrics, \
            "metrics should contain patch_sha256 for re-verification"

    finally:
        import shutil
        shutil.rmtree(artifacts, ignore_errors=True)

def test_tampering_detection():
    """Verify command detects tampered events."""
    artifacts = Path('artifacts_test_verify3')
    artifacts.mkdir(exist_ok=True)
    try:
        pp = PatchPilot(str(artifacts))
        task = pp.load_task('fixtures/tasks/issue-001-normal.yaml')
        run = pp.run_task(task)

        # Tamper with an event in SQLite
        store = EvidenceStore(str(artifacts))
        import sqlite3
        db = sqlite3.connect(str(artifacts / 'patchpilot.sqlite3'))
        events = store.list_events(run.run_id)
        target_event = events[2]  # Tamper with third event

        # Modify event_hash in database
        tampered_payload = json.dumps({**target_event, 'event_hash': 'TAMPERED'})
        db.execute("UPDATE events SET payload=? WHERE event_id=?",
                   (tampered_payload, target_event['event_id']))
        db.commit()
        db.close()

        # Verify should detect tampering
        events_after = store.list_events(run.run_id)
        prev_hash = None
        tamper_detected = False

        for event in events_after:
            expected_hash = compute_event_hash(Event(**event), prev_hash)
            actual_hash = event.get('event_hash')
            if actual_hash != expected_hash:
                tamper_detected = True
                break
            prev_hash = actual_hash

        assert tamper_detected, "Tampering should be detected"

    finally:
        import shutil
        shutil.rmtree(artifacts, ignore_errors=True)

def test_evidence_parents():
    """Evidence records have proper parent relationships."""
    artifacts = Path('artifacts_test_verify4')
    artifacts.mkdir(exist_ok=True)
    try:
        pp = PatchPilot(str(artifacts))
        task = pp.load_task('fixtures/tasks/issue-001-normal.yaml')
        run = pp.run_task(task)

        store = EvidenceStore(str(artifacts))
        evidence = store.list_evidence(run.run_id)

        # Find reproduction evidence (root)
        repro = [e for e in evidence if e['kind'] == 'reproduction'][0]
        assert repro['parents'] == [], "Reproduction should be root evidence"

        # Find localization evidence (should reference reproduction)
        loc = [e for e in evidence if e['kind'] == 'localization'][0]
        assert repro['evidence_id'] in loc['parents'], \
            "Localization should reference reproduction"

        # Find verification evidence (should reference reproduction)
        verif = [e for e in evidence if e['kind'] == 'verification'][0]
        assert repro['evidence_id'] in verif['parents'], \
            "Verification should reference reproduction"

    finally:
        import shutil
        shutil.rmtree(artifacts, ignore_errors=True)
