"""Per-node documents derived from the manifest, and their semantic forms.

A BACnet-uc node is configured through three files (``/lfs/cfg/device.json``,
``io.json``, ``apps.json``, see ``third_party/management-protocol.md``). The
builders here turn a ``system.nodes[]`` entry, ``system.apps`` and
``system.links`` into those documents and ``uc_app install`` manifests. The
``normalize_*`` functions give the form in which a desired and a live
document are compared: firmware defaults filled in and order-insensitive
lists sorted, so a plan never shows a change that would not change the node.
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from typing import Any

from ..core.ids import OBJECT_TYPES, PointRef

CFG_DEVICE = "/lfs/cfg/device.json"
CFG_IO = "/lfs/cfg/io.json"
CFG_APPS = "/lfs/cfg/apps.json"
APPS_DIR = "/lfs/apps"
UC_LINK_FILE = f"{APPS_DIR}/uc-link.wasm"

#: The stock uc-link app handles at most this many links per instance.
LINKS_PER_APP = 8
#: apps.json holds at most this many apps (apps.schema.json maxItems).
MAX_APPS_PER_NODE = 8

APP_DEFAULTS: dict[str, Any] = {"autostart": True, "period_ms": 1000, "heap_kb": 8, "stack_kb": 4}
LINK_DEFAULTS: dict[str, Any] = {
    "mode": "cov", "period_ms": 1000, "priority": 0, "scale": 1.0, "offset": 0.0,
}
_IO_POINT_DEFAULTS: dict[str, Any] = {
    "scale": 1.0, "offset": 0.0, "cov_increment": 0.1, "sample_ms": 100,
    "invert": False, "debounce_ms": 20,
}
_DEVICE_SECTION_DEFAULTS: dict[str, dict[str, Any]] = {
    "network": {"dhcp": True},
    "bacnet": {"udp_port": 47808, "apdu_timeout_ms": 3000, "apdu_retries": 3},
    "log": {"level": "inf"},
}
#: device.json sections the manifest may set; absent ones are left as the node has them.
MANAGED_OPTIONAL_SECTIONS = ("network", "bacnet")
#: Optional device.json sections (device.schema.json) carried over from the node.
KEPT_SECTIONS = ("network", "bacnet", "log")

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


# -- desired documents ----------------------------------------------------------------
def device_doc(node: dict[str, Any]) -> dict[str, Any]:
    """``/lfs/cfg/device.json`` for a ``system.nodes[]`` entry."""
    doc: dict[str, Any] = {"schema": 1, "device": copy.deepcopy(node["device"])}
    for key in MANAGED_OPTIONAL_SECTIONS:
        if key in node:
            doc[key] = copy.deepcopy(node[key])
    return doc


def io_doc(node: dict[str, Any]) -> dict[str, Any]:
    """``/lfs/cfg/io.json`` for a ``system.nodes[]`` entry."""
    return {"schema": 1, "points": copy.deepcopy(node.get("io") or [])}


def merge_device_doc(desired: dict[str, Any], live: dict[str, Any] | None) -> dict[str, Any]:
    """The device.json to upload: the manifest's sections, plus every section of
    the live document the manifest does not manage (``log``, and ``network`` /
    ``bacnet`` when the node entry omits them). Omitting ``network`` therefore
    never switches a statically addressed node to DHCP by accident. Anything
    else on the node (unknown keys, sections that are not objects) is dropped,
    so the upload always matches device.schema.json."""
    out = copy.deepcopy(desired)
    for key in KEPT_SECTIONS:
        value = (live or {}).get(key)
        if key not in out and isinstance(value, dict):
            out[key] = copy.deepcopy(value)
    return out


# -- apps -----------------------------------------------------------------------
@dataclass(slots=True)
class DesiredApp:
    """One application the manifest wants on a node."""

    name: str
    node: str
    #: ``uc_app install`` manifest without ``sha256`` (params as a str -> str map).
    manifest: dict[str, Any]
    #: Prebuilt module relative to the manifest directory.
    wasm: str | None = None
    #: C source relative to the manifest directory (not buildable yet).
    source: str | None = None
    #: A generated instance of the stock uc-link app.
    link: bool = False
    #: Index in ``system.apps`` (None for generated link apps).
    index: int | None = None
    #: ``system.links`` indexes realised by a generated link app.
    link_indexes: list[int] = field(default_factory=list)

    @property
    def file(self) -> str:
        return str(self.manifest["file"])


def param_text(value: Any) -> str:
    """App params are strings on the node (``uc_param_get``)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return number_text(value)
    return str(value)


def number_text(value: float | int) -> str:
    """Shortest text that ``strtod`` reads back as the same number."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if math.isfinite(value) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def resolve_placeholders(text: str, system: dict[str, Any], site: dict[str, Any]) -> str:
    """Expand ``{{ nodes.<name>.<path> }}`` and ``{{ external_devices.<name>.<path> }}``.

    ``system.schema.json`` allows these in string values so an app can name
    another node's device instance without repeating it. Raises ``KeyError``
    with a readable message for an unknown reference.
    """

    def repl(m: re.Match[str]) -> str:
        expr = m.group(1)
        parts = expr.split(".")
        if len(parts) < 3 or parts[0] not in ("nodes", "external_devices"):
            raise KeyError(f"unknown placeholder {{{{ {expr} }}}}")
        items = system.get("nodes") if parts[0] == "nodes" else site.get("external_devices")
        found: Any = next((d for d in items or [] if d.get("name") == parts[1]), None)
        if found is None:
            raise KeyError(f"placeholder {{{{ {expr} }}}}: no {parts[0][:-1].replace('_', ' ')} {parts[1]!r}")
        for key in parts[2:]:
            if isinstance(found, dict) and key in found:
                found = found[key]
            elif isinstance(found, list) and key.isdigit() and int(key) < len(found):
                found = found[int(key)]
            else:
                raise KeyError(f"placeholder {{{{ {expr} }}}}: {key!r} not found")
        if isinstance(found, (dict, list)) or found is None:
            raise KeyError(f"placeholder {{{{ {expr} }}}} is not a scalar")
        return param_text(found)

    return _PLACEHOLDER_RE.sub(repl, text)


def app_file(app: dict[str, Any]) -> str:
    ext = "aot" if app.get("aot") else "wasm"
    return f"{APPS_DIR}/{app['name']}.{ext}"


def user_app(app: dict[str, Any], index: int, system: dict[str, Any],
             site: dict[str, Any]) -> DesiredApp:
    """A ``system.apps[]`` entry as a DesiredApp (placeholders expanded)."""
    manifest: dict[str, Any] = {"name": app["name"], "file": app_file(app)}
    for key, default in APP_DEFAULTS.items():
        manifest[key] = app.get(key, default)
    manifest["perms"] = list(dict.fromkeys(app.get("perms") or []))
    manifest["params"] = {
        str(k): resolve_placeholders(param_text(v), system, site) if isinstance(v, str) else param_text(v)
        for k, v in (app.get("params") or {}).items()
    }
    return DesiredApp(
        name=app["name"], node=app["node"], manifest=manifest,
        wasm=app.get("wasm"), source=app.get("source"), index=index,
    )


@dataclass(slots=True)
class Link:
    """A ``system.links[]`` entry with defaults applied."""

    index: int
    source: PointRef
    dest: PointRef
    mode: str
    period_ms: int
    priority: int
    scale: float
    offset: float

    @classmethod
    def from_entry(cls, index: int, entry: dict[str, Any], site: str) -> Link:
        values = {k: entry.get(k, d) for k, d in LINK_DEFAULTS.items()}
        return cls(
            index=index,
            source=PointRef.parse(entry["from"], default_site=site),
            dest=PointRef.parse(entry["to"], default_site=site),
            mode=str(values["mode"]),
            period_ms=int(values["period_ms"]),
            priority=int(values["priority"]),
            scale=float(values["scale"]),
            offset=float(values["offset"]),
        )

    def uc_link_param(self, src_device: int) -> str:
        """One ``l<i>`` parameter: ``"<src_device> <src_type> <src_instance>
        <dst_type> <dst_instance> <mode> <period_ms> <priority> <scale> <offset>"``
        with numeric BACnet object types."""
        src, dst = self.source.bacnet, self.dest.bacnet
        if src is None or dst is None:
            raise ValueError(f"link {self.source} -> {self.dest} is not between BACnet objects")
        return " ".join((
            str(src_device), str(OBJECT_TYPES[src[0]]), str(src[1]),
            str(OBJECT_TYPES[dst[0]]), str(dst[1]), self.mode, str(self.period_ms),
            str(self.priority), number_text(self.scale), number_text(self.offset),
        ))

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": str(self.source), "to": str(self.dest), "mode": self.mode,
            "period_ms": self.period_ms, "priority": self.priority,
            "scale": self.scale, "offset": self.offset,
        }


def link_apps(node: str, local_instance: int | None, links: list[Link],
              instances: dict[str, int | None]) -> list[DesiredApp]:
    """uc-link instances for the links whose destination is ``node``: one app
    ``link``, or ``link-1``, ``link-2``, ... when there are more than
    LINKS_PER_APP. ``instances`` maps device names to BACnet device instances
    (bacnet-uc nodes and bacnet-ip devices)."""
    mine = [lk for lk in links if lk.dest.device == node]
    if not mine:
        return []
    chunks = [mine[i:i + LINKS_PER_APP] for i in range(0, len(mine), LINKS_PER_APP)]
    apps: list[DesiredApp] = []
    for n, chunk in enumerate(chunks, start=1):
        name = "link" if len(chunks) == 1 else f"link-{n}"
        params: dict[str, str] = {"count": str(len(chunk))}
        remote = False
        for i, lk in enumerate(chunk):
            src_device = instances.get(lk.source.device)
            if src_device is None:
                raise ValueError(f"link source device {lk.source.device} has no BACnet device instance")
            remote = remote or src_device != local_instance
            params[f"l{i}"] = lk.uc_link_param(src_device)
        manifest: dict[str, Any] = {
            "name": name, "file": UC_LINK_FILE, **APP_DEFAULTS,
            "period_ms": max(100, min(lk.period_ms for lk in chunk)),
            "perms": ["bacnet.local", "bacnet.remote"] if remote else ["bacnet.local"],
            "params": params,
        }
        apps.append(DesiredApp(
            name=name, node=node, manifest=manifest, link=True,
            link_indexes=[lk.index for lk in chunk],
        ))
    return apps


def apps_entry(app: DesiredApp, sha256_hex: str | None = None) -> dict[str, Any]:
    """The ``apps.json`` entry the firmware will store for ``app``."""
    m = app.manifest
    entry: dict[str, Any] = {k: copy.deepcopy(m[k]) for k in ("name", "file", *APP_DEFAULTS, "perms")}
    entry["params"] = [{"key": k, "value": v} for k, v in m["params"].items()]
    if sha256_hex is not None:
        entry["sha256"] = sha256_hex
    return entry


# -- semantic forms -----------------------------------------------------------
def normalize_device(doc: dict[str, Any] | None) -> dict[str, Any]:
    """device.json with firmware defaults filled in (absent optional sections too)."""
    out = copy.deepcopy(doc) if isinstance(doc, dict) else {}
    for section, defaults in _DEVICE_SECTION_DEFAULTS.items():
        value = out.get(section)
        if value is None:
            value = {}
        if isinstance(value, dict):
            value = {**defaults, **value}
            if not value.get("static_bindings"):
                value.pop("static_bindings", None)
            out[section] = value
    return out


def normalize_io_point(point: dict[str, Any]) -> dict[str, Any]:
    out = {**_IO_POINT_DEFAULTS, **point}
    out.setdefault("name", point.get("channel", ""))
    return out


def normalize_io(doc: dict[str, Any] | None) -> dict[str, Any]:
    """io.json with point defaults filled in, points sorted by channel. Node
    documents are untrusted: odd values sort as text instead of failing."""
    points = (doc or {}).get("points") if isinstance(doc, dict) else None
    items = [normalize_io_point(p) for p in points if isinstance(p, dict)] if isinstance(points, list) else []
    items.sort(key=lambda p: (str(p.get("channel", "")), str(p.get("type", "")), str(p.get("instance", ""))))
    return {"schema": (doc or {}).get("schema", 1) if isinstance(doc, dict) else 1, "points": items}


def normalize_app(entry: dict[str, Any]) -> dict[str, Any]:
    """An app in comparable form, from an install manifest, an apps.json entry
    (params as a key/value list) or an ``<app status>`` map. Malformed
    ``params``/``perms`` from a node count as empty, so they show up as a
    difference instead of breaking the plan."""
    params = entry.get("params")
    if isinstance(params, list):
        params = {str(p.get("key")): str(p.get("value")) for p in params if isinstance(p, dict)}
    elif not isinstance(params, dict):
        params = {}
    perms = entry.get("perms")
    sha = entry.get("sha256")
    if isinstance(sha, (bytes, bytearray)):
        sha = bytes(sha).hex()
    out: dict[str, Any] = {"name": entry.get("name"), "file": entry.get("file")}
    for key, default in APP_DEFAULTS.items():
        out[key] = entry.get(key, default)
    out["perms"] = sorted({str(p) for p in perms}) if isinstance(perms, list) else []
    out["params"] = {str(k): str(v) for k, v in params.items()}
    out["sha256"] = sha.lower() if isinstance(sha, str) else None
    return out


def io_point_key(point: dict[str, Any]) -> str:
    return str(point.get("channel", ""))


def changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Dotted paths whose values differ (lists compared as a whole)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                out.append(path)
            else:
                out.extend(changed_paths(before[key], after[key], path))
        return out
    return [] if before == after else [prefix or "<root>"]
