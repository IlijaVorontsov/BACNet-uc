from __future__ import annotations

from typing import Any

import pytest

from uc_hub.drivers.mqtt.jsonpath import JsonPath, JsonPathError

DOC: dict[str, Any] = {
    "ppm": 612.5,
    "bat": {"pct": 97, "low": False},
    "list": [10, 20, {"deep": ["x", "y"]}],
    "a.b": 1,
    "it's": 2,
    "0": "key zero",
    "": "empty key",
    "nothing": None,
    "sp ace": 3,
}


@pytest.mark.parametrize(("path", "value"), [
    ("$", DOC),
    ("$.ppm", 612.5),
    ("$.bat.pct", 97),
    ("$.bat.low", False),
    ("$['bat']['pct']", 97),
    ('$["bat"].pct', 97),
    ("$.list[0]", 10),
    ("$.list[-1].deep[1]", "y"),
    ("$.list[ 1 ]", 20),
    ("$['a.b']", 1),
    ("$['it\\'s']", 2),
    ('$["it\'s"]', 2),
    ("$['0']", "key zero"),
    ("$['']", "empty key"),
    ("$['sp ace']", 3),
    ("$.nothing", None),
    ("ppm", 612.5),
    ("bat.pct", 97),
    (".bat.pct", 97),
    ("  $.ppm  ", 612.5),
])
def test_found(path: str, value: Any) -> None:
    assert JsonPath.compile(path).find(DOC) == (True, value)


@pytest.mark.parametrize(("path", "doc"), [
    ("$.missing", DOC),
    ("$.ppm.deeper", DOC),
    ("$.list[3]", DOC),
    ("$.list[-4]", DOC),
    ("$[0]", DOC),              # an index never reads a key "0"
    ("$.list.0", DOC),          # a key never reads a list element
    ("$.bat[0]", DOC),
    ("$.x", [1, 2]),
    ("$[0]", "text"),
    ("$.a", None),
    ("$[0]", {"0": 1}),
])
def test_missing(path: str, doc: Any) -> None:
    assert JsonPath.compile(path).find(doc) == (False, None)


def test_root_of_scalar_and_list() -> None:
    assert JsonPath.compile("$").find(21.5) == (True, 21.5)
    assert JsonPath.compile("$[1]").find([1, 2]) == (True, 2)
    assert JsonPath.compile("[1]").find([1, 2]) == (True, 2)


@pytest.mark.parametrize("path", [
    "", "   ", "$.", "$..ppm", "$.bat.", "$[", "$[1", "$[x]", "$[1.5]", "$['a'",
    "$['a]", "$['a'x]", "$['a\\", "$ .a", "$.a b", "$.a]", "$*", "$[*]", "$.a[]",
])
def test_syntax_errors(path: str) -> None:
    with pytest.raises(JsonPathError):
        JsonPath.compile(path)


def test_not_a_string() -> None:
    with pytest.raises(JsonPathError):
        JsonPath.compile(5)  # type: ignore[arg-type]


@pytest.mark.parametrize(("path", "canonical"), [
    ("ppm", "$.ppm"),
    ("$['bat'].pct", "$.bat.pct"),
    ("$.list[-1]", "$.list[-1]"),
    ("$['a.b']", "$['a.b']"),
    ('$["it\'s"]', "$['it\\'s']"),
    ("$['sp ace'][0]", "$['sp ace'][0]"),
    ("$", "$"),
])
def test_str_round_trips(path: str, canonical: str) -> None:
    compiled = JsonPath.compile(path)
    assert str(compiled) == canonical
    assert JsonPath.compile(canonical) == compiled


def test_json_path_error_is_value_error() -> None:
    assert issubclass(JsonPathError, ValueError)
