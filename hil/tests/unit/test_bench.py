"""bench.yml loading and validation (hilrig.bench)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from hilrig.bench import CATALOGS, RIG_CHANNELS, Bench, BenchError

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
    assert bench.la is not None and bench.la.channels["sync"] == 7
    assert bench.mstp is not None and bench.mstp.baud == 38400
    assert bench.power is not None and bench.power.method == "stim"
    assert bench.path == HOST / "bench.yml.example"


def test_sil_bench_is_valid() -> None:
    bench = Bench.load(HOST / "bench-sil.yml")
    assert (bench.mode, bench.dut.board, bench.dut.mac, bench.net.dut_iface) == ("sil", "bacserv", None, None)
    assert bench.stim is None and bench.la is None and bench.mstp is None


def test_optional_sections_may_be_absent_and_mac_is_normalised() -> None:
    data = example()
    for key in ("stim", "la", "mstp", "power"):
        del data[key]
    data["dut"]["mac"] = "02:80:E1:AB:CD:EF"
    bench = Bench.from_dict(data)
    assert bench.stim is None and bench.power is None
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
