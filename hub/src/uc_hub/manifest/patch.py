"""RFC 6902 JSON Patch and RFC 6901 JSON Pointer.

The agent edits the draft manifest only through ``manifest_edit`` patches. A
patch is applied to a deep copy, so a failing operation leaves the draft
untouched; the error names the index of the failing operation so the model
can correct exactly that one.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from ..core.errors import InvalidRequest, NotFound

_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_OPS = ("add", "remove", "replace", "move", "copy", "test")


class PointerError(ValueError):
    """A JSON Pointer is malformed or does not resolve."""


def parse_pointer(ptr: str) -> list[str]:
    """``"/a/b~1c"`` -> ``["a", "b/c"]``; ``""`` is the whole document."""
    if not isinstance(ptr, str):
        raise PointerError(f"a JSON Pointer must be a string, not {type(ptr).__name__}")
    if ptr == "":
        return []
    if not ptr.startswith("/"):
        raise PointerError(f"JSON Pointer {ptr!r} must start with '/'")
    tokens = ptr[1:].split("/")
    for t in tokens:
        if re.search(r"~(?![01])", t):
            raise PointerError(f"JSON Pointer {ptr!r} has an invalid escape (only ~0 and ~1)")
    return [t.replace("~1", "/").replace("~0", "~") for t in tokens]


def format_pointer(tokens: list[str | int]) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def _array_index(token: str, array: list[Any], *, allow_end: bool) -> int:
    if token == "-" and allow_end:
        return len(array)
    if not _INDEX_RE.match(token):
        raise PointerError(f"{token!r} is not an array index")
    index = int(token)
    limit = len(array) if allow_end else len(array) - 1
    if index > limit:
        raise PointerError(f"index {index} is out of range (array length {len(array)})")
    return index


def _resolve(doc: Any, tokens: list[str]) -> Any:
    current = doc
    for depth, token in enumerate(tokens):
        where = format_pointer(list(tokens[: depth + 1]))
        if isinstance(current, dict):
            if token not in current:
                raise PointerError(f"{where} does not exist")
            current = current[token]
        elif isinstance(current, list):
            try:
                current = current[_array_index(token, current, allow_end=False)]
            except PointerError as e:
                raise PointerError(f"{where}: {e}") from None
        else:
            raise PointerError(f"{where}: cannot descend into a {_json_type(current)}")
    return current


def get_pointer(doc: Any, ptr: str) -> Any:
    """The value at ``ptr``; raises ``NotFound`` when it does not resolve."""
    try:
        return _resolve(doc, parse_pointer(ptr))
    except PointerError as e:
        raise NotFound(str(e)) from None


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def json_equal(a: Any, b: Any) -> bool:
    """Equality with JSON semantics: ``1 == 1.0``, but ``true != 1``."""
    ta, tb = _json_type(a), _json_type(b)
    if ta != tb:
        return False
    if ta == "object":
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if ta == "array":
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


class _Doc:
    """The document being patched; the root can be replaced."""

    def __init__(self, root: Any) -> None:
        self.root = root

    def parent(self, tokens: list[str]) -> tuple[Any, str]:
        return _resolve(self.root, tokens[:-1]), tokens[-1]

    def add(self, tokens: list[str], value: Any) -> None:
        if not tokens:
            self.root = value
            return
        container, key = self.parent(tokens)
        if isinstance(container, dict):
            container[key] = value
        elif isinstance(container, list):
            container.insert(_array_index(key, container, allow_end=True), value)
        else:
            raise PointerError(f"{format_pointer(list(tokens[:-1])) or '<root>'} is a "
                               f"{_json_type(container)}, not an object or array")

    def remove(self, tokens: list[str]) -> Any:
        if not tokens:
            raise PointerError("cannot remove the whole document")
        container, key = self.parent(tokens)
        if isinstance(container, dict):
            if key not in container:
                raise PointerError(f"{format_pointer(list(tokens))} does not exist")
            return container.pop(key)
        if isinstance(container, list):
            return container.pop(_array_index(key, container, allow_end=False))
        raise PointerError(f"{format_pointer(list(tokens[:-1])) or '<root>'} is a {_json_type(container)}")

    def replace(self, tokens: list[str], value: Any) -> None:
        if not tokens:
            self.root = value
            return
        container, key = self.parent(tokens)
        if isinstance(container, dict):
            if key not in container:
                raise PointerError(f"{format_pointer(list(tokens))} does not exist")
            container[key] = value
        elif isinstance(container, list):
            container[_array_index(key, container, allow_end=False)] = value
        else:
            raise PointerError(f"{format_pointer(list(tokens[:-1])) or '<root>'} is a {_json_type(container)}")


def apply_patch(doc: Any, patch: Any) -> Any:
    """Apply an RFC 6902 patch to a deep copy of ``doc`` and return it.

    Raises ``InvalidRequest`` naming the failing operation's index; ``doc``
    itself is never modified.
    """
    if not isinstance(patch, list):
        raise InvalidRequest(f"a JSON Patch must be an array of operations, not {_json_type(patch)}")
    target = _Doc(copy.deepcopy(doc))
    for i, op in enumerate(patch):
        try:
            _apply_op(target, op)
        except PointerError as e:
            name = op.get("op") if isinstance(op, dict) else None
            raise InvalidRequest(f"patch operation {i} ({name}): {e}") from None
    return target.root


def _member(op: dict[str, Any], key: str) -> Any:
    if key not in op:
        raise PointerError(f"missing {key!r}")
    return op[key]


def _apply_op(target: _Doc, op: Any) -> None:
    if not isinstance(op, dict):
        raise PointerError(f"an operation must be an object, not {_json_type(op)}")
    name = _member(op, "op")
    if name not in _OPS:
        raise PointerError(f"unknown op {name!r} (expected one of {', '.join(_OPS)})")
    path = _member(op, "path")
    tokens = parse_pointer(path)
    if name == "add":
        target.add(tokens, copy.deepcopy(_member(op, "value")))
    elif name == "remove":
        target.remove(tokens)
    elif name == "replace":
        target.replace(tokens, copy.deepcopy(_member(op, "value")))
    elif name == "test":
        expected = _member(op, "value")
        actual = _resolve(target.root, tokens)
        if not json_equal(actual, expected):
            raise PointerError(f"test failed at {path or '<root>'}: value is {_short(actual)}, "
                               f"expected {_short(expected)}")
    else:
        source = parse_pointer(_member(op, "from"))
        if name == "move":
            if source == tokens:
                return
            if tokens[: len(source)] == source:
                raise PointerError(f"cannot move {op['from']} into its own child {path}")
            value = target.remove(source)
        else:
            value = copy.deepcopy(_resolve(target.root, source))
        target.add(tokens, value)


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
