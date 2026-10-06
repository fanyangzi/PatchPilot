"""Policy gate enforcement - the current dead code brought to life."""
from __future__ import annotations
from .schema import Policy
from ..domain import VerifierResult, FailureClass, FailureState

class PolicyGate:
    """Enforce repository policy on verification results.

    This is the mechanism that was stubbed in skills/policy.py but never called.
    It takes a verification result and a policy, and determines if the result
    satisfies the policy's requirements.
    """

    def __init__(self, policy: Policy):
        self.policy = policy

    def enforce(self, result: VerifierResult) -> tuple[bool, str]:
        """Check if verification result satisfies policy requirements.

        Returns (passes, reason).
        - passes=True: result meets all policy requirements
        - passes=False: result violates policy (with explanation)

        When policy is missing or undefined (no allowed_paths), fail-closed.
        """
        # Fail-closed: if policy specifies allowed_paths but check_diff_scope is missing,
        # or if sensitive_patterns exist but check wasn't run, reject
        if self.policy.allowed_paths and 'diff_scope' not in result.checks:
            return False, "Policy requires diff_scope check but it was not run"

        if self.policy.sensitive_patterns and 'sensitive_paths' not in result.checks:
            return False, "Policy requires sensitive_paths check but it was not run"

        # Check all required checks passed
        for check in self.policy.required_checks:
            if check not in result.checks:
                return False, f"Required check '{check}' was not run"
            if not result.checks[check]:
                return False, f"Required check '{check}' failed"

        # All requirements met
        return True, "All policy requirements satisfied"

    def classify_failure(self, result: VerifierResult) -> FailureState:
        """Classify a verification failure for recovery planning.

        This replaces the hardcoded classification in engine.py.
        """
        checks = result.checks

        # Security/risk failures: diff_scope, sensitive_paths, license_sbom
        if not checks.get('diff_scope', True):
            return FailureState(
                FailureClass.LICENSE_OR_RISK,
                'Patch modifies paths outside allowed scope',
                ['rollback', 'rebuild_context'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        if not checks.get('sensitive_paths', True):
            return FailureState(
                FailureClass.LICENSE_OR_RISK,
                'Patch touches sensitive paths (credentials, CI, lockfiles)',
                ['rollback', 'rebuild_context'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        if not checks.get('license_sbom', True):
            return FailureState(
                FailureClass.LICENSE_OR_RISK,
                'License or dependency check failed',
                ['rollback', 'rebuild_context'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        # Regression: previously passing tests now fail
        if not checks.get('regression_tests', True):
            return FailureState(
                FailureClass.REGRESSION,
                'Patch caused regression in non-target tests',
                ['rollback', 'rebuild_context', 'retry_patch'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        # Reproduction not confirmed: fail-before/pass-after pattern not observed
        if not checks.get('reproduction', True):
            return FailureState(
                FailureClass.REPRO_NOT_CONFIRMED,
                'Reproduction pattern not confirmed (expected fail-before/pass-after)',
                ['rebuild_context', 'retry_patch'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        # Target tests still failing: patch didn't fix the issue
        if not checks.get('target_tests', True):
            return FailureState(
                FailureClass.TEST_ASSERTION,
                'Target tests still failing after patch',
                ['rebuild_context', 'retry_patch'],
                attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
            )

        # Unknown failure
        return FailureState(
            FailureClass.TEST_ASSERTION,
            'Verification failed (unknown cause)',
            ['retry_patch'],
            attempt=getattr(result.failure, 'attempt', 1) if result.failure else 1
        )
