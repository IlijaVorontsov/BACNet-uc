"""bench.yml: what is connected to one rig, validated with errors that name the bad key.

A bench file describes one DUT and the instruments wired to it::

    name: bench1
    mode: hil                    # hil (hardware) or sil (native_sim / bacserv stand-in)
    dut:     {profile: P1, board: nucleo_f767zi, serial: /dev/hil/dut0, probe: <ST-LINK serial>,
              uid: <24 hex digits>, mac: 02:80:e1:..., ip: 192.0.2.10, bacnet_instance: 260001,
              mqtt_client_id: z<20 base32 digits>}
    stim:    {serial: /dev/hil/stim0, probe: <ST-LINK serial>, chans: [di1, do3, ai0, nrst, ...]}
    net:     {dut_iface: enx...}
    la:      {driver: fx2lafw, channels: {nrst: 0, m0: 1, ...}}
    mstp:    {ftdi: /dev/hil/rs485, baud: 38400}
    power:   {method: stim}
    hub:     {location: 1-1, dut_port: 1, stim_port: 2}          # uhubctl
    commissioning: {v_on_released_v: 2.9, v_on_cut_v: 0.05, nrst_idle_v: 3.25}   # DMM, W2

Stimulus channel names are the DUT's I/O catalog names (firmware/boards/io/<board>.dtsi on
the BACnet branch, plus the ``hil-io`` snippet extras) and the rig channels (D22). Levels on
DUT channels are physical wire levels; :func:`drive_level` and :func:`logical` apply the
catalog polarity. ``stim``, ``la``, ``mstp``, ``power``, ``hub`` and ``commissioning`` are
optional: tests that need them skip on benches without them.

The DUT's identity is learned, never forced (D29): :func:`stm32_mac` and
:func:`stm32_client_id` compute the MAC and the MQTT client id from the UID words read over
SWD at commissioning, and a bench that lists ``dut.uid`` must agree with them.
"""

from __future__ import annotations

import base64
import re
import struct
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal, cast

import yaml

# DUT profiles (decision B2) and the SIL stand-ins.
PROFILES: Mapping[str, str] = {
    "P1": "nucleo_f767zi",
    "P2": "frdm_mcxn947/mcxn947/cpu0",
    "P3": "nucleo_h563zi",
}
SIL_BOARDS = ("native_sim/native/64", "bacserv")
STM32_BOARDS = ("nucleo_f767zi", "nucleo_h563zi")

Kind = Literal["di", "do", "ai", "ao"]


@dataclass(frozen=True)
class CatalogChannel:
    """One uc,io-channels entry: its kind, the GPIO_ACTIVE_LOW flag, and whether it is an extra.

    ``hil_io`` channels come from the ``hil-io`` snippet and exist on instrumented BACnet
    images only; they are appended, so the ids of the base catalog do not change (FW-01).
    """

    name: str
    kind: Kind
    active_low: bool = False
    hil_io: bool = False


def _chans(spec: str, *, low: str = "", hil_io: bool = False) -> tuple[CatalogChannel, ...]:
    lows = set(low.split())
    return tuple(CatalogChannel(n, cast(Kind, n[:2]), n in lows, hil_io) for n in spec.split())


# uc,io-channels catalogs of the BACnet firmware (origin/claude/zephyr-bacnet-stm32-162k1g,
# 2161be7, firmware/boards/io/*.dtsi) in devicetree (= id) order. P3 is MQTT-only.
CATALOG: Mapping[str, tuple[CatalogChannel, ...]] = {
    "nucleo_f767zi": _chans(
        "di0 di1 di2 do0 do1 do2 do3 do4 ai0 ai1 ai2 ai3 ai4 ai5 ao0 ao1 ao2", low="di1 di2"
    )
    + _chans("di3 di4 do5 do6", hil_io=True),
    "frdm_mcxn947/mcxn947/cpu0": _chans(
        "di0 di1 di2 di3 do0 do1 do2 do3 do4 ai0 ai1 ai2 ao0 ao1 ao2", low="di0 di1 di2 di3 do0 do1 do2"
    ),
    "native_sim/native/64": _chans("di0 di1 do0 do1 ai0 ai1 ao0 ao1"),
}
# Base catalog names (release images), kept for callers that only need the names.
CATALOGS: Mapping[str, tuple[str, ...]] = {
    board: tuple(c.name for c in chans if not c.hil_io) for board, chans in CATALOG.items()
}
RIG_CHANNELS = ("nrst", "nrst_sense", "pwr", "sync", "m0", "m1", "m2", "m3", "lb", "v3v3", "v5")
MSTP_BAUDS = (9600, 19200, 38400, 57600, 76800, 115200)
POWER_METHODS = ("stim", "uhubctl", "none")
SUBNET_A = IPv4Network("192.0.2.0/24")
RIG_ADDRESSES = ("192.0.2.1", "192.0.2.2", "192.0.2.11", "192.0.2.12", "192.0.2.254")
MAX_INSTANCE = 4194302  # 4194303 is the wildcard

# "saleae" (logic2-automation) or a sigrok driver spec: fx2lafw, dreamsourcelab-dslogic, demo,
# optionally with options (fx2lafw:conn=1.5); hilrig.la.open_analyzer() takes it as is.
_LA_DRIVER = re.compile(r"^[a-z0-9-]+(:\S+)?$")
_MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,23}$")  # 23 chars: what MQTT 3.1.1 guarantees
_UID = re.compile(r"^([0-9a-f]{2}){4,16}$")
_HUB = re.compile(r"^\d+(-\d+(\.\d+)*)?$")  # uhubctl location, e.g. 1-1 or 2-1.4


class BenchError(ValueError):
    """bench.yml is invalid; the message names the file and the key."""


# ---- identity (D29) -----------------------------------------------------------------------
def stm32_uid(w0: int, w1: int, w2: int) -> bytes:
    """The 12-byte UID as Zephyr's hwinfo returns it on STM32: be32(w2) + be32(w1) + be32(w0).

    ``w0``..``w2`` are the words at UID_BASE + 0, 4, 8 (0x1FF0F420/424/428 on F7), as
    ``mdw 0x1ff0f420 3`` prints them (hwinfo_stm32.c reads word 2 first, each big-endian).
    """
    return struct.pack(">III", w2, w1, w0)


def mac_from_uid(uid: bytes) -> str:
    """02:80:E1 plus the low 3 bytes, little-endian, of crc32_ieee(uid) (eth_stm32_hal_common.c)."""
    crc = zlib.crc32(uid)
    return ":".join(f"{b:02x}" for b in (0x02, 0x80, 0xE1, crc & 0xFF, (crc >> 8) & 0xFF, (crc >> 16) & 0xFF))


def stm32_mac(w0: int, w1: int, w2: int) -> str:
    """The MAC Zephyr 4.4.2 derives on an STM32 DUT without a configured address (D29)."""
    return mac_from_uid(stm32_uid(w0, w1, w2))


def client_id_from_uid(uid: bytes, prefix: str = "z") -> str:
    """apps/mqtt_tls's derived client id: prefix + lower-case base32hex of the UID, unpadded.

    Only UIDs of up to 12 bytes are encoded as they are; the app condenses longer ones
    (SHA-256 on MCX N), which this function does not model.
    """
    if len(uid) > 12:
        raise ValueError(f"a {len(uid)}-byte UID is condensed by the app; not modelled here")
    return prefix + base64.b32hexencode(uid).decode().rstrip("=").lower()


def stm32_client_id(w0: int, w1: int, w2: int, prefix: str = "z") -> str:
    """The MQTT client id apps/mqtt_tls derives on an STM32 DUT (``z`` + 20 base32 digits)."""
    return client_id_from_uid(stm32_uid(w0, w1, w2), prefix)


def native_uid(device_id: int) -> bytes:
    """native_sim's hwinfo device id: be32 of ``--device_id`` (hwinfo_native.c)."""
    return struct.pack(">I", device_id)


# ---- polarity -----------------------------------------------------------------------------
def channel(board: str, name: str) -> CatalogChannel:
    """The catalog entry of ``name`` on ``board`` (KeyError if there is none)."""
    for chan in CATALOG.get(board, ()):
        if chan.name == name:
            return chan
    raise KeyError(f"{name!r} is not in the {board} I/O catalog")


def drive_level(chan: CatalogChannel, active: bool) -> Literal[0, 1, "z"]:
    """The stimulus level that makes a DUT input read ``active``: the active level, or Hi-Z.

    Inactive is Hi-Z (a dry contact): the DUT's pull (internal or on the board) sets it.
    """
    if chan.kind != "di":
        raise ValueError(f"{chan.name} is a DUT {chan.kind}, the stimulus drives only di channels")
    if not active:
        return "z"
    return 0 if chan.active_low else 1


def logical(chan: CatalogChannel, physical: int) -> int:
    """The logical value (as uc_io and BACnet see it) of a physical wire level."""
    return physical ^ int(chan.active_low)


# ---- bench file ---------------------------------------------------------------------------
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
    uid: str | None = None  # hex, as hwinfo returns it (the MQTT info 'hwid')


@dataclass(frozen=True)
class StimConfig:
    """The stimulus board, its probe (for OpenOCD resets) and the channels wired on this bench."""

    serial: str
    chans: tuple[str, ...]
    baud: int = 115200
    probe: str | None = None


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
class HubConfig:
    """The uhubctl hub and the ports of the DUT's and the stimulus's ST-LINK."""

    location: str
    dut_port: int | None = None
    stim_port: int | None = None


@dataclass(frozen=True)
class Commissioning:
    """DMM readings recorded at commissioning (HIL.md 7.2), checked by PWR-02."""

    v_on_released_v: float | None = None
    v_on_cut_v: float | None = None
    nrst_idle_v: float | None = None


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
    hub: HubConfig | None = None
    commissioning: Commissioning | None = None
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
        top.allow("name", "mode", "dut", "stim", "net", "la", "mstp", "power", "hub", "commissioning")
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
            hub=_hub(top.section("hub")) if "hub" in top else None,
            commissioning=_commissioning(top.section("commissioning")) if "commissioning" in top else None,
            path=path,
        )

    @property
    def catalog(self) -> tuple[CatalogChannel, ...]:
        """The DUT board's full catalog (base and hil-io extras)."""
        return CATALOG.get(self.dut.board, ())


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

    def optional_integer(self, key: str, lo: int, hi: int) -> int | None:
        return self.integer(key, lo, hi) if self.raw(key, False) is not None else None

    def number(self, key: str, lo: float, hi: float) -> float | None:
        value = self.raw(key, False)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
            raise self.fail(key, f"expected a number {lo}..{hi}, got {value!r}")
        return float(value)

    def choice(self, key: str, choices: tuple[Any, ...]) -> Any:
        value = self.raw(key, True)
        if value not in choices:
            raise self.fail(key, f"{value!r} is not one of {', '.join(map(str, choices))}")
        return value


def _required(value: str | None) -> str:
    assert value is not None  # _Section.text(required=True) never returns None
    return value


def _dut(s: _Section, mode: str) -> DutConfig:
    s.allow("profile", "board", "serial", "probe", "mac", "ip", "bacnet_instance", "mqtt_client_id", "uid")
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
                f"{mac!r} is not a MAC address (xx:xx:xx:xx:xx:xx); it keys the dnsmasq "
                "reservation, so compute it from the UID at commissioning (D29, hilrig.bench.stm32_mac)",
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
    uid = s.text("uid", required=False)
    if uid is not None:
        uid = uid.lower()
        if not _UID.match(uid):
            raise s.fail("uid", f"{uid!r}: expected 4-16 bytes in hex, as hwinfo returns the UID")
        if board in STM32_BOARDS:
            _check_stm32_identity(s, bytes.fromhex(uid), mac, client_id)
    return DutConfig(
        profile=profile,
        board=board,
        bacnet_instance=s.integer("bacnet_instance", 0, MAX_INSTANCE),
        serial=s.text("serial", required=False),
        probe=s.text("probe", required=False),
        mac=mac,
        ip=ip,
        mqtt_client_id=client_id,
        uid=uid,
    )


def _check_stm32_identity(s: _Section, uid: bytes, mac: str | None, client_id: str | None) -> None:
    """A bench's MAC and client id must be the ones the firmware derives from its UID (D29)."""
    if len(uid) != 12:
        raise s.fail("uid", f"an STM32 UID has 12 bytes, got {len(uid)}")
    if mac is not None and mac != mac_from_uid(uid):
        raise s.fail("mac", f"{mac} is not the MAC derived from dut.uid ({mac_from_uid(uid)}, D29)")
    derived = client_id_from_uid(uid)
    if client_id is not None and client_id.startswith("z") and client_id != derived:
        raise s.fail("mqtt_client_id", f"{client_id} is not the id derived from dut.uid ({derived})")


def _net(s: _Section) -> NetConfig:
    s.allow("dut_iface")
    return NetConfig(s.text("dut_iface", required=False))


def _power(s: _Section) -> PowerConfig:
    s.allow("method")
    return PowerConfig(s.choice("method", POWER_METHODS))


def _hub(s: _Section) -> HubConfig:
    s.allow("location", "dut_port", "stim_port")
    location = _required(s.text("location"))
    if not _HUB.match(location):
        raise s.fail("location", f"{location!r} is not a uhubctl hub location (e.g. 1-1 or 2-1.4)")
    return HubConfig(location, s.optional_integer("dut_port", 1, 32), s.optional_integer("stim_port", 1, 32))


def _commissioning(s: _Section) -> Commissioning:
    s.allow("v_on_released_v", "v_on_cut_v", "nrst_idle_v")
    return Commissioning(
        s.number("v_on_released_v", 0.0, 6.0),
        s.number("v_on_cut_v", 0.0, 6.0),
        s.number("nrst_idle_v", 0.0, 4.0),
    )


def _stim(s: _Section, board: str) -> StimConfig:
    s.allow("serial", "baud", "chans", "probe")
    chans = s.raw("chans", True)
    if not isinstance(chans, list) or not chans or not all(isinstance(c, str) for c in chans):
        raise s.fail("chans", "expected a non-empty list of channel names")
    catalog = [c.name for c in CATALOG.get(board, ())]
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
        probe=s.text("probe", required=False),
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
