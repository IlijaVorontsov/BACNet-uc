"""Data model of controller.yaml v1 (DESIGN.md §5.1) and the derived values.

The dataclasses mirror site/controller.schema.json one to one. They are
built by config.load() after schema validation and default filling, so every
field is present. Lists become tuples; ``services.hub.overrides`` stays a
dict because it is free-form.
"""

from __future__ import annotations

import dataclasses
import os
import sysconfig
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# controller.yaml


@dataclass(frozen=True)
class SiteInfo:
    id: str
    name: str
    domain: str
    timezone: str


@dataclass(frozen=True)
class Controller:
    hostname: str
    hardware: str               # pi4 | vm
    rtc: str                    # ds3231 | rv3028 | none
    ups_gpio: int | None
    ssd_reserve_percent: int


@dataclass(frozen=True)
class ItNet:
    """IT side: static (address, gateway, dns) or dhcp. vlan only in trunk mode."""
    vlan: int | None = None
    address: str | None = None
    gateway: str | None = None
    dns: tuple[str, ...] = ()
    dhcp: bool = False


@dataclass(frozen=True)
class BmsNet:
    vlan: int | None
    address: str
    nic_mac: str | None


@dataclass(frozen=True)
class Sources:
    admin: tuple[str, ...]
    ui: tuple[str, ...]
    backup: tuple[str, ...]
    bbmd: tuple[str, ...]


@dataclass(frozen=True)
class Network:
    mode: str                   # trunk | dual | flat
    parent: str
    mac: str | None             # "auto", None, or a MAC address
    it: ItNet | None
    bms: BmsNet
    sources: Sources


@dataclass(frozen=True)
class Time:
    servers: tuple[str, ...]
    nts: tuple[str, ...]
    serve: bool


@dataclass(frozen=True)
class Dhcp:
    enabled: bool
    range: tuple[str, ...]
    lease: str


@dataclass(frozen=True)
class Syslog:
    enabled: bool


@dataclass(frozen=True)
class Mqtt:
    topic_root: str
    extra_roots: tuple[str, ...]
    broker_name: str | None
    expected_devices: int | None
    third_party_listener: bool


@dataclass(frozen=True)
class History:
    expected_points: int
    raw_days: int
    log_days: int
    event_days: int
    budget_percent: int


@dataclass(frozen=True)
class HubUser:
    user: str
    roles: tuple[str, ...]


@dataclass(frozen=True)
class Llm:
    provider: str               # none | zai


@dataclass(frozen=True)
class Hub:
    enabled: bool
    users: tuple[HubUser, ...]
    llm: Llm
    manifest: str | None
    overrides: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Ui:
    names: tuple[str, ...]
    cert: str                   # site | provided


@dataclass(frozen=True)
class Updates:
    online: bool
    auto_security: bool


@dataclass(frozen=True)
class Services:
    dhcp: Dhcp
    syslog: Syslog
    mqtt: Mqtt
    history: History
    hub: Hub
    ui: Ui
    updates: Updates


@dataclass(frozen=True)
class BackupTarget:
    type: str                   # sftp | usb
    url: str | None = None      # sftp
    label: str | None = None    # usb


@dataclass(frozen=True)
class Backup:
    recipients: tuple[str, ...]
    targets: tuple[BackupTarget, ...]
    history_repo: str | None
    at: str


@dataclass(frozen=True)
class Admin:
    name: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class Site:
    version: int
    site: SiteInfo
    controller: Controller
    network: Network
    time: Time
    services: Services
    backup: Backup
    admins: tuple[Admin, ...]
    ssh_user_ca: tuple[str, ...]
    source: str = ""            # file the site was loaded from (not part of the schema)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("source", None)
        return d


def build(cls, data):
    """Build dataclass ``cls`` from a (default-filled) dict, recursively."""
    return _build(cls, data)


def _build(tp, value):
    if dataclasses.is_dataclass(tp):
        if value is None:
            return None
        hints = typing.get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name in value:
                kwargs[f.name] = _build(hints[f.name], value[f.name])
        return tp(**kwargs)
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        if value is None:
            return None
        inner = [a for a in typing.get_args(tp) if a is not type(None)]
        return _build(inner[0], value)
    if origin is tuple:
        (item,) = typing.get_args(tp)[:1]
        return tuple(_build(item, v) for v in (value or ()))
    if tp is dict or origin is dict:
        return dict(value or {})
    return value


# ---------------------------------------------------------------------------
# Derived values (§5.1 table)


@dataclass(frozen=True)
class Derived:
    # BMS network
    bms_ip: str                 # 10.20.0.2
    bms_prefix: int             # 24
    bms_net: str                # 10.20.0.0/24
    bms_bcast: str              # 10.20.0.255
    bms_netmask: str            # 255.255.255.0
    bms_cidr: str               # 10.20.0.2/24
    # IT network (None/empty in flat mode)
    it_address: str | None      # 192.0.2.20/24 (static only)
    it_ip: str | None
    it_net: str | None
    it_gateway: str | None
    it_dns: tuple[str, ...]
    it_dhcp: bool
    # interfaces by mode
    parent: str                 # onboard NIC kernel name (eth0)
    it_if: str | None           # it0, or None in flat mode
    bms_if: str                 # bms0
    it_vlan: int | None         # trunk only
    bms_vlan: int | None        # trunk only
    bms_nic_mac: str | None     # dual only
    pinned_mac: str | None      # MAC pinned on the onboard NIC, if any
    zones_on_bms: tuple[str, ...]   # nft zone chains bms0 jumps to (flat: it and bms)
    # broker
    broker_name: str
    broker_sans: tuple[str, ...]
    # hub
    hub_bacnet_ip_interface: str
    hub_discover_broadcast: tuple[str, ...]
    hub_mqtt: dict
    # services
    chrony_allow: str
    dhcp_options: dict
    fqdn: str
    expected_devices: int
    topic_roots: tuple[str, ...]


# ---------------------------------------------------------------------------
# Facts about the machine (faked in tests, see testing.FakeFacts)


@dataclass(frozen=True)
class Facts:
    multiarch: str                      # aarch64-linux-gnu
    mosquitto: str | None               # upstream version, e.g. "2.1.2"; None if not installed
    pg_major: int | None                # 18
    rtc: bool                           # /dev/rtc0 present
    container: bool                     # running in a container
    root_partuuid: str | None = None    # PARTUUID of / (e.g. "4f1c2a3b-02"), None if unknown
    # Paths that exist on the target. None: look at the real file system.
    paths_present: frozenset | None = None

    def exists(self, path: str) -> bool:
        """Does ``path`` (a system path) exist on the target?"""
        if self.paths_present is not None:
            return path in self.paths_present
        from . import paths as _paths
        return os.path.exists(_paths.p(path))

    @classmethod
    def detect(cls, runner=None) -> "Facts":
        from . import paths as _paths
        from .runner import Runner
        runner = runner or Runner()
        multiarch = sysconfig.get_config_var("MULTIARCH") or _multiarch_from_uname()
        mosq = None
        res = runner.run(["dpkg-query", "-W", "-f=${Version}", "mosquitto"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            # "2.1.2-1~trixie" -> "2.1.2"; strip an epoch too
            mosq = res.stdout.strip().split(":")[-1].split("-")[0]
        pg_major = None
        pg_root = Path(_paths.p("/usr/lib/postgresql"))
        if pg_root.is_dir():
            majors = [int(d.name) for d in pg_root.iterdir() if d.name.isdigit()]
            pg_major = max(majors) if majors else None
        partuuid = None
        res = runner.run(["findmnt", "-no", "PARTUUID", "/"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            partuuid = res.stdout.strip()
        return cls(
            multiarch=multiarch,
            mosquitto=mosq,
            pg_major=pg_major,
            rtc=os.path.exists(_paths.p(_paths.DEV_RTC)),
            container=_in_container(),
            root_partuuid=partuuid,
        )


def _multiarch_from_uname() -> str:
    machine = os.uname().machine
    return {"aarch64": "aarch64-linux-gnu", "x86_64": "x86_64-linux-gnu",
            "armv7l": "arm-linux-gnueabihf"}.get(machine, f"{machine}-linux-gnu")


def _in_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/run/systemd/container") as fh:
            return bool(fh.read().strip())
    except OSError:
        pass
    try:
        with open("/proc/1/environ", "rb") as fh:
            return b"container=" in fh.read()
    except OSError:
        return False
