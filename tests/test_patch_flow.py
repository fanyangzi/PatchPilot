"""Patch files, test_patch, workspace and path-matching behaviour of the engine."""
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest
from patchpilot.orchestrator import PatchPilot
from patchpilot.verifier.diff import matches_pattern


@pytest.mark.parametrize('path,pattern,expected', [
    ('src/click/_utils.py', 'src/click/', True), ('src/clicker/x.py', 'src/click/', False),
    ('src/x.py', 'src/', True), ('id.key', '**/*.key', True), ('a/b/id.key', '**/*.key', True),
    ('a.pem', '*.pem', True), ('a/b.pem', '*.pem', False),
    ('.github/workflows/ci.yml', '.github/workflows/**', True),
    ('src/a.py', 'src/*.py', True), ('src/s/a.py', 'src/*.py', False),
    ('src/s/a.py', 'src/**/*.py', True), ('src/a.py', 'src/**/*.py', True),
    ('Jenkinsfile', 'Jenkinsfile', True), ('x/Jenkinsfile', 'Jenkinsfile', False),
])
def test_matches_pattern(path, pattern, expected):
    assert matches_pattern(path, pattern) is expected


def test_load_task_reads_patch_files_and_keeps_urls(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('[project]\nname="x"\n')
    (tmp_path / 'p.diff').write_text('--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n')
    (tmp_path / 't.diff').write_text('--- a/t\n+++ b/t\n@@ -1 +1 @@\n-a\n+b\n')
    y = tmp_path / 'tasks' / 'x.yaml'; y.parent.mkdir()
    y.write_text('task_id: x\nrepo: https://example.com/o/r.git\ncommit: abc\nissue_title: t\n'
                 'patch_files: [p.diff]\ntest_patch_file: t.diff\ntarget_tests: ["t.py::a"]\n')
    task = PatchPilot(str(tmp_path / 'art')).load_task(y)
    assert task.repo == 'https://example.com/o/r.git'
    assert task.patches[0].startswith('--- a/f') and task.patch_sources == ['p.diff']
    assert task.test_patch.startswith('--- a/t') and task.target_tests == ['t.py::a']


def test_load_task_rejects_patch_outside_root(tmp_path):
    (tmp_path / 'pyproject.toml').write_text('')
    y = tmp_path / 'x.yaml'
    y.write_text('task_id: x\nrepo: r\nissue_title: t\npatch_files: ["../etc.diff"]\n')
    with pytest.raises(ValueError):
        PatchPilot(str(tmp_path / 'art')).load_task(y)


def test_git_apply_roundtrip(tmp_path):
    (tmp_path / 'f.txt').write_text('a\n')
    diff = '--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-a\n+b\n'
    assert PatchPilot._git_apply(tmp_path, diff).returncode == 0
    assert (tmp_path / 'f.txt').read_text() == 'b\n'
    assert PatchPilot._git_apply(tmp_path, diff).returncode != 0
    assert PatchPilot._git_apply(tmp_path, diff, reverse=True).returncode == 0
    assert (tmp_path / 'f.txt').read_text() == 'a\n'


def test_repo_label_strips_credentials():
    assert PatchPilot._repo_label('https://user:tok@github.com/o/r.git') == 'https://github.com/o/r.git'
    assert PatchPilot._repo_label('/abs/path/fixtures/repos/buggy_calculator') == 'patchpilot/buggy_calculator'


def test_unreachable_commit_is_infra_error_not_verdict(tmp_path):
    repo = tmp_path / 'r'; repo.mkdir()
    for c in (['init', '-q'], ['-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-q', '--allow-empty', '-m', 'x']):
        subprocess.run(['git', '-C', str(repo), *c], check=True, capture_output=True)
    y = tmp_path / 'pyproject.toml'; y.write_text('')
    t = tmp_path / 't.yaml'
    t.write_text(f'task_id: x\nrepo: {repo}\ncommit: deadbeef00\nissue_title: t\n')
    pp = PatchPilot(str(tmp_path / 'art'))
    run = pp.run_task(pp.load_task(t))
    assert run.conclusion == 'INFRA_ERROR'


def test_failure_fixture_retries_with_second_patch_file():
    pp = PatchPilot('artifacts_test_patch_flow')
    try:
        task = pp.load_task('fixtures/tasks/issue-007-failure-calculator-retry.yaml')
        assert len(task.patches) == 2
        run = pp.run_task(task)
        assert run.conclusion == 'TRUSTED_DELIVERY' and run.metrics['attempts'] == 2
        assert run.metrics['recovery_success_rate'] == 1
    finally:
        import shutil; shutil.rmtree('artifacts_test_patch_flow', ignore_errors=True)
