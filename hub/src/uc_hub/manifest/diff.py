"""Unified diffs for the approver.

JSON documents are rendered with sorted keys and two-space indentation, so
the same pair of documents always gives the same diff whatever the key order
on the node or in the manifest.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

CONTEXT_LINES = 3


def canonical_json(doc: Any) -> str:
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def text_diff(before: str | None, after: str | None, name: str, context: int = CONTEXT_LINES) -> str:
    """Unified diff of two texts; ``None`` is a missing file (``/dev/null``)."""
    a = _lines(before)
    b = _lines(after)
    if a == b:
        return ""
    fromfile = f"a/{name}" if before is not None else "/dev/null"
    tofile = f"b/{name}" if after is not None else "/dev/null"
    return "".join(difflib.unified_diff(a, b, fromfile, tofile, n=context))


def json_diff(before: Any, after: Any, name: str, context: int = CONTEXT_LINES) -> str:
    """Unified diff of two JSON documents; ``None`` is a missing document."""
    return text_diff(
        None if before is None else canonical_json(before),
        None if after is None else canonical_json(after),
        name, context,
    )


def yaml_diff(before: str | None, after: str | None, name: str = "site.yaml",
              context: int = CONTEXT_LINES) -> str:
    return text_diff(before, after, name, context)


def _lines(text: str | None) -> list[str]:
    if not text:
        return []
    lines = text.splitlines(keepends=True)
    if not lines[-1].endswith("\n"):
        lines[-1] += "\n\\ No newline at end of file\n"
    return lines
