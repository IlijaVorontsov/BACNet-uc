"""bench.yml loading and validation (hilrig.bench)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from hilrig.bench import (
    CATALOG,
    CATALOGS,
    RIG_CHANNELS,
    Bench,
    BenchError,
    channel,
    client_id_from_uid,
    drive_level,
    logical,
    native_uid,
    stm32_client_id,
    stm32_mac,
    stm32_uid,
)

HOST = Path(__file__).resolve().parents[2] / "host"


def example() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((HOST / "bench.yml.example").read_text())
    return data


def test_example_bench_is_valid_and_lists_the_catalog() -> None:
    bench = Bench.load(HOST / "bench.yml.example")
    assert (bench.mode, bench.dut.profile, bench.dut.board) == ("hil", "P1", "nucleo_f767zi")
    assert bench.dut.ip == "192.0.2.10" and bench.dut.bacnet_instance == 260001
    assert bench.stim is not None and bench.stim.baud == 115200
    assert set(bench.stim.chans) == set(CATALOGS["nucleo_f767zi"]) | set(RIG_CHANNELS)
    assert bench.net.dut_iface == "enx00e04c680001"
    assert bench.la is not None and bench.la.channels == {
        "nrst": 0,
        "m0": 1,
        "m1": 2,
        "di1": 3,
        "do3": 4,
        "do0": 5,
        "vcp_tx": 6,
        "sync": 7,
    }  # HIL.md section 6, It1 FX2
    assert bench.dut.mqtt_client_id == "z6grjichj718h801900s0" and bench.dut.uid is not None
    assert bench.stim.probe and bench.hub is not None and bench.hub.stim_port == 2
    assert bench.commissioning is not None and bench.commissioning.nrst_idle_v == 3.25
    assert bench.mstp is not None and bench.mstp.baud == 38400
    assert bench.power is not None and bench.power.method == "stim"
    assert bench.path == HOST / "bench.yml.example"


def test_sil_bench_is_valid() -> None:
    bench = Bench.load(HOST / "bench-sil.yml")
    assert (bench.mode, bench.dut.board, bench.dut.mac, bench.net.dut_iface) == ("sil", "bacserv", None, None)
    assert bench.stim is None and bench.la is None and bench.mstp is None


def test_optional_sections_may_be_absent_and_mac_is_normalised() -> None:
    data = example()
    for key in ("stim", "la", "mstp", "power", "hub", "commissioning"):
        del data[key]
    del data["dut"]["uid"]  # without a UID the MAC is not cross-checked
    data["dut"]["mac"] = "02:80:E1:AB:CD:EF"
    bench = Bench.from_dict(data)
    assert bench.stim is None and bench.power is None and bench.hub is None
    assert bench.dut.mac == "02:80:e1:ab:cd:ef"


def edit(path: str, value: Any, delete: bool = False) -> dict[str, Any]:
    """Return the example with one dotted key changed (or deleted)."""
    data = copy.deepcopy(example())
    *parents, last = path.split(".")
    node = data
    for p in parents:
        node = node[p]
    if delete:
        del node[last]
    else:
        node[last] = value
    return data


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("colour", "red", "bench: unknown key(s) colour (allowed:"),
        ("mode", "lab", "bench: mode: 'lab' is not one of hil, sil"),
        ("dut.profile", "P9", "bench: dut.profile: 'P9' is not a DUT profile"),
        ("dut.board", "nucleo_h563zi", "bench: dut.board: profile P1 is nucleo_f767zi, not 'nucleo_h563zi'"),
        ("dut.mac", "02:80:e1", "bench: dut.mac: '02:80:e1' is not a MAC address"),
        ("dut.ip", "192.0.2.1", "bench: dut.ip: 192.0.2.1 must be a free host address"),
        ("dut.ip", "10.0.0.5", "bench: dut.ip: 10.0.0.5 must be a free host address"),
        ("dut.ip", "not-an-ip", "bench: dut.ip: 'not-an-ip' is not an IPv4 address"),
        ("dut.bacnet_instance", 4194303, "bench: dut.bacnet_instance: expected an integer 0..4194302"),
        ("dut.bacnet_instance", "260001", "bench: dut.bacnet_instance: expected an integer"),
        ("dut.mqtt_client_id", "x" * 24, "bench: dut.mqtt_client_id:"),
        ("dut.flash", "yes", "bench: dut: unknown key(s) flash"),
        ("stim.chans", ["do3", "do9"], "'do9' is neither a rig channel"),
        ("stim.chans", ["do3", "do3"], "bench: stim.chans: lists a channel twice"),
        ("stim.chans", [], "bench: stim.chans: expected a non-empty list"),
        ("la.driver", "Fx2 LAFW", "bench: la.driver: 'Fx2 LAFW' is neither 'saleae' nor a sigrok driver"),
        ("la.channels", {"sync": 16}, "bench: la.channels.sync: expected a channel number 0..15"),
        ("la.channels", {"a": 1, "b": 1}, "bench: la.channels: uses a channel number twice"),
        ("mstp.baud", 12345, "bench: mstp.baud: 12345 is not one of"),
        ("power.method", "relay", "bench: power.method: 'relay' is not one of stim, uhubctl, none"),
        ("net.dut_iface", None, "bench: net.dut_iface: required in hil mode"),
        ("dut.ip", "192.0.2.2", "bench: dut.ip: 192.0.2.2 must be a free host address"),
        ("dut.uid", "xyz", "bench: dut.uid: 'xyz': expected 4-16 bytes in hex"),
        ("dut.uid", "00" * 8, "bench: dut.uid: an STM32 UID has 12 bytes, got 8"),
        (
            "dut.mac",
            "02:80:e1:00:00:01",
            "bench: dut.mac: 02:80:e1:00:00:01 is not the MAC derived from dut.uid",
        ),
        (
            "dut.mqtt_client_id",
            "z0000",
            "bench: dut.mqtt_client_id: z0000 is not the id derived from dut.uid",
        ),
        ("hub.location", "usb1", "bench: hub.location: 'usb1' is not a uhubctl hub location"),
        ("hub.stim_port", 0, "bench: hub.stim_port: expected an integer 1..32"),
        ("commissioning.nrst_idle_v", "high", "bench: commissioning.nrst_idle_v: expected a number"),
    ],
)
def test_invalid_values_name_the_key(path: str, value: Any, message: str) -> None:
    with pytest.raises(BenchError) as err:
        Bench.from_dict(edit(path, value))
    assert message in str(err.value)


@pytest.mark.parametrize("path", ["dut", "dut.mac", "dut.board", "stim.serial", "mode", "name"])
def test_missing_required_keys(path: str) -> None:
    with pytest.raises(BenchError, match=re.escape(f"bench: {path}: required")):
        Bench.from_dict(edit(path, None, delete=True))


@pytest.mark.parametrize(
    "driver", ["saleae", "fx2lafw", "dreamsourcelab-dslogic", "demo", "fx2lafw:conn=1.5"]
)
def test_la_driver_is_saleae_or_a_sigrok_spec(driver: str) -> None:
    bench = Bench.from_dict(edit("la.driver", driver))
    assert bench.la is not None and bench.la.driver == driver


def test_sil_mode_rules() -> None:
    sil = yaml.safe_load((HOST / "bench-sil.yml").read_text())
    Bench.from_dict(sil)
    with pytest.raises(BenchError, match="sil mode needs profile 'sil'"):
        Bench.from_dict({**sil, "dut": {**sil["dut"], "board": "nucleo_f767zi"}})
    with pytest.raises(BenchError, match=re.escape("bench: net.dut_iface: not used in sil mode")):
        Bench.from_dict({**sil, "net": {"dut_iface": "eth1"}})


def test_file_errors_name_the_file(tmp_path: Path) -> None:
    with pytest.raises(BenchError, match=r"missing\.yml: cannot read"):
        Bench.load(tmp_path / "missing.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("dut: [unclosed\n")
    with pytest.raises(BenchError, match=r"bad\.yml: not valid YAML"):
        Bench.load(bad)
    listed = tmp_path / "list.yml"
    listed.write_text("- a\n- b\n")
    with pytest.raises(BenchError, match=re.escape(f"{listed}: expected a mapping, got list")):
        Bench.load(listed)


# ---- identity (D29) ------------------------------------------------------------------------
# Expected values computed by Zephyr's own code: crc32_ieee() from subsys/crc/crc32_sw.c,
# the hwinfo_stm32.c byte order and eth_stm32_hal_common.c's little-endian memcpy, compiled
# with gcc (and apps/mqtt_tls/src/device_id.c's base32_encode() for the client id).
@pytest.mark.parametrize(
    ("words", "mac", "client_id"),
    [
        ((0x00290038, 0x33385114, 0x34373932), "02:80:e1:67:44:68", "z6grjichj718h801900s0"),
        ((0x12345678, 0x9ABCDEF0, 0x0FEDCBA9), "02:80:e1:34:07:60", "z1vmsnacqnjff04hkaps0"),
        ((0, 0, 0), "02:80:e1:6f:c6:d5", "z" + "0" * 20),
    ],
)
def test_stm32_mac_and_client_id_match_the_firmware(
    words: tuple[int, int, int], mac: str, client_id: str
) -> None:
    assert stm32_mac(*words) == mac
    assert stm32_client_id(*words) == client_id and len(client_id) == 21


def reference_crc32(data: bytes) -> int:
    """Bitwise CRC-32 (IEEE, reflected 0xEDB88320), independent of zlib."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def test_stm32_mac_byte_order() -> None:
    """Regression guard for D29: word 2 first, each word big-endian, CRC bytes little-endian.

    A CRC over the words in mdw order would give another MAC and fail R-07 every session.
    """
    w0, w1, w2 = 0x11223344, 0x55667788, 0x99AABBCC
    uid = stm32_uid(w0, w1, w2)
    assert uid == bytes.fromhex("99aabbcc5566778811223344")
    crc = reference_crc32(uid)
    assert stm32_mac(w0, w1, w2) == "02:80:e1:" + ":".join(f"{crc >> s & 0xFF:02x}" for s in (0, 8, 16))
    naive = reference_crc32(bytes.fromhex("112233445566778899aabbcc"))
    assert stm32_mac(w0, w1, w2) != "02:80:e1:" + ":".join(f"{naive >> s & 0xFF:02x}" for s in (0, 8, 16))


def test_native_sim_identity() -> None:
    assert native_uid(0x48494C31).hex() == "48494c31"
    assert client_id_from_uid(native_uid(0x48494C31)) == "z914koc8"
    with pytest.raises(ValueError, match="condensed"):
        client_id_from_uid(bytes(16))


# ---- catalog polarity ------------------------------------------------------------------------
def test_catalog_polarity_and_hil_io_extras() -> None:
    f767 = CATALOG["nucleo_f767zi"]
    assert [c.name for c in f767 if c.active_low] == ["di1", "di2"]
    assert [c.name for c in f767 if c.hil_io] == ["di3", "di4", "do5", "do6"]
    assert CATALOGS["nucleo_f767zi"][-1] == "ao2" and len(CATALOGS["nucleo_f767zi"]) == 17  # FW-01 ids
    mcxn = CATALOG["frdm_mcxn947/mcxn947/cpu0"]
    assert [c.name for c in mcxn if c.active_low] == ["di0", "di1", "di2", "di3", "do0", "do1", "do2"]
    di1, di0 = channel("nucleo_f767zi", "di1"), channel("nucleo_f767zi", "di0")
    assert (drive_level(di1, True), drive_level(di1, False)) == (0, "z")
    assert (drive_level(di0, True), drive_level(di0, False)) == (1, "z")
    assert (logical(di1, 0), logical(di1, 1), logical(di0, 1)) == (1, 0, 1)
    with pytest.raises(ValueError, match="drives only di"):
        drive_level(channel("nucleo_f767zi", "do3"), True)
    with pytest.raises(KeyError):
        channel("nucleo_f767zi", "do9")


def test_stim_may_list_hil_io_extras() -> None:
    bench = Bench.from_dict(edit("stim.chans", ["di3", "do6", "lb", "v3v3", "v5"]))
    assert bench.stim is not None and bench.stim.chans == ("di3", "do6", "lb", "v3v3", "v5")
