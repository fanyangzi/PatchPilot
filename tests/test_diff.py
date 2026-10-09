from __future__ import annotations

import pytest

from patchpilot.verifier.diff import parse_unified_diff
from patchpilot.api_v1 import _validate_repair_patch


def test_parse_header_only_unified_diff_supports_modify_add_and_delete():
    patch = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1 +1 @@\n"
        "-return old\n"
        "+return new\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+created\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-removed\n"
    )

    entries = parse_unified_diff(patch)

    assert [(item.path, item.change_kind) for item in entries] == [
        ("calc.py", "modify"),
        ("new.py", "add"),
        ("old.py", "delete"),
    ]


def test_parse_git_diff_does_not_duplicate_file_headers():
    patch = (
        "diff --git a/calc.py b/calc.py\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1 +1 @@\n"
        "-return old\n"
        "+return new\n"
    )

    entries = parse_unified_diff(patch)

    assert len(entries) == 1
    assert entries[0].path == "calc.py"


def test_header_only_parser_rejects_unsafe_paths():
    with pytest.raises(ValueError, match="Path escapes root"):
        parse_unified_diff(
            "--- a/../../outside.py\n"
            "+++ b/../../outside.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )


def test_repair_validation_accepts_provider_style_header_only_patch():
    patch = (
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1 +1 @@\n"
        "-return old\n"
        "+return new\n"
    )

    assert _validate_repair_patch(patch) == ["calc.py"]
