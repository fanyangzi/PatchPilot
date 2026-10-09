from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, Sequence


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Conservative normalized test-run result.

    ``outcome`` is one of ``pass``, ``fail``, ``error``, ``skipped``,
    ``not_run``, ``flaky`` or ``unsupported``.  ``supported=False`` is
    reserved for a command that no registered adapter can interpret.
    """

    framework: str
    supported: bool
    outcome: str
    count: int = 0
    return_code: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework": self.framework,
            "supported": self.supported,
            "outcome": self.outcome,
            "count": self.count,
            "return_code": self.return_code,
            "details": self.details,
        }


class TestAdapter(Protocol):
    framework: str

    @classmethod
    def supports(cls, command: Sequence[str]) -> bool:
        ...

    def parse(
        self,
        stdout: str = "",
        stderr: str = "",
        return_code: int | None = None,
        *,
        timed_out: bool = False,
        report: str | bytes | None = None,
    ) -> AdapterResult:
        ...


def _executable_tokens(command: Sequence[str] | Iterable[str]) -> list[str]:
    return [str(item).lower().replace("\\", "/") for item in command if str(item)]


def adapter_for_command(command: Sequence[str]) -> TestAdapter | None:
    """Select a registered adapter without guessing unsupported frameworks."""
    from .pytest import PytestAdapter
    from .vitest import VitestAdapter

    tokens = _executable_tokens(command)
    if PytestAdapter.supports(tokens):
        return PytestAdapter()
    if VitestAdapter.supports(tokens):
        return VitestAdapter()
    return None


def unsupported_result(command: Sequence[str] | None = None) -> AdapterResult:
    return AdapterResult(
        framework="unknown",
        supported=False,
        outcome="unsupported",
        details={"reason": "no registered adapter", "command": list(command or ())},
    )
