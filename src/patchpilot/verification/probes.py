"""Bounded counterexample probes shared by the verification and review layers.

The probe helpers are intentionally framework agnostic.  A caller supplies a
small oracle (usually an adapter or a contract condition); this module only
generates deterministic boundary cases, compares repeated observations and
shrinks a failing input.  It never turns a missing oracle or an execution
error into a passing result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ProbeCase:
    """One bounded input proposed for an oracle."""

    value: Any
    label: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class ProbeObservation:
    """A single base/candidate measurement for one probe input."""

    input_hash: str
    base: str
    candidate: str
    expected: str | None = None
    actual: str | None = None
    error: str | None = None
    repeat: int = 1
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProbeClassification:
    """Conservative classification of a set of repeated observations."""

    kind: str
    status: str
    message: str
    stable: bool
    repeats: int
    input_hash: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShrinkStep:
    """One durable delta-debugging decision.

    A shrink is evidence only when every accepted step preserves the same
    failure predicate.  Keeping both accepted and rejected candidates makes
    the result auditable after a worker restart instead of exposing only the
    final value.
    """

    step: int
    input: Any
    input_hash: str
    failed: bool
    decision: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "input": self.input,
            "input_hash": self.input_hash,
            "failed": self.failed,
            "decision": self.decision,
            **({"error": self.error} if self.error else {}),
        }


def _stable_hash(value: Any) -> str:
    import hashlib
    import json

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _add(values: list[tuple[Any, str]], value: Any, label: str) -> None:
    marker = _stable_hash(value)
    if all(_stable_hash(existing) != marker for existing, _ in values):
        values.append((value, label))


def generate_boundary_inputs(seed: Any = None, *, max_cases: int = 12) -> list[ProbeCase]:
    """Generate a deterministic, bounded set of useful edge inputs.

    The function is deliberately small and side-effect free.  It accepts a
    scalar, string, sequence or mapping seed and always caps the result so a
    remote or local verifier cannot accidentally turn a probe into an
    unbounded fuzzing job.
    """

    if max_cases < 1 or max_cases > 64:
        raise ValueError("max_cases must be between 1 and 64")
    values: list[tuple[Any, str]] = []
    _add(values, seed, "seed")
    if isinstance(seed, bool):
        _add(values, not seed, "boolean-opposite")
    elif isinstance(seed, (int, float)) and not isinstance(seed, bool):
        _add(values, 0, "zero")
        _add(values, 1, "one")
        _add(values, -1, "negative-one")
        _add(values, seed - 1, "below-seed")
        _add(values, seed + 1, "above-seed")
    elif isinstance(seed, str):
        _add(values, "", "empty")
        _add(values, " ", "whitespace")
        _add(values, seed[:1], "first-character")
        _add(values, seed[:-1], "without-last-character")
        _add(values, seed + "\n", "trailing-newline")
    elif isinstance(seed, (list, tuple)):
        seq = list(seed)
        _add(values, [], "empty-sequence")
        _add(values, seq[:1], "first-item")
        _add(values, seq[:-1], "without-last-item")
        _add(values, seq + [None], "null-tail")
    elif isinstance(seed, Mapping):
        mapping = dict(seed)
        _add(values, {}, "empty-object")
        _add(values, {next(iter(mapping)): mapping[next(iter(mapping))]} if mapping else {}, "first-field")
        _add(values, {**mapping, "__unknown__": None}, "unknown-field")
        _add(values, {key: None for key in mapping}, "null-fields")
    _add(values, None, "null")
    return [ProbeCase(value=value, label=label, input_hash=_stable_hash(value)) for value, label in values[:max_cases]]


def minimize_failure(value: Any, fails: Callable[[Any], bool], *, max_steps: int = 32) -> Any:
    """Shrink a failing value while preserving ``fails(value)``.

    This is a bounded delta-debugging pass for JSON-like values.  If the
    original value does not fail, or the predicate raises, the input is
    returned unchanged.  The caller can therefore record the attempt without
    claiming a minimized counterexample was found.
    """

    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    try:
        if not fails(value):
            return value
    except Exception:
        return value
    current = value
    steps = 0

    def candidates(item: Any) -> Iterable[Any]:
        if isinstance(item, str):
            if item:
                yield ""
                yield item[: len(item) // 2]
                yield item[1:]
        elif isinstance(item, (list, tuple)):
            seq = list(item)
            yield type(item)()
            for index in range(len(seq)):
                yield type(item)(seq[:index] + seq[index + 1 :])
            if len(seq) > 1:
                yield type(item)(seq[: len(seq) // 2])
        elif isinstance(item, Mapping):
            mapping = dict(item)
            yield {}
            for key in list(mapping):
                reduced = dict(mapping)
                reduced.pop(key, None)
                yield reduced
            for key in mapping:
                if mapping[key] is not None:
                    reduced = dict(mapping)
                    reduced[key] = None
                    yield reduced
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            yield 0
            yield item // 2 if isinstance(item, int) else item / 2

    changed = True
    while changed and steps < max_steps:
        changed = False
        for candidate in candidates(current):
            steps += 1
            try:
                keep = bool(fails(candidate))
            except Exception:
                keep = False
            if keep:
                current = candidate
                changed = True
                break
            if steps >= max_steps:
                break
    return current


def shrink_failure_with_trace(
    value: Any,
    fails: Callable[[Any], bool],
    *,
    max_steps: int = 32,
) -> tuple[Any, list[ShrinkStep]]:
    """Shrink a failing input and return the complete bounded decision trace.

    ``minimize_failure`` predates persisted probe evidence and intentionally
    returns only a value.  This companion keeps the same conservative
    semantics while recording every predicate call, including errors.  It is
    useful to persist a reviewable trajectory without claiming global
    minimality: the trace is capped and the final value is simply the smallest
    value found within that budget.
    """

    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    trace: list[ShrinkStep] = []

    def evaluate(candidate: Any, step: int, decision: str) -> bool:
        try:
            failed = bool(fails(candidate))
            trace.append(ShrinkStep(step, candidate, _stable_hash(candidate), failed, decision))
            return failed
        except Exception as exc:  # a broken oracle is never a preserved failure
            trace.append(ShrinkStep(step, candidate, _stable_hash(candidate), False, "error", str(exc)[:500]))
            return False

    if not evaluate(value, 0, "seed"):
        return value, trace
    current = value
    steps = 0

    def candidates(item: Any) -> Iterable[Any]:
        if isinstance(item, str):
            if item:
                yield ""
                yield item[: len(item) // 2]
                yield item[1:]
        elif isinstance(item, (list, tuple)):
            seq = list(item)
            yield type(item)()
            for index in range(len(seq)):
                yield type(item)(seq[:index] + seq[index + 1 :])
            if len(seq) > 1:
                yield type(item)(seq[: len(seq) // 2])
        elif isinstance(item, Mapping):
            mapping = dict(item)
            yield {}
            for key in list(mapping):
                reduced = dict(mapping)
                reduced.pop(key, None)
                yield reduced
            for key in mapping:
                if mapping[key] is not None:
                    reduced = dict(mapping)
                    reduced[key] = None
                    yield reduced
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            yield 0
            yield item // 2 if isinstance(item, int) else item / 2

    changed = True
    while changed and steps < max_steps:
        changed = False
        for candidate in candidates(current):
            steps += 1
            if evaluate(candidate, steps, "candidate"):
                # Rewrite the last decision without re-running the oracle.
                last = trace[-1]
                trace[-1] = ShrinkStep(last.step, last.input, last.input_hash, True, "accepted")
                current = candidate
                changed = True
                break
            else:
                last = trace[-1]
                if last.decision == "candidate":
                    trace[-1] = ShrinkStep(last.step, last.input, last.input_hash, last.failed, "rejected", last.error)
            if steps >= max_steps:
                break
    return current, trace


def classify_observations(observations: Sequence[ProbeObservation], *, oracle_valid: bool = True) -> ProbeClassification:
    """Classify repeated base/candidate observations fail-closed.

    The returned vocabulary maps directly to the F04 review states: a stable
    candidate-only failure is a regression, a base-only failure is a
    pre-existing defect, disagreement across repeats is flaky, and an absent
    or invalid oracle is a contract issue.
    """

    if not observations:
        return ProbeClassification("not_run", "inconclusive", "没有可比较的探针观测。", False, 0)
    if not oracle_valid:
        return ProbeClassification("requirement_conflict", "contract_issue", "验收条件缺少有效 oracle，不能确认反例。", False, len(observations))
    if any(item.error for item in observations):
        return ProbeClassification("environment_error", "inconclusive", "探针执行包含环境错误，结果不能作为确定反例。", False, len(observations))
    signatures = {(item.base, item.candidate, item.expected, item.actual) for item in observations}
    if len(signatures) > 1:
        return ProbeClassification("flaky", "inconclusive", "重复探针的观测结果不一致。", False, len(observations), observations[0].input_hash, {"signatures": [list(item) for item in signatures]})
    item = observations[0]
    if item.base == "fail" and item.candidate == "pass":
        return ProbeClassification("pre_existing_defect", "observed", "基线失败且候选通过；该问题属于候选前已存在的缺陷。", True, len(observations), item.input_hash)
    if item.base == "pass" and item.candidate == "fail":
        return ProbeClassification("regression", "confirmed", "基线通过而候选失败；稳定重复后确认回归。", True, len(observations), item.input_hash)
    if item.candidate == "fail":
        return ProbeClassification("candidate_failure", "observed", "候选在该输入上失败，但当前观测不足以归因到合同违反。", True, len(observations), item.input_hash)
    return ProbeClassification("no_counterexample", "inconclusive", "当前有界输入未观察到反例。", True, len(observations), item.input_hash)


__all__ = ["ProbeCase", "ProbeObservation", "ProbeClassification", "ShrinkStep", "generate_boundary_inputs", "minimize_failure", "shrink_failure_with_trace", "classify_observations"]
