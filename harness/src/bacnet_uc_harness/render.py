# SPDX-License-Identifier: Apache-2.0
"""Render the per-node configuration documents of a system manifest.

For every node the renderer produces ``device.json``, ``io.json`` and
``apps.json`` exactly as defined by ``schemas/*.schema.json`` (and validates
them):

- ``device.json``: identity, network and BACnet options of the node plus
  ``bacnet.static_bindings`` for the other nodes of the system (entries
  written in the manifest take precedence; nodes the node talks to through
  links or app parameters come first; at most 16).
- ``io.json``: the node's IO points.
- ``apps.json``: the manifest's apps on the node and one ``uc-link``
  instance per 8 links that target the node (``link``, or ``link-1``,
  ``link-2``, ... when more than 8 links target it).

Addresses: a node's BACnet/IP address is ``bacnet_address`` if given, else
its static ``network.ipv4``, else the UDP transport host, else the address
the simulation allocated (``addresses`` argument or
:func:`sim_address_plan`).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bacnet_uc_harness import paths
from bacnet_uc_harness.bacnet import enums
from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.manifest import (
    APP_KINDS,
    LINKS_PER_APP,
    AppSpec,
    Issue,
    LinkSpec,
    ManifestError,
    NodeSpec,
    System,
    _stringify,
    is_remote_device,
    validate_document,
)

DOC_NAMES = ("device", "io", "apps")
CFG_DIR = "/lfs/cfg"
APPS_DIR = "/lfs/apps"
LOG_DIR = "/lfs/log"
#: CONFIG_UC_CONFIG_DOC_MAX default (bytes a configuration document may have)
DOC_MAX = 8192
#: staged document: ``uc_node reload`` activates ``<doc>.json.new`` (management-protocol.md)
STAGED_SUFFIX = ".new"
#: app heap of the generated uc-link instances (it does not allocate)
LINK_HEAP_KB = 0
STATIC_BINDINGS_MAX = 16
PERM_ORDER = ("bacnet.local", "bacnet.remote", "io", "kv")
SIM_SUBNET = "10.47.0"
SIM_HOST_ADDRESS = f"{SIM_SUBNET}.254"
SIM_NETMASK = "255.255.255.0"


class RenderError(ManifestError):
    """A rendered document violates its schema or a firmware limit."""


def doc_path(doc: str) -> str:
    """``/lfs/cfg/<doc>.json``."""
    if doc not in DOC_NAMES:
        raise HarnessError(f"unknown configuration document {doc!r} (one of {DOC_NAMES})")
    return f"{CFG_DIR}/{doc}.json"


def doc_bytes(doc: Mapping[str, Any]) -> bytes:
    """Canonical file content of a configuration document (compact JSON + LF).

    The planner compares the node's file hash with :func:`doc_sha256`, so the
    serialisation must stay stable."""
    return (json.dumps(doc, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def doc_sha256(doc: Mapping[str, Any]) -> str:
    return hashlib.sha256(doc_bytes(doc)).hexdigest()


def sort_perms(perms: Any) -> list[str]:
    uniq = list(dict.fromkeys(str(p) for p in perms))
    return sorted(uniq, key=lambda p: PERM_ORDER.index(p) if p in PERM_ORDER else len(PERM_ORDER))


def format_number(value: float | int) -> str:
    """Shortest text of a number (``1`` for 1.0, ``0.1`` for 0.1)."""
    f = float(value)
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


# --- artifacts ---------------------------------------------------------------------------


@dataclass
class AppArtifact:
    """A built application module destined for one node."""

    app: str
    node: str
    path: Path
    sha256: str
    size: int
    aot: bool = False
    imports: list[str] = field(default_factory=list)  # "module.name"
    exports: list[str] = field(default_factory=list)

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()

    def to_dict(self) -> dict[str, Any]:
        return {"app": self.app, "node": self.node, "path": str(self.path),
                "sha256": self.sha256, "size": self.size, "aot": self.aot,
                "imports": self.imports, "exports": self.exports}


# --- addresses ----------------------------------------------------------------------------


def sim_address_plan(system: System, subnet: str = SIM_SUBNET) -> dict[str, str]:
    """Addresses of the simulated nodes: ``transport.ipv4`` when given, else
    ``<subnet>.1``, ``.2``, ... in manifest order (``.254`` is the host)."""
    taken = {n.transport.ipv4 for n in system.sim_nodes() if n.transport.ipv4}
    out: dict[str, str] = {}
    nxt = 1
    for n in system.sim_nodes():
        if n.transport.ipv4:
            out[n.name] = n.transport.ipv4
            continue
        while f"{subnet}.{nxt}" in taken or nxt == 254:
            nxt += 1
        if nxt > 253:
            raise HarnessError(f"simulation subnet {subnet}.0/24 exhausted")
        out[n.name] = f"{subnet}.{nxt}"
        taken.add(out[n.name])
        nxt += 1
    return out


def bacnet_endpoint(node: NodeSpec, addresses: Mapping[str, str] | None = None
                    ) -> tuple[str, int] | None:
    """``(host, port)`` of the node's BACnet/IP interface, ``None`` if unknown."""
    port = node.bacnet_port
    if node.bacnet_address:
        host, _, p = node.bacnet_address.partition(":")
        return host, int(p) if p else port
    static = node.static_ipv4()
    if static:
        return static, port
    if node.transport.kind == "udp" and node.transport.host:
        return node.transport.host, port
    if node.is_sim:
        addr = (addresses or {}).get(node.name) or node.transport.ipv4
        if addr:
            return addr, port
    return None


def smp_endpoint(node: NodeSpec, addresses: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Management transport of a node as an inventory-style dict."""
    t = node.transport
    if t.kind == "udp":
        return {"transport": "udp", "host": t.host, "port": t.port}
    if t.kind == "serial":
        return {"transport": "serial", "device": t.device, "baud": t.baud}
    addr = (addresses or {}).get(node.name) or t.ipv4
    return {"transport": "sim", "host": addr, "port": 1337}


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


# --- links -----------------------------------------------------------------------------------


def link_line(system: System, link: LinkSpec) -> str:
    """The uc-link ``l<i>`` parameter of a link (wasm/examples/uc-link/README.md)."""
    src = system.node(link.src_node)
    fields = [
        str(src.instance), str(link.src_type), str(link.src_instance),
        str(link.dst_type), str(link.dst_instance), link.mode, str(link.period_ms),
        str(link.priority), format_number(link.scale), format_number(link.offset),
    ]
    return " ".join(fields)


def link_apps(system: System, node: str) -> list[AppSpec]:
    """The uc-link application instances realising the links to ``node``."""
    links = system.links_to(node)
    if not links:
        return []
    chunks = [links[i:i + LINKS_PER_APP] for i in range(0, len(links), LINKS_PER_APP)]
    source = paths.wasm_examples_dir() / "uc-link" / "uc_link.c"
    out = []
    for k, chunk in enumerate(chunks):
        name = "link" if len(chunks) == 1 else f"link-{k + 1}"
        params: dict[str, Any] = {"count": str(len(chunk))}
        for i, lk in enumerate(chunk):
            params[f"l{i}"] = link_line(system, lk)
        out.append(AppSpec(
            name=name, node=node, source=source, wasm=None, aot=False, autostart=True,
            # uc-link does not allocate: no app heap (the WAMR heap must be a
            # multiple of 4 KiB, uc-link/README.md)
            period_ms=max(100, min(lk.period_ms for lk in chunk)), heap_kb=LINK_HEAP_KB,
            stack_kb=4,
            perms=["bacnet.local", "bacnet.remote"], params=params, kind="uc-link",
            generated=True,
        ))
    return out


def node_apps(system: System, node: str) -> list[AppSpec]:
    """Manifest apps on ``node`` followed by its generated uc-link instances."""
    return system.apps_on(node) + link_apps(system, node)


# --- permissions --------------------------------------------------------------------------------


def default_perms(app: AppSpec, own_instance: int | None,
                  artifact: AppArtifact | None = None) -> list[str]:
    """Permissions of an app without explicit ``perms``: from the stock app
    knowledge (params), else from the module's imports, else local + remote."""
    if app.perms is not None:
        return sort_perms(app.perms)
    kind = APP_KINDS.get(app.kind or "")
    if kind is not None:
        return sort_perms(kind.perms(app.params, own_instance))
    if artifact is not None and artifact.imports:
        from bacnet_uc_harness.wasm_build import perms_for_imports

        remote = any(k.endswith("device") and is_remote_device(v, own_instance)
                     for k, v in app.params.items())
        devices = [k for k in app.params if k.endswith("device")]
        return sort_perms(perms_for_imports(artifact.imports, remote=remote or not devices))
    return ["bacnet.local", "bacnet.remote"]


# --- rendering ----------------------------------------------------------------------------------


@dataclass
class RenderedApp:
    spec: AppSpec
    entry: dict[str, Any]

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def file(self) -> str:
        return str(self.entry["file"])


@dataclass
class NodeRender:
    node: str
    board: str
    device: dict[str, Any]
    io: dict[str, Any]
    apps: dict[str, Any]
    app_list: list[RenderedApp]
    warnings: list[str] = field(default_factory=list)

    def docs(self) -> dict[str, dict[str, Any]]:
        return {"device": self.device, "io": self.io, "apps": self.apps}

    def doc(self, name: str) -> dict[str, Any]:
        return self.docs()[name]

    def app(self, name: str) -> RenderedApp:
        for a in self.app_list:
            if a.name == name:
                return a
        raise HarnessError(f"node {self.node!r} has no app {name!r} in the manifest")

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "board": self.board, "device": self.device, "io": self.io,
                "apps": self.apps, "warnings": self.warnings,
                "sha256": {d: doc_sha256(v) for d, v in self.docs().items()}}


def _peer_order(system: System, node: NodeSpec) -> list[NodeSpec]:
    """Other nodes, the ones this node talks to first."""
    wanted: list[str] = []
    for lk in system.links:
        if lk.dst_node == node.name and lk.src_node != node.name:
            wanted.append(lk.src_node)
        if lk.src_node == node.name and lk.dst_node != node.name:
            wanted.append(lk.dst_node)
    by_instance = {n.instance: n.name for n in system.nodes}
    for app in system.apps_on(node.name):
        for key, value in app.params.items():
            if key.endswith("device") and is_remote_device(value, node.instance):
                try:
                    peer = by_instance.get(int(float(_stringify(value))))
                except ValueError:
                    peer = None
                if peer:
                    wanted.append(peer)
    order = list(dict.fromkeys(wanted))
    order += [n.name for n in system.nodes if n.name not in order]
    return [system.node(name) for name in order if name != node.name]


def _static_bindings(system: System, node: NodeSpec, addresses: Mapping[str, str] | None,
                     warnings: list[str]) -> list[dict[str, Any]]:
    bindings: list[dict[str, Any]] = [dict(b) for b in (node.bacnet or {}).get(
        "static_bindings", [])]
    have = {int(b["device"]) for b in bindings}
    for peer in _peer_order(system, node):
        if peer.instance in have:
            continue
        ep = bacnet_endpoint(peer, addresses)
        if ep is None:
            warnings.append(f"no BACnet address known for node {peer.name!r} (device "
                            f"{peer.instance}): set bacnet_address; no static binding")
            continue
        host, port = ep
        if not _is_ipv4(host):
            warnings.append(f"node {peer.name!r}: {host!r} is not an IPv4 address; set "
                            "bacnet_address for a static binding")
            continue
        if len(bindings) >= STATIC_BINDINGS_MAX:
            warnings.append(f"more than {STATIC_BINDINGS_MAX} peers: no static binding for "
                            f"node {peer.name!r} (Who-Is needed)")
            continue
        bindings.append({"device": peer.instance, "address": host, "port": port})
        have.add(peer.instance)
    return bindings


def render_device(system: System, node: NodeSpec, addresses: Mapping[str, str] | None = None,
                  warnings: list[str] | None = None) -> dict[str, Any]:
    warnings = warnings if warnings is not None else []
    doc: dict[str, Any] = {"schema": 1, "device": dict(node.device)}
    network = dict(node.network) if node.network else {}
    if node.is_sim:
        addr = (addresses or {}).get(node.name) or node.transport.ipv4
        if addr and not ipaddress.IPv4Address(addr).is_loopback:
            # NSOS binds BACnet/IP to network.ipv4; it must be the namespace's address
            network.update({"dhcp": False, "ipv4": addr, "netmask": SIM_NETMASK})
    if network:
        doc["network"] = network
    bacnet = dict(node.bacnet or {})
    bindings = _static_bindings(system, node, addresses, warnings)
    if bindings:
        bacnet["static_bindings"] = bindings
    else:
        bacnet.pop("static_bindings", None)
    if bacnet:
        doc["bacnet"] = bacnet
    return doc


def render_app_entry(app: AppSpec, own_instance: int | None,
                     artifact: AppArtifact | None = None) -> dict[str, Any]:
    ext = "aot" if (artifact.aot if artifact is not None else app.aot) else "wasm"
    entry: dict[str, Any] = {
        "name": app.name,
        "file": f"{APPS_DIR}/{app.name}.{ext}",
        "autostart": app.autostart,
        "period_ms": app.period_ms,
        "heap_kb": app.heap_kb,
        "stack_kb": app.stack_kb,
        "perms": default_perms(app, own_instance, artifact),
    }
    if app.params:
        entry["params"] = [{"key": str(k), "value": _stringify(v)} for k, v in app.params.items()]
    if artifact is not None:
        entry["sha256"] = artifact.sha256
    return entry


def render_node(system: System, node: str, *, addresses: Mapping[str, str] | None = None,
                artifacts: Mapping[str, AppArtifact] | None = None,
                validate: bool = True) -> NodeRender:
    """Render the three documents of one node.

    Args:
        addresses: simulation addresses by node name (default:
            :func:`sim_address_plan`).
        artifacts: built modules by app name (adds ``sha256``, picks ``.aot``,
            derives permissions of unknown apps from their imports).
        validate: validate against the schemas and the document size limit.

    Raises:
        RenderError: a document is invalid.
    """
    spec = system.node(node)
    if addresses is None:
        addresses = sim_address_plan(system)
    warnings: list[str] = []
    device = render_device(system, spec, addresses, warnings)
    io = {"schema": 1, "points": [dict(p) for p in spec.io]}
    rendered_apps = []
    for app in node_apps(system, node):
        art = (artifacts or {}).get(app.name)
        rendered_apps.append(RenderedApp(app, render_app_entry(app, spec.instance, art)))
    apps = {"schema": 1, "apps": [a.entry for a in rendered_apps]}
    result = NodeRender(node=node, board=spec.board, device=device, io=io, apps=apps,
                        app_list=rendered_apps, warnings=warnings)
    if validate:
        issues: list[Issue] = []
        for name, doc in result.docs().items():
            issues += validate_document(name, doc, (node, f"{name}.json"))
            size = len(doc_bytes(doc))
            if size > DOC_MAX:
                issues.append(Issue(f"/{node}/{name}.json",
                                    f"{size} bytes, CONFIG_UC_CONFIG_DOC_MAX is {DOC_MAX}"))
        if issues:
            raise RenderError(issues, f"(rendered documents of node {node!r})")
    return result


def render_system(system: System, *, addresses: Mapping[str, str] | None = None,
                  artifacts: Mapping[tuple[str, str], AppArtifact] | None = None,
                  validate: bool = True) -> dict[str, NodeRender]:
    """Render every node. ``artifacts`` is keyed by ``(node, app)``."""
    if addresses is None:
        addresses = sim_address_plan(system)
    out = {}
    for n in system.nodes:
        arts = {app: a for (nd, app), a in (artifacts or {}).items() if nd == n.name}
        out[n.name] = render_node(system, n.name, addresses=addresses, artifacts=arts,
                                  validate=validate)
    return out


def object_summary(system: System, node: str) -> list[dict[str, Any]]:
    """Objects the manifest expects on a node (IO points, app and link objects)."""
    spec = system.node(node)
    out = []
    for obj_type, inst, p in spec.io_objects():
        out.append({"object": enums.format_object_ref(obj_type, inst), "owner": "io",
                    "channel": p["channel"], "name": p.get("name", p["channel"])})
    for app in node_apps(system, node):
        for claim in app.object_claims():
            out.append({"object": claim.ref, "owner": f"app:{app.name}", "role": claim.role})
    return out
