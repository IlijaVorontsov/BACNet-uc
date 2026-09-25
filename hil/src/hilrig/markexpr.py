"""Marker expressions for ``--hil-select`` (``not destructive``, ``rig or (release and not slow)``).

Twister gives each scenario its ``-m`` in ``pytest_args``; the nightly run narrows it
further with ``--hil-select EXPR`` (HIL.md 8.2, 10). pytest accepts ``-m`` once, so the
conftest deselects with this evaluator. It is the grammar of ``pytest -m`` (``and``,
``or``, ``not``, parentheses, marker names), parsed here so no pytest internals are needed.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_TOKEN = re.compile(r"\s*(\(|\)|[A-Za-z_][A-Za-z0-9_]*)")

Predicate = Callable[[frozenset[str]], bool]


class MarkExprError(ValueError):
    """The expression does not parse."""


def compile_expr(expr: str) -> Predicate:
    """Compile ``expr`` into a predicate over an item's set of marker names."""
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN.match(expr, pos)
        if not m:
            raise MarkExprError(f"--hil-select {expr!r}: unexpected {expr[pos:].strip()!r}")
        tokens.append(m[1])
        pos = m.end()
    if not tokens:
        raise MarkExprError("--hil-select: empty expression")
    predicate, rest = _or(tokens)
    if rest:
        raise MarkExprError(f"--hil-select {expr!r}: unexpected {' '.join(rest)!r}")
    return predicate


def _or(tokens: list[str]) -> tuple[Predicate, list[str]]:
    left, tokens = _and(tokens)
    while tokens and tokens[0] == "or":
        right, tokens = _and(tokens[1:])
        left = (lambda a, b: lambda marks: a(marks) or b(marks))(left, right)
    return left, tokens


def _and(tokens: list[str]) -> tuple[Predicate, list[str]]:
    left, tokens = _not(tokens)
    while tokens and tokens[0] == "and":
        right, tokens = _not(tokens[1:])
        left = (lambda a, b: lambda marks: a(marks) and b(marks))(left, right)
    return left, tokens


def _not(tokens: list[str]) -> tuple[Predicate, list[str]]:
    if not tokens:
        raise MarkExprError("--hil-select: expression ends early")
    if tokens[0] == "not":
        inner, rest = _not(tokens[1:])
        return (lambda marks: not inner(marks)), rest
    if tokens[0] == "(":
        inner, rest = _or(tokens[1:])
        if not rest or rest[0] != ")":
            raise MarkExprError("--hil-select: missing ')'")
        return inner, rest[1:]
    name = tokens[0]
    if name in ("and", "or", ")"):
        raise MarkExprError(f"--hil-select: unexpected {name!r}")
    return (lambda marks: name in marks), tokens[1:]
