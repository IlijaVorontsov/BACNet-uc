"""RFC 6902 JSON Patch, RFC 6901 pointers, and unified diffs."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from uc_hub.core.errors import InvalidRequest, NotFound, ValidationFailed
from uc_hub.manifest import SiteManifest, apply_patch, get_pointer, json_diff, text_diff, yaml_diff
from uc_hub.manifest.patch import PointerError, format_pointer, json_equal, parse_pointer

DOC: dict[str, Any] = {"a": {"b": [1, 2, 3], "c/d": "slash", "e~f": "tilde"}, "n": None, "": "empty key"}


def patched(ops: list[dict[str, Any]], doc: Any = None) -> Any:
    source = copy.deepcopy(DOC if doc is None else doc)
    before = copy.deepcopy(source)
    out = apply_patch(source, ops)
    assert source == before, "the input must not be modified"
    return out


def failing(ops: Any, doc: Any = None) -> str:
    with pytest.raises(InvalidRequest) as info:
        patched(ops, doc)
    return str(info.value)


# -- pointers ------------------------------------------------------------------------------
@pytest.mark.parametrize(("ptr", "tokens"), [
    ("", []), ("/", [""]), ("/a/b/0", ["a", "b", "0"]), ("/a/c~1d", ["a", "c/d"]), ("/a/e~0f", ["a", "e~f"]),
    ("/~01", ["~1"]), ("/a//b", ["a", "", "b"]),
])
def test_parse_pointer(ptr: str, tokens: list[str]) -> None:
    assert parse_pointer(ptr) == tokens
    assert format_pointer(list(tokens)) == ptr


@pytest.mark.parametrize("ptr", ["a/b", "/a~2", "/a~", 5])
def test_bad_pointers(ptr: Any) -> None:
    with pytest.raises(PointerError):
        parse_pointer(ptr)


def test_get_pointer() -> None:
    assert get_pointer(DOC, "/a/c~1d") == "slash"
    assert get_pointer(DOC, "/a/e~0f") == "tilde"
    assert get_pointer(DOC, "/a/b/2") == 3
    assert get_pointer(DOC, "/") == "empty key"
    assert get_pointer(DOC, "") == DOC
    for bad in ("/a/b/3", "/a/b/-", "/a/b/01", "/x", "/n/x", "/a/b/0/x"):
        with pytest.raises(NotFound):
            get_pointer(DOC, bad)


# -- operations -----------------------------------------------------------------------------------
def test_add() -> None:
    out = patched([
        {"op": "add", "path": "/a/b/-", "value": 4},
        {"op": "add", "path": "/a/b/0", "value": 0},
        {"op": "add", "path": "/a/b/5", "value": 5},
        {"op": "add", "path": "/a/new", "value": {"x": [1]}},
        {"op": "add", "path": "/a/c~1d", "value": "replaced by add"},
    ])
    assert out["a"]["b"] == [0, 1, 2, 3, 4, 5]
    assert out["a"]["new"] == {"x": [1]}
    assert out["a"]["c/d"] == "replaced by add"


def test_add_root_replaces_document() -> None:
    assert patched([{"op": "add", "path": "", "value": [1]}]) == [1]


def test_remove_and_replace() -> None:
    out = patched([
        {"op": "remove", "path": "/a/b/1"},
        {"op": "remove", "path": "/a/e~0f"},
        {"op": "replace", "path": "/n", "value": 7},
        {"op": "replace", "path": "/a/b/0", "value": "first"},
    ])
    assert out["a"]["b"] == ["first", 3]
    assert "e~f" not in out["a"]
    assert out["n"] == 7
    assert patched([{"op": "replace", "path": "", "value": {"z": 1}}]) == {"z": 1}


def test_move_and_copy() -> None:
    out = patched([
        {"op": "copy", "from": "/a/b", "path": "/copy"},
        {"op": "move", "from": "/a/b/0", "path": "/a/b/-"},
        {"op": "move", "from": "/a/c~1d", "path": "/moved"},
        {"op": "move", "from": "/moved", "path": "/moved"},
    ])
    assert out["copy"] == [1, 2, 3]
    assert out["a"]["b"] == [2, 3, 1]
    assert out["moved"] == "slash" and "c/d" not in out["a"]
    out["copy"].append(9)
    assert out["a"]["b"] == [2, 3, 1], "copy must not alias"


def test_values_are_copied() -> None:
    value = {"k": [1]}
    out = patched([{"op": "add", "path": "/v", "value": value}])
    value["k"].append(2)
    assert out["v"] == {"k": [1]}


def test_test_op() -> None:
    assert patched([{"op": "test", "path": "/a/b", "value": [1, 2.0, 3]}]) == DOC
    assert patched([{"op": "test", "path": "/n", "value": None}]) == DOC
    message = failing([{"op": "add", "path": "/x", "value": 1}, {"op": "test", "path": "/a/b/0", "value": True}])
    assert message.startswith("patch operation 1 (test): test failed at /a/b/0: value is 1, expected True")


@pytest.mark.parametrize(("a", "b", "equal"), [
    (1, 1.0, True), (True, 1, False), (False, 0, False), ({"a": [1]}, {"a": [1.0]}, True),
    ({"a": 1}, {"a": 1, "b": 2}, False), ([1, 2], [2, 1], False), (None, None, True), ("1", 1, False),
])
def test_json_equal(a: Any, b: Any, equal: bool) -> None:
    assert json_equal(a, b) is equal


@pytest.mark.parametrize(("ops", "message"), [
    ({"op": "add"}, "a JSON Patch must be an array of operations, not object"),
    (["add"], "patch operation 0 (None): an operation must be an object, not string"),
    ([{"path": "/x", "value": 1}], "patch operation 0 (None): missing 'op'"),
    ([{"op": "merge", "path": "/x"}], "patch operation 0 (merge): unknown op 'merge'"),
    ([{"op": "add", "value": 1}], "patch operation 0 (add): missing 'path'"),
    ([{"op": "add", "path": "/x"}], "patch operation 0 (add): missing 'value'"),
    ([{"op": "copy", "path": "/x"}], "patch operation 0 (copy): missing 'from'"),
    ([{"op": "add", "path": "x", "value": 1}], "patch operation 0 (add): JSON Pointer 'x' must start with '/'"),
    ([{"op": "add", "path": "/missing/x", "value": 1}], "patch operation 0 (add): /missing does not exist"),
    ([{"op": "add", "path": "/a/b/4", "value": 1}], "index 4 is out of range (array length 3)"),
    ([{"op": "add", "path": "/a/b/01", "value": 1}], "'01' is not an array index"),
    ([{"op": "add", "path": "/n/x", "value": 1}], "/n is a null, not an object or array"),
    ([{"op": "remove", "path": "/a/b/-"}], "'-' is not an array index"),
    ([{"op": "remove", "path": "/a/b/3"}], "index 3 is out of range"),
    ([{"op": "remove", "path": "/zz"}], "/zz does not exist"),
    ([{"op": "remove", "path": ""}], "cannot remove the whole document"),
    ([{"op": "replace", "path": "/zz", "value": 1}], "/zz does not exist"),
    ([{"op": "move", "from": "/a", "path": "/a/b/0"}], "cannot move /a into its own child /a/b/0"),
    ([{"op": "move", "from": "/zz", "path": "/y"}], "/zz does not exist"),
    ([{"op": "test", "path": "/zz", "value": 1}], "/zz does not exist"),
])
def test_errors(ops: Any, message: str) -> None:
    assert message in failing(ops)


def test_failing_patch_leaves_nothing_behind() -> None:
    doc = copy.deepcopy(DOC)
    with pytest.raises(InvalidRequest, match="operation 2"):
        apply_patch(doc, [
            {"op": "add", "path": "/x", "value": 1},
            {"op": "remove", "path": "/a/b/0"},
            {"op": "remove", "path": "/nope"},
        ])
    assert doc == DOC


def test_patch_then_validate_a_site(doc: dict[str, Any]) -> None:
    patch = [
        {"op": "add", "path": "/system/nodes/0/io/-", "value": {"channel": "do0", "type": "binary-output",
                                                               "instance": 1, "name": "R204 Heater"}},
        {"op": "replace", "path": "/system/apps/0/params/setpoint", "value": 22},
        {"op": "add", "path": "/tags/r204-ctl~1binary-output:1", "value": ["Heater_Command"]},
    ]
    site = SiteManifest.from_dict(apply_patch(doc, patch))
    assert site.io_json("r204-ctl")["points"][-1]["channel"] == "do0"
    assert site.tags["hq/r204-ctl/binary-output:1"] == ["Heater_Command"]
    bad = apply_patch(doc, [{"op": "replace", "path": "/system/apps/0/node", "value": "nowhere"}])
    with pytest.raises(ValidationFailed):
        SiteManifest.from_dict(bad)


# -- diffs ----------------------------------------------------------------------------------------
def test_json_diff_is_stable() -> None:
    before = {"schema": 1, "points": [{"type": "analog-input", "channel": "ai0", "instance": 1}]}
    after = {"points": [{"instance": 2, "channel": "ai0", "type": "analog-input"}], "schema": 1}
    diff = json_diff(before, after, "io.json")
    assert diff.startswith("--- a/io.json\n+++ b/io.json\n@@ ")
    assert '-      "instance": 1,\n+      "instance": 2,\n' in diff
    reordered = {"schema": 1, "points": [{"channel": "ai0", "type": "analog-input", "instance": 1}]}
    assert json_diff(before, reordered, "io.json") == ""
    assert json_diff(after, dict(reversed(list(after.items()))), "x") == ""


def test_diff_of_new_and_removed_documents() -> None:
    added = json_diff(None, {"a": "ü"}, "device.json")
    assert added.startswith("--- /dev/null\n+++ b/device.json\n")
    assert '+  "a": "ü"\n' in added
    assert json_diff({"a": 1}, None, "x").startswith("--- a/x\n+++ /dev/null\n")
    assert json_diff(None, None, "x") == ""


def test_text_and_yaml_diffs() -> None:
    assert yaml_diff("a: 1\nb: 2\n", "a: 1\nb: 3\n") == (
        "--- a/site.yaml\n+++ b/site.yaml\n@@ -1,2 +1,2 @@\n a: 1\n-b: 2\n+b: 3\n")
    assert "\\ No newline at end of file" in text_diff("x", "y\n", "f")
    assert text_diff("same\n", "same\n", "f") == ""
