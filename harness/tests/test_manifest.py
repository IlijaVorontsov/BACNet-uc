# SPDX-License-Identifier: Apache-2.0
"""System manifests: schema validation, placeholders, semantic checks."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness import manifest as m

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "systems"


def base_doc() -> dict[str, Any]:
    return {
        "apiVersion": "bacnet-uc/v1",
        "kind": "System",
        "metadata": {"name": "t"},
        "nodes": [
            {"name": "a", "board": "native_sim/native/64", "transport": {"kind": "sim"},
             "device": {"instance": 11, "name": "a"},
             "io": [{"channel": "ai0", "type": "analog-input", "instance": 1},
                    {"channel": "di0", "type": "binary-input", "instance": 1}]},
            {"name": "b", "board": "nucleo_f767zi",
             "transport": {"kind": "udp", "host": "192.168.1.2"},
             "device": {"instance": 12, "name": "b"},
             "io": [{"channel": "ao0", "type": "analog-output", "instance": 1},
                    {"channel": "do0", "type": "binary-output", "instance": 1}]},
        ],
        "apps": [{"name": "thermostat", "node": "b", "wasm": "t.wasm",
                  "params": {"sensor_device": "{{ nodes.a.device.instance }}"}}],
        "links": [{"from": "a/binary-input:1", "to": "b/binary-output:1"}],
        "tests": [{"name": "t1", "steps": [{"force": {"node": "a", "channel": "di0", "value": 1}},
                                          {"expect": {"point": "b/binary-output:1", "op": "eq",
                                                      "value": 1, "within_ms": 100}}]}],
    }


def errors(doc: dict[str, Any], **kw: Any) -> list[m.Issue]:
    _, issues = m.check_system(doc, **kw)
    return [i for i in issues if i.severity == "error"]


def warnings(doc: dict[str, Any], **kw: Any) -> list[m.Issue]:
    _, issues = m.check_system(doc, **kw)
    return [i for i in issues if i.severity == "warning"]


def assert_error(doc: dict[str, Any], path: str, fragment: str, **kw: Any) -> None:
    errs = errors(doc, **kw)
    assert any(i.path == path and fragment in i.message for i in errs), errs


# --- good manifests ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hvac-demo.yaml", "sim-demo.yaml"])
def test_examples_load(name: str) -> None:
    system = m.load_system(EXAMPLES / name)
    assert system.name == name.removesuffix(".yaml")
    assert not system.warnings
    assert system.nodes and system.tests
    for app in system.apps:
        assert app.source is not None and app.source.is_file()


def test_base_doc_is_valid() -> None:
    system, issues = m.check_system(base_doc())
    assert system is not None, issues
    assert system.node("b").transport.host == "192.168.1.2"
    assert system.node("a").is_sim
    assert system.apps[0].kind == "thermostat"
    assert system.links[0].src_type == 3 and system.links[0].dst_type == 4


def test_system_model_helpers() -> None:
    system = m.load_system_dict(base_doc())
    assert system.node_names() == ["a", "b"]
    assert [a.name for a in system.apps_on("b")] == ["thermostat"]
    assert [lk.to_ref for lk in system.links_to("b")] == ["b/binary-output:1"]
    assert [n.name for n in system.sim_nodes()] == ["a"]
    assert system.test("t1").steps[0]["force"]["channel"] == "di0"
    summary = system.summary()
    assert summary["nodes"][0]["device_instance"] == 11
    with pytest.raises(m.HarnessError):
        system.node("zz")


def test_json_manifest(tmp_path: Path) -> None:
    import json

    f = tmp_path / "s.json"
    doc = base_doc()
    doc["apps"] = []
    f.write_text(json.dumps(doc))
    assert m.load_system(f).path == f.resolve()


# --- placeholders -----------------------------------------------------------------------------


def test_placeholder_keeps_type() -> None:
    system = m.load_system_dict(base_doc())
    assert system.apps[0].params["sensor_device"] == 11


def test_placeholder_interpolation_and_paths() -> None:
    doc = base_doc()
    doc["metadata"]["description"] = "{{ metadata.name }} with {{ nodes.1.device.name }}"
    doc["nodes"][1]["device"]["location"] = "near {{ nodes.a.transport.kind }}"
    doc["apps"][0]["params"]["note"] = "{{nodes.b.io.0.channel}}-x"
    system = m.load_system_dict(doc)
    assert system.description == "t with b"
    assert system.node("b").device["location"] == "near sim"
    assert system.apps[0].params["note"] == "ao0-x"


def test_placeholder_chain() -> None:
    doc = base_doc()
    doc["nodes"][1]["device"]["instance"] = "{{ nodes.a.device.instance }}"
    doc["nodes"][0]["device"]["instance"] = 7
    doc["nodes"][1]["device"]["name"] = "b{{ nodes.b.device.instance }}"
    resolved, issues = m.resolve_placeholders(doc)
    assert not issues
    assert resolved["nodes"][1]["device"]["instance"] == 7
    assert resolved["nodes"][1]["device"]["name"] == "b7"


def test_placeholder_unknown_and_cycle() -> None:
    doc = base_doc()
    doc["apps"][0]["params"]["x"] = "{{ nodes.zz.device.instance }}"
    doc["nodes"][0]["device"]["name"] = "{{ nodes.a.device.description }}"
    doc["nodes"][0]["device"]["description"] = "{{ nodes.a.device.name }}"
    _, issues = m.resolve_placeholders(doc)
    msgs = {i.path: i.message for i in issues}
    assert "no element named 'zz'" in msgs["/apps/0/params/x"]
    assert "cycle" in msgs["/nodes/0/device/name"]
    assert m.check_system(doc)[0] is None


def test_placeholder_bool_and_float_text() -> None:
    doc = base_doc()
    doc["apps"][0]["params"]["flag"] = "{{ apps.0.autostart_x }}"
    assert errors(doc)
    doc["apps"][0]["autostart"] = False
    doc["apps"][0]["params"]["flag"] = "v={{ apps.0.autostart }}"
    doc["apps"][0]["params"]["sp"] = "{{ apps.0.params.base }}"
    doc["apps"][0]["params"]["base"] = 21.0
    system = m.load_system_dict(doc)
    assert system.apps[0].params["flag"] == "v=false"
    assert system.apps[0].params["sp"] == 21.0


def test_lookup_path_errors() -> None:
    with pytest.raises(KeyError):
        m.lookup_path({"a": [1]}, "a.5")
    with pytest.raises(KeyError):
        m.lookup_path({"a": 1}, "a.b")


# --- YAML -------------------------------------------------------------------------------------


def test_yaml_12_booleans() -> None:
    doc = m.parse_text("params: {on: 1, off: 0, yes: y}\nflag: true\n")
    assert doc["params"] == {"on": 1, "off": 0, "yes": "y"}
    assert doc["flag"] is True


def test_yaml_parse_error() -> None:
    with pytest.raises(m.ManifestError, match="cannot parse"):
        m.parse_text("a: [1, 2")


def test_missing_file() -> None:
    with pytest.raises(m.ManifestError, match="cannot read"):
        m.load_system("/nonexistent/x.yaml")


# --- schema errors ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "path", "fragment"),
    [
        (lambda d: d.update(apiVersion="v2"), "/apiVersion", "bacnet-uc/v1"),
        (lambda d: d["metadata"].update(name="Bad Name"), "/metadata/name", "does not match"),
        (lambda d: d["nodes"][0].update(board="esp32"), "/nodes/0/board", "is not one of"),
        (lambda d: d["nodes"][0]["device"].update(instance=4194303), "/nodes/0/device/instance",
         "maximum"),
        (lambda d: d["nodes"][1]["transport"].pop("host"), "/nodes/1/transport",
         "'host' is a required property"),
        (lambda d: d["nodes"][0]["io"][0].update(type="analog-thing"), "/nodes/0/io/0/type",
         "is not one of"),
        (lambda d: d["nodes"][0].update(extra=1), "/nodes/0", "Additional properties"),
        (lambda d: d["apps"][0].update(source="x.c"), "/apps/0", "valid under each"),
        (lambda d: d["apps"][0].update(perms=["root"]), "/apps/0/perms/0", "is not one of"),
        (lambda d: d["links"][0].update(to="b-binary-output"), "/links/0/to", "does not match"),
        (lambda d: d["links"][0].update(period_ms=10), "/links/0/period_ms", "minimum"),
        (lambda d: d["tests"][0]["steps"][1]["expect"].update(op="like"),
         "/tests/0/steps/1/expect/op", "is not one of"),
        (lambda d: d["nodes"][0].update(network={"dhcp": False, "ipv4": "300.1.1.1"}),
         "/nodes/0/network/ipv4", "is not a 'ipv4'"),
        (lambda d: d.update(nodes=[]), "/nodes", "should be non-empty"),
    ],
)
def test_schema_errors(mutate: Any, path: str, fragment: str) -> None:
    doc = base_doc()
    mutate(doc)
    assert_error(doc, path, fragment)


def test_manifest_error_lists_all() -> None:
    doc = base_doc()
    doc["apiVersion"] = "x"
    doc["metadata"]["name"] = "BAD"
    with pytest.raises(m.ManifestError) as exc:
        m.load_system_dict(doc, source="mem")
    assert len(exc.value.errors) == 2
    assert "2 error(s) in manifest mem" in str(exc.value)
    assert all(i.path for i in exc.value.errors)
    assert exc.value.messages[0].startswith("/")


def test_not_a_mapping() -> None:
    assert errors([1, 2])[0].message == "manifest must be a mapping"  # type: ignore[arg-type]


# --- semantic errors --------------------------------------------------------------------------


def test_duplicate_node_name_and_instance() -> None:
    doc = base_doc()
    doc["nodes"][1]["name"] = "a"
    doc["nodes"][1]["device"]["instance"] = 11
    doc["apps"] = []
    doc["links"] = []
    doc["tests"] = []
    errs = errors(doc)
    assert any(i.path == "/nodes/1/name" and "duplicate node name" in i.message for i in errs)
    assert any(i.path == "/nodes/1/device/instance" and "already used" in i.message
               for i in errs)


def test_duplicate_app_and_unknown_node() -> None:
    doc = base_doc()
    doc["apps"].append(copy.deepcopy(doc["apps"][0]))
    doc["apps"].append({"name": "x", "node": "zz", "wasm": "x.wasm"})
    errs = errors(doc)
    assert any(i.path == "/apps/1/name" and "duplicate app" in i.message for i in errs)
    assert any(i.path == "/apps/2/node" and "unknown node 'zz'" in i.message for i in errs)


def test_reserved_link_app_name() -> None:
    doc = base_doc()
    doc["apps"].append({"name": "link", "node": "b", "wasm": "x.wasm"})
    assert_error(doc, "/apps/1/name", "reserved")


def test_link_errors() -> None:
    doc = base_doc()
    doc["links"] = [
        {"from": "zz/analog-input:1", "to": "b/analog-output:1"},
        {"from": "a/analog-input:1", "to": "b/analog-input:5"},
        {"from": "a/wrong-type:1", "to": "b/analog-value:5"},
        {"from": "b/analog-output:1", "to": "b/analog-output:1"},
    ]
    errs = errors(doc)
    assert any(i.path == "/links/0/from" and "unknown node 'zz'" in i.message for i in errs)
    assert any(i.path == "/links/1/to" and "not writable" in i.message for i in errs)
    assert any(i.path == "/links/2/from" and "unknown BACnet object type" in i.message
               for i in errs)
    assert any(i.path == "/links/3" and "onto itself" in i.message for i in errs)


def test_test_step_errors() -> None:
    doc = base_doc()
    doc["tests"][0]["steps"] += [
        {"force": {"node": "zz", "channel": "di0", "value": 1}},
        {"expect": {"point": "a/foo-bar:1", "op": "eq", "value": 1}},
        {"write": {"point": "zz/analog-value:1", "value": 1, "property": "no-such-prop"}},
        {"sleep": 10},
    ]
    doc["tests"].append({"name": "t1", "steps": [{"wait": 1}]})
    errs = errors(doc)
    paths = {i.path for i in errs}
    assert "/tests/0/steps/2/force/node" in paths
    assert "/tests/0/steps/3/expect/point" in paths
    assert "/tests/0/steps/4/write/point" in paths
    assert "/tests/0/steps/4/write/property" in paths
    assert "/tests/0/steps/5/sleep" in paths
    assert "/tests/1/name" in paths


def test_sim_transport_needs_sim_board() -> None:
    doc = base_doc()
    doc["nodes"][1]["transport"] = {"kind": "sim"}
    assert_error(doc, "/nodes/1/transport/kind", "native_sim")


def test_app_param_limits() -> None:
    doc = base_doc()
    doc["apps"][0]["params"]["long"] = "x" * 96
    doc["apps"][0]["params"]["bad key!"] = "1"
    errs = errors(doc)
    assert any("longer than 95" in i.message for i in errs)
    assert any("1..23 characters" in i.message for i in errs)


def test_too_many_apps() -> None:
    doc = base_doc()
    doc["apps"] = [{"name": f"a{i}", "node": "b", "wasm": "x.wasm"} for i in range(8)]
    assert_error(doc, "/nodes/1", "apps.json allows 8")
    doc["apps"] = doc["apps"][:4]
    assert any("CONFIG_UC_APPS_MAX" in w.message for w in warnings(doc))


def test_files_checked_relative_to_base_dir(tmp_path: Path) -> None:
    doc = base_doc()
    assert_error(doc, "/apps/0/wasm", "file not found", base_dir=tmp_path)
    (tmp_path / "t.wasm").write_bytes(b"\0asm\1\0\0\0")
    system = m.load_system_dict(doc, base_dir=tmp_path)
    assert system.apps[0].wasm == tmp_path / "t.wasm"


# --- IO catalogs ------------------------------------------------------------------------------


def test_builtin_catalog_parsing() -> None:
    cat = m.builtin_catalog("nucleo_f767zi")
    assert cat is not None
    assert cat["di0"] == "di" and cat["ai5"] == "ai" and cat["ao2"] == "ao"
    assert "channel@3" not in cat
    assert m.builtin_catalog("native_sim")["ao1"] == "ao"
    assert m.builtin_catalog("nonexistent_board") is None


def test_builtin_catalog_findings_are_warnings() -> None:
    doc = base_doc()
    doc["nodes"][1]["io"].append({"channel": "ai9", "type": "analog-input", "instance": 9})
    doc["nodes"][1]["io"].append({"channel": "di1", "type": "analog-input", "instance": 10})
    ws = warnings(doc)
    assert any(w.path == "/nodes/1/io/2/channel" and "ai9" in w.message for w in ws)
    assert any(w.path == "/nodes/1/io/3/type" and "cannot be bound" in w.message for w in ws)
    assert not warnings(doc, builtin_catalogs=False)


def test_explicit_catalog_findings_are_errors() -> None:
    doc = base_doc()
    live = {"board": "x", "channels": [{"name": "ai0", "kind": "ai"}, {"name": "di0",
                                                                        "kind": "di"}]}
    assert not errors(doc, catalogs={"a": live})
    assert_error(doc, "/nodes/1/io/0/channel", "not in the IO catalog",
                 catalogs={"nucleo_f767zi": ["di0"]})
    doc["tests"][0]["steps"][0]["force"]["channel"] = "di7"
    assert_error(doc, "/tests/0/steps/0/force/channel", "not in the IO catalog",
                 catalogs={"a": {"ai0": "ai", "di0": "di"}})


def test_duplicate_channel_is_warning() -> None:
    doc = base_doc()
    doc["nodes"][0]["io"].append({"channel": "di0", "type": "binary-input", "instance": 2})
    assert any("bound twice" in w.message for w in warnings(doc))


# --- object collisions ------------------------------------------------------------------------


def test_io_io_collision() -> None:
    doc = base_doc()
    doc["nodes"][0]["io"].append({"channel": "di1", "type": "binary-input", "instance": 1})
    assert_error(doc, "/nodes/0/io/2/instance", "already defined by io point 'di0'")


def test_app_object_collides_with_io() -> None:
    doc = base_doc()
    doc["nodes"][1]["io"].append({"channel": "ao1", "type": "analog-value", "instance": 1})
    assert_error(doc, "/apps/0/params", "collides with io point 'ao1'")
    doc["apps"][0]["params"]["sp_instance"] = 5
    assert not errors(doc)


def test_app_app_collision_known_kinds() -> None:
    doc = base_doc()
    doc["apps"].append({"name": "alarm", "node": "b", "wasm": "alarm.wasm",
                        "params": {"bv_instance": 3, "count_instance": 1}})
    doc["apps"].append({"name": "blink", "node": "b", "source": "blinky.c",
                        "params": {"type": 5, "instance": 3}})
    errs = errors(doc)
    assert any(i.path == "/apps/1/params" and "setpoint" in i.message for i in errs)
    assert any(i.path == "/apps/2/params" and "binary-value:3" in i.message for i in errs)


def test_link_destination_collisions() -> None:
    doc = base_doc()
    doc["links"] = [
        {"from": "a/analog-input:1", "to": "b/analog-value:1"},  # thermostat setpoint
        {"from": "a/analog-input:1", "to": "b/analog-output:1", "priority": 8},
        {"from": "a/binary-input:1", "to": "b/analog-output:1", "priority": 8},
        {"from": "a/binary-input:1", "to": "b/binary-output:7"},  # not an IO point
    ]
    errs = errors(doc)
    assert any(i.path == "/links/2/to" and "same priority" in i.message for i in errs)
    ws = warnings(doc)
    assert any(w.path == "/links/0/to" and "start order" in w.message for w in ws)
    assert any(w.path == "/links/3/to" and "neither an IO point" in w.message for w in ws)
    doc["links"][2]["priority"] = 9
    assert not errors(doc)


def test_link_value_object_priorities() -> None:
    """Value objects have no priority array: one writer only, priority ignored."""
    doc = base_doc()
    doc["links"] = [
        {"from": "a/analog-input:1", "to": "b/analog-value:9"},
        {"from": "a/binary-input:1", "to": "b/analog-value:9", "priority": 9},
    ]
    assert_error(doc, "/links/1/to", "value object has no priority array")
    ws = warnings(doc)
    assert any(w.path == "/links/1/priority" and "ignored" in w.message for w in ws)
    doc["links"] = [{"from": "a/analog-input:1", "to": "b/analog-output:1", "priority": 6}]
    assert_error(doc, "/links/0/priority", "reserved for minimum on/off")
    doc["links"][0]["priority"] = 8
    assert not errors(doc) and not [w for w in warnings(doc) if "priority" in w.path]


def test_test_write_value_object_warnings() -> None:
    doc = base_doc()
    doc["tests"][0]["steps"] = [
        {"write": {"point": "b/analog-value:1", "value": 23.0, "priority": 8}},
        {"write": {"point": "b/analog-value:1", "value": None, "priority": 8}},
        {"write": {"point": "b/analog-value:1", "value": 21.0}},
        {"write": {"point": "b/analog-output:1", "value": 5, "priority": 6}},
    ]
    ws = {w.path for w in warnings(doc)}
    assert "/tests/0/steps/0/write/priority" in ws and "/tests/0/steps/1/write/value" in ws
    assert not any(p.startswith("/tests/0/steps/2") for p in ws)
    assert_error(doc, "/tests/0/steps/3/write/priority", "reserved")


def test_link_source_warning() -> None:
    doc = base_doc()
    doc["links"] = [{"from": "a/analog-value:77", "to": "b/analog-output:1"}]
    assert any(w.path == "/links/0/from" and "analog-value:77" in w.message
               for w in warnings(doc))


def test_bacnet_address_checks() -> None:
    doc = base_doc()
    doc["nodes"][1]["bacnet_address"] = "10.0.0.2:x"
    assert_error(doc, "/nodes/1/bacnet_address", "invalid bacnet_address")
    doc["nodes"][1]["bacnet_address"] = "10.0.0.2:47809"
    assert any("differs from bacnet.udp_port" in w.message for w in warnings(doc))


# --- app knowledge ----------------------------------------------------------------------------


def test_detect_app_kind() -> None:
    assert m.detect_app_kind("x", "src/uc_link.c", None) == "uc-link"
    assert m.detect_app_kind("x", None, "build/alarm.wasm") == "alarm"
    assert m.detect_app_kind("blinky", None, "foo.wasm") == "blinky"
    assert m.detect_app_kind("x", "mine.c", None) is None


def test_is_remote_device() -> None:
    assert not m.is_remote_device("local", 5)
    assert not m.is_remote_device(5, 5)
    assert not m.is_remote_device("4294967295", 5)
    assert m.is_remote_device(6, 5)
    assert m.is_remote_device("peer", 5)


def test_parse_link_line() -> None:
    link = m.parse_link_line("1001 0 1 2 10 cov 1000 0 1 0")
    assert link == {"src_device": 1001, "src_type": 0, "src_instance": 1, "dst_type": 2,
                    "dst_instance": 10, "mode": "cov", "period_ms": 1000, "priority": 0,
                    "scale": 1.0, "offset": 0.0}
    with pytest.raises(ValueError):
        m.parse_link_line("1 2 3")


def test_validation_report(tmp_path: Path) -> None:
    rep = m.validation_report(EXAMPLES / "sim-demo.yaml")
    assert rep["ok"] and rep["system"]["name"] == "sim-demo"
    doc = base_doc()
    doc["kind"] = "Other"
    rep = m.validation_report(doc)
    assert not rep["ok"] and rep["errors"][0]["path"] == "/kind"
    bad = tmp_path / "bad.yaml"
    bad.write_text("nodes: [")
    assert not m.validation_report(bad)["ok"]


def test_validate_document_other_schemas() -> None:
    assert not m.validate_document("io", {"schema": 1, "points": []})
    issues = m.validate_document("apps", {"schema": 1, "apps": [{"name": "X", "file": "/x"}]})
    assert {i.path for i in issues} == {"/apps/0/name", "/apps/0/file"}
    with pytest.raises(m.HarnessError):
        m.load_schema("nope")
    assert m.schema_names() == ["apps", "device", "io", "system"]


def test_app_points_and_objects_in_use() -> None:
    link = m.app_points("uc-link", {"count": "2", "l0": "11 0 1 1 1 cov 1000 8 1 0",
                                    "l1": "12 3 1 5 7 poll 500 0 1 0"})
    assert link == [m.PointRef(11, 0, 1, "input"), m.PointRef(None, 1, 1, "output"),
                    m.PointRef(12, 3, 1, "input"), m.PointRef(None, 5, 7, "output")]
    th = m.app_points("thermostat", {"sensor_device": "local", "out_device": "4294967295"})
    assert [(r.device, r.key) for r in th] == [(None, (0, 1)), (None, (1, 1))]
    assert m.app_points("blinky", {"channel": "do0"}) == []
    assert m.app_points(None, {"a": 1}) == []
    entries = [
        {"name": "link", "file": "/lfs/apps/link.wasm",
         "params": [{"key": "count", "value": "1"},
                    {"key": "l0", "value": "11 0 1 1 1 cov 1000 8 1 0"}]},
        {"name": "alarm", "file": "/lfs/apps/alarm.wasm", "params": {"src_device": "11"}},
        {"name": "t2", "file": "/lfs/apps/thermostat.wasm",
         "params": [{"key": "sensor_device", "value": "99"},
                    {"key": "out_device", "value": "99"}]},
        {"name": "custom", "file": "/lfs/apps/custom.wasm"},
    ]
    assert m.apps_using_objects(entries, {(0, 1)}, 11) == ["link", "alarm"]
    assert m.apps_using_objects(entries, {(1, 1)}, 11) == ["link"]
    assert m.apps_using_objects(entries, set(), 11) == []


# --- firmware limits the schema cannot express (CON-6) ---------------------------------------


E_ACUTE = "é"  # 2 bytes in UTF-8


@pytest.mark.parametrize(("doc_name", "doc", "path", "fragment"), [
    ("device", {"schema": 1, "device": {"instance": 1, "name": E_ACUTE * 40}},
     "/device/name", "80 bytes in UTF-8"),
    ("device", {"schema": 1, "device": {"instance": 1, "name": "水" * 22}},
     "/device/name", "66 bytes in UTF-8"),
    ("device", {"schema": 1, "device": {"instance": 1001.0, "name": "a"}},
     "/device/instance", "not an integer"),
    ("device", {"schema": 1, "device": {"instance": 1, "name": "a"},
                "network": {"dhcp": False, "ipv4": "10.0.0.2", "netmask": "255.0.255.0"}},
     "/network/netmask", "not a contiguous netmask"),
    ("io", {"schema": 1, "points": [{"channel": "ai0", "type": "analog-input",
                                     "instance": 1, "name": E_ACUTE * 40}]},
     "/points/0/name", "80 bytes"),
    ("apps", {"schema": 1, "apps": [{"name": "a", "file": "/lfs/apps/a.wasm",
                                     "params": [{"key": "v", "value": E_ACUTE * 60}]}]},
     "/apps/0/params/0/value", "120 bytes"),
    ("apps", {"schema": 1, "apps": [{"name": "a", "file": "/lfs/apps/a.wasm",
                                     "params": [{"key": "..", "value": "x"}]}]},
     "/apps/0/params/0/key", "not allowed"),
    ("apps", {"schema": 1, "apps": [{"name": "a", "file": "/lfs/apps/a.wasm",
                                     "params": [{"key": "k", "value": "x"},
                                                {"key": "k", "value": "y"}]}]},
     "/apps/0/params/1/key", "appears twice"),
])
def test_documents_checked_with_firmware_limits(doc_name: str, doc: dict[str, Any], path: str,
                                                fragment: str) -> None:
    issues = m.validate_document(doc_name, doc)
    assert any(i.path == path and fragment in i.message for i in issues), issues


def test_firmware_limits_accept_what_the_node_accepts() -> None:
    ok = {"schema": 1, "device": {"instance": 1001, "name": E_ACUTE * 31},
          "network": {"dhcp": False, "ipv4": "10.0.0.2", "netmask": "255.255.240.0"}}
    assert not m.validate_document("device", ok)
    apps = {"schema": 1, "apps": [{"name": "a", "file": "/lfs/apps/a.wasm",
                                   "params": [{"key": "a.b", "value": E_ACUTE * 47}]}]}
    assert not m.validate_document("apps", apps)
    assert m.netmask_valid("255.255.255.255") and not m.netmask_valid("0.255.255.255")


def test_system_manifest_firmware_limits() -> None:
    doc = base_doc()
    doc["nodes"][1]["network"] = {"dhcp": False, "ipv4": "10.0.0.2", "netmask": "255.0.255.0"}
    doc["nodes"][1]["device"]["name"] = E_ACUTE * 40
    doc["apps"][0]["params"]["note"] = E_ACUTE * 48
    doc["apps"][0]["params"][".."] = "x"
    errs = errors(doc)
    assert any(i.path == "/nodes/1/network/netmask" for i in errs)
    assert any(i.path == "/nodes/1/device/name" and "bytes" in i.message for i in errs)
    doc = base_doc()
    doc["apps"][0]["params"]["note"] = E_ACUTE * 48
    doc["apps"][0]["params"][".."] = "x"
    errs = errors(doc)
    assert any(i.path == "/apps/0/params/note" and "96 in UTF-8" in i.message for i in errs)
    assert any(i.path == "/apps/0/params/.." and "'.' or '..'" in i.message for i in errs)


@pytest.mark.parametrize("dst", ["analog-value:9", "binary-value:9", "multi-state-value:9",
                                 "analog-output:1"])
def test_link_priority_6_is_an_error_for_every_destination(dst: str) -> None:
    """The node rejects priority 6 for AV as well as for outputs (CON-1)."""
    doc = base_doc()
    doc["links"] = [{"from": "a/analog-input:1", "to": f"b/{dst}", "priority": 6}]
    assert_error(doc, "/links/0/priority", "reserved for minimum on/off")


def test_link_period_limit_of_uc_link() -> None:
    """uc-link skips a link with period_ms > 3600000 as malformed (CON-5); the
    system schema carries the same maximum, so the schema check reports it."""
    doc = base_doc()
    doc["links"] = [{"from": "a/analog-input:1", "to": "b/analog-value:21", "mode": "poll",
                     "period_ms": 7200000}]
    assert_error(doc, "/links/0/period_ms", "3600000")
    doc["links"][0]["period_ms"] = 3600000
    assert not errors(doc)
