"""Loading and checking controller.yaml (DESIGN.md §5.1, §5.4 step 1).

``load(path)`` reads the YAML file, fills schema defaults, validates it
against site/controller.schema.json (Draft 2020-12), builds the frozen
dataclasses of model.py and runs the semantic checks. Every problem is
reported at once as ``ConfigError(['json.path: message', ...])``.

``derive(site, devices, board)`` computes the §5.1 table of derived values.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import ipaddress
import json
import logging
import os
from typing import Any, Iterable

import yaml

from . import model
from . import paths as _paths

log = logging.getLogger(__name__)

RESERVED_USERS = frozenset("""
root daemon bin sys sync games man lp mail news uucp proxy www-data backup list irc
gnats nobody _apt sshd messagebus systemd-network systemd-resolve systemd-timesync
systemd-journal-remote polkitd avahi dnsmasq mosquitto postgres _chrony nginx
uc-hub uc-historian uc-health ssh-admins sudo admin
""".split())


class ConfigError(Exception):
    """controller.yaml (or a derived input) is invalid. ``errors`` lists
    'json.path: message' strings. Commands exit with 2 on it."""

    def __init__(self, errors: Iterable[str] | str):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


# ---------------------------------------------------------------------------
# YAML


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys (PyYAML keeps the last)."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def read_yaml(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.load(fh, Loader=_UniqueKeyLoader)  # noqa: S506 (safe loader subclass)
    except FileNotFoundError:
        raise ConfigError(f"{path}: file not found") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from None


# ---------------------------------------------------------------------------
# Schema: defaults and validation


_schema_cache: dict[str, dict] = {}


def schema() -> dict:
    path = str(_paths.schema_path())
    if path not in _schema_cache:
        with open(path, encoding="utf-8") as fh:
            _schema_cache[path] = json.load(fh)
    return _schema_cache[path]


def _resolve(sub: dict, root: dict) -> dict:
    """Follow local $ref chains ("#/$defs/name"), merging sibling keywords."""
    seen = 0
    while isinstance(sub, dict) and "$ref" in sub and seen < 20:
        ref = sub["$ref"]
        if not ref.startswith("#/"):
            break
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part]
        merged = dict(target)
        merged.update({k: v for k, v in sub.items() if k != "$ref"})
        sub = merged
        seen += 1
    return sub


_MISSING = object()


def _default_of(sub: dict, root: dict):
    if "default" in sub:
        return sub["default"]
    resolved = _resolve(sub, root)
    return resolved.get("default", _MISSING)


def _branch_valid(branch: dict, instance, root: dict) -> bool:
    from jsonschema import Draft202012Validator
    standalone = dict(branch)
    standalone["$defs"] = root.get("$defs", {})
    return Draft202012Validator(standalone).is_valid(instance)


def fill_defaults(instance, sub: dict, root: dict):
    """Insert schema defaults into instance (in place) and return it.

    Tolerant of wrong types: it only descends into dicts and lists, so the
    validator reports type errors afterwards. For oneOf/anyOf it uses the
    first branch the instance already satisfies.
    """
    sub = _resolve(sub, root)
    if isinstance(instance, dict):
        for key, prop in sub.get("properties", {}).items():
            if key not in instance:
                d = _default_of(prop, root)
                if d is not _MISSING:
                    instance[key] = copy.deepcopy(d)
            if key in instance:
                fill_defaults(instance[key], prop, root)
    elif isinstance(instance, list) and isinstance(sub.get("items"), dict):
        for item in instance:
            fill_defaults(item, sub["items"], root)
    for kw in ("oneOf", "anyOf"):
        for branch in sub.get(kw, ()):
            if _branch_valid(branch, instance, root):
                fill_defaults(instance, branch, root)
                break
    return instance


def _json_path(parts) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out or "(top level)"


def schema_errors(data) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import best_match

    validator = Draft202012Validator(schema())
    errors = []
    for err in sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.absolute_path]):
        if err.context:  # oneOf/anyOf: report the most specific sub-error
            inner = best_match(err.context)
            path = _json_path(list(err.absolute_path) + list(inner.relative_path))
            msg = inner.message
        else:
            path = _json_path(err.absolute_path)
            msg = err.message
        if err.validator == "type" and isinstance(err.instance, int) and "string" in str(err.validator_value):
            msg += " (quote the value in YAML)"
        errors.append(f"{path}: {msg}")
    return errors


# ---------------------------------------------------------------------------
# age recipients (bech32, BIP-173)

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def bech32_decode(s: str) -> tuple[str, bytes] | None:
    """Decode a bech32 string to (hrp, 8-bit data), or None if invalid."""
    if s.lower() != s and s.upper() != s:
        return None
    s = s.lower()
    pos = s.rfind("1")
    if pos < 1 or pos + 7 > len(s):
        return None
    hrp, rest = s[:pos], s[pos + 1:]
    try:
        values = [_BECH32.index(c) for c in rest]
    except ValueError:
        return None
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    if _bech32_polymod(expanded + values) != 1:
        return None
    acc = bits = 0
    out = bytearray()
    for v in values[:-6]:
        acc = (acc << 5) | v
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        return None
    return hrp, bytes(out)


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode 8-bit data as bech32 (used to make test recipients)."""
    acc = bits = 0
    values = []
    for b in data:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            values.append((acc >> bits) & 31)
    if bits:
        values.append((acc << (5 - bits)) & 31)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    poly = _bech32_polymod(expanded + values + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[v] for v in values + checksum)


def is_age_recipient(s: str) -> bool:
    """X25519 age recipient (age1...) or an SSH public key age accepts."""
    if s.startswith("age1"):
        dec = bech32_decode(s)
        return dec is not None and dec[0] == "age" and len(dec[1]) == 32
    parts = s.split()
    return len(parts) >= 2 and parts[0] in ("ssh-ed25519", "ssh-rsa") and len(parts[1]) > 60


# ---------------------------------------------------------------------------
# Semantic checks


def _walk_strings(obj, path=()):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, path + (i,))


def _host_iface(value: str, where: str, errors: list) -> ipaddress.IPv4Interface | None:
    try:
        iface = ipaddress.IPv4Interface(value)
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
        return None
    net = iface.network
    if net.prefixlen <= 30 and iface.ip in (net.network_address, net.broadcast_address):
        errors.append(f"{where}: {value} is the network or broadcast address of {net}")
    if net.prefixlen > 30:
        errors.append(f"{where}: prefix /{net.prefixlen} is too small for a host network")
    return iface


def semantic_errors(site: model.Site, base_dir: str = ".") -> list[str]:
    errors: list[str] = []
    net = site.network

    for path, value in _walk_strings(site.to_dict()):
        if "\n" in value or "\r" in value or "\0" in value:
            errors.append(f"{_json_path(path)}: control characters are not allowed")

    # -- mode-specific interfaces ------------------------------------------------
    it = net.it
    if net.mode in ("trunk", "dual") and it is None:
        errors.append(f"network.it: required in {net.mode} mode")
    if net.mode == "flat" and it is not None:
        errors.append("network.it: must be null in flat mode (IT access uses network.sources on bms0)")
    if net.mode == "trunk":
        if it is not None and it.vlan is None:
            errors.append("network.it.vlan: required in trunk mode")
        if net.bms.vlan is None:
            errors.append("network.bms.vlan: required in trunk mode")
        if it is not None and it.vlan is not None and it.vlan == net.bms.vlan:
            errors.append(f"network.bms.vlan: VLAN {net.bms.vlan} is also used by network.it.vlan")
    else:
        if it is not None and it.vlan is not None:
            errors.append(f"network.it.vlan: only used in trunk mode, not in {net.mode} mode")
        if net.bms.vlan is not None:
            errors.append(f"network.bms.vlan: only used in trunk mode, not in {net.mode} mode")
    if net.mode == "dual":
        if not net.bms.nic_mac:
            errors.append("network.bms.nic_mac: required in dual mode (MAC of the USB NIC)")
        elif net.mac not in (None, "auto") and net.mac.lower() == net.bms.nic_mac.lower():
            errors.append("network.bms.nic_mac: equals network.mac")
    elif net.bms.nic_mac:
        errors.append(f"network.bms.nic_mac: dual mode only, not {net.mode} mode")

    # -- addresses ------------------------------------------------------------------
    bms = _host_iface(net.bms.address, "network.bms.address", errors)
    it_iface = None
    if it is not None and not it.dhcp and it.address:
        it_iface = _host_iface(it.address, "network.it.address", errors)
        if it_iface and it.gateway:
            gw = ipaddress.IPv4Address(it.gateway)
            if gw not in it_iface.network:
                errors.append(f"network.it.gateway: {gw} is not inside {it_iface.network}")
            elif gw == it_iface.ip:
                errors.append("network.it.gateway: equals network.it.address")
    if bms and it_iface and bms.network.overlaps(it_iface.network):
        errors.append(f"network.bms.address: {bms.network} overlaps the IT network {it_iface.network}")

    for kind in ("admin", "ui", "backup", "bbmd"):
        for i, src in enumerate(getattr(net.sources, kind)):
            try:
                ipaddress.IPv4Network(src, strict=True)
            except ValueError as exc:
                errors.append(f"network.sources.{kind}[{i}]: {exc}")

    # -- DHCP --------------------------------------------------------------------
    dhcp = site.services.dhcp
    if len(dhcp.range) not in (0, 2):
        errors.append("services.dhcp.range: needs exactly two addresses [first, last]")
    elif dhcp.enabled and len(dhcp.range) != 2:
        errors.append("services.dhcp.range: required when services.dhcp.enabled")
    if len(dhcp.range) == 2 and bms:
        first, last = (ipaddress.IPv4Address(a) for a in dhcp.range)
        for i, a in enumerate((first, last)):
            if a not in bms.network:
                errors.append(f"services.dhcp.range[{i}]: {a} is not inside {bms.network}")
            elif a in (bms.network.network_address, bms.network.broadcast_address):
                errors.append(f"services.dhcp.range[{i}]: {a} is the network or broadcast address")
        if first > last:
            errors.append("services.dhcp.range: first address is after the last one")
        elif first <= bms.ip <= last:
            errors.append(f"network.bms.address: {bms.ip} is inside the DHCP range {first}-{last}")

    # -- backup ----------------------------------------------------------------------
    for i, rec in enumerate(site.backup.recipients):
        if not is_age_recipient(rec):
            errors.append(f"backup.recipients[{i}]: not a valid age recipient (age1... or ssh-ed25519/ssh-rsa key)")

    # -- hub ---------------------------------------------------------------------------
    hub = site.services.hub
    if hub.enabled and not hub.users:
        errors.append("services.hub.users: must not be empty while the hub is enabled "
                      "(without tokens the hub runs in dev mode)")
    seen = set()
    for i, u in enumerate(hub.users):
        if u.user in seen:
            errors.append(f"services.hub.users[{i}].user: duplicate user {u.user!r}")
        seen.add(u.user)
    if hub.manifest:
        mpath = hub.manifest if os.path.isabs(hub.manifest) else os.path.join(base_dir, hub.manifest)
        try:
            manifest = read_yaml(mpath)
        except ConfigError as exc:
            errors.append(f"services.hub.manifest: {exc}")
        else:
            name = (manifest or {}).get("metadata", {}).get("name") if isinstance(manifest, dict) else None
            if name != site.site.id:
                errors.append(f"services.hub.manifest: metadata.name {name!r} differs from site.id {site.site.id!r}")
    roots = (site.services.mqtt.topic_root, *site.services.mqtt.extra_roots)
    if len(set(roots)) != len(roots):
        errors.append("services.mqtt.extra_roots: repeats topic_root")

    # -- admins ----------------------------------------------------------------------
    seen = set()
    for i, a in enumerate(site.admins):
        if a.name in seen:
            errors.append(f"admins[{i}].name: duplicate admin {a.name!r}")
        if a.name in RESERVED_USERS or a.name.startswith("systemd-"):
            errors.append(f"admins[{i}].name: {a.name!r} is a reserved account name")
        seen.add(a.name)

    if site.controller.ups_gpio in (2, 3):
        errors.append("controller.ups_gpio: GPIO2/3 carry the RTC's I2C bus")
    return errors


# ---------------------------------------------------------------------------
# Public API


def _normalise(data: dict) -> None:
    """Lower-case MAC addresses (the schema accepts either case)."""
    net = data.get("network")
    if isinstance(net, dict):
        if isinstance(net.get("mac"), str) and net["mac"] != "auto":
            net["mac"] = net["mac"].lower()
        bms = net.get("bms")
        if isinstance(bms, dict) and isinstance(bms.get("nic_mac"), str):
            bms["nic_mac"] = bms["nic_mac"].lower()


def load_dict(data: Any, source: str = "<dict>", base_dir: str = ".") -> model.Site:
    """Validate an already parsed controller.yaml document."""
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: the top level must be a mapping")
    data = copy.deepcopy(data)
    root = schema()
    fill_defaults(data, root, root)
    errors = schema_errors(data)
    if errors:
        raise ConfigError(errors)
    _normalise(data)
    site = dataclasses.replace(model.build(model.Site, data), source=source)
    errors = semantic_errors(site, base_dir)
    if errors:
        raise ConfigError(errors)
    return site


def load(path: str) -> model.Site:
    """Load and fully check controller.yaml."""
    data = read_yaml(path)
    return load_dict(data, source=str(path), base_dir=os.path.dirname(os.path.abspath(path)))


def load_devices() -> list:
    """devices.yaml through uc_controller.devices (WP2); [] while it does not exist."""
    modname = f"{__package__}.devices"
    try:
        devices = importlib.import_module(modname)
    except ImportError as exc:
        if exc.name != modname:
            log.warning("devices module failed to import: %s", exc)
        return []
    try:
        return list(devices.load())
    except FileNotFoundError:
        return []
    except ConfigError:
        raise
    except Exception as exc:  # a broken devices.yaml must refuse the apply
        raise ConfigError(f"devices.yaml: {exc}") from exc


def load_board(p: _paths.Paths | None = None) -> dict:
    """board.json (WP6) or {}."""
    path = (p or _paths.current()).board_json
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _dev_attr(dev, name):
    if isinstance(dev, dict):
        return dev.get(name)
    return getattr(dev, name, None)


def active_devices(devices) -> list:
    return [d for d in devices if _dev_attr(d, "status") in (None, "active")]


def derive(site: model.Site, devices=(), board: dict | None = None) -> model.Derived:
    """The §5.1 table of derived values."""
    net = site.network
    bms = ipaddress.IPv4Interface(net.bms.address)
    bnet = bms.network
    it = net.it
    it_iface = ipaddress.IPv4Interface(it.address) if it is not None and it.address and not it.dhcp else None

    if net.mac is None:
        pinned = None
    elif net.mac == "auto":
        pinned = (board or {}).get("pinned_mac") or None
        pinned = pinned.lower() if isinstance(pinned, str) else None
    else:
        pinned = net.mac.lower()

    broker = site.services.mqtt.broker_name or f"mqtt.{site.site.domain}"
    sans = []
    for s in (broker, site.controller.hostname, str(bms.ip)):
        if s not in sans:
            sans.append(s)
    expected = site.services.mqtt.expected_devices
    if expected is None:
        expected = len(active_devices(devices))

    return model.Derived(
        bms_ip=str(bms.ip),
        bms_prefix=bnet.prefixlen,
        bms_net=str(bnet),
        bms_bcast=str(bnet.broadcast_address),
        bms_netmask=str(bnet.netmask),
        bms_cidr=f"{bms.ip}/{bnet.prefixlen}",
        it_address=str(it_iface) if it_iface else None,
        it_ip=str(it_iface.ip) if it_iface else None,
        it_net=str(it_iface.network) if it_iface else None,
        it_gateway=it.gateway if it is not None and not it.dhcp else None,
        it_dns=tuple(it.dns) if it is not None and not it.dhcp else (),
        it_dhcp=bool(it is not None and it.dhcp),
        parent=net.parent,
        it_if=None if net.mode == "flat" else "it0",
        bms_if="bms0",
        it_vlan=it.vlan if net.mode == "trunk" and it is not None else None,
        bms_vlan=net.bms.vlan if net.mode == "trunk" else None,
        bms_nic_mac=net.bms.nic_mac if net.mode == "dual" else None,
        pinned_mac=pinned,
        zones_on_bms=("it", "bms") if net.mode == "flat" else ("bms",),
        broker_name=broker,
        broker_sans=tuple(sans),
        hub_bacnet_ip_interface=f"{bms.ip}/{bnet.prefixlen}",
        hub_discover_broadcast=(f"{bnet.broadcast_address}:1337",),
        hub_mqtt={"host": "127.0.0.1", "port": 1883, "username": "uc-hub"},
        chrony_allow=str(bnet),
        dhcp_options={"dns-server": str(bms.ip), "log-server": str(bms.ip), "ntp-server": str(bms.ip)},
        fqdn=f"{site.controller.hostname}.{site.site.domain}",
        expected_devices=expected,
        topic_roots=(site.services.mqtt.topic_root, *site.services.mqtt.extra_roots),
    )
