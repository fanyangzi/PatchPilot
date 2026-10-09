from __future__ import annotations

from patchpilot.verification.probes import (
    ProbeObservation,
    classify_observations,
    generate_boundary_inputs,
    minimize_failure,
)


def test_boundary_inputs_are_deterministic_and_bounded():
    first = generate_boundary_inputs("abcdef", max_cases=6)
    second = generate_boundary_inputs("abcdef", max_cases=6)
    assert [item.input_hash for item in first] == [item.input_hash for item in second]
    assert len(first) == 6
    assert {item.label for item in first} >= {"seed", "empty"}


def test_minimizer_preserves_failure_and_stops_at_small_input():
    result = minimize_failure("prefix:bad:suffix", lambda value: "bad" in value)
    assert result == "bad"


def test_classification_is_fail_closed_for_invalid_oracle_and_flaky_runs():
    observation = ProbeObservation("input-1", "pass", "fail")
    invalid = classify_observations([observation], oracle_valid=False)
    assert invalid.kind == "requirement_conflict"
    assert invalid.status == "contract_issue"

    flaky = classify_observations([
        observation,
        ProbeObservation("input-1", "pass", "pass"),
    ])
    assert flaky.kind == "flaky"
    assert flaky.status == "inconclusive"


def test_classification_distinguishes_regression_and_pre_existing_defect():
    regression = classify_observations([ProbeObservation("i", "pass", "fail"), ProbeObservation("i", "pass", "fail")])
    assert regression.kind == "regression"
    assert regression.status == "confirmed"

    pre_existing = classify_observations([ProbeObservation("i", "fail", "pass")])
    assert pre_existing.kind == "pre_existing_defect"
    assert pre_existing.status == "observed"
