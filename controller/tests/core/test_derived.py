"""Derived values (DESIGN.md §5.1 table), pinned per example."""

import pytest

from uc_controller import config

from conftest import CONTROLLER

EXPECTED = {
    "trunk": dict(bms_ip="10.20.0.2", bms_prefix=24, bms_net="10.20.0.0/24", bms_bcast="10.20.0.255",
                  it_address="192.0.2.20/24", it_gateway="192.0.2.1", it_dns=("192.0.2.53",), it_dhcp=False,
                  parent="eth0", it_if="it0", bms_if="bms0", it_vlan=10, bms_vlan=20, bms_nic_mac=None,
                  pinned_mac=None, zones_on_bms=("bms",),
                  broker_name="mqtt.hq.internal", broker_sans=("mqtt.hq.internal", "ucc-hq", "10.20.0.2"),
                  hub_bacnet_ip_interface="10.20.0.2/24", hub_discover_broadcast=("10.20.0.255:1337",),
                  chrony_allow="10.20.0.0/24", fqdn="ucc-hq.hq.internal", topic_roots=("bacnet-uc",)),
    "dual": dict(bms_ip="10.30.0.2", bms_prefix=24, bms_net="10.30.0.0/24", bms_bcast="10.30.0.255",
                 it_address=None, it_gateway=None, it_dns=(), it_dhcp=True,
                 parent="eth0", it_if="it0", bms_if="bms0", it_vlan=None, bms_vlan=None,
                 bms_nic_mac="00:e0:4c:68:00:01", pinned_mac="dc:a6:32:01:02:03", zones_on_bms=("bms",),
                 broker_name="mqtt.plant2.internal",
                 broker_sans=("mqtt.plant2.internal", "ucc-plant2", "10.30.0.2"),
                 hub_bacnet_ip_interface="10.30.0.2/24", hub_discover_broadcast=("10.30.0.255:1337",),
                 chrony_allow="10.30.0.0/24", fqdn="ucc-plant2.plant2.internal",
                 topic_roots=("bacnet-uc", "legacy-uc")),
    "flat": dict(bms_ip="10.20.0.2", bms_prefix=24, bms_net="10.20.0.0/24", bms_bcast="10.20.0.255",
                 it_address=None, it_gateway=None, it_dns=(), it_dhcp=False,
                 parent="eth0", it_if=None, bms_if="bms0", it_vlan=None, bms_vlan=None, bms_nic_mac=None,
                 pinned_mac=None, zones_on_bms=("it", "bms"),
                 broker_name="mqtt.lab.internal", broker_sans=("mqtt.lab.internal", "ucc-lab", "10.20.0.2"),
                 hub_bacnet_ip_interface="10.20.0.2/24", hub_discover_broadcast=("10.20.0.255:1337",),
                 chrony_allow="10.20.0.0/24", fqdn="ucc-lab.lab.internal", topic_roots=("bacnet-uc",)),
}


def load(name):
    return config.load(str(CONTROLLER / "site" / "examples" / f"{name}.yaml"))


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_derived_values(name):
    d = config.derive(load(name))
    for key, want in EXPECTED[name].items():
        assert getattr(d, key) == want, key
    assert d.hub_mqtt == {"host": "127.0.0.1", "port": 1883, "username": "uc-hub"}
    assert d.dhcp_options == {"dns-server": d.bms_ip, "log-server": d.bms_ip, "ntp-server": d.bms_ip}
    assert "255.255.255.255" not in " ".join(d.hub_discover_broadcast)


def test_broker_name_override_and_mac_auto_from_board():
    site = load("trunk")
    import dataclasses
    mqtt = dataclasses.replace(site.services.mqtt, broker_name="broker.example.org")
    site = dataclasses.replace(site, services=dataclasses.replace(site.services, mqtt=mqtt))
    d = config.derive(site, board={"pinned_mac": "DC:A6:32:AA:BB:CC"})
    assert d.broker_name == "broker.example.org"
    assert d.broker_sans == ("broker.example.org", "ucc-hq", "10.20.0.2")
    assert d.pinned_mac == "dc:a6:32:aa:bb:cc"          # network.mac: auto + board.json


def test_expected_devices_counts_active_devices():
    devices = [{"id": "za", "status": "active"}, {"id": "zb", "status": "revoked"}, {"id": "zc", "status": "active"}]
    assert config.derive(load("trunk"), devices).expected_devices == 2
