"""Semantic rules (docs/ai-harness/SITE.md) on top of the schema."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from uc_hub.core.errors import ValidationFailed
from uc_hub.manifest import SiteManifest, apply_patch
from uc_hub.manifest.validate import is_relative_inside, semantic_errors

Mutation = Callable[[dict[str, Any]], Any]


def nodes(d: dict[str, Any]) -> list[dict[str, Any]]:
    return d["system"]["nodes"]


def apps(d: dict[str, Any]) -> list[dict[str, Any]]:
    return d["system"]["apps"]


def links(d: dict[str, Any]) -> list[dict[str, Any]]:
    return d["system"]["links"]


def steps(d: dict[str, Any]) -> list[dict[str, Any]]:
    return d["system"]["tests"][0]["steps"]


def app(name: str, node: str = "r204-ctl") -> dict[str, Any]:
    return {"name": name, "node": node, "wasm": f"apps/{name}.wasm"}


CASES: list[tuple[str, Mutation, str]] = [
    # devices
    ("duplicate device name", lambda d: d["external_devices"][0].update(name="r204-ctl"),
     "/external_devices/0/name: device name 'r204-ctl' is already used by /system/nodes/0"),
    ("duplicate BACnet instance", lambda d: d["external_devices"][0].update(device_instance=2041),
     "/external_devices/0/device_instance: BACnet device instance 2041 is also used by r204-ctl"),
    ("duplicate management address", lambda d: nodes(d)[1]["transport"].update(port=13204),
     "/system/nodes/1/transport: management address udp 127.0.0.1:13204 is also used by r204-ctl"),
    ("management port out of range", lambda d: nodes(d)[1]["transport"].update(port=70000),
     "/system/nodes/1/transport/port: port 70000 is not in 1..65535"),
    ("BACnet/IP port out of range", lambda d: d["external_devices"][0].update(address="10.0.2.10:99999"),
     "/external_devices/0/address: port 99999 is not in 1..65535"),
    # spaces and placement
    ("duplicate space", lambda d: d["spaces"].append({"id": "f2", "name": "again"}),
     "/spaces/4/id: duplicate space id 'f2'"),
    ("unknown parent", lambda d: d["spaces"][3].update(parent="roof"), "/spaces/3/parent: unknown space 'roof'"),
    ("parent cycle", lambda d: d["spaces"][0].update(parent="r204"),
     "/spaces/0/parent: parent cycle: f2 -> r204 -> f2"),
    ("self parent", lambda d: d["spaces"][3].update(parent="plant"), "/spaces/3/parent: parent cycle: plant -> plant"),
    ("placement of unknown device", lambda d: d["placement"].update({"r999-ctl": "f2"}),
     "/placement/r999-ctl: unknown device 'r999-ctl'"),
    ("placement in unknown space", lambda d: d["placement"].update({"r204-ctl": "mars"}),
     "/placement/r204-ctl: unknown space 'mars'"),
    # links
    ("link from unknown device", lambda d: links(d)[0].update({"from": "ahu9-ctl/analog-value:3"}),
     "/system/links/0/from: link source 'ahu9-ctl/analog-value:3': unknown device 'ahu9-ctl'"),
    ("link from mqtt device", lambda d: links(d)[0].update({"from": "r204-co2/analog-value:1"}),
     "/system/links/0/from: link source device r204-co2 is mqtt"),
    ("link to bacnet-ip device", lambda d: links(d)[0].update(to="ahu1-ctl/analog-value:5"),
     "/system/links/0/to: link destination device ahu1-ctl is not a bacnet-uc node"),
    ("link with unknown object type", lambda d: links(d)[0].update(to="r204-ctl/analog-thing:5"),
     "/system/links/0/to: link destination: unknown BACnet object type 'analog-thing'"),
    ("link from device without instance", lambda d: d["external_devices"][0].pop("device_instance"),
     "/system/links/0/from: link source device ahu1-ctl needs a device_instance"),
    ("link to itself", lambda d: links(d)[0].update({"from": "r204-ctl/analog-value:10"}),
     "/system/links/0: link source and destination are the same point"),
    ("link to life-safety point", lambda d: d["safety"].update({"r204-ctl/analog-value:10": "life-safety"}),
     "/system/links/0/to: hq/r204-ctl/analog-value:10 is a life-safety point"),
    ("two links to one point", lambda d: links(d).append({"from": "r205-ctl/binary-input:1",
                                                           "to": "r204-ctl/analog-value:10"}),
     "/system/links/1/to: hq/r204-ctl/analog-value:10 is already written by /system/links/0"),
    ("link at an operator priority", lambda d: links(d)[0].update(priority=8),
     "/system/links/0/priority: priority 8 is reserved for life safety, critical equipment and operators"),
    # bridges
    ("bridge from unknown device", lambda d: d["bridges"][0].update({"from": "r999-co2/co2"}),
     "/bridges/0/from: bridge source 'r999-co2/co2': unknown device 'r999-co2'"),
    ("bridge to mqtt device", lambda d: d["bridges"][0].update({"from": "r204-ctl/analog-input:1",
                                                                "to": "r204-co2/co2"}),
     "/bridges/0/to: bridge destination device r204-co2 is mqtt; it must be bacnet-uc or bacnet-ip"),
    ("bridge to non-BACnet object", lambda d: d["bridges"][0].update(to="r204-ctl/co2"),
     "/bridges/0/to: bridge destination 'co2' is not a BACnet object"),
    ("bridge of another site", lambda d: d["bridges"][0].update({"from": "b2/r204-co2/co2"}),
     "/bridges/0/from: bridge source 'b2/r204-co2/co2' names site 'b2', but this site is 'hq'"),
    ("bridge to itself", lambda d: d["bridges"][0].update({"from": "r204-ctl/analog-value:20"}),
     "/bridges/0: bridge source and destination are the same point"),
    ("bridge to life-safety point", lambda d: d["bridges"][0].update(to="ahu1-ctl/binary-output:9"),
     "/bridges/0/to: hq/ahu1-ctl/binary-output:9 is a life-safety point"),
    ("bridge from a non-BACnet object of a BACnet device",
     lambda d: d["bridges"][0].update({"from": "r205-ctl/window"}),
     "/bridges/0/from: bridge source 'window' is not a BACnet object (<type>:<instance>) of bacnet-uc device"),
    ("bridge from an unknown MQTT point", lambda d: d["bridges"][0].update({"from": "r204-co2/humidity"}),
     "/bridges/0/from: bridge source: r204-co2 has no point 'humidity' (points: co2)"),
    ("bridge to a link destination", lambda d: d["bridges"][0].update(to="hq/r204-ctl/analog-value:10"),
     "/bridges/0/to: hq/r204-ctl/analog-value:10 is already written by /system/links/0"),
    ("two bridges to one point", lambda d: d["bridges"].append({"from": "r204-co2/co2",
                                                                 "to": "r204-ctl/analog-value:20"}),
     "/bridges/1/to: hq/r204-ctl/analog-value:20 is already written by /bridges/0"),
    # apps
    ("app on unknown node", lambda d: apps(d)[0].update(node="r999-ctl"),
     "/system/apps/0/node: 'r999-ctl' is not a bacnet-uc node (unknown)"),
    ("app on bacnet-ip device", lambda d: apps(d)[0].update(node="ahu1-ctl"),
     "/system/apps/0/node: 'ahu1-ctl' is not a bacnet-uc node (a bacnet-ip device)"),
    ("duplicate app", lambda d: apps(d).append(app("thermostat")),
     "/system/apps/1/name: app 'thermostat' is already on r204-ctl (system/apps/0)"),
    ("app named like the link app", lambda d: apps(d).append(app("link")),
     "/system/apps/1/name: app name 'link' is reserved on r204-ctl"),
    ("app named like the link module", lambda d: apps(d).append(app("uc-link", "r205-ctl")),
     "/system/apps/1/name: /lfs/apps/uc-link.wasm is reserved for the stock uc-link module"),
    ("absolute module path", lambda d: apps(d)[0].update(wasm="/etc/passwd"),
     "/system/apps/0/wasm: '/etc/passwd' must be a relative path inside the manifest directory"),
    ("module path leaving the directory", lambda d: apps(d)[0].update(wasm="../../x.wasm"),
     "/system/apps/0/wasm: '../../x.wasm' must be a relative path"),
    ("unknown placeholder", lambda d: apps(d)[0]["params"].update(dev="{{ nodes.r999-ctl.device.instance }}"),
     "/system/apps/0/params: placeholder {{ nodes.r999-ctl.device.instance }}: no node 'r999-ctl'"),
    ("placeholder to a mapping", lambda d: apps(d)[0]["params"].update(dev="{{ nodes.r205-ctl.device }}"),
     "/system/apps/0/params: placeholder {{ nodes.r205-ctl.device }} is not a scalar"),
    ("param value too long", lambda d: apps(d)[0]["params"].update(text="x" * 96),
     "/system/apps/0/params/text: 'xxxx"),
    ("param key the firmware refuses", lambda d: apps(d)[0]["params"].update({"bad key": 1}),
     "/system/apps/0/params/bad key: 'bad key' does not match"),
    ("too many params", lambda d: apps(d)[0].update(params={f"p{i}": i for i in range(17)}),
     "/system/apps/0/params: [{"),
    ("heap too large for apps.json", lambda d: apps(d)[0].update(heap_kb=512),
     "/system/apps/0/heap_kb: 512 is greater than the maximum of 256"),
    ("too many apps", lambda d: apps(d).extend(app(f"a{i}") for i in range(7)),
     "/system/nodes/0: 9 apps on r204-ctl (including 1 generated uc-link apps); a node holds at most 8"),
    # io
    ("duplicate channel", lambda d: nodes(d)[0]["io"].append({"channel": "ai0", "type": "analog-input",
                                                               "instance": 2}),
     "/system/nodes/0/io/2/channel: channel 'ai0' is already bound by io/0"),
    ("duplicate object", lambda d: nodes(d)[0]["io"].append({"channel": "ai1", "type": "analog-input",
                                                              "instance": 1}),
     "/system/nodes/0/io/2/instance: object analog-input:1 is already defined by io/0"),
    ("channel kind mismatch", lambda d: nodes(d)[0]["io"][0].update(type="binary-input"),
     "/system/nodes/0/io/0/type: ai channel ai0 can be bound to analog-input, not binary-input"),
    ("too many io points", lambda d: nodes(d)[1].update(io=[
        {"channel": f"x{i}", "type": "analog-value", "instance": i} for i in range(33)]),
     "/system/nodes/1/io: 33 io points; io.json holds at most 32"),
    # tags and safety
    ("tag of unknown device", lambda d: d["tags"].update({"r999-ctl/analog-input:1": ["X"]}),
     "/tags/r999-ctl~1analog-input:1: key 'r999-ctl/analog-input:1': unknown device 'r999-ctl'"),
    ("tag of another site", lambda d: d["tags"].update({"b2/r204-ctl/analog-input:1": ["X"]}),
     "/tags/b2~1r204-ctl~1analog-input:1: key 'b2/r204-ctl/analog-input:1' names site 'b2'"),
    ("same point tagged twice", lambda d: d["tags"].update({"hq/r204-ctl/analog-input:1": ["X"]}),
     "/tags/hq~1r204-ctl~1analog-input:1: same point as /tags/r204-ctl~1analog-input:1"),
    ("safety of unknown device", lambda d: d["safety"].update({"r999-ctl/binary-output:1": "critical"}),
     "/safety/r999-ctl~1binary-output:1: key 'r999-ctl/binary-output:1': unknown device 'r999-ctl'"),
    ("safety key with a typo", lambda d: d["safety"].update({"ahu1-ctl/binary-output9": "life-safety"}),
     "/safety/ahu1-ctl~1binary-output9: key 'binary-output9' is not a BACnet object (<type>:<instance>) of "
     "bacnet-ip device ahu1-ctl"),
    ("tag of an unknown MQTT point", lambda d: d["tags"].update({"r204-co2/c02": ["Zone_Air_CO2_Sensor"]}),
     "/tags/r204-co2~1c02: key: r204-co2 has no point 'c02' (points: co2)"),
    # external devices
    ("duplicate MQTT point id", lambda d: d["external_devices"][1]["points"].append({"id": "co2", "path": "$.x"}),
     "/external_devices/1/points/1/id: point 'co2' is already listed as points/0"),
    ("duplicate BACnet/IP allow-list entry", lambda d: d["external_devices"][0].update(
        points=[{"obj": "analog-value:3"}, {"obj": "analog-value:3", "name": "again"}]),
     "/external_devices/0/points/1/obj: point 'analog-value:3' is already listed as points/0"),
    # tests
    ("unknown step", lambda d: steps(d).append({"pause": 5}),
     "/system/tests/0/steps/3: unknown step 'pause' (expected one of force, release, write, wait, expect)"),
    ("force on an external device", lambda d: steps(d)[0]["force"].update(node="ahu1-ctl"),
     "/system/tests/0/steps/0/force/node: 'ahu1-ctl' is not a bacnet-uc node"),
    ("expect on unknown device", lambda d: steps(d)[1]["expect"].update(point="r999-ctl/analog-output:1"),
     "/system/tests/0/steps/1/expect/point: point 'r999-ctl/analog-output:1': unknown device 'r999-ctl'"),
    ("test writes a life-safety point", lambda d: steps(d).append(
        {"write": {"point": "ahu1-ctl/binary-output:9", "value": 1, "priority": 12}}),
     "/system/tests/0/steps/3/write/point: tests may not write the life-safety point hq/ahu1-ctl/binary-output:9"),
    ("test forces a channel bound to a life-safety point",
     lambda d: d["safety"].update({"r204-ctl/analog-input:1": "life-safety"}),
     "/system/tests/0/steps/0/force/channel: tests may not force r204-ctl channel ai0: it is bound to the "
     "life-safety point hq/r204-ctl/analog-input:1"),
    ("test writes at an operator priority", lambda d: steps(d).append(
        {"write": {"point": "r204-ctl/analog-output:1", "value": 100, "priority": 1}}),
     "/system/tests/0/steps/3/write/priority: tests may not write at priority 1"),
    ("test writes an output without a priority", lambda d: steps(d).append(
        {"write": {"point": "r204-ctl/analog-output:1", "value": 100}}),
     "/system/tests/0/steps/3/write/priority: a test write to analog-output:1 needs a priority (9..16)"),
    ("test expects an unknown MQTT point", lambda d: steps(d)[1]["expect"].update(point="r204-co2/analog-value:1"),
     "/system/tests/0/steps/1/expect/point: point: r204-co2 has no point 'analog-value:1'"),
    ("duplicate test name", lambda d: d["system"]["tests"].append({"name": "valve opens when cold", "steps": []}),
     "/system/tests/1/name: duplicate test name 'valve opens when cold' (system/tests/0)"),
    # policy
    ("lease default above max", lambda d: d.update(policy={"default_lease_s": 7200}),
     "/policy/default_lease_s: default_lease_s 7200 exceeds max_lease_s 3600"),
]


@pytest.mark.parametrize(("mutate", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_semantic_rule(doc: dict[str, Any], mutate: Mutation, expected: str) -> None:
    mutate(doc)
    with pytest.raises(ValidationFailed) as info:
        SiteManifest.from_dict(doc)
    assert any(e.startswith(expected) for e in info.value.errors), info.value.errors


def test_app_errors_point_into_the_manifest(doc: dict[str, Any]) -> None:
    """An error path can be used as the path of a JSON Patch that fixes it."""
    apps(doc)[0]["params"]["text"] = "x" * 96
    with pytest.raises(ValidationFailed) as info:
        SiteManifest.from_dict(doc)
    (error,) = info.value.errors
    fixed = apply_patch(doc, [{"op": "replace", "path": error.split(":")[0], "value": "short"}])
    SiteManifest.from_dict(fixed)


def test_valid_site_has_no_semantic_errors(doc: dict[str, Any]) -> None:
    assert semantic_errors(doc) == []


def test_every_semantic_error_is_reported(doc: dict[str, Any]) -> None:
    doc["placement"]["r999-ctl"] = "f2"
    apps(doc)[0]["node"] = "r999-ctl"
    doc["policy"] = {"default_lease_s": 4000}
    with pytest.raises(ValidationFailed) as info:
        SiteManifest.from_dict(doc)
    assert len(info.value.errors) == 3
    assert "3 errors" in str(info.value)


def test_schema_errors_come_before_semantic_rules(doc: dict[str, Any]) -> None:
    doc["placement"]["r999-ctl"] = "f2"
    doc["metadata"]["name"] = "Bad Name"
    with pytest.raises(ValidationFailed) as info:
        SiteManifest.from_dict(doc)
    assert info.value.errors == ["/metadata/name: 'Bad Name' does not match '^[a-z0-9][a-z0-9-]{0,62}$'"]


def test_local_link_sources_and_mqtt_devices_are_fine(doc: dict[str, Any]) -> None:
    links(doc).append({"from": "r205-ctl/binary-input:1", "to": "r204-ctl/binary-value:3"})
    links(doc).append({"from": "r204-ctl/analog-input:1", "to": "r204-ctl/analog-value:11"})
    doc["tags"]["r204-co2/co2"] = ["Zone_Air_CO2_Sensor"]
    doc["bridges"].append({"from": "r204-co2/co2", "to": "ahu1-ctl/analog-value:7"})
    SiteManifest.from_dict(doc)


def test_agent_priorities_and_harmless_steps_are_fine(doc: dict[str, Any]) -> None:
    links(doc)[0]["priority"] = 0
    links(doc).append({"from": "r205-ctl/binary-input:1", "to": "r204-ctl/binary-value:3", "priority": 9})
    doc["safety"]["r204-ctl/analog-input:1"] = "critical"
    doc["safety"]["r205-ctl/binary-input:1"] = "life-safety"
    doc["safety"]["r204-co2/co2"] = "critical"
    doc["safety"]["ahu1-ctl/life-safety-point:1"] = "life-safety"
    steps(doc).extend([
        {"release": {"node": "r205-ctl", "channel": "di0"}},
        {"force": {"node": "r205-ctl", "channel": "di1", "value": 1}},
        {"write": {"point": "r204-ctl/analog-output:1", "value": 100, "priority": 16}},
        {"write": {"point": "r204-ctl/analog-value:1", "value": 21}},
        {"write": {"point": "r204-ctl/analog-output:1", "property": "relinquish-default", "value": 0}},
        {"expect": {"point": "r205-ctl/binary-input:1", "op": "eq", "value": 0}},
    ])
    SiteManifest.from_dict(doc)


@pytest.mark.parametrize(("path", "ok"), [
    ("apps/t.wasm", True), ("t.wasm", True), ("a/../t.wasm", True), ("./t.wasm", True),
    ("", False), (".", False), ("..", False), ("../t.wasm", False), ("a/../../t.wasm", False),
    ("/abs/t.wasm", False), ("a\\b.wasm", False),
])
def test_relative_paths(path: str, ok: bool) -> None:
    assert is_relative_inside(path) is ok
