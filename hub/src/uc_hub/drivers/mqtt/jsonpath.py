"""JSONPath-lite: the subset of JSONPath the generic-json profile needs to pick
one value out of an MQTT payload.

A path is ``$`` followed by any number of segments: ``.key``, ``['key']`` or
``["key"]`` (a backslash escapes the next character) and ``[n]`` (a list
index; negative ``n`` counts from the end). There are no wildcards, filters or
slices, because a point maps to exactly one value. A path without the leading
``$`` is taken relative to the root, so ``ppm`` means ``$.ppm``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_KEY_STOP = frozenset(".[]'\"\\ \t\r\n")
_INDEX = re.compile(r"-?[0-9]+")
_SHORTHAND = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")

Segment = str | int


class JsonPathError(ValueError):
    """The path is not valid JSONPath-lite."""


@dataclass(frozen=True, slots=True)
class JsonPath:
    segments: tuple[Segment, ...]

    @classmethod
    def compile(cls, text: str) -> JsonPath:
        if not isinstance(text, str):
            raise JsonPathError(f"a path is a string, not {type(text).__name__}")
        return cls(_parse(text))

    def find(self, doc: Any) -> tuple[bool, Any]:
        """``(True, value)`` when the path exists in ``doc``, else ``(False, None)``.

        Keys only match objects and indexes only match arrays, so ``[0]``
        never reads a key ``"0"`` and ``.0`` never reads a list element.
        """
        cur = doc
        for seg in self.segments:
            if isinstance(seg, int):
                if not isinstance(cur, list) or not -len(cur) <= seg < len(cur):
                    return False, None
                cur = cur[seg]
            else:
                if not isinstance(cur, dict) or seg not in cur:
                    return False, None
                cur = cur[seg]
        return True, cur

    def __str__(self) -> str:
        out = ["$"]
        for seg in self.segments:
            if isinstance(seg, int):
                out.append(f"[{seg}]")
            elif _SHORTHAND.fullmatch(seg):
                out.append(f".{seg}")
            else:
                escaped = seg.replace("\\", "\\\\").replace("'", "\\'")
                out.append(f"['{escaped}']")
        return "".join(out)


def _parse(text: str) -> tuple[Segment, ...]:
    path = text.strip()
    if not path:
        raise JsonPathError("empty path")
    if path[0] != "$":
        path = "$" + (path if path[0] in ".[" else "." + path)
    segs: list[Segment] = []
    i, n = 1, len(path)
    while i < n:
        c = path[i]
        if c == ".":
            j = i + 1
            while j < n and path[j] not in _KEY_STOP:
                j += 1
            if j == i + 1:
                raise JsonPathError(f"{text!r}: empty key after '.' at position {i}")
            segs.append(path[i + 1:j])
            i = j
        elif c == "[":
            if i + 1 < n and path[i + 1] in "'\"":
                key, i = _quoted(text, path, i + 1)
                segs.append(key)
            else:
                j = path.find("]", i)
                if j < 0:
                    raise JsonPathError(f"{text!r}: '[' at position {i} is not closed")
                body = path[i + 1:j].strip()
                if not _INDEX.fullmatch(body):
                    raise JsonPathError(f"{text!r}: {body!r} is not an array index")
                segs.append(int(body))
                i = j + 1
        else:
            raise JsonPathError(f"{text!r}: unexpected {c!r} at position {i}")
    return tuple(segs)


def _quoted(text: str, path: str, start: int) -> tuple[str, int]:
    """Parse ``'key']`` from ``start`` (the quote); return the key and the
    position after the closing bracket."""
    quote = path[start]
    buf: list[str] = []
    j = start + 1
    n = len(path)
    while True:
        if j >= n:
            raise JsonPathError(f"{text!r}: unterminated string at position {start}")
        ch = path[j]
        if ch == "\\":
            if j + 1 >= n:
                raise JsonPathError(f"{text!r}: unterminated string at position {start}")
            buf.append(path[j + 1])
            j += 2
            continue
        if ch == quote:
            break
        buf.append(ch)
        j += 1
    j += 1
    if j >= n or path[j] != "]":
        raise JsonPathError(f"{text!r}: expected ']' at position {j}")
    return "".join(buf), j + 1
