"""controller.yaml: schema, defaults and semantic checks (DESIGN.md §5.1)."""

import copy
import json

import pytest
import yaml

from uc_controller import config
from uc_controller.config import ConfigError

from conftest import CONTROLLER, EXAMPLES


def load_example(name):
    with open(CONTROLLER / "site" / "examples" / f"{name}.yaml") as fh:
        return yaml.safe_load(fh)


def test_schema_is_draft_2020_12_and_closed():
    from jsonschema import Draft202012Validator
    schema = config.schema()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    # additionalProperties:false on every object schema, except the free-form hub overrides
    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                if path != "properties.services.properties.hub.properties.overrides":
                    assert node.get("additionalProperties") is False, path
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(schema, "")


@pytest.mark.parametrize("name", EXAMPLES)
def test_examples_validate(name):
    site = config.load(str(CONTROLLER / "site" / "examples" / f"{name}.yaml"))
    assert site.version == 1
    assert site.network.mode == name


def test_enums_match_the_design():
    s = config.schema()["properties"]
    assert s["network"]["properties"]["mode"]["enum"] == ["trunk", "dual", "flat"]
    assert s["controller"]["properties"]["hardware"]["enum"] == ["pi4", "vm"]
    assert s["controller"]["properties"]["rtc"]["enum"] == ["ds3231", "rv3028", "none"]
    svc = s["services"]["properties"]
    assert svc["hub"]["properties"]["llm"]["properties"]["provider"]["enum"] == ["none", "zai"]
    assert svc["ui"]["properties"]["cert"]["enum"] == ["site", "provided"]
    types = [b["properties"]["type"]["const"] for b in s["backup"]["properties"]["targets"]["items"]["oneOf"]]
    assert types == ["sftp", "usb"]


def test_defaults_are_filled():
    site = config.load_dict(load_example("flat"))
    assert site.site.timezone == "UTC"
    assert site.controller.ups_gpio is None and site.controller.ssd_reserve_percent == 0
    assert site.time.nts == () and site.time.serve is True
    assert site.services.syslog.enabled is True
    assert site.services.mqtt.topic_root == "bacnet-uc"
    assert site.services.mqtt.broker_name is None
    assert site.services.history.raw_days == 400 and site.services.history.expected_points == 200
    assert site.services.hub.enabled is True and site.services.hub.overrides == {}
    assert site.services.ui.cert == "site"
    assert site.backup.at == "02:15" and site.backup.history_repo is None
    assert site.backup.targets[0].label == "UCBACKUP"      # default inside a oneOf branch
    assert site.network.it is None and site.network.parent == "eth0"
    assert site.network.sources.backup == () and site.network.sources.bbmd == ()
    assert site.ssh_user_ca == ()


def test_network_mac_forms():
    for mac in ("auto", None, "DC:A6:32:00:00:01"):
        data = load_example("trunk")
        data["network"]["mac"] = mac
        site = config.load_dict(data)
        assert site.network.mac == (mac.lower() if mac and mac != "auto" else mac)
    data = load_example("trunk")
    data["network"]["mac"] = "dc:a6:32:00:00"
    with pytest.raises(ConfigError):
        config.load_dict(data)


def test_it_static_or_dhcp():
    data = load_example("trunk")
    data["network"]["it"] = {"vlan": 10, "dhcp": True}
    site = config.load_dict(data)
    assert site.network.it.dhcp is True and site.network.it.address is None
    data["network"]["it"] = {"vlan": 10, "dhcp": True, "address": "192.0.2.20/24"}
    with pytest.raises(ConfigError):
        config.load_dict(data)


def _mutate(example, fn):
    data = load_example(example)
    fn(data)
    return data


def _set(path, value):
    def fn(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return fn


def _delete(path):
    def fn(data):
        node = data
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return fn


INVALID = {
    "unknown top-level key": ("trunk", _set(["colour"], "blue"), "colour"),
    "unknown nested key": ("trunk", _set(["services", "dhcp", "leases"], "12h"), "leases"),
    "bad CIDR prefix": ("trunk", _set(["network", "bms", "address"], "10.20.0.2/33"), "network.bms.address"),
    "bad CIDR octet": ("trunk", _set(["network", "sources", "admin"], ["192.0.300.0/27"]), "network.sources.admin"),
    "source with host bits": ("trunk", _set(["network", "sources", "ui"], ["192.0.2.5/24"]), "network.sources.ui[0]"),
    "bms address inside the DHCP range": ("flat", _set(["services", "dhcp", "range"], ["10.20.0.1", "10.20.0.99"]),
                                          "inside the DHCP range"),
    "DHCP range outside the subnet": ("flat", _set(["services", "dhcp", "range"], ["10.20.1.100", "10.20.1.199"]),
                                      "services.dhcp.range[0]"),
    "DHCP enabled without range": ("flat", _set(["services", "dhcp"], {"enabled": True}), "services.dhcp.range"),
    "duplicate VLAN ids": ("trunk", _set(["network", "bms", "vlan"], 10), "network.bms.vlan"),
    "trunk without VLAN": ("trunk", _delete(["network", "bms", "vlan"]), "network.bms.vlan"),
    "empty hub users while enabled": ("trunk", _set(["services", "hub", "users"], []), "services.hub.users"),
    "malformed age recipient": ("trunk", _set(["backup", "recipients"],
                                              ["age1tc9wwg2vtgtd29psvsgy8rwlzmuqt5nrvfpg0rqg8y03yq2ct8zsm492fx"]),
                                "backup.recipients[0]"),
    "age recipient of wrong length": ("trunk", _set(["backup", "recipients"], ["age1qqqqqqqqqqqqqqqqqqqqqqqqq"]),
                                      "backup.recipients[0]"),
    "gateway outside the IT subnet": ("trunk", _set(["network", "it", "gateway"], "198.51.100.1"),
                                      "network.it.gateway"),
    "IT and BMS overlap": ("trunk", _set(["network", "it", "address"], "10.20.0.3/16"), "overlaps"),
    "bms address is the network address": ("flat", _set(["network", "bms", "address"], "10.20.0.0/24"),
                                           "network or broadcast"),
    "dual without nic_mac": ("dual", _set(["network", "bms", "nic_mac"], None), "network.bms.nic_mac"),
    "nic_mac outside dual": ("trunk", _set(["network", "bms", "nic_mac"], "00:e0:4c:68:00:01"), "dual mode only"),
    "flat with IT block": ("flat", _set(["network", "it"], {"address": "192.0.2.20/24"}), "network.it"),
    "bad mode": ("trunk", _set(["network", "mode"], "bridge"), "network.mode"),
    "bad llm provider": ("trunk", _set(["services", "hub", "llm", "provider"], "openai"), "provider"),
    "bad site id": ("trunk", _set(["site", "id"], "HQ_1"), "site.id"),
    "bad hub role": ("trunk", _set(["services", "hub", "users"], [{"user": "x", "roles": ["root"]}]), "roles"),
    "duplicate admin": ("dual", lambda d: d["admins"].append(copy.deepcopy(d["admins"][0])), "duplicate"),
    "reserved admin name": ("trunk", lambda d: d["admins"][0].update(name="root"), "reserved"),
    "newline in a key comment": ("trunk", lambda d: d["admins"][0].update(
        keys=[d["admins"][0]["keys"][0] + "\ncommand=\"/bin/sh\" ssh-ed25519 AAAA"]), "admins[0].keys"),
    "backup time unquoted (YAML int)": ("trunk", _set(["backup", "at"], 735), "quote the value"),
    "no admins": ("trunk", _set(["admins"], []), "admins"),
    "wrong version": ("trunk", _set(["version"], 2), "version"),
    "sftp url": ("trunk", _set(["backup", "targets"], [{"type": "sftp", "url": "ftp://x/y"}]), "backup.targets[0]"),
    "ups gpio on the RTC bus": ("trunk", _set(["controller", "ups_gpio"], 3), "controller.ups_gpio"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_configs_are_refused(case):
    example, mutate, needle = INVALID[case]
    data = _mutate(example, mutate)
    with pytest.raises(ConfigError) as err:
        config.load_dict(data)
    text = "\n".join(err.value.errors)
    assert needle in text, text
    # every message has the 'json.path: message' form
    assert all(": " in e for e in err.value.errors)


def test_at_least_ten_invalid_cases():
    assert len(INVALID) >= 10


def test_manifest_name_must_match(tmp_path):
    data = load_example("trunk")
    (tmp_path / "site.yaml").write_text(yaml.safe_dump({"apiVersion": "bacnet-uc/v1", "kind": "Site",
                                                        "metadata": {"name": "other"}}))
    data["services"]["hub"]["manifest"] = "site.yaml"
    with pytest.raises(ConfigError) as err:
        config.load_dict(data, base_dir=str(tmp_path))
    assert "metadata.name 'other' differs" in str(err.value)
    (tmp_path / "site.yaml").write_text(yaml.safe_dump({"metadata": {"name": "hq"}}))
    config.load_dict(data, base_dir=str(tmp_path))


def test_duplicate_yaml_keys_are_refused(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("version: 1\nversion: 1\n")
    with pytest.raises(ConfigError) as err:
        config.load(str(f))
    assert "duplicate key" in str(err.value)


def test_errors_are_collected_not_first_only():
    data = load_example("trunk")
    data["network"]["bms"]["vlan"] = 10
    data["services"]["hub"]["users"] = []
    with pytest.raises(ConfigError) as err:
        config.load_dict(data)
    assert len(err.value.errors) >= 2


def test_bech32_roundtrip():
    rec = config.bech32_encode("age", bytes(range(32)))
    assert config.is_age_recipient(rec)
    assert config.bech32_decode(rec) == ("age", bytes(range(32)))
    assert not config.is_age_recipient(rec[:-1] + ("q" if rec[-1] != "q" else "p"))


def test_schema_file_is_valid_json():
    json.loads((CONTROLLER / "site" / "controller.schema.json").read_text())
