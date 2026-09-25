"""bench.yml: what is connected to one rig, validated with errors that name the bad key.

A bench file describes one DUT and the instruments wired to it::

    name: bench1
    mode: hil                    # hil (hardware) or sil (native_sim / bacserv stand-in)
    dut:     {profile: P1, board: nucleo_f767zi, serial: /dev/hil/dut0, probe: <id>,
              mac: 00:80:e1:..., ip: 192.0.2.10, bacnet_instance: 260001, mqtt_client_id: z...}
    stim:    {serial: /dev/hil/stim0, chans: [di1, do3, ai0, ao0, nrst, ...]}
    net:     {dut_iface: enx...}
    la:      {driver: fx2lafw, channels: {do3: 0, di1: 1}}
    mstp:    {ftdi: /dev/hil/rs485, baud: 38400}
    power:   {method: stim}

Stimulus channel names are the DUT's I/O catalog names (firmware/boards/io/<board>.dtsi on
the BACnet branch), plus the rig channels (decisions B4, B5). ``stim``, ``la``, ``mstp`` and
``power`` are optional: tests that need them skip on benches without them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal

import yaml

# DUT profiles (decision B2) and the SIL stand-ins.
PROFILES: Mapping[str, str] = {
    "P1": "nucleo_f767zi",
    "P2": "frdm_mcxn947/mcxn947/cpu0",
    "P3": "nucleo_h563zi",
}
SIL_BOARDS = ("native_sim/native/64", "bacserv")

# uc,io-channels catalogs of the BACnet firmware (origin/claude/zephyr-bacnet-stm32-162k1g,
# f547c90). P3 is MQTT-only and has no catalog.
CATALOGS: Mapping[str, tuple[str, ...]] = {
    "nucleo_f767zi": tuple("di0 di1 di2 do0 do1 do2 do3 do4 ai0 ai1 ai2 ai3 ai4 ai5 ao0 ao1 ao2".split()),
    "frdm_mcxn947/mcxn947/cpu0": tuple("di0 di1 di2 di3 do0 do1 do2 do3 do4 ai0 ai1 ai2 ao0 ao1 ao2".split()),
    "native_sim/native/64": tuple("di0 di1 do0 do1 ai0 ai1 ao0 ao1".split()),
}
RIG_CHANNELS = ("nrst", "nrst_sense", "pwr", "sync", "m0", "m1", "m2", "m3")
MSTP_BAUDS = (9600, 19200, 38400, 57600, 76800, 115200)
POWER_METHODS = ("stim", "uhubctl", "none")
SUBNET_A = IPv4Network("192.0.2.0/24")
RIG_ADDRESSES = ("192.0.2.1", "192.0.2.11", "192.0.2.12", "192.0.2.254")
MAX_INSTANCE = 4194302  # 4194303 is the wildcard

# "saleae" (logic2-automation) or a sigrok driver spec: fx2lafw, dreamsourcelab-dslogic, demo,
# optionally with options (fx2lafw:conn=1.5); hilrig.la.open_analyzer() takes it as is.
_LA_DRIVER = re.compile(r"^[a-z0-9-]+(:\S+)?$")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,23}$")  # 23 chars: what MQTT 3.1.1 guarantees


class BenchError(ValueError):
    """bench.yml is invalid; the message names the file and the key."""


@dataclass(frozen=True)
class DutConfig:
    """The device under test and how it is reached."""

    profile: str
    board: str
    bacnet_instance: int
    serial: str | None = None
    probe: str | None = None
    mac: str | None = None
    ip: str = "192.0.2.10"
    mqtt_client_id: str | None = None


@dataclass(frozen=True)
class StimConfig:
    """The stimulus board and the channels wired on this bench."""

    serial: str
    chans: tuple[str, ...]
    baud: int = 115200


@dataclass(frozen=True)
class NetConfig:
    """The host NIC cabled to the DUT (moved into netns lan-a); None in SIL."""

    dut_iface: str | None = None


@dataclass(frozen=True)
class LaConfig:
    """Logic analyzer driver (``saleae`` or a sigrok driver spec) and signal -> channel number."""

    driver: str
    channels: Mapping[str, int]


@dataclass(frozen=True)
class MstpConfig:
    """The bench's RS-485 adapter and MS/TP baud rate."""

    ftdi: str
    baud: int


@dataclass(frozen=True)
class PowerConfig:
    """How the DUT's power is switched."""

    method: str


@dataclass(frozen=True)
class Bench:
    """A validated bench.yml."""

    name: str
    mode: Literal["hil", "sil"]
    dut: DutConfig
    net: NetConfig
    stim: StimConfig | None = None
    la: LaConfig | None = None
    mstp: MstpConfig | None = None
    power: PowerConfig | None = None
    path: Path | None = None

    @classmethod
    def load(cls, path: str | Path) -> Bench:
        """Read and validate a bench file."""
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text())
        except OSError as e:
            raise BenchError(f"{path}: cannot read: {e.strerror}") from None
        except yaml.YAMLError as e:
            raise BenchError(f"{path}: not valid YAML: {e}") from None
        return cls.from_dict(data, source=str(path), path=path)

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "bench", path: Path | None = None) -> Bench:
        """Validate a parsed bench file; ``source`` prefixes error messages."""
        top = _Section(data, source)
        top.allow("name", "mode", "dut", "stim", "net", "la", "mstp", "power")
        mode = top.choice("mode", ("hil", "sil"))
        dut = _dut(top.section("dut"), mode)
        net = _net(top.section("net", required=False))
        if mode == "hil" and not net.dut_iface:
            raise BenchError(f"{source}: net.dut_iface: required in hil mode (the NIC cabled to the DUT)")
        if mode == "sil" and net.dut_iface:
            raise BenchError(f"{source}: net.dut_iface: not used in sil mode")
        return cls(
            name=_required(top.text("name")),
            mode="hil" if mode == "hil" else "sil",
            dut=dut,
            net=net,
            stim=_stim(top.section("stim"), dut.board) if "stim" in top else None,
            la=_la(top.section("la")) if "la" in top else None,
            mstp=_mstp(top.section("mstp")) if "mstp" in top else None,
            power=_power(top.section("power")) if "power" in top else None,
            path=path,
        )


class _Section:
    """One mapping of the file, with typed getters whose errors read ``<file>: dut.mac: ...``."""

    def __init__(self, data: Any, source: str, path: str = "") -> None:
        self.source, self.path = source, path
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise BenchError(f"{self.where()}: expected a mapping, got {type(data).__name__}")
        self.data = data

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def key(self, key: str) -> str:
        return f"{self.path}.{key}" if self.path else key

    def where(self, key: str = "") -> str:
        dotted = self.key(key) if key else self.path
        return f"{self.source}: {dotted}" if dotted else self.source

    def fail(self, key: str, problem: str) -> BenchError:
        return BenchError(f"{self.where(key)}: {problem}")

    def allow(self, *keys: str) -> None:
        unknown = sorted(set(self.data) - set(keys))
        if unknown:
            raise BenchError(
                f"{self.where()}: unknown key(s) {', '.join(map(str, unknown))} (allowed: {', '.join(keys)})"
            )

    def raw(self, key: str, required: bool) -> Any:
        if key not in self.data or self.data[key] is None:
            if required:
                raise self.fail(key, "required")
            return None
        return self.data[key]

    def section(self, key: str, required: bool = True) -> _Section:
        return _Section(self.raw(key, required), self.source, self.key(key))

    def text(self, key: str, required: bool = True) -> str | None:
        value = self.raw(key, required)
        if value is not None and not isinstance(value, str | int):
            raise self.fail(key, f"expected text, got {value!r}")
        return None if value is None else str(value)

    def integer(self, key: str, lo: int, hi: int, default: int | None = None) -> int:
        value = self.raw(key, default is None)
        if value is None:
            assert default is not None
            return default
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise self.fail(key, f"expected an integer {lo}..{hi}, got {value!r}")
        return value

    def choice(self, key: str, choices: tuple[Any, ...]) -> Any:
        value = self.raw(key, True)
        if value not in choices:
            raise self.fail(key, f"{value!r} is not one of {', '.join(map(str, choices))}")
        return value


def _required(value: str | None) -> str:
    assert value is not None  # _Section.text(required=True) never returns None
    return value


def _dut(s: _Section, mode: str) -> DutConfig:
    s.allow("profile", "board", "serial", "probe", "mac", "ip", "bacnet_instance", "mqtt_client_id")
    profile = _required(s.text("profile"))
    board = _required(s.text("board"))
    if mode == "hil":
        if profile not in PROFILES:
            raise s.fail("profile", f"{profile!r} is not a DUT profile ({', '.join(PROFILES)})")
        if board != PROFILES[profile]:
            raise s.fail("board", f"profile {profile} is {PROFILES[profile]}, not {board!r}")
    elif profile != "sil" or board not in SIL_BOARDS:
        raise BenchError(
            f"{s.where()}: sil mode needs profile 'sil' and board one of {', '.join(SIL_BOARDS)}"
        )
    mac = s.text("mac", required=mode == "hil")
    if mac is not None:
        mac = mac.lower()
        if not _MAC.match(mac):
            raise s.fail(
                "mac",
                f"{mac!r} is not a MAC address (xx:xx:xx:xx:xx:xx); it keys the "
                "dnsmasq reservation, so learn it once from the DUT's console",
            )
    ip = s.text("ip", required=False) or "192.0.2.10"
    try:
        address = IPv4Address(ip)
    except ValueError:
        raise s.fail("ip", f"{ip!r} is not an IPv4 address") from None
    if address not in SUBNET_A or ip in RIG_ADDRESSES or address in (SUBNET_A[0], SUBNET_A[-1]):
        raise s.fail(
            "ip", f"{ip} must be a free host address in {SUBNET_A} (rig uses {', '.join(RIG_ADDRESSES)})"
        )
    client_id = s.text("mqtt_client_id", required=False)
    if client_id is not None and not _CLIENT_ID.match(client_id):
        raise s.fail("mqtt_client_id", f"{client_id!r}: use 1-23 of [A-Za-z0-9._-]")
    return DutConfig(
        profile=profile,
        board=board,
        bacnet_instance=s.integer("bacnet_instance", 0, MAX_INSTANCE),
        serial=s.text("serial", required=False),
        probe=s.text("probe", required=False),
        mac=mac,
        ip=ip,
        mqtt_client_id=client_id,
    )


def _net(s: _Section) -> NetConfig:
    s.allow("dut_iface")
    return NetConfig(s.text("dut_iface", required=False))


def _power(s: _Section) -> PowerConfig:
    s.allow("method")
    return PowerConfig(s.choice("method", POWER_METHODS))


def _stim(s: _Section, board: str) -> StimConfig:
    s.allow("serial", "baud", "chans")
    chans = s.raw("chans", True)
    if not isinstance(chans, list) or not chans or not all(isinstance(c, str) for c in chans):
        raise s.fail("chans", "expected a non-empty list of channel names")
    catalog = CATALOGS.get(board, ())
    for chan in chans:
        if chan not in RIG_CHANNELS and chan not in catalog:
            raise s.fail(
                "chans",
                f"{chan!r} is neither a rig channel ({', '.join(RIG_CHANNELS)}) "
                f"nor in the {board} I/O catalog ({', '.join(catalog) or 'none'})",
            )
    if len(set(chans)) != len(chans):
        raise s.fail("chans", "lists a channel twice")
    return StimConfig(
        serial=_required(s.text("serial")),
        chans=tuple(chans),
        baud=s.integer("baud", 1200, 4_000_000, default=115200),
    )


def _la(s: _Section) -> LaConfig:
    s.allow("driver", "channels")
    driver = _required(s.text("driver"))
    if not _LA_DRIVER.match(driver):
        raise s.fail("driver", f"{driver!r} is neither 'saleae' nor a sigrok driver spec (e.g. fx2lafw)")
    channels = s.raw("channels", True)
    if not isinstance(channels, dict) or not channels:
        raise s.fail("channels", "expected a mapping of signal name to channel number")
    for name, number in channels.items():
        if isinstance(number, bool) or not isinstance(number, int) or not 0 <= number <= 15:
            raise s.fail(f"channels.{name}", f"expected a channel number 0..15, got {number!r}")
    if len(set(channels.values())) != len(channels):
        raise s.fail("channels", "uses a channel number twice")
    return LaConfig(driver=driver, channels={str(k): v for k, v in channels.items()})


def _mstp(s: _Section) -> MstpConfig:
    s.allow("ftdi", "baud")
    return MstpConfig(ftdi=_required(s.text("ftdi")), baud=s.choice("baud", MSTP_BAUDS))
