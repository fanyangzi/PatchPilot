"""Real verification checks based on measured test results and diff analysis."""
from __future__ import annotations
from pathlib import Path
from ..domain import FailureClass, FailureState, VerifierResult
from .diff import parse_unified_diff, parse_name_status, matches_pattern

def parse_pytest_verbose(output: str) -> dict[str, bool]:
    """Parse pytest -v output into {test_name: passed}.

    Example line: tests/test_calculator.py::test_divide_zero_is_explicit FAILED
    """
    results = {}
    for line in output.split('\n'):
        line = line.strip()
        if ' PASSED' in line:
            test_name = line.split(' PASSED')[0].strip()
            results[test_name] = True
        elif ' FAILED' in line:
            test_name = line.split(' FAILED')[0].strip()
            results[test_name] = False
    return results

def check_reproduction(before_output: str, after_output: str, target_tests: list[str]) -> tuple[bool, str]:
    """Target tests must fail before patch and pass after (fail-before/pass-after).

    Returns (ok, message).
    """
    before = parse_pytest_verbose(before_output)
    after = parse_pytest_verbose(after_output)

    if not target_tests:
        return False, "No target tests specified"

    # All target tests must be in both runs
    for test in target_tests:
        if test not in before:
            return False, f"Target test {test} not found in before run"
        if test not in after:
            return False, f"Target test {test} not found in after run"

    # All must fail before
    failed_before = [t for t in target_tests if not before[t]]
    if len(failed_before) != len(target_tests):
        return False, f"Target tests must fail before patch; {len(target_tests) - len(failed_before)} already passing"

    # All must pass after
    passed_after = [t for t in target_tests if after[t]]
    if len(passed_after) != len(target_tests):
        return False, f"Target tests must pass after patch; {len(target_tests) - len(passed_after)} still failing"

    return True, f"Reproduced: {len(target_tests)} tests failed → passed"

def check_target_tests(after_output: str, target_tests: list[str]) -> tuple[bool, str]:
    """All target tests must pass after the patch."""
    after = parse_pytest_verbose(after_output)

    if not target_tests:
        return False, "No target tests specified"

    failed = [t for t in target_tests if t not in after or not after[t]]
    if failed:
        return False, f"{len(failed)} target tests still failing: {', '.join(failed[:3])}"

    return True, f"All {len(target_tests)} target tests passing"

def check_regression_tests(before_output: str, after_output: str, target_tests: list[str]) -> tuple[bool, str]:
    """Tests that passed before must still pass after (excluding target tests)."""
    before = parse_pytest_verbose(before_output)
    after = parse_pytest_verbose(after_output)

    # Tests passing before (excluding targets)
    target_set = set(target_tests)
    passing_before = {t for t, ok in before.items() if ok and t not in target_set}

    # Check they still pass
    regressed = [t for t in passing_before if t not in after or not after[t]]

    if regressed:
        return False, f"{len(regressed)} tests regressed: {', '.join(regressed[:3])}"

    return True, f"{len(passing_before)} non-target tests still passing"

def check_diff_scope(diff_text: str, allowed_patterns: list[str]) -> tuple[bool, list[str], str]:
    """Changed paths must match allowed patterns.

    Returns (ok, violating_paths, message).
    """
    if not diff_text.strip():
        # Empty diff is treated as "no changes" - not a scope violation
        # This allows retry on failure scenarios where attempt 1 produces no patch
        return True, [], "No changes (empty diff)"

    try:
        entries = parse_unified_diff(diff_text)
    except Exception as e:
        return False, [], f"Diff parse error: {e}"

    if not allowed_patterns:
        return False, [e.path for e in entries], "No allowed patterns specified (fail-closed)"

    violating = []
    for entry in entries:
        path = entry.path
        if path == '/dev/null':
            continue
        if not any(matches_pattern(path, pat) for pat in allowed_patterns):
            violating.append(path)

    if violating:
        return False, violating, f"{len(violating)} paths outside allowed scope"

    return True, [], f"All {len(entries)} changed paths within allowed scope"

def check_sensitive_paths(diff_text: str, sensitive_patterns: list[str]) -> tuple[bool, list[str], str]:
    """Changed paths must not match sensitive patterns.

    Returns (ok, hits, message).
    """
    if not diff_text.strip():
        return True, [], "No diff to check"

    try:
        entries = parse_unified_diff(diff_text)
    except Exception as e:
        return False, [], f"Diff parse error: {e}"

    if not sensitive_patterns:
        return True, [], "No sensitive patterns configured"

    hits = []
    for entry in entries:
        path = entry.path
        if path == '/dev/null':
            continue
        for pattern in sensitive_patterns:
            if matches_pattern(path, pattern):
                hits.append(f"{path} matches {pattern}")

    if hits:
        return False, hits, f"{len(hits)} sensitive paths touched"

    return True, [], f"No sensitive paths in {len(entries)} changes"

def check_license_sbom(repo_root: str, diff_text: str) -> tuple[bool, str]:
    """Check for LICENSE file and scan for new incompatible dependencies.

    Simplified: checks LICENSE exists, notes dependency files changed.
    Real implementation would parse pyproject.toml/requirements.txt/poetry.lock.
    """
    root = Path(repo_root)

    # Check LICENSE exists
    license_files = [name for name in ('LICENSE', 'LICENSE.txt', 'COPYING')
                     if (root / name).exists()]
    if not license_files:
        return False, "No LICENSE file found"

    # Check if dependency files changed
    try:
        entries = parse_unified_diff(diff_text) if diff_text.strip() else []
    except Exception:
        entries = []

    dep_files = ['pyproject.toml', 'requirements.txt', 'requirements-dev.txt',
                 'poetry.lock', 'Pipfile', 'Pipfile.lock']
    changed_deps = [e.path for e in entries if Path(e.path).name in dep_files]

    if changed_deps:
        return False, f"Dependency files changed: {', '.join(changed_deps)} (manual review required)"

    return True, f"LICENSE present ({license_files[0]}), no dependency changes"

class Verifier:
    """Real verification based on measured test results and diff analysis."""

    def verify(self, before_output: str, after_output: str, target_tests: list[str],
               diff_text: str, task=None, policy=None) -> VerifierResult:
        """Run all 6 checks and produce a verdict.

        Args:
            before_output: pytest -v output before patch
            after_output: pytest -v output after patch
            target_tests: list of test names that are expected to be fixed
            diff_text: unified diff of the patch
            task: TaskSpec (for repo path)
            policy: Policy instance (for allowed_paths, sensitive_patterns)
        """
        checks = {}

        # 1. Reproduction: fail-before/pass-after
        repro_ok, repro_msg = check_reproduction(before_output, after_output, target_tests)
        checks['reproduction'] = repro_ok

        # 2. Target tests pass
        target_ok, target_msg = check_target_tests(after_output, target_tests)
        checks['target_tests'] = target_ok

        # 3. Regression tests (non-target passing tests remain passing)
        regr_ok, regr_msg = check_regression_tests(before_output, after_output, target_tests)
        checks['regression_tests'] = regr_ok

        # 4. Diff scope - use policy if provided, otherwise fall back to task.risk_policy
        if policy:
            allowed = policy.allowed_paths
        else:
            allowed = task.risk_policy.get('allowed_paths', []) if task else []
        scope_ok, violating, scope_msg = check_diff_scope(diff_text, allowed)
        checks['diff_scope'] = scope_ok

        # 5. Sensitive paths - use policy if provided, otherwise fall back to task.risk_policy
        if policy:
            sensitive = policy.sensitive_patterns
        else:
            sensitive = task.risk_policy.get('sensitive_patterns', []) if task else []
        sens_ok, hits, sens_msg = check_sensitive_paths(diff_text, sensitive)
        checks['sensitive_paths'] = sens_ok

        # 6. License/SBOM
        repo = task.repo if task else '.'
        lic_ok, lic_msg = check_license_sbom(repo, diff_text)
        checks['license_sbom'] = lic_ok

        # Determine verdict
        if not all(checks.values()):
            # Classify failure
            if not scope_ok or not sens_ok or not lic_ok:
                fc = FailureClass.LICENSE_OR_RISK
                status = 'BLOCKED'
            elif not regr_ok:
                fc = FailureClass.REGRESSION
                status = 'FAILED'
            elif not repro_ok:
                fc = FailureClass.REPRO_NOT_CONFIRMED
                status = 'FAILED'
            else:
                fc = FailureClass.TEST_ASSERTION
                status = 'FAILED'

            failed_checks = [k for k, v in checks.items() if not v]
            msg = '; '.join(failed_checks)

            return VerifierResult(
                status, 0.35, checks,
                FailureState(fc, msg, ['rollback', 'rebuild_context', 'retry_patch'], 1)
            )

        return VerifierResult('PASSED', 0.94, checks)
