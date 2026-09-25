# SPDX-License-Identifier: Apache-2.0
"""Rendering of device.json / io.json / apps.json from system manifests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness import manifest as m
from bacnet_uc_harness import render as r

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "systems"
REPO = Path(__file__).resolve().parents[2]


def node(name: str, instance: int, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"name": name, "board": "nucleo_f767zi",
                         "transport": {"kind": "udp", "host": f"10.0.0.{instance % 250}"},
                         "device": {"instance": instance, "name": name}}
    d.update(extra)
    return d


def system(nodes: list[dict[str, Any]], **extra: Any) -> m.System:
    doc = {"apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "r"},
           "nodes": nodes, **extra}
    return m.load_system_dict(doc, builtin_catalogs=False)


@pytest.mark.parametrize("name", ["hvac-demo.yaml", "sim-demo.yaml"])
def test_examples_render_valid(name: str) -> None:
    s = m.load_system(EXAMPLES / name)
    out = r.render_system(s)
    assert set(out) == set(s.node_names())
    for nr in out.values():
        for doc_name, doc in nr.docs().items():
            assert not m.validate_document(doc_name, doc), (nr.node, doc_name)
            assert len(r.doc_bytes(doc)) <= r.DOC_MAX
        assert not nr.warnings


def test_hvac_documents() -> None:
    s = m.load_system(EXAMPLES / "hvac-demo.yaml")
    out = r.render_system(s)
    sup = out["supervisor"]
    assert sup.device["device"] == {"instance": 1003, "name": "uc-supervisor",
                                    "location": "Lab 1"}
    assert sup.device["bacnet"]["static_bindings"] == [
        {"device": 1001, "address": "192.168.10.51", "port": 47808},
        {"device": 1002, "address": "192.168.10.52", "port": 47808},
    ]
    thermo = sup.app("thermostat").entry
    assert thermo["file"] == "/lfs/apps/thermostat.wasm"
    assert thermo["perms"] == ["bacnet.local", "bacnet.remote", "kv"]
    params = {p["key"]: p["value"] for p in thermo["params"]}
    assert params["sensor_device"] == "1001" and params["out_device"] == "1002"
    assert params["setpoint"] == "21.5"
    link = sup.app("link").entry
    assert {p["key"]: p["value"] for p in link["params"]} == {
        "count": "1", "l0": "1001 0 1 2 10 poll 5000 0 1 0"}
    assert link["period_ms"] == 5000
    assert link["heap_kb"] == 0 and link["stack_kb"] == 4  # uc-link does not allocate
    assert link["perms"] == ["bacnet.local", "bacnet.remote"]
    # the bacnet section (password) is passed through to device.json
    assert sup.device["bacnet"]["password"] == "hvac-demo-change-me"
    assert out["actuator"].device["bacnet"]["password"] == "hvac-demo-change-me"
    act = out["actuator"]
    assert [a.name for a in act.app_list] == ["link"]
    assert act.io["points"][0]["channel"] == "ao0"
    assert out["sensor"].apps == {"schema": 1, "apps": []}


def test_link_line_matches_readme_example() -> None:
    """wasm/examples/uc-link/README.md: l0 = "1001 0 1 2 10 cov 1000 0 1 0"."""
    s = system([node("src", 1001, io=[{"channel": "ai0", "type": "analog-input",
                                       "instance": 1}]),
                node("dst", 1002)],
               links=[{"from": "src/analog-input:1", "to": "dst/analog-value:10"}])
    assert r.link_line(s, s.links[0]) == "1001 0 1 2 10 cov 1000 0 1 0"
    readme = (REPO / "wasm" / "examples" / "uc-link" / "README.md").read_text()
    assert '`l0 = "1001 0 1 2 10 cov 1000 0 1 0"`' in readme
    parsed = m.parse_link_line(r.link_line(s, s.links[0]))
    assert parsed["dst_instance"] == 10


def test_link_line_scale_offset_priority_and_local_source() -> None:
    s = system([node("n", 7, io=[{"channel": "ai0", "type": "analog-input", "instance": 1}])],
               links=[{"from": "n/analog-input:1", "to": "n/analog-output:3", "mode": "poll",
                       "period_ms": 250, "priority": 9, "scale": 0.1, "offset": -2.5}])
    line = r.link_line(s, s.links[0])
    assert line == "7 0 1 1 3 poll 250 9 0.1 -2.5"
    apps = r.link_apps(s, "n")
    assert len(apps) == 1 and apps[0].params["l0"] == line
    assert apps[0].period_ms == 250


def test_links_are_chunked_by_eight() -> None:
    links = [{"from": "src/analog-input:1", "to": f"dst/analog-value:{i}",
              "period_ms": 1000 + i * 100} for i in range(10)]
    s = system([node("src", 1, io=[{"channel": "ai0", "type": "analog-input", "instance": 1}]),
                node("dst", 2)], links=links)
    apps = r.link_apps(s, "dst")
    assert [a.name for a in apps] == ["link-1", "link-2"]
    assert apps[0].params["count"] == "8" and apps[1].params["count"] == "2"
    assert apps[1].params["l1"].split()[4] == "9"
    assert apps[0].period_ms == 1000 and apps[1].period_ms == 1800
    nr = r.render_node(s, "dst")
    assert [a["file"] for a in nr.apps["apps"]] == ["/lfs/apps/link-1.wasm",
                                                    "/lfs/apps/link-2.wasm"]
    assert all(a.spec.generated for a in nr.app_list)


def test_static_bindings_order_override_and_limits() -> None:
    nodes = [node("hub", 100)] + [node(f"n{i}", 200 + i) for i in range(20)]
    nodes[0]["bacnet"] = {"udp_port": 47809,
                          "static_bindings": [{"device": 205, "address": "172.16.0.5"}]}
    s = system(nodes, links=[{"from": "n19/analog-input:1", "to": "hub/analog-value:1"}])
    nr = r.render_node(s, "hub")
    b = nr.device["bacnet"]["static_bindings"]
    assert len(b) == 16
    assert b[0] == {"device": 205, "address": "172.16.0.5"}  # manifest entry first
    assert b[1]["device"] == 219  # link peer next
    assert nr.device["bacnet"]["udp_port"] == 47809
    assert sum("more than 16 peers" in w for w in nr.warnings) == 20 - 16
    # the hub's own port appears in the peers' bindings
    peer = r.render_node(s, "n0").device["bacnet"]["static_bindings"]
    assert {"device": 100, "address": "10.0.0.100", "port": 47809} in peer


def test_binding_address_sources() -> None:
    s = system([
        node("a", 1, bacnet_address="192.168.0.9:47810"),
        node("b", 2, network={"dhcp": False, "ipv4": "192.168.0.10"},
             transport={"kind": "serial", "device": "/dev/ttyACM0"}),
        node("c", 3, transport={"kind": "serial", "device": "/dev/ttyACM1"}),
        node("d", 4, transport={"kind": "udp", "host": "node-d.local"}),
    ])
    nr = r.render_node(s, "a")
    assert nr.device["bacnet"]["static_bindings"] == [
        {"device": 2, "address": "192.168.0.10", "port": 47808}]
    assert any("'c'" in w and "no BACnet address" in w for w in nr.warnings)
    assert any("'d'" in w and "not an IPv4 address" in w for w in nr.warnings)
    assert r.render_node(s, "b").device["bacnet"]["static_bindings"][0] == {
        "device": 1, "address": "192.168.0.9", "port": 47810}


def test_sim_addresses_and_network() -> None:
    s = system([
        {"name": "s1", "board": "native_sim/native/64", "transport": {"kind": "sim"},
         "device": {"instance": 1, "name": "s1"}},
        {"name": "s2", "board": "native_sim/native/64",
         "transport": {"kind": "sim", "ipv4": "10.47.0.1"}, "device": {"instance": 2,
                                                                        "name": "s2"}},
        {"name": "s3", "board": "native_sim", "transport": {"kind": "sim"},
         "device": {"instance": 3, "name": "s3"}},
    ])
    plan = r.sim_address_plan(s)
    assert plan == {"s1": "10.47.0.2", "s2": "10.47.0.1", "s3": "10.47.0.3"}
    nr = r.render_node(s, "s1")
    assert nr.device["network"] == {"dhcp": False, "ipv4": "10.47.0.2",
                                    "netmask": "255.255.255.0"}
    assert [b["address"] for b in nr.device["bacnet"]["static_bindings"]] == [
        "10.47.0.1", "10.47.0.3"]
    # host mode: loopback, no network section
    nr = r.render_node(s, "s1", addresses={"s1": "127.0.0.1"})
    assert "network" not in nr.device
    assert r.smp_endpoint(s.node("s1"), plan) == {"transport": "sim", "host": "10.47.0.2",
                                                  "port": 1337}


def test_app_entry_defaults_params_and_artifact(tmp_path: Path) -> None:
    s = system([node("n", 1, io=[{"channel": "do0", "type": "binary-output", "instance": 1}])],
               apps=[{"name": "blink", "node": "n", "wasm": "blinky.wasm", "aot": True,
                      "autostart": False, "period_ms": 0, "heap_kb": 0, "stack_kb": 2,
                      "params": {"type": 4, "on": True, "ratio": 0.5}},
                     {"name": "custom", "node": "n", "wasm": "custom.wasm",
                      "params": {"peer_device": 2}},
                     {"name": "given", "node": "n", "wasm": "g.wasm",
                      "perms": ["kv", "io", "kv"]}])
    nr = r.render_node(s, "n")
    blink = nr.app("blink").entry
    assert blink == {"name": "blink", "file": "/lfs/apps/blink.aot", "autostart": False,
                     "period_ms": 0, "heap_kb": 0, "stack_kb": 2, "perms": ["bacnet.local"],
                     "params": [{"key": "type", "value": "4"}, {"key": "on", "value": "true"},
                                {"key": "ratio", "value": "0.5"}]}
    assert nr.app("custom").entry["perms"] == ["bacnet.local", "bacnet.remote"]
    assert nr.app("given").entry["perms"] == ["io", "kv"]
    art = r.AppArtifact(app="custom", node="n", path=tmp_path / "c.wasm", sha256="ab" * 32,
                        size=10, imports=["bacnet_uc.uc_io_read", "bacnet_uc.uc_kv_set",
                                          "env.printf"])
    nr = r.render_node(s, "n", artifacts={"custom": art})
    entry = nr.app("custom").entry
    assert entry["sha256"] == "ab" * 32
    assert entry["file"] == "/lfs/apps/custom.wasm"
    assert entry["perms"] == ["io", "kv"]


def test_render_validation_error() -> None:
    s = system([node("n", 1)])
    s.node("n").io = [{"channel": "ai0", "type": "analog-input", "instance": 1,
                       "sample_ms": 1}]
    with pytest.raises(r.RenderError) as exc:
        r.render_node(s, "n")
    assert exc.value.errors[0].path == "/n/io.json/points/0/sample_ms"


def test_doc_bytes_and_hash() -> None:
    doc = {"schema": 1, "points": [{"channel": "ai0", "type": "analog-input", "instance": 1,
                                    "name": "Température"}]}
    data = r.doc_bytes(doc)
    assert data.endswith(b"}\n") and b" " not in data.replace(b"Temp\xc3\xa9rature", b"")
    assert json.loads(data) == doc
    assert r.doc_sha256(doc) == hashlib.sha256(data).hexdigest()
    assert r.doc_path("io") == "/lfs/cfg/io.json"
    with pytest.raises(r.HarnessError):
        r.doc_path("other")


def test_format_number_and_perms() -> None:
    assert r.format_number(1.0) == "1"
    assert r.format_number(0.1) == "0.1"
    assert r.format_number(-3) == "-3"
    assert r.format_number(1e20) == "1e+20"
    assert r.sort_perms(["kv", "io", "bacnet.remote", "kv"]) == ["bacnet.remote", "io", "kv"]


def test_object_summary() -> None:
    s = m.load_system(EXAMPLES / "hvac-demo.yaml")
    objs = r.object_summary(s, "supervisor")
    assert {"object": "analog-value:1", "owner": "app:thermostat", "role": "setpoint"} in objs
    assert {"object": "analog-value:10", "owner": "app:link", "role": "link-0"} in objs
    sensor = r.object_summary(s, "sensor")
    assert sensor[0]["owner"] == "io" and sensor[0]["object"] == "analog-input:1"
