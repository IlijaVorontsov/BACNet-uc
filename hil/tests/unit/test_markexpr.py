"""--hil-select marker expressions (hilrig.markexpr), same grammar as pytest -m."""

from __future__ import annotations

import pytest

from hilrig.markexpr import MarkExprError, compile_expr


@pytest.mark.parametrize(
    ("expr", "marks", "expected"),
    [
        ("not destructive", {"release"}, True),
        ("not destructive", {"release", "destructive"}, False),
        ("rig or release", {"rig"}, True),
        ("rig or release", {"instrumented"}, False),
        ("rig or (release and not slow)", {"release", "slow"}, False),
        ("rig or (release and not slow)", {"release"}, True),
        ("not (a or b) and c", {"c"}, True),
        ("not (a or b) and c", {"b", "c"}, False),
        ("not not a", {"a"}, True),
    ],
)
def test_expressions(expr: str, marks: set[str], expected: bool) -> None:
    assert compile_expr(expr)(frozenset(marks)) is expected


@pytest.mark.parametrize("expr", ["", "a and", "(a or b", "a b", "or a", "a )", "a-b"])
def test_bad_expressions_are_refused(expr: str) -> None:
    with pytest.raises(MarkExprError):
        compile_expr(expr)
