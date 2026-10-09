from __future__ import annotations

from patchpilot.application.verification_service import VerificationService
from patchpilot.domain.entities import CheckOutcome


def _service(timeout: int = 2) -> VerificationService:
    service = VerificationService.__new__(VerificationService)
    service.timeout = timeout
    return service


def test_unknown_node_test_runner_is_unsupported_without_execution(tmp_path):
    result = _service()._measure(tmp_path, ("node", "custom-runner.js"))
    assert result.outcome is CheckOutcome.UNSUPPORTED
    assert result.count == 0
    assert result.details["reason"] == "no_registered_adapter"


def test_vitest_adapter_normalizes_text_output(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 1001
        returncode = 0

        def communicate(self, timeout=None):
            return "Tests 2 passed (2)", ""

    monkeypatch.setattr(
        "patchpilot.application.verification_service.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    result = _service()._measure(tmp_path, ("npx", "vitest", "run"))
    assert result.outcome is CheckOutcome.PASS
    assert result.count == 2
    assert result.details["framework"] == "vitest"
    assert result.details["adapter_supported"] is True


def test_adapter_path_preserves_pytest_zero_collection_as_not_run(monkeypatch, tmp_path):
    class FakeProcess:
        pid = 1002
        returncode = 5

        def communicate(self, timeout=None):
            return "no tests ran in 0.01s", ""

    monkeypatch.setattr(
        "patchpilot.application.verification_service.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    result = _service()._measure(tmp_path, ("pytest", "-q"))
    assert result.outcome is CheckOutcome.NOT_RUN
    assert result.count == 0
    assert result.details["reason"] == "zero_test_cases"
