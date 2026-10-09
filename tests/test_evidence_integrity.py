from __future__ import annotations

import json

from patchpilot.cli import main
from patchpilot.evidence.store import EvidenceStore


def test_bundle_check_validates_digests_and_fails_closed_on_corruption(tmp_path, capsys):
    store = EvidenceStore(tmp_path)
    artifact = store.write_artifact("run-integrity", "report", b"immutable evidence", "report.md")

    assert main(["bundle", "check", "--run-id", "run-integrity", "--artifacts", str(tmp_path), "--json"]) == 0
    valid = json.loads(capsys.readouterr().out)
    assert valid["valid"] is True
    assert valid["artifact_count"] == 1

    # Simulate post-export corruption.  The CLI must refuse the bundle rather
    # than trusting the recorded digest or returning a successful status.
    with open(artifact.path, "wb") as handle:
        handle.write(b"tampered")
    assert main(["bundle", "check", "--run-id", "run-integrity", "--artifacts", str(tmp_path), "--json"]) == 1
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["valid"] is False
    assert invalid["failures"][0]["error"] == "artifact digest mismatch"


def test_bundle_listing_does_not_emit_host_absolute_paths(tmp_path, capsys):
    store = EvidenceStore(tmp_path)
    store.write_artifact("run-list", "report", b"ok", "report.md")
    assert main(["bundle", "--run-id", "run-list", "--artifacts", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["files"] == ["report.md"]
    assert str(tmp_path) not in output
