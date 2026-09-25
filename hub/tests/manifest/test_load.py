from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from uc_hub.core.errors import ValidationFailed
from uc_hub.core.ids import PointRef
from uc_hub.core.types import ProtocolName, SafetyClass
from uc_hub.manifest import SiteManifest, load_site, parse_yaml

from .conftest import site_doc

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "demo"


def errors_of(doc: Any) -> list[str]:
    with pytest.raises(ValidationFailed) as info:
        SiteManifest.from_dict(doc)
    return info.value.errors


# -- the demo site --------------------------------------------------------------------
def test_demo_site_validates() -> None:
    site = load_site(EXAMPLES / "site.yaml")
    assert site.name == "hq"
    assert site.base_dir == EXAMPLES
    assert [n for n in site.nodes] == [f"r20{i}-ctl" for i in range(1, 6)]
    ports = {n["transport"]["port"] for n in site.nodes.values()}
    assert len(ports) == 5
    assert site.devices["ahu1-ctl"].protocol is ProtocolName.BACNET_IP
    assert site.devices["r204-co2"].protocol is ProtocolName.MQTT
    assert site.devices["f2-mqtt"].space == "f2-riser"
    assert site.safety["hq/ahu1-ctl/binary-output:9"] is SafetyClass.LIFE_SAFETY
    assert [a.name for a in site.apps("r204-ctl")] == ["thermostat", "link"]
    assert site.apps("r204-ctl")[1].manifest["params"]["l0"] == "100 2 3 2 10 cov 5000 0 1 0"
    assert site.bridges[0].dest == PointRef("hq", "r204-ctl", "analog-value:20")
    for node in site.nodes:
        assert [p["channel"] for p in site.io_json(node)["points"]] == ["ai0", "ao0", "di0", "do0"]


def test_demo_thermostat_module_is_wasm() -> None:
    data = (EXAMPLES / "apps" / "thermostat.wasm").read_bytes()
    assert data[:8] == b"\0asm\x01\0\0\0"
    assert (EXAMPLES / "apps" / "thermostat.c").exists()


# -- SiteManifest views -------------------------------------------------------------------
def test_views(doc: dict[str, Any]) -> None:
    site = SiteManifest.from_dict(doc)
    assert site.description == "test site"
    assert [s.id for s in site.spaces] == ["f2", "r204", "r205", "plant"]
    assert site.space_tree() == [
        {"id": "f2", "name": "Floor 2", "children": [
            {"id": "r204", "name": "Room 204", "children": []},
            {"id": "r205", "name": "Room 205", "children": []},
        ]},
        {"id": "plant", "name": "Plant", "children": []},
    ]
    assert site.space_path("r204") == ["f2", "r204"]
    assert site.spaces_within("f2") == {"f2", "r204", "r205"}
    assert site.placement["ahu1-ctl"] == "plant"
    assert list(site.devices) == ["r204-ctl", "r205-ctl", "ahu1-ctl", "r204-co2"]
    assert site.devices["r204-ctl"].managed and not site.devices["ahu1-ctl"].managed
    assert site.device_instance("ahu1-ctl") == 100
    assert site.device_instance("r204-ctl") == 2041
    assert site.device_instance("r204-co2") is None
    assert site.tags == {"hq/r204-ctl/analog-input:1": ["Zone_Air_Temperature_Sensor"]}
    assert site.point_ref("r204-ctl/analog-input:1") == PointRef("hq", "r204-ctl", "analog-input:1")
    assert site.tests[0]["name"] == "valve opens when cold"


def test_policy_defaults_and_deny_patterns(doc: dict[str, Any]) -> None:
    policy = SiteManifest.from_dict(doc).policy
    assert (policy.agent_write_priority, policy.default_lease_s, policy.max_lease_s) == (12, 300, 3600)
    assert (policy.max_writes_per_minute, policy.max_devices_per_stage, policy.approval_ttl_s) == (30, 5, 1800)
    assert policy.deny == ()
    doc["policy"] = {"agent_write_priority": 14, "deny": ["ahu1-ctl/binary-output:*", "hq/r204-ctl/*",
                                                          "*/analog-value:9"]}
    policy = SiteManifest.from_dict(doc).policy
    assert policy.agent_write_priority == 14
    assert policy.deny == ("hq/ahu1-ctl/binary-output:*", "hq/r204-ctl/*", "hq/*/analog-value:9")
    assert policy.denies("hq/ahu1-ctl/binary-output:9")
    assert policy.denies(PointRef("hq", "r204-ctl", "analog-input:1"))
    assert policy.denies("hq/r205-ctl/analog-value:9")
    assert not policy.denies("hq/ahu1-ctl/analog-value:3")
    assert policy.to_dict()["deny"] == list(policy.deny)


def test_bridge_defaults(doc: dict[str, Any]) -> None:
    doc["policy"] = {"agent_write_priority": 13}
    doc["bridges"] = [{"from": "hq/r204-co2/co2", "to": "r204-ctl/analog-value:20"}]
    bridge = SiteManifest.from_dict(doc).bridges[0]
    assert bridge.to_dict() == {"from": "hq/r204-co2/co2", "to": "hq/r204-ctl/analog-value:20",
                                "max_age_s": 300, "scale": 1.0, "offset": 0.0, "priority": 13}


def test_node_documents(doc: dict[str, Any]) -> None:
    node = doc["system"]["nodes"][0]
    node["network"] = {"dhcp": False, "ipv4": "10.0.2.51", "netmask": "255.255.255.0"}
    node["bacnet"] = {"udp_port": 47809}
    site = SiteManifest.from_dict(doc)
    assert site.device_json("r204-ctl") == {
        "schema": 1,
        "device": {"instance": 2041, "name": "R204 Controller", "location": "Room 204"},
        "network": {"dhcp": False, "ipv4": "10.0.2.51", "netmask": "255.255.255.0"},
        "bacnet": {"udp_port": 47809},
    }
    assert site.device_json("r205-ctl") == {"schema": 1, "device": {"instance": 2051, "name": "R205 Controller"}}
    assert site.io_json("r205-ctl") == {
        "schema": 1, "points": [{"channel": "di0", "type": "binary-input", "instance": 1, "name": "R205 Window"}],
    }
    with pytest.raises(KeyError):
        site.device_json("ahu1-ctl")


def test_documents_are_copies(doc: dict[str, Any]) -> None:
    site = SiteManifest.from_dict(doc)
    site.io_json("r204-ctl")["points"].clear()
    site.nodes["r204-ctl"]["io"].clear()
    site.to_dict()["metadata"]["name"] = "other"
    assert len(site.io_json("r204-ctl")["points"]) == 2
    assert site.name == "hq"


def test_yaml_round_trip_is_stable(doc: dict[str, Any]) -> None:
    shuffled = {k: doc[k] for k in reversed(list(doc))}
    site = SiteManifest.from_dict(shuffled)
    assert list(site.to_dict())[:4] == ["apiVersion", "kind", "metadata", "spaces"]
    text = site.to_yaml()
    again = SiteManifest.from_yaml(text)
    assert again == site
    assert again.to_yaml() == text
    assert site.to_dict() == SiteManifest.from_dict(doc).to_dict()


def test_placeholders_in_app_params(doc: dict[str, Any]) -> None:
    doc["system"]["apps"][0]["params"] = {
        "sensor_device": "{{ nodes.r205-ctl.device.instance }}",
        "label": "ahu {{external_devices.ahu1-ctl.device_instance}}",
        "enabled": True,
    }
    params = SiteManifest.from_dict(doc).apps("r204-ctl")[0].manifest["params"]
    assert params == {"sensor_device": "2051", "label": "ahu 100", "enabled": "true"}


# -- YAML safety ------------------------------------------------------------------------------
def test_yaml_errors_have_positions() -> None:
    with pytest.raises(ValidationFailed) as info:
        parse_yaml("a: [1, 2\nb: 3\n")
    assert "not valid YAML" in str(info.value)
    assert info.value.errors[0].startswith("line ")


@pytest.mark.parametrize(("text", "needle"), [
    ("a: &x [1]\nb: *x\n", "aliases"),
    ("a: 1\na: 2\n", "duplicate key 'a'"),
    ("1: x\n", "keys must be strings"),
    ("a: {? [1, 2] : x}\n", "keys must be strings"),
])
def test_yaml_restrictions(text: str, needle: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        parse_yaml(text)
    assert needle in info.value.errors[0]


def test_yaml_scalars_stay_json() -> None:
    assert parse_yaml("d: 2026-09-24\nt: 2026-09-24T10:00:00Z\n") == {"d": "2026-09-24", "t": "2026-09-24T10:00:00Z"}
    with pytest.raises(ValidationFailed, match="non-finite"):
        parse_yaml("x: .nan\n")
    with pytest.raises(ValidationFailed, match="non-JSON"):
        parse_yaml("x: !!binary aGVsbG8=\n")
    with pytest.raises(ValidationFailed, match="not valid YAML"):
        parse_yaml("x: !!python/object:os.system {}\n")


@pytest.mark.parametrize("depth", [65, 5000])
def test_deep_nesting_is_a_validation_error(depth: int) -> None:
    with pytest.raises(ValidationFailed, match="not valid YAML") as info:
        parse_yaml("a: " + "[" * depth + "]" * depth + "\n")
    assert "nested deeper than 64 levels" in info.value.errors[0]
    nested: Any = 1
    for _ in range(depth):
        nested = [nested]
    doc = site_doc()
    doc["metadata"]["description"] = nested
    with pytest.raises(ValidationFailed, match="nested deeper than 64 levels"):
        SiteManifest.from_dict(doc)


def test_moderate_nesting_is_parsed() -> None:
    value = parse_yaml("a: " + "[" * 60 + "1" + "]" * 60 + "\n")["a"]
    for _ in range(60):
        (value,) = value
    assert value == 1


def test_yaml_size_limit() -> None:
    with pytest.raises(ValidationFailed, match="larger than"):
        parse_yaml("a: '" + "x" * (4 * 1024 * 1024) + "'\n")


def test_load_errors(tmp_path: Path) -> None:
    path = tmp_path / "site.yaml"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationFailed, match="UTF-8"):
        load_site(path)
    path.write_text(yaml.safe_dump(site_doc()))
    assert load_site(path).base_dir == tmp_path


# -- schema ------------------------------------------------------------------------------------
def test_minimal_site_is_valid() -> None:
    site = SiteManifest.from_dict({"apiVersion": "bacnet-uc/v1", "kind": "Site", "metadata": {"name": "hq"}})
    assert site.devices == {} and site.bridges == [] and site.tags == {} and site.policy.deny == ()


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda d: d.pop("metadata"), "<root>: 'metadata' is a required property"),
    (lambda d: d.update(kind="System"), "/kind: 'Site' was expected"),
    (lambda d: d.update(extra=1), "<root>: Additional properties are not allowed ('extra' was unexpected)"),
    (lambda d: d["metadata"].update(name="HQ"), "/metadata/name: 'HQ' does not match"),
    (lambda d: d["spaces"].append({"id": "x"}), "/spaces/4: 'name' is a required property"),
    (lambda d: d["placement"].update({"Bad Name": "f2"}), "/placement: 'Bad Name' does not match"),
    (lambda d: d["bridges"][0].update(priority=5), "/bridges/0/priority: 5 is less than the minimum of 9"),
    (lambda d: d["safety"].update({"r204-ctl/analog-input:1": "high"}),
     "/safety/r204-ctl~1analog-input:1: 'high' is not one of ['critical', 'life-safety']"),
    (lambda d: d["tags"].update({"r204-ctl/analog-input:2": ["has space"]}),
     "/tags/r204-ctl~1analog-input:2/0: 'has space' does not match"),
    (lambda d: d.update(policy={"agent_write_priority": 8}),
     "/policy/agent_write_priority: 8 is less than the minimum of 9"),
    (lambda d: d["external_devices"][0].update(client_id="x"),
     "/external_devices/0: Unevaluated properties are not allowed ('client_id' was unexpected)"),
    (lambda d: d["external_devices"][1].pop("topic"), "/external_devices/1: 'topic' is a required property"),
    (lambda d: d["external_devices"].append({"name": "m1", "protocol": "mqtt"}),
     "/external_devices/2: 'client_id' is a required property"),
    (lambda d: d["external_devices"].append({"name": "z1", "protocol": "zigbee"}),
     "/external_devices/2/protocol: 'zigbee' is not one of ['bacnet-ip', 'mqtt']"),
    (lambda d: d["system"]["nodes"][0].update(transport={"kind": "tcp", "host": "x"}),
     "/system/nodes/0/transport/kind: 'tcp' is not one of ['udp', 'serial', 'sim']"),
    (lambda d: d["system"]["nodes"][0].update(network={"ipv4": "10.0.0.300"}),
     "/system/nodes/0/network/ipv4: '10.0.0.300' is not a 'ipv4'"),
    (lambda d: d["system"]["nodes"][0]["io"][0].update(type="analog-thing"),
     "/system/nodes/0/io/0/type: 'analog-thing' is not one of"),
    (lambda d: d["system"]["apps"][0].update(source="logic/t.c"), "/system/apps/0: {"),
    (lambda d: d["system"]["links"][0].update(to="r204-ctl/co2"), "/system/links/0/to: 'r204-ctl/co2' does not match"),
])
def test_schema_errors(doc: dict[str, Any], mutate: Any, expected: str) -> None:
    mutate(doc)
    errors = errors_of(doc)
    assert any(e.startswith(expected) for e in errors), errors


def test_all_schema_errors_are_reported(doc: dict[str, Any]) -> None:
    doc["metadata"]["name"] = "Bad"
    doc["bridges"][0]["priority"] = 1
    errors = errors_of(doc)
    assert len(errors) == 2
    assert errors[0].startswith("/bridges/0/priority") and errors[1].startswith("/metadata/name")


def test_non_mapping_document() -> None:
    assert errors_of(["not", "a", "site"])[0].startswith("<root>: ['not', 'a', 'site'] is not of type 'object'")


def test_validation_failed_message(doc: dict[str, Any]) -> None:
    doc["metadata"]["name"] = "Bad"
    with pytest.raises(ValidationFailed, match=r"site manifest is invalid \(1 error\)"):
        SiteManifest.from_dict(doc)
