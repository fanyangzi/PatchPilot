from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Any, Sequence

from .base import AdapterResult


_TEST_SUMMARY_RE = re.compile(r"Tests?\s+(?P<passed>\d+)\s+passed(?:\s*\((?P<total>\d+)\))?", re.IGNORECASE)
_FAILED_RE = re.compile(r"Tests?\s+(?P<failed>\d+)\s+failed", re.IGNORECASE)
_SKIPPED_RE = re.compile(r"Tests?\s+(?P<skipped>\d+)\s+skipped", re.IGNORECASE)


class VitestAdapter:
    framework = "vitest"

    @classmethod
    def supports(cls, command: Sequence[str]) -> bool:
        tokens = [str(item).lower().replace("\\", "/") for item in command if str(item)]
        return any(
            token == "vitest"
            or token.endswith("/vitest")
            or token.endswith("/vitest.js")
            or token in {"npx", "npm", "pnpm", "yarn"} and index + 1 < len(tokens) and tokens[index + 1] == "vitest"
            for index, token in enumerate(tokens)
        )

    @staticmethod
    def _from_json(value: str | bytes | None) -> dict[str, int] | None:
        if not value:
            return None
        try:
            data = json.loads(value)
        except (TypeError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        aliases = {
            "numTotalTests": "total",
            "numPassedTests": "passed",
            "numFailedTests": "failed",
            "numPendingTests": "skipped",
            "numTodoTests": "skipped",
        }
        result = {name: int(data.get(key, 0) or 0) for key, name in aliases.items() if key in data}
        return result or None

    @staticmethod
    def _from_junit(value: str | bytes | None) -> dict[str, int] | None:
        if not value:
            return None
        try:
            root = ET.fromstring(value)
        except (ET.ParseError, TypeError, ValueError):
            return None
        cases = list(root.iter("testcase"))
        if not cases:
            return None
        failed = sum(1 for case in cases if case.find("failure") is not None)
        errors = sum(1 for case in cases if case.find("error") is not None)
        skipped = sum(1 for case in cases if case.find("skipped") is not None)
        return {"total": len(cases), "passed": len(cases) - failed - errors - skipped, "failed": failed + errors, "skipped": skipped}

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
        parsed = self._from_json(report) or self._from_junit(report) or self._from_json(stdout)
        if parsed is None:
            passed_match = _TEST_SUMMARY_RE.search(combined)
            failed_match = _FAILED_RE.search(combined)
            skipped_match = _SKIPPED_RE.search(combined)
            passed = int(passed_match.group("passed")) if passed_match else 0
            failed = int(failed_match.group("failed")) if failed_match else 0
            skipped = int(skipped_match.group("skipped")) if skipped_match else 0
            total = int(passed_match.group("total") or (passed + failed + skipped)) if passed_match else passed + failed + skipped
            parsed = {"total": total, "passed": passed, "failed": failed, "skipped": skipped} if total else None
        if "flaky" in combined.lower() or "rerun" in combined.lower():
            return AdapterResult(self.framework, True, "flaky", return_code=return_code, details={"reason": "runner reported rerun/flaky output"})
        if not parsed or int(parsed.get("total", 0)) <= 0:
            outcome = "not_run" if return_code == 0 else "error"
            return AdapterResult(self.framework, True, outcome, 0, return_code, {"reason": "no test case result observed"})
        total = int(parsed.get("total", 0))
        failed = int(parsed.get("failed", 0))
        skipped = int(parsed.get("skipped", 0))
        if failed:
            outcome = "fail"
        elif skipped == total:
            outcome = "skipped"
        elif return_code not in (None, 0):
            outcome = "error"
        else:
            outcome = "pass"
        return AdapterResult(self.framework, True, outcome, total, return_code, {"counts": parsed})
