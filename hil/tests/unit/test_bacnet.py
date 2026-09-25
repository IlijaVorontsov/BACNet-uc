"""bacnet-stack tool wrappers (hilrig.bacnet): output parsers, then live against bacserv."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hilrig import bacnet, netns
from hilrig.bacnet import Bacnet, BacnetError, BacServ, BdtEntry, Device, FdtEntry
from hilrig.netns import HOSTS, Topology

WHOIS_OUT = """\
;Device   MAC (hex)            SNET  SADR (hex)           APDU
;-------- -------------------- ----- -------------------- ----
  260001  C0:00:02:0A:BA:C0    0     00                   1476
  1002    c0:00:02:0c:ba:c0    0     00                   480
;
; Total Devices: 2
"""

RPM_OUT = """\
device #260001
{
    object-name: "HIL standin"
    present-value: BACnet Error: property: unknown-property
}
analog-input #1
{
    units: percent
}
"""

EPICS_OUT = """\
BACnet Standard Application Services Supported:
{
-- services reported by this device
 ReadProperty
 Who-Is
}

List of Objects in Test Device:
{
  {
    object-identifier: (device, 260001)
    object-name: "HIL standin"
    object-list: {
        (device, 260001), (analog-input, 1) }
    database-revision: ?
  -- Found 2 Objects
  },
  {
    object-identifier: (analog-input, 1)
    present-value: ? Writable
    units: percent
  }
}
End of BACnet Protocol Implementation Conformance Statement
"""


def test_parse_whois() -> None:
    devices = bacnet.parse_whois(WHOIS_OUT)
    assert devices == [
        Device(260001, "C0:00:02:0A:BA:C0", 0, "00", 1476),
        Device(1002, "C0:00:02:0C:BA:C0", 0, "00", 480),
    ]
    assert devices[0].address == ("192.0.2.10", 47808)
    with pytest.raises(ValueError, match="not a BACnet/IP address"):
        _ = Device(5, "05", 0, "00", 480).address


def test_parse_tables() -> None:
    assert bacnet.parse_fdt("BBMD: 192.0.2.10:47808\nFDT-001: 198.51.100.10:47808 60s 88s\n") == [
        FdtEntry("198.51.100.10", 47808, 60, 88)
    ]
    assert bacnet.parse_bdt("BBMD: 192.0.2.10:47808\nBDT-001: 192.0.2.10:47808 255.255.255.255\n") == [
        BdtEntry("192.0.2.10", 47808, "255.255.255.255")
    ]
    assert bacnet.parse_fdt("") == []


def test_parse_rpm_keeps_property_errors_as_values() -> None:
    assert bacnet.parse_rpm(RPM_OUT) == {
        ("device", 260001): {
            "object-name": '"HIL standin"',
            "present-value": "BACnet Error: property: unknown-property",
        },
        ("analog-input", 1): {"units": "percent"},
    }


def test_parse_epics() -> None:
    epics = bacnet.parse_epics(EPICS_OUT)
    assert epics.services == ("ReadProperty", "Who-Is")
    assert epics.objects[("device", 260001)]["object-list"] == "{ (device, 260001), (analog-input, 1) }"
    assert epics.objects[("analog-input", 1)] == {
        "object-identifier": "(analog-input, 1)",
        "present-value": "?",
        "units": "percent",
    }
    assert epics.writable == frozenset({(("analog-input", 1), "present-value")})


def test_parse_epics_keeps_double_dashes_inside_strings() -> None:
    """Regression: '--' inside a quoted value was taken for the start of an EPICS comment."""
    text = EPICS_OUT.replace('object-name: "HIL standin"', 'object-name: "HIL--standin" -- the name')
    assert bacnet.parse_epics(text).objects[("device", 260001)]["object-name"] == '"HIL--standin"'


def test_error_fields() -> None:
    err = BacnetError("bacrp", "error", "object: unknown-object")
    assert (err.kind, err.error_class, err.error_code) == ("error", "object", "unknown-object")
    assert BacnetError("bacrp", "timeout", "Error: APDU Timeout!").error_code == ""


def test_tools_dir_follows_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(bacnet.TOOLS_ENV, str(tmp_path))
    assert bacnet.tools_dir() == tmp_path
    monkeypatch.delenv(bacnet.TOOLS_ENV)
    assert bacnet.tools_dir() == bacnet.DEFAULT_TOOLS
    with pytest.raises(FileNotFoundError, match=bacnet.TOOLS_ENV):
        Bacnet(None, "lo", tools=tmp_path).run("bacwi")


def test_tools_ignore_bacnet_variables_of_the_calling_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: an exported BACNET_IP_PORT or BACNET_BBMD_ADDRESS reached every rig tool."""
    (tmp_path / "bacwi").write_text("#!/bin/sh\nenv\n")
    (tmp_path / "bacwi").chmod(0o755)
    monkeypatch.setenv("BACNET_IP_PORT", "47809")
    monkeypatch.setenv("BACNET_BBMD_ADDRESS", "203.0.113.9")
    monkeypatch.setenv("HIL_UNRELATED", "kept")
    env = Bacnet(None, "lo0", tools=tmp_path).foreign("192.0.2.10").run("bacwi").stdout.splitlines()
    assert "BACNET_IFACE=lo0" in env and "BACNET_BBMD_ADDRESS=192.0.2.10" in env
    assert "HIL_UNRELATED=kept" in env
    assert not [v for v in env if v.startswith("BACNET_IP_PORT=")]


# ---- live, on the private topology ---------------------------------------------------------
@pytest.fixture(scope="module")
def tools() -> Path:
    path = bacnet.tools_dir()
    if not (path / "bacserv").exists():
        pytest.skip(f"bacnet-stack tools not found in {path} (set {bacnet.TOOLS_ENV})")
    return path


@pytest.fixture(scope="module")
def devices(unit_net: Topology, tools: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """bacserv 1001 in sim1 (also a BBMD) and 1002 in sim2."""
    logs = tmp_path_factory.mktemp("bacserv")
    with (
        BacServ(unit_net.ns("sim1"), "sim10", 1001, "sim-1001", log=logs / "1001.log", tools=tools),
        BacServ(unit_net.ns("sim2"), "sim20", 1002, "sim-1002", log=logs / "1002.log", tools=tools),
    ):
        yield


@pytest.fixture(scope="module")
def client(unit_net: Topology, tools: Path) -> Bacnet:
    return Bacnet(unit_net.ns("svc"), "svc0", tools=tools)


def test_whois_finds_devices_and_honours_the_range(devices: None, client: Bacnet) -> None:
    found = {d.instance: d for d in client.whois(wait_ms=500)}
    assert set(found) == {1001, 1002}
    assert found[1001].address == (HOSTS["sim1"].ip, 47808)
    assert [d.instance for d in client.whois(1001, wait_ms=500)] == [1001]
    assert client.whois(2000, 3000, wait_ms=300) == []


def test_read_write_and_errors(devices: None, client: Bacnet) -> None:
    assert client.read(1001, "device", 1001, "object-name") == '"sim-1001"'
    client.write(1001, "analog-value", 1, "present-value", 42.5, tag=4)
    assert float(client.read(1001, "analog-value", 1, "present-value")) == 42.5
    with pytest.raises(BacnetError) as err:
        client.read(1001, "analog-input", 4194302, "present-value")
    assert (err.value.error_class, err.value.error_code) == ("object", "unknown-object")
    with pytest.raises(BacnetError) as err:
        client.write(1001, "analog-input", 1, "present-value", 1.0, tag=4)
    assert err.value.error_code == "write-access-denied"
    fast = client.with_env(BACNET_APDU_TIMEOUT="300", BACNET_APDU_RETRIES="0")
    with pytest.raises(BacnetError) as err:
        fast.read(4000, "device", 4000, "object-name")
    assert err.value.kind == "timeout"


def test_read_multiple_and_epics(devices: None, client: Bacnet) -> None:
    values = client.read_multiple(
        1002, {("device", 1002): ["object-name", "vendor-identifier"], ("analog-input", 1): ["units"]}
    )
    assert values[("device", 1002)]["object-name"] == '"sim-1002"'
    assert values[("analog-input", 1)]["units"] == "percent"
    epics = client.epics(1002)
    assert "ReadPropertyMultiple" in epics.services
    assert epics.objects[("device", 1002)]["object-name"] == '"sim-1002"'
    assert ("analog-input", 1) in epics.objects


def test_subnet_b_needs_the_bbmd(devices: None, unit_net: Topology, tools: Path, client: Bacnet) -> None:
    fd = Bacnet(unit_net.ns("fd"), "fd0", tools=tools)
    assert fd.whois(wait_ms=500) == []  # rtr forwards no broadcasts
    registered = fd.foreign(HOSTS["sim1"].ip, ttl_s=60)
    assert {d.instance for d in registered.whois(wait_ms=1500)} == {1001, 1002}
    fdt = client.read_fdt(HOSTS["sim1"].ip)
    assert [(e.ip, e.ttl_s) for e in fdt] == [(HOSTS["fd"].ip, 60)]
    assert 60 < fdt[0].remaining_s <= 90  # TTL plus the 30 s grace period
    assert client.read_bdt(HOSTS["sim1"].ip) == [BdtEntry(HOSTS["sim1"].ip, 47808, "255.255.255.255")]
    fast = client.with_env(BACNET_APDU_TIMEOUT="300", BACNET_APDU_RETRIES="0")
    assert fast.read_fdt(HOSTS["sim2"].ip + "0") == []  # nobody there


def test_bacserv_that_cannot_start_is_reported(
    unit_net: Topology, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[subprocess.Popen[bytes]] = []
    spawn = netns.spawn

    def recording_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        started.append(spawn(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(netns, "spawn", recording_spawn)
    server = BacServ(unit_net.ns("bbmdb"), "bb0", 7, "x", log=tmp_path / "x.log", tools=tmp_path)
    with pytest.raises(RuntimeError, match="did not bind UDP 47808"):
        server.start(timeout=2)
    # Regression: stop() left the stdin pipe that spawn() opens (one fd per bacserv) open.
    [proc] = started
    assert proc.poll() is not None and proc.stdin is not None and proc.stdin.closed
