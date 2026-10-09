#!/usr/bin/env python3
"""Run a real PatchPilot acceptance flow in a disposable git repository.

The script uses the public v1 HTTP surface through FastAPI's in-process
client. It creates a temporary base commit, imports a candidate patch, freezes
a sourced acceptance contract, executes both base and candidate test trees,
renders an immutable report, and writes a self-contained evidence bundle.

This is a clean-room smoke/reproduction aid, not a benchmark. The generated
repository and its result are marked as clean-room data in ``manifest.json``.
The command fails closed when execution is incomplete or the measured verdict
is not accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _expect(response: Any, status: int, label: str) -> dict[str, Any]:
    if response.status_code != status:
        try:
            body = response.json()
        except Exception:
            body = response.text[:2000]
        raise RuntimeError(f"{label}: expected HTTP {status}, got {response.status_code}: {body}")
    return response.json()


def _write_bundle(output: Path, files: dict[str, str]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    archive = output.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        for name in sorted(files):
            tar.add(output / name, arcname=name, recursive=False)
    return archive


def _prepare_repo(parent: Path) -> tuple[Path, str, str]:
    repo = parent / "acceptance-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "patchpilot@example.test")
    _git(repo, "config", "user.name", "PatchPilot reproduction")
    (repo / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a - b\n",
        encoding="utf-8",
    )
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\n"
        "def test_add():\n    assert add(2, 1) == 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "calc.py", "test_calc.py")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()
    # The candidate changes only a comment. Both versions execute the same
    # real test while still exercising git archive and git apply.
    with (repo / "calc.py").open("a", encoding="utf-8") as handle:
        handle.write("\n# candidate instrumentation\n")
    patch = _git(repo, "diff", "--", "calc.py")
    if not patch.strip():
        raise RuntimeError("could not create a candidate patch")
    return repo, base_sha, patch


def run(output: Path) -> Path:
    if shutil.which("git") is None:
        raise RuntimeError("git is required")
    if shutil.which("pytest") is None:
        raise RuntimeError("pytest is required; install the dev extra before running this script")

    output = output.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    # Configure the API before importing it. No remote model or credential is
    # required for this path.
    os.environ.setdefault("PATCHPILOT_REMOTE_PLANNING", "0")
    os.environ.setdefault("PATCHPILOT_USE_REMOTE_PATCH", "0")
    os.environ.setdefault("PATCHPILOT_UNSAFE_LOCAL", "1")
    artifact_root = output / "runtime-artifacts"
    os.environ["PATCHPILOT_ARTIFACT_ROOT"] = str(artifact_root)

    sys.path.insert(0, str(ROOT / "src"))
    try:
        from fastapi.testclient import TestClient
        import patchpilot.api as api
    except ImportError as exc:
        raise RuntimeError(
            "FastAPI and its test client are required; run "
            "python -m pip install -e '.[api,dev]'"
        ) from exc

    with tempfile.TemporaryDirectory(prefix="patchpilot-clean-room-") as temp:
        parent = Path(temp)
        repo, base_sha, patch = _prepare_repo(parent)
        os.environ["PATCHPILOT_WORKSPACE_ROOTS"] = str(parent)
        client = TestClient(api.app)

        intake = _expect(client.post("/api/v1/intakes", json={
            "mode": "local_patch",
            "repo_id": "example/acceptance-repo",
            "base_ref": base_sha,
            "patch_text": patch,
            "issue_title": "Preserve calculator behavior",
            "issue_body": "The acceptance run must compare the pinned base and candidate trees.",
            "source_refs": ["local:reproduction/issue-1"],
        }), 202, "create intake")
        intake_id = intake["intake_id"]

        task = _expect(client.post("/api/v1/tasks", json={"intake_id": intake_id, "mode": "verify"}), 201, "create task")["task"]
        task_id = task["task_id"]
        candidate = _expect(client.post(f"/api/v1/tasks/{task_id}/candidates", json={
            "source": "upload", "base_sha": base_sha, "patch_text": patch,
        }), 201, "create candidate")["candidate"]
        contract = _expect(client.post(f"/api/v1/tasks/{task_id}/contracts/draft", json={
            "source_ids": ["local:reproduction/issue-1"],
            "base_snapshot_id": base_sha,
            "model_profile": "manual",
            "conditions": [{
                "condition_id": "AC-LOCAL-01",
                "kind": "preserve",
                "statement": "The candidate preserves the calculator behavior.",
                "source_refs": ["local:reproduction/issue-1"],
                "required": True,
                "oracle": {"type": "pytest", "command": ["pytest", "-q"]},
            }],
        }), 202, "draft contract")["contract"]
        frozen = _expect(client.post(f"/api/v1/contracts/{contract['contract_id']}/freeze", json={
            "expected_revision": contract["revision"],
            "confirmed_condition_ids": ["AC-LOCAL-01"],
            "actor": "clean-room-reproduction",
        }), 201, "freeze contract")["contract"]
        queued = _expect(client.post(f"/api/v1/tasks/{task_id}/verifications", json={
            "candidate_id": candidate["candidate_id"],
            "contract_id": frozen["contract_id"],
            "suite_id": "pytest-default",
            "environment_id": "clean-room-local",
            "policy_id": "policy-default",
            "baseline_tests_hash": hashlib.sha256(b"pytest -q").hexdigest(),
            "command_argv": [["pytest", "-q"]],
            "verifier_revision": "patchpilot-local-reproduction",
        }), 202, "queue verification")["verification"]

        executed = _expect(client.post(
            f"/api/v1/verifications/{queued['verification_id']}/execute",
            json={"repo_path": str(repo), "suite_id": "pytest-default", "command_argv": [["pytest", "-q"]]},
        ), 201, "execute verification")
        result = executed["verification"]
        if result["run_state"] != "completed" or result["verdict"] != "accepted_within_scope":
            raise RuntimeError(
                "execution did not produce an accepted measured result: "
                f"run_state={result.get('run_state')} verdict={result.get('verdict')} gaps={result.get('gaps')}"
            )
        checks = _expect(client.get(f"/api/v1/verifications/{result['verification_id']}/checks"), 200, "read checks")
        report_meta = _expect(client.post(f"/api/v1/verifications/{result['verification_id']}/reports", json={"format": "markdown"}), 201, "create report")["report"]
        report = _expect(client.get(f"/api/v1/reports/{report_meta['report_id']}/content"), 200, "read report")
        report_content = report["content"]
        if result["verification_key"] not in report_content:
            raise RuntimeError("report does not contain the immutable verification key")

        manifest = {
            "bundle_schema": "patchpilot/evidence-bundle-v1",
            "data_origin": "clean_room_generated_repository",
            "measured": True,
            "network": "disabled_by_configuration",
            "credentials_included": False,
            "task_id": task_id,
            "candidate_id": candidate["candidate_id"],
            "queued_verification_id": queued["verification_id"],
            "result_verification_id": result["verification_id"],
            "verification_key": result["verification_key"],
            "run_state": result["run_state"],
            "verdict": result["verdict"],
            "gaps": result["gaps"],
            "checks_complete": checks["complete"],
            "report_id": report_meta["report_id"],
            "report_sha256": report["content_sha"],
            "base_sha": base_sha,
            "candidate_patch_sha256": candidate["patch_hash"],
            "commands": [["pytest", "-q"]],
        }
        files = {
            "manifest.json": _json(manifest),
            "verification.json": _json(executed),
            "checks.json": _json(checks),
            "report.md": report_content,
        }
        archive = _write_bundle(output / "evidence-bundle", files)
        summary = {
            "status": "verified",
            "bundle_dir": str(output / "evidence-bundle"),
            "bundle_archive": str(archive),
            "verification_key": result["verification_key"],
            "verdict": result["verdict"],
            "checks": len(checks["items"]),
            "credentials_included": False,
        }
        (output / "summary.json").write_text(_json(summary), encoding="utf-8")
        return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.mkdtemp(prefix="patchpilot-reproduction-")),
        help="empty directory for the evidence bundle (default: a temporary directory)",
    )
    args = parser.parse_args(argv)
    try:
        output = run(args.output)
    except Exception as exc:
        print(f"reproduction failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified local acceptance flow; evidence bundle: {output / 'evidence-bundle.tar.gz'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
