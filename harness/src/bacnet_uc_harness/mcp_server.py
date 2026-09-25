# SPDX-License-Identifier: Apache-2.0
"""MCP server of the BACnet-uc development harness.

The server exposes the harness to an AI agent: node inventory, configuration
(device/IO/apps documents), IO channels, BACnet properties, WebAssembly
application build and deployment, logs, firmware build/flash/update,
distributed system manifests (validate, plan, apply, test) and native_sim
simulation.

Run it with ``bacnet-uc-mcp`` (stdio, default) or ``bacnet-uc-mcp --http``
(streamable HTTP on 127.0.0.1:8000/mcp). Built with the official MCP Python
SDK: ``MCPServer`` (mcp >= 2) or ``FastMCP`` (mcp 1.x).
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import shlex
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar

from pydantic import Field

from bacnet_uc_harness import __version__, firmware, paths
from bacnet_uc_harness.bacnet.client import BacnetClient
from bacnet_uc_harness.budget import system_budgets
from bacnet_uc_harness.errors import BacnetError, HarnessError, HarnessTimeout, SmpError
from bacnet_uc_harness.inventory import Inventory, InventoryNode
from bacnet_uc_harness.manifest import (
    CHANNEL_KIND_TYPES,
    Issue,
    ManifestError,
    System,
    load_system,
    normalize_catalog,
    validate_document,
    validation_report,
)
from bacnet_uc_harness.node import Node, jsonable
from bacnet_uc_harness.planner import ArtifactBuilder, apply, fetch_live, plan, wait_reachable
from bacnet_uc_harness.render import bacnet_endpoint, doc_sha256, render_system, sim_address_plan
from bacnet_uc_harness.sim import STATE_FILE, SimManager
from bacnet_uc_harness.testrunner import run_tests
from bacnet_uc_harness.testrunner import summary as test_summary
from bacnet_uc_harness.wasm_build import (
    WasmError,
    aot_compile,
    build_c,
    build_source_text,
    check_module,
    sdk_info,
)

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ResourceError, ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import (  # type: ignore[no-redef]
        ResourceError,
        ToolError,
    )
try:
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - very old SDKs
    ToolAnnotations = None  # type: ignore[assignment,misc]

ENV_ALLOW_SHELL = "BACNET_UC_ALLOW_SHELL"
DOC_ALIASES = {
    "wasm-sdk": ("wasm", "README.md"),
    "uc-link": ("wasm", "examples", "uc-link", "README.md"),
    "harness": ("harness", "README.md"),
}

INSTRUCTIONS = """\
BACnet-uc development harness. Nodes are Zephyr boards (nucleo_f767zi,
frdm_mcxn947/mcxn947/cpu0) or native_sim executables running a BACnet/IP
device, a LittleFS file system, logs, and a WebAssembly runtime for apps.
Management is SMP (MCUmgr) over UDP port 1337 or the serial console.

Typical workflow:
1. list_nodes / add_node (inventory) or sim_start (simulated nodes).
2. node_info, io_catalog, list_objects to see what a node offers.
3. configure_io to bind IO channels to BACnet objects; bacnet_read/write,
   io_force/io_release to test them.
4. sdk_info, build_app (C -> WebAssembly), deploy_app, app_status, read_logs.
5. Distributed applications: write a system manifest (resource
   bacnet-uc://schemas/system), validate_system, plan_system,
   apply_system(dry_run=false), run_system_tests.
Values: IO channels are raw (di/do 0|1, ai mV, ao %); BACnet objects carry
engineering units after io.json scale/offset. Objects are written as
'<type>:<instance>', e.g. 'analog-input:1'. Output objects (analog-/binary-/
multi-state-output) are commandable (priority array, 6 is reserved); value
objects have none: priorities are ignored and null (relinquish) does nothing.
"""

T = TypeVar("T")

# Argument types shared by the tools (module level: the SDK evaluates the annotations).
NodeName = Annotated[str, Field(description="Node name (inventory or loaded system)")]
SystemPath = Annotated[str, Field(description="Path of a system manifest (YAML/JSON), e.g. "
                                  "harness/examples/systems/sim-demo.yaml")]
ObjectRef = Annotated[str, Field(description="BACnet object '<type>:<instance>', e.g. "
                                 "'analog-input:1', 'binary-output:1', 'device:1001'")]


# --- context ----------------------------------------------------------------------------------


class HarnessContext:
    """State shared by the tools: inventory, connection pool, loaded systems,
    simulations and the artifact builder."""

    def __init__(self, home: Path | str | None = None, *, smp_timeout: float = 3.0,
                 smp_retries: int = 2, allow_shell: bool | None = None) -> None:
        self.home = Path(home).expanduser().resolve() if home else None
        self.smp_timeout = smp_timeout
        self.smp_retries = smp_retries
        self.allow_shell = (os.environ.get(ENV_ALLOW_SHELL) == "1" if allow_shell is None
                            else allow_shell)
        self.pool: dict[str, Node] = {}
        self._pool_keys: dict[str, tuple[str, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.systems: dict[str, System] = {}
        self.sims: dict[str, SimManager] = {}
        self._builder: ArtifactBuilder | None = None

    # -- paths
    def state_dir(self) -> Path:
        if self.home is not None:
            return self.home / paths.STATE_DIR_NAME
        return paths.state_dir()

    @property
    def builder(self) -> ArtifactBuilder:
        if self._builder is None:
            self._builder = ArtifactBuilder(self.state_dir() / "cache")
        return self._builder

    def inventory(self) -> Inventory:
        return Inventory.load(self.home)

    def resolve_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if p.is_absolute():
            return p
        for base in (Path.cwd(), self.home, self._repo()):
            if base is not None and (base / p).exists():
                return (base / p).resolve()
        return (Path.cwd() / p).resolve()

    @staticmethod
    def _repo() -> Path | None:
        try:
            return paths.repo_root()
        except HarnessError:
            return None

    # -- locks and connections
    def lock(self, name: str) -> asyncio.Lock:
        if name not in self._locks:
            self._locks[name] = asyncio.Lock()
        return self._locks[name]

    async def _pooled(self, name: str, target: str, bacnet: Any, board: str | None) -> Node:
        key = (target, bacnet)
        node = self.pool.get(name)
        if node is not None and self._pool_keys.get(name) == key:
            return node
        if node is not None:
            await node.close()
        node = Node(name, target, bacnet, board=board, smp_timeout=self.smp_timeout,
                    smp_retries=self.smp_retries)
        self.pool[name] = node
        self._pool_keys[name] = key
        return node

    async def node(self, name: str) -> Node:
        """Connection to a node from the inventory, else from a loaded system."""
        inv = self.inventory()
        if name in inv:
            e = inv.get(name)
            return await self._pooled(name, e.smp_target, e.bacnet_target, e.board)
        for system in self.systems.values():
            if system.has_node(name):
                return (await self.system_nodes(system, [name]))[name]
        raise HarnessError(f"unknown node {name!r}: not in the inventory {inv.path} and not in a "
                           "loaded system (add_node, sim_start or validate_system first)")

    def sim_for(self, system: System) -> SimManager | None:
        mgr = self.sims.get(system.name)
        if mgr is not None and mgr.state is not None:
            return mgr
        wd = self.state_dir() / "sim" / system.name
        if (wd / STATE_FILE).is_file():
            mgr = SimManager.load(wd)
            self.sims[system.name] = mgr
            return mgr
        return None

    def addresses(self, system: System) -> dict[str, str]:
        mgr = self.sim_for(system)
        if mgr is not None and mgr.state is not None:
            return {n: s.address for n, s in mgr.state.nodes.items()}
        return sim_address_plan(system)

    async def system_nodes(self, system: System, only: list[str] | None = None
                           ) -> dict[str, Node]:
        """Connections to the manifest's nodes. A simulated node uses the address of
        the running simulation, else its inventory entry; without either it is left
        out (the planner reports it unreachable) unless requested by name."""
        addrs = dict(self.addresses(system))
        running = self.sim_for(system) is not None
        inv = self.inventory()
        out: dict[str, Node] = {}
        for spec in system.nodes:
            if only is not None and spec.name not in only:
                continue
            if spec.is_sim and not running:
                entry = inv.nodes.get(spec.name)
                if entry is not None and entry.host:
                    addrs[spec.name] = entry.host
                elif only is None:
                    continue
            t = spec.transport
            if t.kind == "udp":
                target = f"udp:{t.host}:{t.port}"
            elif t.kind == "serial":
                target = f"serial:{t.device}:{t.baud}"
            else:
                target = f"udp:{addrs.get(spec.name, t.ipv4 or '127.0.0.1')}:1337"
            ep = bacnet_endpoint(spec, addrs)
            bacnet = f"{ep[0]}:{ep[1]}" if ep else None
            out[spec.name] = await self._pooled(spec.name, target, bacnet, spec.board)
        return out

    def load_system(self, path: str, **kwargs: Any) -> System:
        system = load_system(self.resolve_path(path), **kwargs)
        self.systems[system.name] = system
        return system

    def rebooter(self, system: System) -> Callable[[str], Awaitable[Any]]:
        """Reboot a node: simulated nodes are restarted by the simulation manager
        when it may (root for netns), otherwise (and for boards) ``os reset``; the
        native_sim firmware restarts its process in place (CONFIG_NATIVE_SIM_REBOOT)."""
        async def reboot(name: str) -> None:
            mgr = self.sim_for(system)
            if mgr is not None and mgr.state is not None and name in mgr.state.nodes \
                    and mgr.can_restart():
                await asyncio.to_thread(mgr.restart, name)
                return
            await (await self.node(name)).reboot()

        return reboot

    async def close(self) -> None:
        for node in self.pool.values():
            try:
                await node.close()
            except HarnessError:
                pass
        self.pool.clear()


# --- helpers ------------------------------------------------------------------------------------


def error_payload(exc: BaseException) -> dict[str, Any]:
    """JSON-friendly description of a harness error for a tool error result."""
    out: dict[str, Any] = {"error": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, ManifestError):
        out["issues"] = [i.to_dict() for i in exc.issues]
    if isinstance(exc, SmpError):
        out.update(group=exc.group, rc=exc.rc, rc_name=exc.rc_name)
    if isinstance(exc, BacnetError):
        out.update(kind=exc.kind, error_class=exc.error_class_name,
                   error_code=exc.error_code_name, reason=exc.reason)
    if isinstance(exc, WasmError):
        out["errors"] = exc.errors
        if exc.output:
            out["output"] = exc.output[-4000:]
    if isinstance(exc, HarnessTimeout):
        out["hint"] = ("no answer: is the node running and reachable (transport, address, "
                       "port 1337)? For simulated nodes check sim_status.")
    return out


def tool_errors(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Turn harness errors into structured MCP tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (HarnessError, OSError, ValueError, KeyError) as exc:
            raise ToolError(json.dumps(error_payload(exc), default=str)) from exc

    return wrapper


def annotations(*, read_only: bool = False, destructive: bool = False, idempotent: bool = False,
                open_world: bool = True, title: str | None = None) -> Any:
    if ToolAnnotations is None:
        return None
    data: dict[str, Any] = {"readOnlyHint": read_only, "destructiveHint": destructive,
                            "idempotentHint": idempotent, "openWorldHint": open_world}
    if title:
        data["title"] = title
    return ToolAnnotations.model_validate(data)


def _safe_name(name: str) -> str:
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ResourceError(f"invalid resource name {name!r}")
    return name


def _config_doc(doc: str) -> str:
    if doc not in ("device", "io", "apps"):
        raise HarnessError(f"doc must be 'device', 'io' or 'apps', not {doc!r}")
    return doc


def _points_changes(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, list[str]]:
    o = {p["channel"]: p for p in old}
    n = {p["channel"]: p for p in new}
    return {"added": sorted(set(n) - set(o)), "removed": sorted(set(o) - set(n)),
            "changed": sorted(c for c in set(o) & set(n) if o[c] != n[c])}


def check_io_points(points: list[dict[str, Any]], catalog: dict[str, Any] | None,
                    foreign_objects: set[tuple[str, int]] | None = None) -> list[Issue]:
    """Validate io.json points against the schema, the node's catalog (channel
    exists, object type fits the channel kind) and objects owned by others."""
    issues = validate_document("io", {"schema": 1, "points": points})
    if issues:
        return issues
    cat = normalize_catalog(catalog) if catalog is not None else None
    seen_obj: dict[tuple[str, int], str] = {}
    seen_ch: set[str] = set()
    for i, p in enumerate(points):
        where = f"/points/{i}"
        ch = p["channel"]
        if ch in seen_ch:
            issues.append(Issue(where + "/channel", f"channel {ch!r} bound twice"))
        seen_ch.add(ch)
        key = (p["type"], int(p["instance"]))
        if key in seen_obj:
            issues.append(Issue(where + "/instance",
                                f"{key[0]}:{key[1]} also bound to channel {seen_obj[key]!r}"))
        seen_obj[key] = ch
        if foreign_objects and key in foreign_objects:
            issues.append(Issue(where + "/instance",
                                f"{key[0]}:{key[1]} exists on the node and is owned by an app"))
        if cat is not None:
            if ch not in cat:
                known = ", ".join(sorted(cat))
                issues.append(Issue(where + "/channel",
                                    f"channel {ch!r} not in the catalog ({known})"))
            elif cat[ch] and p["type"] not in CHANNEL_KIND_TYPES.get(str(cat[ch]), ()):
                issues.append(Issue(where + "/type",
                                    f"{p['type']} cannot be bound to {cat[ch]} channel {ch!r}"))
    return issues


# --- server --------------------------------------------------------------------------------------


def create_server(ctx: HarnessContext | None = None) -> Any:
    """Build the MCP server (tools, resources, prompts) around a context."""
    ctx = ctx or HarnessContext()
    server = _Server("bacnet-uc", instructions=INSTRUCTIONS)
    server.harness = ctx  # type: ignore[attr-defined]

    registry: dict[str, Callable[..., Awaitable[Any]]] = {}
    server.harness_tools = registry  # type: ignore[attr-defined]

    def tool(**ann: Any) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
        def deco(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
            wrapped = tool_errors(fn)
            server.tool(annotations=annotations(**ann))(wrapped)
            registry[fn.__name__] = wrapped
            return wrapped
        return deco

    # ---------------------------------------------------------------- inventory

    @tool(read_only=True, idempotent=True, open_world=False)
    async def list_nodes() -> dict[str, Any]:
        """List the nodes in the harness inventory (.bacnet-uc/inventory.yaml) with their
        management (SMP) and BACnet/IP addresses, plus nodes of system manifests loaded
        in this session and running simulations. Use node names from here in all node
        tools."""
        inv = ctx.inventory()
        return {
            "inventory": str(inv.path),
            "nodes": [n.describe() for n in inv.list()],
            "systems": {name: s.node_names() for name, s in ctx.systems.items()},
            "simulations": sorted(p.parent.name for p in
                                  (ctx.state_dir() / "sim").glob(f"*/{STATE_FILE}")),
        }

    @tool(idempotent=True, open_world=False)
    async def add_node(
        name: Annotated[str, Field(description="Node name: lowercase letters, digits, '-'")],
        transport: Annotated[Literal["udp", "serial", "sim"],
                             Field(description="udp: SMP over UDP/IP (port 1337); serial: "
                                   "SMP over the shell UART; sim: a native_sim node")] = "udp",
        host: Annotated[str | None, Field(description="IP address/host name (udp, sim)")] = None,
        port: Annotated[int, Field(description="SMP UDP port")] = 1337,
        device: Annotated[str | None, Field(description="Serial device, e.g. /dev/ttyACM0")]
        = None,
        baud: int = 115200,
        bacnet_address: Annotated[str | None, Field(
            description="BACnet/IP 'host[:port]'; default: the UDP host, port 47808")] = None,
        board: Annotated[str | None, Field(description="Zephyr board target")] = None,
        replace: Annotated[bool, Field(description="Overwrite an existing entry")] = False,
        probe: Annotated[bool, Field(description="Query node_info after adding")] = True,
    ) -> dict[str, Any]:
        """Add (or replace) a node in the inventory and optionally probe it. The node is
        then usable by name in all node tools."""
        inv = ctx.inventory()
        node = InventoryNode(name=name, transport=transport, host=host, port=port, device=device,
                             baud=baud, bacnet_address=bacnet_address, board=board)
        inv.add(node, replace=replace)
        saved = inv.save()
        out: dict[str, Any] = {"node": node.describe(), "saved": str(saved)}
        if probe:
            try:
                async with ctx.lock(name):
                    n = await ctx.node(name)
                    info = await n.info()
                out["probe"] = {"ok": True, "info": info}
                if board is None and info.get("board"):
                    node.board = str(info["board"])
                    inv.add(node, replace=True)
                    inv.save()
                    out["node"] = node.describe()
            except HarnessError as exc:
                out["probe"] = {"ok": False, **error_payload(exc)}
        return out

    @tool(destructive=True, idempotent=True, open_world=False)
    async def remove_node(name: NodeName) -> dict[str, Any]:
        """Remove a node from the inventory (the node itself is not touched)."""
        inv = ctx.inventory()
        removed = inv.remove(name)
        inv.save()
        node = ctx.pool.pop(name, None)
        if node is not None:
            await node.close()
        return {"removed": removed.describe(), "inventory": str(inv.path)}

    @tool(read_only=True)
    async def discover_devices(
        broadcast: Annotated[str, Field(description="Broadcast address of the BACnet/IP "
                                        "network, e.g. 192.168.10.255")] = "255.255.255.255",
        low: Annotated[int | None, Field(description="Lowest device instance")] = None,
        high: Annotated[int | None, Field(description="Highest device instance")] = None,
        timeout_s: Annotated[float, Field(description="Collection time in seconds")] = 3.0,
        target: Annotated[str | None, Field(
            description="Unicast Who-Is to this 'host[:port]' instead of broadcasting")] = None,
    ) -> dict[str, Any]:
        """Send a BACnet Who-Is and list the answering devices (I-Am). Broadcast I-Ams are
        received on UDP 47808 (SO_REUSEADDR). native_sim nodes do not send broadcasts
        (NSOS); query them with target='<address>'."""
        client = BacnetClient(broadcast=broadcast, listen_broadcast=target is None)
        try:
            await client.start()
            found = await client.who_is(low, high, timeout=timeout_s, target=target)
        finally:
            await client.close()
        inv = ctx.inventory()
        by_addr = {n.bacnet_target: n.name for n in inv.list() if n.bacnet_target}
        return {"devices": [
            {"device": i.device, "address": f"{i.address[0]}:{i.address[1]}",
             "max_apdu": i.max_apdu, "segmentation": i.segmentation, "vendor_id": i.vendor_id,
             "inventory_node": by_addr.get(f"{i.address[0]}:{i.address[1]}")} for i in found]}

    # ---------------------------------------------------------------- node + config

    @tool(read_only=True, idempotent=True)
    async def node_info(node: NodeName) -> dict[str, Any]:
        """Firmware version, board, API version, BACnet device identity, network, uptime,
        file system usage, BACnet statistics, installed/running apps and WebAssembly
        runtime capabilities (interp/aot, pool size) of a node."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            info = await n.info()
        return {"node": node, "smp": str(n.target),
                "bacnet": f"{n.bacnet_address[0]}:{n.bacnet_address[1]}"
                if n.bacnet_address else None, "info": info}

    @tool(read_only=True, idempotent=True)
    async def get_config(
        node: NodeName,
        doc: Annotated[Literal["device", "io", "apps"], Field(
            description="device: identity/network/BACnet options; io: IO point mapping; "
                        "apps: installed WebAssembly apps")],
    ) -> dict[str, Any]:
        """Read a configuration document (/lfs/cfg/<doc>.json) from a node. exists=false
        means the node runs on firmware defaults."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            content = await n.get_config(_config_doc(doc))
        return {"node": node, "doc": doc, "exists": content is not None, "content": content,
                "sha256": doc_sha256(content) if content is not None else None}

    @tool(destructive=True, idempotent=True)
    async def set_config(
        node: NodeName,
        doc: Annotated[Literal["device", "io", "apps"], Field(description="Document name")],
        content: Annotated[dict[str, Any], Field(
            description="Complete document; must validate against "
                        "bacnet-uc://schemas/<doc> (e.g. {'schema': 1, 'device': {...}})")],
        reload: Annotated[bool, Field(description="Apply it right away (uc_node reload)")]
        = True,
        force: Annotated[bool, Field(description="Upload even if identical")] = False,
    ) -> dict[str, Any]:
        """Replace a configuration document on a node after schema validation: it is
        uploaded as /lfs/cfg/<doc>.json.new and activated by the reload (the node rejects
        an invalid document, deletes it and keeps its configuration: rc INVALID; without
        reload the staged file waits for the next reload/boot). Changes of device
        instance, network or BACnet port report reboot_required=true (reboot with
        apply_system or node_shell 'kernel reboot'). After an io reload the apps of the
        node that use its IO objects (stock apps, uc-link) are restarted."""
        _config_doc(doc)
        async with ctx.lock(node):
            n = await ctx.node(node)
            out = await n.push_config(doc, content, reload=reload, force=force)
            if doc == "io" and out.get("reloaded"):
                out["restarted_apps"] = await n.restart_io_dependents()
            return out

    @tool(idempotent=True)
    async def reload_config(
        node: NodeName,
        doc: Annotated[Literal["device", "io", "apps", "all"], Field(
            description="Document(s) to re-read")] = "all",
    ) -> dict[str, Any]:
        """Make a node re-read configuration documents from its file system (a staged
        /lfs/cfg/<doc>.json.new is activated first). An io reload restarts the node's apps
        that use its IO objects."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            reboot = await n.reload(doc)
            out: dict[str, Any] = {"node": node, "doc": doc, "reboot_required": reboot}
            if doc in ("io", "all"):
                out["restarted_apps"] = await n.restart_io_dependents()
        return out

    # ---------------------------------------------------------------- IO

    @tool(read_only=True, idempotent=True)
    async def io_catalog(node: NodeName) -> dict[str, Any]:
        """List the IO channels of a node (from its devicetree): name (di0, do0, ai0, ao0,
        ...), kind (di/do/ai/ao), hardware (gpio/adc/pwm/sim), description (pin), forced
        state and the BACnet object bound to it in io.json."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            return {"node": node, **await n.io_catalog()}

    @tool(destructive=True, idempotent=True)
    async def configure_io(
        node: NodeName,
        points: Annotated[list[dict[str, Any]], Field(description=(
            "io.json points: {channel, type, instance, name?, description?, units?, scale?, "
            "offset?, min?, max?, cov_increment?, sample_ms?, invert?, debounce_ms?}. "
            "type per channel kind: di -> binary-input|multi-state-input, do -> "
            "binary-output|binary-value, ai -> analog-input, ao -> analog-output|analog-value. "
            "Analog: PV = raw * scale + offset (ai raw in mV, ao raw in %)."))],
        mode: Annotated[Literal["merge", "replace"], Field(
            description="merge: replace points of the same channel, keep the others; "
                        "replace: the given points become the whole mapping")] = "merge",
        dry_run: Annotated[bool, Field(description="Only validate and return the result")]
        = False,
    ) -> dict[str, Any]:
        """Bind IO channels to BACnet objects (io.json) on a node: validates the points
        against the schema and the node's IO catalog, stages io.json and reloads it (the
        firmware re-creates the IO objects), then restarts the node's apps that use its IO
        objects (uc-link, stock apps) so outputs they drive do not stay at
        Relinquish_Default."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            catalog = await n.io_catalog()
            current = await n.get_config("io") or {"schema": 1, "points": []}
            old = list(current.get("points", []))
            if mode == "replace":
                new = list(points)
            else:
                given = {p.get("channel") for p in points}
                new = [p for p in old if p.get("channel") not in given] + list(points)
            foreign: set[tuple[str, int]] = set()
            try:
                for o in await n.objects():
                    if str(o.get("owner", "")).startswith("app:"):
                        foreign.add((str(o.get("type")), int(o.get("instance", -1))))
            except HarnessError:
                pass
            issues = check_io_points(new, catalog, foreign)
            if issues:
                raise ManifestError(issues, f"(io.json for node {node!r})")
            doc = {"schema": 1, "points": new}
            out: dict[str, Any] = {"node": node, "io": doc,
                                   "changes": _points_changes(old, new), "dry_run": dry_run}
            if not dry_run:
                out["result"] = await n.push_config("io", doc)
                if out["result"].get("reloaded"):
                    out["restarted_apps"] = await n.restart_io_dependents()
        return out

    @tool(read_only=True)
    async def io_read(
        node: NodeName,
        channel: Annotated[str | None, Field(description="Channel name; all when omitted")]
        = None,
    ) -> dict[str, Any]:
        """Read raw channel values (di/do 0|1, ai millivolts, ao percent)."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            return {"node": node, "values": await n.io_read(channel)}

    @tool(destructive=True, idempotent=True)
    async def io_write(node: NodeName,
                       channel: Annotated[str, Field(description="Output channel (do*, ao*)")],
                       value: Annotated[float, Field(description="do: 0/1, ao: 0..100 %")],
                       ) -> dict[str, Any]:
        """Write an output channel directly. If the channel is bound to a BACnet object the
        next IO scan drives it from the object again; use bacnet_write or io_force."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            await n.io_write(channel, value)
            return {"node": node, "channel": channel, "values": await n.io_read(channel)}

    @tool(destructive=True, idempotent=True)
    async def io_force(node: NodeName,
                       channel: Annotated[str, Field(description="Channel name")],
                       value: Annotated[float, Field(
                           description="Raw value: di/do 0|1, ai mV, ao %")]) -> dict[str, Any]:
        """Force a channel until io_release: an input's forced value is what the IO scan
        (and so the bound BACnet object) sees; an output is driven regardless of its
        object. On simulated channels this sets the value. The way to inject test
        stimuli."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            await n.io_force(channel, value)
            return {"node": node, "channel": channel, "forced": value,
                    "values": await n.io_read(channel)}

    @tool(destructive=True, idempotent=True)
    async def io_release(node: NodeName,
                         channel: Annotated[str, Field(description="Channel name")]
                         ) -> dict[str, Any]:
        """Release a forced channel: an input follows its hardware (or simulated value)
        again, an output follows its BACnet object."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            await n.io_release(channel)
            return {"node": node, "channel": channel, "released": True}

    # ---------------------------------------------------------------- BACnet

    @tool(read_only=True)
    async def bacnet_read(
        node: NodeName,
        object: ObjectRef,
        property: Annotated[str, Field(description="Property name, e.g. present-value, "
                                       "object-name, units, priority-array")] = "present-value",
        index: Annotated[int | None, Field(description="Array index (0 = length)")] = None,
        via: Annotated[Literal["auto", "bacnet", "smp"], Field(
            description="auto: BACnet/IP with SMP fallback; bacnet: ReadProperty only; smp: "
                        "uc_node prop_read (local objects, no network)")] = "auto",
    ) -> dict[str, Any]:
        """Read a property of an object on a node."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            if via == "bacnet":
                value, used = await n.bacnet_read(object, property, index), "bacnet"
            elif via == "smp":
                value, used = await n.prop_read(object, property, index), "smp"
            elif index is None:
                value, used = await n.read_point(object, property)
            else:
                value, used = await n.bacnet_read(object, property, index), "bacnet"
        return {"node": node, "object": object, "property": property, "value": jsonable(value),
                "via": used}

    @tool(destructive=True)
    async def bacnet_write(
        node: NodeName,
        object: ObjectRef,
        value: Annotated[float | int | bool | str | None, Field(
            description="Value; null relinquishes the priority slot. Binary PVs: 0/1 or "
                        "'active'/'inactive'")],
        property: Annotated[str, Field(description="Property name")] = "present-value",
        priority: Annotated[int | None, Field(description="Write priority 1..16 "
                                              "(commandable objects)")] = None,
        index: Annotated[int | None, Field(description="Array index")] = None,
        via: Annotated[Literal["auto", "bacnet", "smp"], Field(
            description="auto: BACnet/IP with SMP fallback")] = "auto",
    ) -> dict[str, Any]:
        """Write a property of an object on a node (WriteProperty), then read it back."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            if via == "smp":
                await n.prop_write(object, property, value, priority, index)
                used = "smp"
            elif via == "bacnet" or index is not None:
                await n.bacnet_write(object, property, value, priority, index)
                used = "bacnet"
            else:
                used = await n.write_point(object, property, value, priority)
            try:
                if used == "smp":
                    readback: Any = await n.prop_read(object, property, index)
                else:
                    readback = await n.bacnet_read(object, property, index)
            except HarnessError as exc:
                readback = {"error": str(exc)}
        return {"node": node, "object": object, "property": property, "written": value,
                "priority": priority, "via": used, "readback": jsonable(readback)}

    @tool(read_only=True, idempotent=True)
    async def list_objects(node: NodeName) -> dict[str, Any]:
        """List the BACnet objects of a node with name, owner ('system', 'io' or
        'app:<name>') and present value."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            objs = await n.objects()
        return {"node": node, "total": len(objs), "objects": objs}

    # ---------------------------------------------------------------- apps

    @tool(read_only=True, idempotent=True, open_world=False)
    async def sdk_info() -> dict[str, Any]:
        """Describe the WebAssembly application API (bacnet_uc.h): host functions with
        prototypes, documentation and required permission, app exports (uc_app_init,
        uc_app_tick, uc_app_on_cov, uc_app_on_write, uc_app_deinit), error codes, object
        type / property constants, allowed libc functions and the compiler flags. Read
        this before writing an app; the header itself is resource
        bacnet-uc://sdk/bacnet_uc.h."""
        return await asyncio.to_thread(sdk_info_impl)

    @tool(idempotent=True, open_world=False)
    async def build_app(
        name: Annotated[str, Field(description="App name (1..23 of a-z 0-9 _ -); output "
                                   "file name")],
        source_path: Annotated[str | None, Field(description="C source file")] = None,
        source: Annotated[str | None, Field(description="Inline C source text (instead of "
                                            "source_path)")] = None,
        aot_board: Annotated[str | None, Field(
            description="Also compile ahead of time for this board (nucleo_f767zi, "
                        "frdm_mcxn947/mcxn947/cpu0, native_sim/native/64); the firmware "
                        "loads .aot only with CONFIG_WAMR_AOT=y")] = None,
        opt: Annotated[str, Field(description="Optimisation level (-Oz smallest, -O2)")] = "-Oz",
        defines: Annotated[dict[str, str] | None, Field(description="Preprocessor defines")]
        = None,
        output_dir: Annotated[str | None, Field(
            description="Output directory (default .bacnet-uc/apps)")] = None,
    ) -> dict[str, Any]:
        """Compile C into a BACnet-uc WebAssembly app with the SDK (clang wasm32, ABI
        check). Returns the .wasm path for deploy_app, its size, imports/exports and the
        permissions the imports need. Compiler errors come back in 'errors'/'output'."""
        if bool(source_path) == bool(source):
            raise HarnessError("give exactly one of source_path or source")
        out_dir = Path(output_dir).expanduser() if output_dir else ctx.state_dir() / "apps"
        out = out_dir.resolve() / f"{name}.wasm"
        if source_path:
            res = await asyncio.to_thread(build_c, [ctx.resolve_path(source_path)], out, opt=opt,
                                          defines=defines)
        else:
            res = await asyncio.to_thread(build_source_text, source or "", out, name=name,
                                          opt=opt, defines=defines)
        result: dict[str, Any] = {"wasm": res.to_dict()}
        if aot_board:
            aot = await asyncio.to_thread(aot_compile, res.path, aot_board,
                                          out.with_suffix(".aot"))
            result["aot"] = aot.to_dict()
        return result

    @tool(destructive=True, idempotent=True)
    async def deploy_app(
        node: NodeName,
        name: Annotated[str, Field(description="App name on the node")],
        module_path: Annotated[str, Field(description=".wasm (or .aot) file, e.g. from "
                                          "build_app")],
        autostart: bool = True,
        period_ms: Annotated[int, Field(description="uc_app_tick period, 0 = events only")]
        = 1000,
        heap_kb: Annotated[int, Field(description="App heap (malloc); keep a multiple of 4")]
        = 8,
        stack_kb: int = 4,
        perms: Annotated[list[Literal["bacnet.local", "bacnet.remote", "io", "kv"]] | None,
                         Field(description="Permissions; default: derived from the module's "
                               "imports")] = None,
        params: Annotated[dict[str, str | float | int | bool] | None, Field(
            description="Deployment parameters (uc_param_get); values become strings")]
        = None,
        force: Annotated[bool, Field(description="Upload even if the node has the same "
                                     "module")] = False,
    ) -> dict[str, Any]:
        """Upload a module to /lfs/apps/<name>.wasm|.aot (skipped when the node already has
        identical content), install it with its manifest and (autostart) start it. A
        running app of the same name is replaced."""
        path = ctx.resolve_path(module_path)
        data = path.read_bytes()
        derived = None
        if perms is None:
            if data[:4] == b"\x00asm":
                chk = check_module(data)
                if not chk.ok:
                    raise WasmError("module violates the application ABI", chk.errors)
                derived = chk.perms
            else:
                derived = ["bacnet.local", "bacnet.remote"]
        str_params = {k: (("true" if v else "false") if isinstance(v, bool) else str(v))
                      for k, v in (params or {}).items()}
        async with ctx.lock(node):
            n = await ctx.node(node)
            res = await n.deploy_app(name, data, autostart=autostart, period_ms=period_ms,
                                     heap_kb=heap_kb, stack_kb=stack_kb,
                                     perms=list(perms) if perms is not None else derived,
                                     params=str_params, force=force)
        if derived is not None:
            res["perms_derived"] = derived
        return res

    @tool(destructive=True)
    async def app_control(
        node: NodeName,
        name: Annotated[str, Field(description="App name")],
        action: Annotated[Literal["start", "stop", "restart", "remove"], Field(
            description="remove also deletes the module file and the app's kv data unless "
                        "delete_file=false")],
        delete_file: bool = True,
    ) -> dict[str, Any]:
        """Start, stop, restart or remove an installed app."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            if action == "start":
                return await n.start_app(name)
            if action == "stop":
                return await n.stop_app(name)
            if action == "restart":
                await n.stop_app(name)
                return await n.start_app(name)
            return await n.remove_app(name, delete_file=delete_file)

    @tool(read_only=True, idempotent=True)
    async def list_apps(node: NodeName) -> dict[str, Any]:
        """Installed apps of a node with state (stopped/starting/running/failed), tick and
        event counters, errors and the last error message."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            return {"node": node, "apps": await n.list_apps()}

    @tool(read_only=True, idempotent=True)
    async def app_status(node: NodeName, name: Annotated[str, Field(description="App name")]
                         ) -> dict[str, Any]:
        """Status of one app (state, counters, last_error, uptime)."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            st = await n.app_status(name)
        if st is None:
            raise HarnessError(f"app {name!r} is not installed on node {node!r}")
        return st

    @tool(read_only=True)
    async def read_logs(
        node: NodeName,
        lines: Annotated[int, Field(description="Number of lines (newest last)")] = 200,
        grep: Annotated[str | None, Field(description="Regular expression filter, e.g. an "
                                          "app name or '<err>'")] = None,
    ) -> dict[str, Any]:
        """Read the node's persistent log (/lfs/log/log.NNNN files written by the Zephyr
        file system log backend): firmware modules and apps (uc_log) with timestamps and
        levels."""
        async with ctx.lock(node):
            n = await ctx.node(node)
            return await n.logs(lines, grep)

    @tool(destructive=True)
    async def node_shell(
        node: NodeName,
        command: Annotated[str, Field(description="Shell command line, e.g. 'uc info', "
                                      "'fs ls /lfs', 'kernel reboot cold'")],
    ) -> dict[str, Any]:
        """Run a Zephyr shell command on a node (SMP shell group). Disabled unless the
        server runs with BACNET_UC_ALLOW_SHELL=1."""
        if not ctx.allow_shell:
            raise HarnessError(f"node_shell is disabled; start the server with "
                               f"{ENV_ALLOW_SHELL}=1 to allow it")
        async with ctx.lock(node):
            n = await ctx.node(node)
            rc, out = await n.shell(shlex.split(command))
        return {"node": node, "command": command, "rc": rc, "output": out}

    # ---------------------------------------------------------------- firmware

    @tool(idempotent=True, open_world=False)
    async def build_firmware(
        board: Annotated[Literal["nucleo_f767zi", "frdm_mcxn947/mcxn947/cpu0",
                                 "native_sim/native/64"], Field(description="Board target")],
        pristine: Annotated[bool, Field(description="Clean rebuild")] = False,
        sysbuild: Annotated[bool, Field(description="With MCUboot (needed for "
                                        "update_firmware)")] = False,
        snippets: Annotated[list[str] | None, Field(description="Zephyr snippets, e.g. "
                                                    "['uc-ramfs']")] = None,
        extra_conf: Annotated[list[str] | None, Field(
            description="Extra Kconfig fragments relative to firmware/, e.g. "
                        "['overlay-syslog.conf']")] = None,
        cmake_args: Annotated[list[str] | None, Field(description="e.g. ['-DCONFIG_WAMR_AOT=y']")]
        = None,
        build_dir: Annotated[str | None, Field(description="Default .bacnet-uc/build/fw-<board>")]
        = None,
    ) -> dict[str, Any]:
        """Build the BACnet-uc firmware with west (minutes for a pristine build). Returns
        the build directory, image files and sizes, or the tail of the build log."""
        bdir = Path(build_dir).expanduser() if build_dir else \
            ctx.state_dir() / "build" / f"fw-{paths.board_key(board)}"
        res = await asyncio.to_thread(firmware.west_build, board, bdir, pristine, (), sysbuild,
                                      tuple(snippets or ()), cmake_args=tuple(cmake_args or ()),
                                      extra_conf=tuple(extra_conf or ()))
        return res.to_dict()

    @tool(destructive=True)
    async def flash_firmware(
        build_dir: Annotated[str, Field(description="Build directory from build_firmware")],
        runner: Annotated[str | None, Field(description="west flash runner (openocd, jlink, "
                                            "linkserver, pyocd)")] = None,
        confirm: Annotated[bool, Field(description="Must be true to flash; false returns "
                                       "what would be done")] = False,
    ) -> dict[str, Any]:
        """Flash a board attached to this host with 'west flash'. Requires confirm=true."""
        bdir = ctx.resolve_path(build_dir)
        info = firmware.firmware_info(bdir)
        if not confirm:
            return {"confirm_required": True, "would_run": ["west", "flash", "-d", str(bdir)] +
                    (["-r", runner] if runner else []), "image": info}
        res = await asyncio.to_thread(firmware.west_flash, bdir, runner)
        return res.to_dict()

    @tool(destructive=True)
    async def update_firmware(
        node: NodeName,
        build_dir: Annotated[str, Field(description="Sysbuild (MCUboot) build directory")],
        confirm: Annotated[bool, Field(description="Must be true to update")] = False,
        make_permanent: Annotated[bool, Field(
            description="Confirm the new image once it booted and answers; otherwise MCUboot "
                        "reverts at the next reset")] = True,
        timeout_s: Annotated[float, Field(description="Wait for the reboot")] = 90.0,
    ) -> dict[str, Any]:
        """Over-the-air firmware update through SMP: upload the signed image to the
        secondary slot, mark it for a test boot, reset, wait for the node and confirm the
        new image. Requires confirm=true."""
        bdir = ctx.resolve_path(build_dir)
        image = firmware.update_image(bdir)
        ihash = firmware.image_hash(image)
        plan_ = {"image": str(image), "size": image.stat().st_size, "hash": ihash.hex()}
        if not confirm:
            return {"confirm_required": True, **plan_}
        async with ctx.lock(node):
            n = await ctx.node(node)
            smp = await n.smp()
            await smp.img_upload(image.read_bytes())
            images = await smp.img_state()
            secondary = next((i for i in images if i.get("slot") == 1
                              and int(i.get("image", 0)) == 0), None)
            if secondary is None or not secondary.get("hash"):
                raise HarnessError("the uploaded image is not listed in the secondary slot")
            slot_hash = bytes(secondary["hash"])
            await smp.img_test(slot_hash)
            await smp.os_reset()
            await asyncio.sleep(2.0)
            ok = await wait_reachable(n, timeout_s)
            if not ok:
                raise HarnessError(f"node {node!r} did not come back within {timeout_s:.0f} s")
            images = await smp.img_state()
            active = next((i for i in images if i.get("active")), {})
            running_new = bytes(active.get("hash", b"")) == slot_hash
            if running_new and make_permanent:
                images = await smp.img_confirm()
        return {**plan_, "slot_hash": slot_hash.hex(), "hash_matches_image": slot_hash == ihash,
                "running_new_image": running_new, "confirmed": running_new and make_permanent,
                "images": jsonable([{k: (v.hex() if isinstance(v, bytes) else v)
                                     for k, v in i.items()} for i in images])}

    # ---------------------------------------------------------------- systems

    async def _live_catalogs(system: System) -> dict[str, Any]:
        cats: dict[str, Any] = {}
        nodes = await ctx.system_nodes(system)
        for name, n in nodes.items():
            try:
                async with ctx.lock(name):
                    cats[name] = await n.io_catalog()
            except HarnessError:
                continue
        return cats

    @tool(read_only=True, idempotent=True)
    async def validate_system(
        system: SystemPath,
        live_catalogs: Annotated[bool, Field(
            description="Check IO channels against the catalogs of the reachable nodes "
                        "(errors) instead of the built-in board catalogs (warnings)")] = False,
    ) -> dict[str, Any]:
        """Validate a system manifest: JSON schema, placeholders ({{ nodes.<n>.device.
        instance }}), unique names/instances, references, object collisions per node, link
        priorities, IO channels, and the WAMR pool budget per node (apps are built, cached;
        warning when the estimated pool use exceeds 90 % of CONFIG_UC_APP_POOL_SIZE).
        Returns errors and warnings with JSON-pointer paths, a summary and wamr_pool."""
        path = ctx.resolve_path(system)
        report = validation_report(path)
        if report["ok"]:
            sys_ = ctx.load_system(str(path))
            if live_catalogs:
                cats = await _live_catalogs(sys_)
                report = validation_report(path, catalogs=cats)
                report["catalogs_from"] = sorted(cats)
            renders = render_system(sys_, addresses=ctx.addresses(sys_))
            report["documents"] = {n: {"sha256": {d: doc_sha256(v) for d, v in r.docs().items()},
                                       "apps": [a.name for a in r.app_list],
                                       "warnings": r.warnings} for n, r in renders.items()}
            report["wamr_pool"] = await asyncio.to_thread(_pool_budget, sys_, report)
        return report

    def _pool_budget(sys_: System, report: dict[str, Any]) -> dict[str, Any]:
        """WAMR pool budgets; appends warnings to ``report``."""
        builder = ctx.builder
        try:
            artifacts = builder.build_system(sys_)
        except (HarnessError, OSError) as exc:
            return {"error": f"apps could not be built, no pool estimate: {exc}"}
        budgets = system_budgets(sys_, builder.wasm_for, artifacts)
        index = {n.name: i for i, n in enumerate(sys_.nodes)}
        for name, b in budgets.items():
            msg = b.message()
            if msg:
                report["warnings"].append({"path": f"/nodes/{index[name]}", "message": msg,
                                           "severity": "warning"})
        return {name: b.to_dict() for name, b in budgets.items()}

    async def _plan(system: str, prune: bool) -> tuple[System, Any, dict[str, Node], Any]:
        sys_ = ctx.load_system(system)
        artifacts = await asyncio.to_thread(ctx.builder.build_system, sys_)
        addrs = ctx.addresses(sys_)
        renders = render_system(sys_, addresses=addrs, artifacts=artifacts)
        nodes = await ctx.system_nodes(sys_)
        for name in nodes:
            await ctx.lock(name).acquire()
        try:
            live = await fetch_live(sys_, nodes, renders)
        finally:
            for name in nodes:
                ctx.lock(name).release()
        p = plan(sys_, live, renders=renders, artifacts=artifacts, prune=prune)
        return sys_, p, nodes, live

    @tool(read_only=True)
    async def plan_system(
        system: SystemPath,
        prune: Annotated[bool, Field(description="Plan removal of apps that are not in the "
                                     "manifest")] = False,
    ) -> dict[str, Any]:
        """Compare a system manifest with the live nodes and list the actions apply_system
        would take (push_config, reload, deploy_app, start_app, remove_app) plus notes
        such as unreachable nodes or pending reboots. Builds the apps (cached)."""
        _, p, _, live = await _plan(system, prune)
        out = p.to_dict()
        out["nodes"] = {n: s.to_dict() for n, s in live.items()}
        out["log"] = list(ctx.builder.log[-20:])
        return out

    @tool(destructive=True)
    async def apply_system(
        system: SystemPath,
        dry_run: Annotated[bool, Field(description="Only report what would be done "
                                       "(default); set false to change the nodes")] = True,
        prune: Annotated[bool, Field(description="Remove apps that are not in the manifest")]
        = False,
        reboot: Annotated[bool, Field(description="Reboot nodes whose device.json change needs "
                                      "it (simulated nodes are restarted)")] = True,
    ) -> dict[str, Any]:
        """Bring the nodes to the manifest's state in a safe order: device config, IO
        config, apps, links (uc-link instances). Default is a dry run."""
        sys_, p, nodes, _ = await _plan(system, prune)
        for name in nodes:
            await ctx.lock(name).acquire()
        try:
            report = await apply(p, nodes, dry_run=dry_run, reboot=reboot,
                                 rebooter=ctx.rebooter(sys_))
        finally:
            for name in nodes:
                ctx.lock(name).release()
        return report.to_dict()

    @tool(destructive=True)
    async def run_system_tests(
        system: SystemPath,
        tests: Annotated[list[str] | None, Field(description="Test names; all when omitted")]
        = None,
        stop_on_failure: Annotated[bool, Field(description="Stop a test at its first failed "
                                               "step")] = True,
    ) -> dict[str, Any]:
        """Run the manifest's acceptance tests (force/release IO, write points, wait, expect
        with retries) against the nodes. Forced channels are released afterwards."""
        sys_ = ctx.load_system(system)
        nodes = await ctx.system_nodes(sys_)
        results = await run_tests(sys_, nodes, tests, stop_on_failure=stop_on_failure)
        return test_summary(results)

    @tool(read_only=True)
    async def system_status(system: SystemPath) -> dict[str, Any]:
        """Health of a system: per node reachability, firmware, device identity, uptime,
        app states and whether its configuration and apps match the manifest."""
        sys_, p, _, live = await _plan(system, False)
        nodes = {}
        for name, st in live.items():
            info = st.info or {}
            nodes[name] = {
                "reachable": st.reachable, "error": st.error,
                "fw": info.get("fw"), "board": info.get("board"),
                "device": info.get("device"), "uptime_s": info.get("uptime_s"),
                "apps": {a: s.get("state") for a, s in st.apps_status.items()},
                "pending_actions": [a.describe() for a in p.for_node(name)],
            }
        sim = ctx.sim_for(sys_)
        return {"system": sys_.name, "in_sync": p.empty and not any(
            n.kind == "unreachable" for n in p.notes), "nodes": nodes,
                "notes": [n.to_dict() for n in p.notes],
                "simulation": sim.status() if sim is not None else None}

    # ---------------------------------------------------------------- simulation

    def _default_exe() -> Path:
        cands = [ctx.state_dir() / "build" / "fw-native_sim_native_64" / "zephyr" / "zephyr.exe"]
        for c in cands:
            if c.is_file():
                return c
        raise HarnessError("no native_sim firmware found: run build_firmware(board="
                           "'native_sim/native/64') or pass firmware_exe")

    @tool(destructive=True)
    async def sim_start(
        system: SystemPath,
        firmware_exe: Annotated[str | None, Field(
            description="native_sim zephyr.exe (default: the build_firmware output)")] = None,
        mode: Annotated[Literal["netns", "host", "compose"], Field(
            description="netns: one network namespace per node on bridge bnuc0, "
                        "10.47.0.<n> (needs root); host: one node on 127.0.0.1; compose: "
                        "docker compose")] = "netns",
        erase_flash: Annotated[bool, Field(description="Start with empty file systems")] = False,
        apply_config: Annotated[bool, Field(
            description="Then apply the manifest (config, apps, links) and restart nodes "
                        "whose network settings changed")] = False,
    ) -> dict[str, Any]:
        """Start the manifest's simulated (transport 'sim') nodes as native_sim processes
        and add them to the inventory. Use apply_system (or apply_config=true) afterwards
        so each node gets its address, static bindings, IO mapping and apps."""
        sys_ = ctx.load_system(system)
        exe = ctx.resolve_path(firmware_exe) if firmware_exe else _default_exe()
        old = ctx.sim_for(sys_)
        if old is not None and old.state is not None:
            raise HarnessError(f"simulation of {sys_.name!r} is already running (sim_stop first)")
        mgr = SimManager(ctx.state_dir() / "sim" / sys_.name, mode)
        addrs = await asyncio.to_thread(mgr.start, sys_, exe, None, erase_flash=erase_flash)
        ctx.sims[sys_.name] = mgr
        inv = ctx.inventory()
        for entry in mgr.inventory_nodes():
            name = str(entry.pop("name"))
            inv.add(InventoryNode.from_dict(name, entry), replace=True)
        inv.save()
        nodes = await ctx.system_nodes(sys_, list(addrs))
        reachable = {}
        for name, n in nodes.items():
            reachable[name] = await wait_reachable(n, 15.0, 0.5)
        out: dict[str, Any] = {"system": sys_.name, "addresses": addrs, "reachable": reachable,
                               "status": mgr.status()}
        if apply_config:
            _, p, all_nodes, _ = await _plan(system, False)
            report = await apply(p, all_nodes, reboot=True, rebooter=ctx.rebooter(sys_))
            out["apply"] = report.to_dict()
        return out

    @tool(destructive=True, idempotent=True)
    async def sim_stop(system: Annotated[str, Field(description="Manifest path or system "
                                                    "name")]) -> dict[str, Any]:
        """Stop the simulated nodes of a system and remove their network (flash images are
        kept, so the next sim_start resumes with the same configuration)."""
        name = system
        if system.endswith((".yaml", ".yml", ".json")):
            name = ctx.load_system(system).name
        mgr = ctx.sims.pop(name, None)
        if mgr is None:
            wd = ctx.state_dir() / "sim" / name
            if not (wd / STATE_FILE).is_file():
                raise HarnessError(f"no simulation of {name!r} is running")
            mgr = SimManager.load(wd)
        result = await asyncio.to_thread(mgr.stop)
        inv = ctx.inventory()
        for n in result["stopped"]:
            if n in inv and inv.get(n).transport == "sim":
                inv.remove(n)
            node = ctx.pool.pop(n, None)
            if node is not None:
                await node.close()
        inv.save()
        return {"system": name, **result}

    @tool(read_only=True, idempotent=True, open_world=False)
    async def sim_status(system: Annotated[str | None, Field(
            description="Manifest path or system name; all simulations when omitted")] = None
    ) -> dict[str, Any]:
        """Status of running simulations: mode, node addresses, process ids, liveness,
        flash images and log files (the console output of each zephyr.exe)."""
        base = ctx.state_dir() / "sim"
        names: list[str]
        if system:
            names = [ctx.load_system(system).name if system.endswith((".yaml", ".yml", ".json"))
                     else system]
        else:
            names = sorted(p.parent.name for p in base.glob(f"*/{STATE_FILE}"))
        out = {}
        for name in names:
            mgr = ctx.sims.get(name)
            if mgr is None and (base / name / STATE_FILE).is_file():
                mgr = SimManager.load(base / name)
                ctx.sims[name] = mgr
            if mgr is None:
                out[name] = {"running": False}
                continue
            st = mgr.status()
            if mgr.state is not None:
                st["log_tail"] = {n: mgr.log_tail(n, 5) for n in mgr.state.nodes}
            out[name] = st
        return {"simulations": out}

    # ---------------------------------------------------------------- resources

    def _repo_file(*parts: str) -> Path:
        p = paths.repo_root().joinpath(*parts)
        if not p.is_file():
            raise ResourceError(f"{'/'.join(parts)} not found")
        return p

    @server.resource("bacnet-uc://docs/{name}", mime_type="text/markdown",
                     description="Project documentation: docs/<name> (e.g. "
                                 "management-protocol.md), 'wasm-sdk', 'uc-link', 'harness'")
    def doc_resource(name: str) -> str:
        name = _safe_name(name)
        if name in DOC_ALIASES:
            return _repo_file(*DOC_ALIASES[name]).read_text(encoding="utf-8")
        if not name.endswith(".md"):
            name += ".md"
        return _repo_file("docs", name).read_text(encoding="utf-8")

    @server.resource("bacnet-uc://schemas/{name}", mime_type="application/json",
                     description="JSON schemas: device, io, apps, system (or "
                                 "<name>.schema.json); example-device, example-io, "
                                 "example-apps for filled-in documents")
    def schema_resource(name: str) -> str:
        name = _safe_name(name)
        if name.startswith("example-"):
            return _repo_file("schemas", "examples",
                              name.removeprefix("example-").removesuffix(".json") + ".json"
                              ).read_text(encoding="utf-8")
        if not name.endswith(".json"):
            name += ".schema.json"
        return _repo_file("schemas", name).read_text(encoding="utf-8")

    @server.resource("bacnet-uc://sdk/bacnet_uc.h", mime_type="text/x-c",
                     description="WebAssembly guest ABI header (host functions, exports)")
    def header_resource() -> str:
        return paths.bacnet_uc_header().read_text(encoding="utf-8")

    @server.resource("bacnet-uc://nodes/{name}/info", mime_type="application/json",
                     description="Live node_info of an inventory node")
    async def node_info_resource(name: str) -> str:
        try:
            async with ctx.lock(name):
                n = await ctx.node(_safe_name(name))
                info = await n.info()
        except HarnessError as exc:
            raise ResourceError(str(exc)) from exc
        return json.dumps(info, indent=2, default=str)

    # ---------------------------------------------------------------- prompts

    @server.prompt(description="Plan and build a distributed BACnet application across nodes")
    def design_distributed_app(goal: str) -> str:
        return DESIGN_PROMPT.format(goal=goal)

    @server.prompt(description="Bring a new node into service")
    def commission_node(node: str) -> str:
        return COMMISSION_PROMPT.format(node=node)

    @server.prompt(description="Find out why an app misbehaves")
    def debug_app(node: str, app: str) -> str:
        return DEBUG_PROMPT.format(node=node, app=app)

    return server


def sdk_info_impl() -> dict[str, Any]:
    info = sdk_info()
    info["harness_version"] = __version__
    info["examples"] = {}
    try:
        for d in sorted(paths.wasm_examples_dir().iterdir()):
            if d.is_dir():
                srcs = [p.name for p in d.glob("*.c") if not p.name.startswith("test_")]
                info["examples"][d.name] = [str(d / s) for s in srcs]
    except (OSError, HarnessError):
        pass
    return info


DESIGN_PROMPT = """\
Goal: {goal}

Design and deploy this as a BACnet-uc distributed application:

1. Inventory: call list_nodes; for each candidate node call node_info and io_catalog
   (channels, kinds di/do/ai/ao). Without hardware, plan native_sim nodes
   (transport kind 'sim') and use sim_start.
2. Read the contracts: resource bacnet-uc://schemas/system (manifest schema),
   bacnet-uc://docs/uc-link (links), sdk_info (app API) and the example apps it lists
   (thermostat, alarm, blinky, uc-link).
3. Write a system manifest (YAML, apiVersion bacnet-uc/v1, kind System):
   - nodes: board, transport, device.instance (unique), io points binding channels to
     objects (di -> binary-input, do -> binary-output, ai -> analog-input with scale/offset
     from mV, ao -> analog-output in %),
   - apps: prefer the stock apps with params; reference other nodes' devices with
     "{{{{ nodes.<name>.device.instance }}}}",
   - links: '<node>/<type>:<instance>' -> writable destination (AO/AV/BO/BV/MSO/MSV);
     realised by uc-link instances on the destination node (max 8 per instance);
     priority 1..16 (not 6) for output objects (AO/BO/MSO), 0 for value objects
     (AV/BV/MSV have no priority array: one writer per value object),
   - nodes: bacnet.password to allow DeviceCommunicationControl/ReinitializeDevice,
   - tests: force inputs, expect outputs with within_ms; restore a value object by
     writing the old value (a null write does not relinquish it).
   Keep at most 4 apps per node (CONFIG_UC_APPS_MAX default, uc-link counts).
4. validate_system until there are no errors (check warnings too, including the
   WAMR pool budget per node in wamr_pool).
5. plan_system, then apply_system with dry_run=false (reboot=true when device
   identities or addresses change).
6. run_system_tests; on failures use debug_app / read_logs / list_objects, fix the
   manifest or app, and repeat from step 4.
Report the manifest, the plan and the test results.
"""

COMMISSION_PROMPT = """\
Commission node '{node}':

1. node_info: check firmware version, board, API version, file system ready, network.
   If it fails, check the inventory entry (list_nodes) and the transport.
2. get_config device: set a unique device instance, a descriptive name and location with
   set_config (keep network settings unless asked); reboot if reboot_required.
3. io_catalog: list the channels. Propose an IO mapping (object type per channel kind,
   units, scale/offset for analog inputs in mV) and apply it with configure_io.
4. list_objects: verify the IO objects exist with the expected names.
5. Exercise every point: io_force inputs and bacnet_read their objects; bacnet_write
   outputs (priority 8) and io_read the channels; release forces and relinquish writes
   (value objects bound to outputs have no priority array: write the old value back).
6. read_logs with grep '<err>|<wrn>' and report anything unusual.
Summarise the final configuration.
"""

DEBUG_PROMPT = """\
Debug app '{app}' on node '{node}':

1. app_status: state, ticks, events, errors, last_error. 'failed' with a load error
   usually means an ABI/version mismatch or a missing permission.
2. read_logs with grep '{app}' (and '<err>'): host call errors, traps, watchdog kills.
3. get_config apps: check the entry's perms and params (values are strings; the app
   parses them with uc_param_get/uc_param_get_number).
4. list_objects: are the objects the app should create present (owner 'app:{app}')?
   Do IO objects it uses exist? bacnet_read the relevant points.
5. sdk_info: compare the host functions the app uses with the permissions it has
   (bacnet.remote for other devices, io for raw channels, kv for persistence).
6. For remote points check the static bindings in device.json and that the peer answers
   (bacnet_read on the peer node).
7. Fix (params via deploy_app, code via build_app + deploy_app) and app_control restart;
   confirm with app_status and read_logs.
"""


# --- entry point -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bacnet-uc-mcp", description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--home", help="directory holding .bacnet-uc (default: "
                        "$BACNET_UC_HOME or the current directory)")
    parser.add_argument("--allow-shell", action="store_true",
                        help=f"enable node_shell (same as {ENV_ALLOW_SHELL}=1)")
    args = parser.parse_args(argv)
    ctx = HarnessContext(args.home, allow_shell=True if args.allow_shell else None)
    server = create_server(ctx)
    if not args.http:
        server.run("stdio")
        return 0
    if hasattr(server, "run_streamable_http_async"):
        import anyio

        try:
            anyio.run(functools.partial(server.run_streamable_http_async, host=args.host,
                                        port=args.port))
        except TypeError:  # mcp 1.x: host/port live in the settings
            server.settings.host = args.host
            server.settings.port = args.port
            server.run("streamable-http")
    else:
        server.settings.host = args.host
        server.settings.port = args.port
        server.run("streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
