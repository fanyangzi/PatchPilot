from __future__ import annotations

import subprocess

import pytest

from patchpilot.application.verification_service import (
    VerificationExecutionError,
    WorkspaceResolutionError,
    WorkspaceResolver,
    VerificationService,
)


def _git(repo):
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "security@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "PatchPilot security tests"], cwd=repo, check=True)


def test_git_archive_rejects_symlink_entries(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo)
    (repo / "target.txt").write_text("safe", encoding="utf-8")
    (repo / "link.txt").symlink_to("target.txt")
    subprocess.run(["git", "add", "target.txt", "link.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "symlink"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    with pytest.raises(VerificationExecutionError, match="unsupported link"):
        VerificationService._archive_commit(repo, sha, tmp_path / "workspace")


def test_workspace_resolver_rejects_symlinked_path_escaping_allowlist(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    _git(outside)
    link = allowed / "repo-link"
    link.symlink_to(outside, target_is_directory=True)
    resolver = WorkspaceResolver([allowed])
    with pytest.raises(WorkspaceResolutionError, match="outside configured"):
        resolver.resolve(repo_path=link)


def test_candidate_workspace_symlink_is_rejected(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "outside").write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to("outside")
    with pytest.raises(VerificationExecutionError, match="unsupported symbolic link"):
        VerificationService._reject_symlinks(workspace)
