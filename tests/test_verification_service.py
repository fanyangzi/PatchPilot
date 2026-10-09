from __future__ import annotations

from patchpilot.application.verification_service import _parse_results
from patchpilot.domain.entities import CheckOutcome


def test_zero_test_collection_is_not_run_even_when_runner_exits_nonzero():
    outcome, count, details = _parse_results("collected 0 items\nno tests ran in 0.01s", "", 5, False)
    assert outcome is CheckOutcome.NOT_RUN
    assert count == 0
    assert details["reason"] == "zero_test_cases"


def test_runner_rerun_marker_is_flaky_and_never_pass():
    outcome, count, details = _parse_results("test_case RERUN\n1 passed", "", 0, False)
    assert outcome is CheckOutcome.FLAKY
    assert count == 0
    assert "flaky" in details["reason"]
