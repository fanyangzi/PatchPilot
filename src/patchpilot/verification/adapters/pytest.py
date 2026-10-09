from __future__ import annotations

import re
from typing import Sequence

from .base import AdapterResult


_SUMMARY_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|error|errors?|skipped|xfailed|xpassed|deselected)",
    re.IGNORECASE,
)
_CASE_RE = re.compile(r"^.*?::[^ ]+\s+(?P<kind>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s*$", re.MULTILINE)


class PytestAdapter:
    framework = "pytest"

    @classmethod
    def supports(cls, command: Sequence[str]) -> bool:
        tokens = [str(item).lower().replace("\\", "/") for item in command if str(item)]
        if not tokens:
            return False
        return any(
            token == "pytest"
            or token.endswith("/pytest")
            or token.endswith("/pytest.exe")
            or token in {"-m", "pytest"} and index + 1 < len(tokens) and tokens[index + 1] == "pytest"
            for index, token in enumerate(tokens)
        )

    def parse(
        self,
        stdout: str = "",
        stderr: str = "",
        return_code: int | None = None,
        *,
        timed_out: bool = False,
        report: str | bytes | None = None,
    ) -> AdapterResult:
        combined = (stdout or "") + "\n" + (stderr or "")
        if timed_out:
            return AdapterResult(self.framework, True, "error", return_code=return_code, details={"reason": "timeout"})
        if "flaky" in combined.lower() or "rerun" in combined.lower():
            return AdapterResult(self.framework, True, "flaky", return_code=return_code, details={"reason": "runner reported rerun/flaky output"})
        counts: dict[str, int] = {}
        for match in _SUMMARY_RE.finditer(combined):
            kind = match.group("kind").lower().rstrip("s")
            counts[kind] = counts.get(kind, 0) + int(match.group("count"))
        if not counts:
            for match in _CASE_RE.finditer(combined):
                kind = match.group("kind").lower()
                counts[kind] = counts.get(kind, 0) + 1
        count = sum(counts.values())
        if count == 0:
            outcome = "not_run" if return_code == 0 else "error"
            reason = "no test case result observed" if outcome == "not_run" else "command failed without a test result"
            details = {"reason": reason}
            if return_code not in (None, 0):
                details["return_code"] = return_code
            return AdapterResult(self.framework, True, outcome, count, return_code, details)
        if counts.get("failed", 0) or counts.get("error", 0):
            return AdapterResult(self.framework, True, "fail", count, return_code, {"counts": counts})
        if counts.get("skipped", 0) == count or counts.get("deselected", 0) == count:
            return AdapterResult(self.framework, True, "skipped", count, return_code, {"counts": counts})
        if return_code not in (None, 0):
            return AdapterResult(self.framework, True, "error", count, return_code, {"counts": counts, "return_code": return_code})
        return AdapterResult(self.framework, True, "pass", count, return_code, {"counts": counts})
