from __future__ import annotations

from patchpilot.verification.adapters import PytestAdapter, VitestAdapter, adapter_for_command


def test_pytest_adapter_is_fail_closed_for_empty_and_collection_error():
    adapter = PytestAdapter()
    assert adapter.parse("", "", 0).outcome == "not_run"
    result = adapter.parse("ERROR collecting tests/test_x.py", "", 2)
    assert result.outcome == "error" and result.count == 0
    assert adapter.parse("1 passed in 0.01s", "", 0).outcome == "pass"
    assert adapter.parse("1 failed, 1 passed", "", 1).outcome == "fail"


def test_vitest_adapter_supports_json_and_junit_reports():
    adapter = VitestAdapter()
    json_result = adapter.parse(report='{"numTotalTests": 3, "numPassedTests": 2, "numFailedTests": 1}', return_code=1)
    assert json_result.outcome == "fail" and json_result.count == 3
    junit = "<testsuite tests='2' failures='0'><testcase name='a'/><testcase name='b'><skipped/></testcase></testsuite>"
    junit_result = adapter.parse(report=junit, return_code=0)
    assert junit_result.outcome == "pass" and junit_result.count == 2


def test_adapter_selection_never_guesses_an_unknown_framework():
    assert isinstance(adapter_for_command(["pytest", "-q"]), PytestAdapter)
    assert isinstance(adapter_for_command(["npx", "vitest", "run"]), VitestAdapter)
    assert adapter_for_command(["custom-test-runner", "--all"]) is None
