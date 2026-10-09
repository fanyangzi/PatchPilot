"""Unified diff parsing and path matching for verification."""
from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

@dataclass
class DiffEntry:
    """A single file change in a unified diff."""
    path: str  # Normalized relative path
    change_kind: Literal['add', 'modify', 'delete', 'rename']
    is_binary: bool
    is_mode_change: bool
    old_path: str | None  # For renames

def parse_unified_diff(diff_text: str) -> list[DiffEntry]:
    """Parse unified diff output into structured entries.

    Handles:
    - Normal adds/modifies/deletes
    - Renames (--- a/old +++ b/new)
    - Binary files
    - Mode changes

    Returns normalized relative paths (no leading /, no ..).
    """
    entries = []
    lines = diff_text.split('\n')
    i = 0

    while i < len(lines):
        line = lines[i]

        # Look for file header: diff --git a/path b/path
        if line.startswith('diff --git '):
            parts = line.split()
            if len(parts) >= 4:
                old_path = parts[2][2:]  # Remove a/ prefix
                new_path = parts[3][2:]  # Remove b/ prefix

                # Look ahead for more context
                is_binary = False
                is_mode_change = False
                change_kind = 'modify'

                j = i + 1
                while j < len(lines) and not lines[j].startswith('diff --git'):
                    if lines[j].startswith('Binary files'):
                        is_binary = True
                    elif lines[j].startswith('old mode') or lines[j].startswith('new mode'):
                        is_mode_change = True
                    elif lines[j].startswith('deleted file'):
                        change_kind = 'delete'
                    elif lines[j].startswith('new file'):
                        change_kind = 'add'
                    elif lines[j].startswith('rename from'):
                        change_kind = 'rename'
                    elif lines[j].startswith('--- '):
                        break
                    j += 1

                # Normalize the path
                path = new_path if change_kind != 'delete' else old_path
                if path == '/dev/null':
                    path = old_path if change_kind == 'delete' else new_path

                norm_path = normalize_path(path)
                norm_old = normalize_path(old_path) if change_kind == 'rename' else None

                entries.append(DiffEntry(
                    path=norm_path,
                    change_kind=change_kind,
                    is_binary=is_binary,
                    is_mode_change=is_mode_change,
                    old_path=norm_old
                ))

                # ``diff --git`` records already contain the following
                # ``---``/``+++`` file headers.  Skip the complete record so
                # the header-only parser below cannot report the same file a
                # second time.  Hunk lines are not needed to identify the
                # changed paths and a context line can legally begin with
                # text that resembles a header.
                next_diff = i + 1
                while next_diff < len(lines) and not lines[next_diff].startswith('diff --git '):
                    next_diff += 1
                i = next_diff - 1

        # Providers commonly emit a POSIX unified diff without the optional
        # ``diff --git`` metadata line.  Git itself accepts this form, and the
        # repair prompt explicitly asks for ``--- a/path``/``+++ b/path``.
        # Recognise the paired file headers while requiring the conventional
        # a/ and b/ prefixes (or /dev/null) so removed hunk content such as
        # ``--- example`` is not mistaken for a new file record.
        elif (
            line.startswith('--- ')
            and i + 1 < len(lines)
            and lines[i + 1].startswith('+++ ')
            and _looks_like_file_header(line[4:], 'a')
            and _looks_like_file_header(lines[i + 1][4:], 'b')
        ):
            old_path = _header_path(line[4:])
            new_path = _header_path(lines[i + 1][4:])
            is_binary = False
            is_mode_change = False
            change_kind: Literal['add', 'modify', 'delete', 'rename'] = 'modify'
            if old_path == '/dev/null':
                change_kind = 'add'
            elif new_path == '/dev/null':
                change_kind = 'delete'

            path = new_path if change_kind != 'delete' else old_path
            if path == '/dev/null':
                path = old_path if change_kind == 'delete' else new_path
            norm_path = normalize_path(path)
            entries.append(DiffEntry(
                path=norm_path,
                change_kind=change_kind,
                is_binary=is_binary,
                is_mode_change=is_mode_change,
                old_path=None,
            ))

        i += 1

    return entries


def _header_path(value: str) -> str:
    """Return the path portion of a unified-diff file header.

    GNU diff may append a tab-separated timestamp.  Paths are intentionally
    kept as text here; ``normalize_path`` performs the security checks used by
    the verifier afterwards.
    """
    path = value.split('\t', 1)[0].strip()
    if path == '/dev/null':
        return path
    if path.startswith(('a/', 'b/')):
        return path[2:]
    return path


def _looks_like_file_header(value: str, prefix: str) -> bool:
    """Check the conventional prefix used by a header-only unified diff."""
    path = value.split('\t', 1)[0].strip()
    return path == '/dev/null' or path.startswith(prefix + '/')

def parse_name_status(output: str) -> list[DiffEntry]:
    """Parse git diff --name-status output.

    Format: <status>\t<path>\t[<old_path>]
    Status: A (add), M (modify), D (delete), R (rename), etc.
    """
    entries = []
    for line in output.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\t')
        if len(parts) < 2:
            continue

        status = parts[0][0]  # First char (R100 -> R)
        path = parts[1]
        old_path = parts[2] if len(parts) > 2 else None

        if status == 'A':
            kind = 'add'
        elif status == 'D':
            kind = 'delete'
        elif status == 'R':
            kind = 'rename'
        else:
            kind = 'modify'

        entries.append(DiffEntry(
            path=normalize_path(path),
            change_kind=kind,
            is_binary=False,
            is_mode_change=False,
            old_path=normalize_path(old_path) if old_path else None
        ))

    return entries

def normalize_path(path: str) -> str:
    """Normalize a path for matching: remove leading /, resolve .., convert to posix."""
    if not path or path == '/dev/null':
        return path

    p = PurePosixPath(path)

    # Reject absolute paths (security: prevent escaping repo)
    if p.is_absolute():
        raise ValueError(f"Absolute paths not allowed: {path}")

    # Resolve .. but don't allow escaping root
    parts = []
    for part in p.parts:
        if part == '..':
            if parts:
                parts.pop()
            else:
                raise ValueError(f"Path escapes root: {path}")
        elif part != '.':
            parts.append(part)

    return '/'.join(parts) if parts else '.'

def _glob_regex(pattern: str):
    out, i = '', 0
    while i < len(pattern):
        if pattern.startswith('**/', i): out += '(?:.*/)?'; i += 3
        elif pattern.startswith('**', i): out += '.*'; i += 2
        elif pattern[i] == '*': out += '[^/]*'; i += 1
        elif pattern[i] == '?': out += '[^/]'; i += 1
        else: out += re.escape(pattern[i]); i += 1
    return re.compile(out + r'\Z')

def matches_pattern(path: str, pattern: str) -> bool:
    """Match a normalized path against a policy pattern, by path components.

    - `src/click/` or `src/click` (no glob chars): that directory and everything under it, or that exact file
    - `*.py`: root-level files only; `src/*.py`: one level; `**` spans any depth, `**/` may match zero directories
    """
    path = normalize_path(path)
    pattern = pattern.rstrip('/')
    if not any(c in pattern for c in '*?'):
        return path == pattern or path.startswith(pattern + '/')
    return bool(_glob_regex(pattern).match(path))
