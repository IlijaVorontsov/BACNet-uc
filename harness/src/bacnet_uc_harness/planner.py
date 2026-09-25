# SPDX-License-Identifier: Apache-2.0
"""Plan and apply a system manifest against live nodes.

1. :class:`ArtifactBuilder` builds every application module of the system
   (manifest apps and the generated ``uc-link`` instances), cached by
   content hash.
2. :func:`fetch_live` reads the live state of each node: node info,
   SHA-256 of ``device.json``/``io.json`` and of staged documents
   (``<doc>.json.new``), the installed apps (``apps.json`` entries +
   ``uc_app list``), SHA-256 of the module files and the object list.
3. :func:`plan` compares desired and live state and returns a
   :class:`Plan` of actions: ``clear_staged`` (remove a staged document the
   next reload or boot would activate), ``push_config`` (staged upload +
   reload), ``reload``, ``remove_app`` (only with ``prune``), ``deploy_app``
   (upload if the module changed, install with manifest), ``start_app``,
   ``restart_app``, plus notes (e.g. a pending reboot).
4. :func:`apply` executes a plan in a safe order: device configuration, IO
   configuration, applications, links (uc-link instances), restarts;
   optionally reboots nodes that report ``reboot_required``.

An ``io.json`` reload deletes and re-creates every IO object of the node:
outputs fall back to their Relinquish_Default and COV subscriptions on the
old objects are gone. uc-link only writes a destination when its source
changes, so a link-driven output would stay at Relinquish_Default until
then. The plan therefore restarts every app (on any node) whose inputs or
outputs are IO objects of a node whose ``io.json`` is pushed or reloaded
(stock apps and uc-link instances, from their parameters), unless the app is
(re)deployed or started anyway.

:func:`plan` is pure; :func:`fetch_live` and :func:`apply` only use the
duck-typed node methods of :class:`bacnet_uc_harness.node.Node`, so tests can
use fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from bacnet_uc_harness import paths
from bacnet_uc_harness.bacnet import enums
from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.manifest import AppSpec, System
from bacnet_uc_harness.render import (
    DOC_NAMES,
    STAGED_SUFFIX,
    AppArtifact,
    NodeRender,
    RenderedApp,
    doc_path,
    doc_sha256,
    node_apps,
    render_system,
    sim_address_plan,
    sort_perms,
)
from bacnet_uc_harness.wasm_build import aot_compile, build_c, check_module

ActionKind = Literal["clear_staged", "push_config", "reload", "remove_app", "deploy_app",
                     "start_app", "restart_app"]
PHASE_DEVICE, PHASE_IO, PHASE_APPS, PHASE_LINKS = 0, 1, 2, 3
PHASE_NAMES = {PHASE_DEVICE: "device", PHASE_IO: "io", PHASE_APPS: "apps", PHASE_LINKS: "links"}
_KIND_ORDER = {"clear_staged": 0, "push_config": 1, "reload": 2, "remove_app": 3,
               "deploy_app": 4, "start_app": 5, "restart_app": 6}
DEFAULT_OPT = "-Oz"
_QUOTED_INCLUDE = re.compile(rb'^[ \t]*#[ \t]*include[ \t]*"([^"\n]+)"', re.M)


def source_dependencies(source: Path) -> list[Path]:
    """``source`` and the files it includes with ``#include "..."``, found
    relative to the including file, recursively (headers of the SDK include
    directory are hashed separately). Conditional includes count as well."""
    seen: set[Path] = set()
    todo = [source.resolve()]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        seen.add(f)
        for m in _QUOTED_INCLUDE.finditer(data):
            cand = (f.parent / m.group(1).decode("utf-8", "replace")).resolve()
            if cand.is_file() and cand not in seen:
                todo.append(cand)
    return sorted(seen)


# --- artifacts ------------------------------------------------------------------------------


class ArtifactBuilder:
    """Builds application modules into a content-addressed cache.

    Args:
        cache_dir: default ``<home>/.bacnet-uc/cache``.
        opt: optimisation level for ``uc-cc`` (the SDK recommends ``-Oz``).
    """

    def __init__(self, cache_dir: Path | None = None, opt: str = DEFAULT_OPT) -> None:
        self.cache_dir = cache_dir or paths.cache_dir()
        self.opt = opt
        self.log: list[str] = []
        self._toolchain: str | None = None

    def _sdk_digest(self) -> str:
        h = hashlib.sha256()
        try:
            for p in sorted(paths.wasm_include_dir().glob("*.h")):
                h.update(p.read_bytes())
        except HarnessError:
            pass
        return h.hexdigest()

    def _toolchain_digest(self) -> str:
        """uc-cc and its checker modules, and the clang version (once per builder)."""
        if self._toolchain is None:
            h = hashlib.sha256()
            try:
                sdk = paths.wasm_sdk_dir()
                for p in [sdk / "uc-cc", *sorted((sdk / "tools").glob("*.py"))]:
                    if p.is_file():
                        h.update(p.read_bytes())
            except HarnessError:
                pass
            clang = os.environ.get("UC_CLANG", "clang")
            try:
                res = subprocess.run([clang, "--version"], capture_output=True, text=True,
                                     check=False, timeout=30)
                h.update(res.stdout.encode())
            except (OSError, subprocess.SubprocessError):
                pass
            self._toolchain = h.hexdigest()
        return self._toolchain

    def wasm_key(self, app: AppSpec) -> str:
        """Cache key of an app built from source: the source and every local
        header it includes (:func:`source_dependencies`), the options, the SDK
        headers and the toolchain."""
        assert app.source is not None
        h = hashlib.sha256()
        base = app.source.resolve().parent
        for dep in source_dependencies(app.source):
            try:
                name = str(dep.relative_to(base))
            except ValueError:
                name = str(dep)
            h.update(name.encode() + b"\0" + hashlib.sha256(dep.read_bytes()).digest())
        h.update(f"opt={self.opt};heap_kb={app.heap_kb}".encode())
        h.update(self._sdk_digest().encode() + self._toolchain_digest().encode())
        return h.hexdigest()[:16]

    def wasm_for(self, app: AppSpec) -> Path:
        """The ``.wasm`` of an app (built from ``source`` if necessary)."""
        if app.wasm is not None:
            if not app.wasm.is_file():
                raise HarnessError(f"app {app.name!r}: wasm file not found: {app.wasm}")
            return app.wasm
        if app.source is None:
            raise HarnessError(f"app {app.name!r} has neither source nor wasm")
        if not app.source.is_file():
            raise HarnessError(f"app {app.name!r}: source not found: {app.source}")
        key = self.wasm_key(app)
        out = self.cache_dir / "wasm" / key / f"{app.source.stem}.wasm"
        if not out.is_file():
            res = build_c([app.source], out, opt=self.opt, heap_kb=app.heap_kb)
            self.log.append(f"built {out.name} from {app.source} ({res.size} B)")
        return out

    def artifact(self, node: str, board: str, app: AppSpec) -> AppArtifact:
        wasm = self.wasm_for(app)
        path = wasm
        if app.aot:
            data = wasm.read_bytes()
            key = hashlib.sha256(data).hexdigest()[:16]
            path = self.cache_dir / "aot" / key / paths.board_key(board) / f"{wasm.stem}.aot"
            if not path.is_file():
                aot_compile(wasm, board, path)
                self.log.append(f"AOT {path.name} for {board}")
        chk = check_module(wasm)
        if not chk.ok:
            raise HarnessError(f"app {app.name!r}: {'; '.join(chk.errors)}")
        data = path.read_bytes()
        return AppArtifact(app=app.name, node=node, path=path,
                           sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                           aot=app.aot, imports=chk.imports, exports=chk.exports)

    def build_system(self, system: System, nodes: list[str] | None = None
                     ) -> dict[tuple[str, str], AppArtifact]:
        """Artifacts of all apps (including uc-link instances), keyed ``(node, app)``."""
        out: dict[tuple[str, str], AppArtifact] = {}
        for n in system.nodes:
            if nodes is not None and n.name not in nodes:
                continue
            for app in node_apps(system, n.name):
                out[(n.name, app.name)] = self.artifact(n.name, n.board, app)
        return out


# --- live state -------------------------------------------------------------------------------


@dataclass
class LiveState:
    node: str
    reachable: bool = True
    error: str | None = None
    info: dict[str, Any] = field(default_factory=dict)
    config_sha256: dict[str, str | None] = field(default_factory=dict)
    #: SHA-256 of a staged ``<doc>.json.new`` by document, ``None``: none staged
    staged_sha256: dict[str, str | None] = field(default_factory=dict)
    apps_cfg: list[dict[str, Any]] = field(default_factory=list)
    apps_status: dict[str, dict[str, Any]] = field(default_factory=dict)
    files_sha256: dict[str, str | None] = field(default_factory=dict)
    objects: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "reachable": self.reachable, "error": self.error,
                "info": self.info, "config_sha256": self.config_sha256,
                "staged": sorted(d for d, h in self.staged_sha256.items() if h is not None),
                "apps": [{"name": n, "state": s.get("state")} for n, s in self.apps_status.items()]}


async def fetch_live_node(node: Any, render: NodeRender | None = None,
                          with_objects: bool = True) -> LiveState:
    """Read the live state of one node (see module docstring)."""
    state = LiveState(node=node.name)
    try:
        state.info = await node.info()
        for doc in ("device", "io"):
            state.config_sha256[doc] = await node.config_sha256(doc)
        for doc in DOC_NAMES:
            state.staged_sha256[doc] = await node.file_sha256(doc_path(doc) + STAGED_SUFFIX)
        apps_doc = await node.get_config("apps")
        if isinstance(apps_doc, dict):
            state.apps_cfg = [dict(e) for e in apps_doc.get("apps", []) if isinstance(e, dict)]
        state.apps_status = {str(a.get("name")): dict(a) for a in await node.list_apps()}
        files: set[str] = {str(e.get("file")) for e in state.apps_cfg if e.get("file")}
        files |= {str(s.get("file")) for s in state.apps_status.values() if s.get("file")}
        if render is not None:
            files |= {a.file for a in render.app_list}
        for f in sorted(files):
            state.files_sha256[f] = await node.file_sha256(f)
        if with_objects:
            try:
                state.objects = await node.objects()
            except HarnessError:
                state.objects = None
    except (HarnessError, OSError) as exc:
        state.reachable = False
        state.error = f"{type(exc).__name__}: {exc}"
    return state


async def fetch_live(system: System, nodes: Mapping[str, Any],
                     renders: Mapping[str, NodeRender] | None = None,
                     with_objects: bool = True) -> dict[str, LiveState]:
    """Live state of every manifest node present in ``nodes`` (concurrently)."""
    names = [n.name for n in system.nodes if n.name in nodes]
    states = await asyncio.gather(*(
        fetch_live_node(nodes[name], (renders or {}).get(name), with_objects) for name in names))
    return dict(zip(names, states, strict=True))


# --- plan -------------------------------------------------------------------------------------


@dataclass
class Action:
    kind: ActionKind
    node: str
    target: str  # document or app name
    reason: str
    phase: int
    data: dict[str, Any] = field(default_factory=dict)

    def sort_key(self, node_order: Mapping[str, int]) -> tuple[int, int, int, str]:
        return (self.phase, node_order.get(self.node, 1 << 20), _KIND_ORDER[self.kind],
                self.target)

    def describe(self) -> str:
        return f"{self.node}: {self.kind} {self.target} ({self.reason})"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "node": self.node, "target": self.target,
                               "reason": self.reason, "phase": PHASE_NAMES[self.phase]}
        if self.kind == "deploy_app":
            art = self.data.get("artifact")
            out["upload"] = self.data.get("upload", True)
            out["entry"] = self.data.get("entry")
            if isinstance(art, AppArtifact):
                out["artifact"] = {"path": str(art.path), "sha256": art.sha256,
                                   "size": art.size}
        elif self.kind == "push_config":
            out["sha256"] = doc_sha256(self.data["doc"])
        return out


@dataclass
class Note:
    node: str
    message: str
    kind: Literal["info", "warning", "reboot_required", "unreachable"] = "info"

    def to_dict(self) -> dict[str, str]:
        return {"node": self.node, "kind": self.kind, "message": self.message}


@dataclass
class Plan:
    system: str
    actions: list[Action] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    node_order: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.actions

    def ordered(self) -> list[Action]:
        order = {n: i for i, n in enumerate(self.node_order)}
        return sorted(self.actions, key=lambda a: a.sort_key(order))

    def for_node(self, node: str) -> list[Action]:
        return [a for a in self.ordered() if a.node == node]

    def summary(self) -> str:
        if self.empty:
            return f"system {self.system}: in sync"
        lines = [f"system {self.system}: {len(self.actions)} action(s)"]
        lines += [f"  {a.describe()}" for a in self.ordered()]
        lines += [f"  note {n.node}: {n.message}" for n in self.notes]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"system": self.system, "in_sync": self.empty,
                "actions": [a.to_dict() for a in self.ordered()],
                "notes": [n.to_dict() for n in self.notes]}


def _norm_params(value: Any) -> dict[str, str]:
    if isinstance(value, list):
        return {str(p.get("key")): str(p.get("value")) for p in value if isinstance(p, dict)}
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return {}


def _norm_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """apps.json entry with defaults applied, for comparisons."""
    return {
        "file": entry.get("file"),
        "autostart": bool(entry.get("autostart", True)),
        "period_ms": int(entry.get("period_ms", 1000)),
        "heap_kb": int(entry.get("heap_kb", 8)),
        "stack_kb": int(entry.get("stack_kb", 4)),
        "perms": sort_perms(entry.get("perms", [])),
        "params": _norm_params(entry.get("params", [])),
    }


def entry_diff(desired: Mapping[str, Any], live: Mapping[str, Any]) -> list[str]:
    """Names of the manifest fields that differ between two apps.json entries."""
    a, b = _norm_entry(desired), _norm_entry(live)
    return [k for k in a if a[k] != b[k]]


def _expected_io_objects(render: NodeRender) -> set[tuple[int, int]]:
    out = set()
    for p in render.io["points"]:
        try:
            out.add((enums.object_type_number(p["type"]), int(p["instance"])))
        except ValueError:
            continue
    return out


def _live_io_objects(objects: list[dict[str, Any]]) -> set[tuple[int, int]]:
    out = set()
    for o in objects:
        if o.get("owner") != "io":
            continue
        try:
            out.add((enums.object_type_number(o["type"]), int(o["instance"])))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def io_dependents(system: System, io_nodes: set[str]) -> dict[tuple[str, str], list[str]]:
    """Apps (``(node, app)``, generated uc-link instances included) that read
    or write an IO object of one of ``io_nodes``, with the objects concerned
    (``"sim-a/analog-input:1"``)."""
    io_keys = {n.name: {(t, i) for t, i, _ in n.io_objects()} for n in system.nodes
               if n.name in io_nodes}
    by_instance = {n.instance: n.name for n in system.nodes}
    out: dict[tuple[str, str], list[str]] = {}
    for node in system.nodes:
        for app in node_apps(system, node.name):
            hits = []
            for ref in app.point_refs():
                owner = node.name if ref.device is None else by_instance.get(ref.device)
                if owner in io_keys and ref.key in io_keys[owner]:
                    hits.append(f"{owner}/{enums.format_object_ref(*ref.key)}")
            if hits:
                out[(node.name, app.name)] = list(dict.fromkeys(hits))
    return out


def _reboot_hint(system: System, node: str, render: NodeRender, info: Mapping[str, Any]
                 ) -> str | None:
    dev = info.get("device", {}) if isinstance(info, Mapping) else {}
    net = info.get("net", {}) if isinstance(info, Mapping) else {}
    want = render.device
    reasons = []
    if isinstance(dev, Mapping) and "instance" in dev and \
            int(dev["instance"]) != int(want["device"]["instance"]):
        reasons.append(f"device instance {dev['instance']} -> {want['device']['instance']}")
    port = int(want.get("bacnet", {}).get("udp_port", 47808))
    if isinstance(net, Mapping) and "bacnet_port" in net and int(net["bacnet_port"]) != port:
        reasons.append(f"BACnet port {net['bacnet_port']} -> {port}")
    ipv4 = want.get("network", {}).get("ipv4")
    if ipv4 and want.get("network", {}).get("dhcp") is False and isinstance(net, Mapping) \
            and net.get("ipv4") and net.get("ipv4") != ipv4:
        reasons.append(f"IPv4 {net.get('ipv4')} -> {ipv4}")
    return ", ".join(reasons) if reasons else None


def plan(
    system: System,
    live: Mapping[str, LiveState],
    *,
    renders: Mapping[str, NodeRender] | None = None,
    artifacts: Mapping[tuple[str, str], AppArtifact] | None = None,
    prune: bool = False,
    addresses: Mapping[str, str] | None = None,
) -> Plan:
    """Compute the actions that bring the live nodes to the manifest's state.

    Args:
        live: live state by node name (nodes missing here are reported as
            unreachable).
        renders: rendered documents (default: rendered here with ``artifacts``).
        artifacts: built modules keyed ``(node, app)``; apps without an
            artifact cannot be deployed (a note is added).
        prune: remove apps that are installed on a node but not in the
            manifest.
    """
    arts = artifacts or {}
    if renders is None:
        renders = render_system(system, addresses=addresses or sim_address_plan(system),
                                artifacts=arts)
    p = Plan(system=system.name, node_order=system.node_names())
    for node in system.nodes:
        name = node.name
        st = live.get(name)
        render = renders[name]
        if st is None or not st.reachable:
            why = st.error if st else ("no connection (simulation not running: sim_start)"
                                       if node.is_sim else "no connection")
            p.notes.append(Note(name, f"node unreachable: {why}", "unreachable"))
            continue
        # device.json
        want = doc_sha256(render.device)
        have = st.config_sha256.get("device")
        if have != want:
            hint = _reboot_hint(system, name, render, st.info)
            reason = "missing on the node" if have is None else "content differs"
            p.actions.append(Action("push_config", name, "device", reason, PHASE_DEVICE,
                                    {"doc": render.device}))
            if hint:
                p.notes.append(Note(name, f"reboot required after device.json ({hint})",
                                    "reboot_required"))
        else:
            hint = _reboot_hint(system, name, render, st.info)
            if hint:
                p.notes.append(Note(name, f"device.json is current but the node still runs the "
                                    f"old settings ({hint}): reboot pending", "reboot_required"))
            _plan_clear_staged(p, st, name, "device", want, PHASE_DEVICE)
        # io.json
        want = doc_sha256(render.io)
        have = st.config_sha256.get("io")
        if have != want:
            reason = "missing on the node" if have is None else "content differs"
            p.actions.append(Action("push_config", name, "io", reason, PHASE_IO,
                                    {"doc": render.io}))
        else:
            _plan_clear_staged(p, st, name, "io", want, PHASE_IO)
            if st.objects is not None:
                missing = _expected_io_objects(render) - _live_io_objects(st.objects)
                if missing:
                    refs = ", ".join(sorted(enums.format_object_ref(*k) for k in missing))
                    p.actions.append(Action("reload", name, "io",
                                            f"io.json current but objects missing: {refs}",
                                            PHASE_IO))
        # apps.json: the node writes it itself on install/remove; a staged
        # one would replace the installed apps at the next reload or boot
        _plan_clear_staged(p, st, name, "apps", None, PHASE_APPS)
        # applications
        desired: dict[str, RenderedApp] = {a.name: a for a in render.app_list}
        live_cfg = {str(e.get("name")): e for e in st.apps_cfg}
        installed = set(live_cfg) | set(st.apps_status)
        for extra in sorted(installed - set(desired)):
            if prune:
                p.actions.append(Action("remove_app", name, extra, "not in the manifest",
                                        PHASE_APPS, {"delete_file": True}))
            else:
                p.notes.append(Note(name, f"app {extra!r} is installed but not in the manifest "
                                    "(apply with prune to remove it)", "warning"))
        for app_name, ra in desired.items():
            phase = PHASE_LINKS if ra.spec.generated else PHASE_APPS
            art = arts.get((name, app_name))
            if art is None:
                p.notes.append(Note(name, f"app {app_name!r} has no built module: not planned",
                                    "warning"))
                continue
            entry = dict(ra.entry)
            entry["sha256"] = art.sha256
            status = st.apps_status.get(app_name)
            cfg = live_cfg.get(app_name)
            file_sha = st.files_sha256.get(ra.file)
            data = {"artifact": art, "entry": entry, "upload": file_sha != art.sha256}
            if status is None and cfg is None:
                p.actions.append(Action("deploy_app", name, app_name, "not installed", phase,
                                        data))
            elif file_sha != art.sha256:
                why = "module missing" if file_sha is None else "module changed"
                p.actions.append(Action("deploy_app", name, app_name, why, phase, data))
            else:
                diff = entry_diff(entry, cfg if cfg is not None else status or {})
                if diff:
                    p.actions.append(Action("deploy_app", name, app_name,
                                            f"manifest changed ({', '.join(diff)})", phase, data))
                elif entry.get("autostart", True) and status is not None and \
                        status.get("state") not in ("running", "starting"):
                    err = status.get("last_error") or ""
                    p.actions.append(Action(
                        "start_app", name, app_name,
                        f"state {status.get('state')}" + (f": {err}" if err else ""), phase))
    _plan_restarts(system, p, renders, live)
    return p


def _plan_clear_staged(p: Plan, st: LiveState, node: str, doc: str, want: str | None,
                       phase: int) -> None:
    """``clear_staged`` for a staged ``<doc>.json.new`` that differs from the
    desired document (``want``; ``None``: any) while no ``push_config`` of
    that document replaces it: the node would activate it at the next reload
    or boot (including the reboot that :func:`apply` may do)."""
    staged = st.staged_sha256.get(doc)
    if staged is None or staged == want:
        return
    p.actions.append(Action("clear_staged", node, doc,
                            f"stale staged {doc}.json.new would be activated at the next "
                            "reload or boot", phase))


def _plan_restarts(system: System, p: Plan, renders: Mapping[str, NodeRender],
                   live: Mapping[str, LiveState]) -> None:
    """``restart_app`` for apps that depend on IO objects re-created by the
    planned io.json pushes/reloads (see the module docstring)."""
    io_nodes = {a.node for a in p.actions if a.target == "io" and a.kind in ("push_config",
                                                                               "reload")}
    if not io_nodes:
        return
    busy = {(a.node, a.target) for a in p.actions if a.kind in ("deploy_app", "start_app",
                                                                 "remove_app")}
    for (node, app_name), refs in io_dependents(system, io_nodes).items():
        st = live.get(node)
        if (node, app_name) in busy or st is None or not st.reachable:
            continue
        try:
            entry = renders[node].app(app_name).entry
        except (KeyError, HarnessError):
            continue
        status = st.apps_status.get(app_name)
        if not entry.get("autostart", True) or status is None:
            continue
        p.actions.append(Action(
            "restart_app", node, app_name,
            f"uses re-created IO objects {', '.join(refs)}", PHASE_LINKS,
            {"objects": refs}))


# --- apply ------------------------------------------------------------------------------------


@dataclass
class ActionResult:
    action: Action
    status: Literal["ok", "failed", "skipped", "dry-run"]
    detail: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = self.action.to_dict()
        out.update({"status": self.status, "error": self.error})
        if self.detail is not None:
            out["result"] = self.detail
        return out


@dataclass
class ApplyReport:
    system: str
    dry_run: bool
    results: list[ActionResult] = field(default_factory=list)
    reboot_required: list[str] = field(default_factory=list)
    rebooted: list[str] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return all(r.status != "failed" for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {"system": self.system, "ok": self.ok, "dry_run": self.dry_run,
                "results": [r.to_dict() for r in self.results],
                "reboot_required": self.reboot_required, "rebooted": self.rebooted,
                "notes": [n.to_dict() for n in self.notes],
                "duration_s": round(self.duration_s, 2)}


Rebooter = Callable[[str], Awaitable[Any]]


async def _execute(action: Action, node: Any) -> Any:
    if action.kind == "clear_staged":
        return await node.clear_staged(action.target)
    if action.kind == "push_config":
        return await node.push_config(action.target, action.data["doc"])
    if action.kind == "reload":
        return {"reboot_required": await node.reload(action.target)}
    if action.kind == "remove_app":
        return await node.remove_app(action.target,
                                     delete_file=action.data.get("delete_file", True))
    if action.kind == "start_app":
        return await node.start_app(action.target)
    if action.kind == "restart_app":
        res = await node.restart_app(action.target)
        return {"name": action.target, "state": (res or {}).get("state")}
    if action.kind == "deploy_app":
        art: AppArtifact = action.data["artifact"]
        e = action.data["entry"]
        res = await node.deploy_app(
            action.target, art.path.read_bytes(), aot=art.aot,
            autostart=e.get("autostart", True), period_ms=e.get("period_ms", 1000),
            heap_kb=e.get("heap_kb", 8), stack_kb=e.get("stack_kb", 4), perms=e.get("perms"),
            params=e.get("params"))
        if isinstance(res, dict):
            res = {k: v for k, v in res.items() if k != "status"} | {
                "state": (res.get("status") or {}).get("state")}
        return res
    raise HarnessError(f"unknown action {action.kind}")


async def wait_reachable(node: Any, timeout: float = 30.0, interval: float = 1.0) -> bool:
    """Poll ``node.info()`` until it answers or ``timeout`` expires."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            await node.info()
            return True
        except (HarnessError, OSError):
            await asyncio.sleep(interval)
    return False


async def apply(
    p: Plan,
    nodes: Mapping[str, Any],
    dry_run: bool = False,
    *,
    reboot: bool = False,
    rebooter: Rebooter | None = None,
    reboot_timeout: float = 30.0,
) -> ApplyReport:
    """Execute a plan (device -> io -> apps -> links, node by node within a
    phase). After a failed action the remaining actions of that node are
    skipped; other nodes continue.

    Args:
        reboot: reboot nodes whose reload reported ``reboot_required`` (and
            nodes with a pending reboot note) at the end, then wait until they
            answer again.
        rebooter: ``async (node_name)`` used instead of ``node.reboot()``
            (e.g. restart of a simulated node).
    """
    start = time.monotonic()
    report = ApplyReport(system=p.system, dry_run=dry_run, notes=list(p.notes))
    failed: set[str] = set()
    for action in p.ordered():
        node = nodes.get(action.node)
        if dry_run:
            report.results.append(ActionResult(action, "dry-run"))
            continue
        if node is None:
            report.results.append(ActionResult(action, "failed",
                                               error=f"no connection to node {action.node!r}"))
            failed.add(action.node)
            continue
        if action.node in failed:
            report.results.append(ActionResult(action, "skipped",
                                               error="an earlier action on this node failed"))
            continue
        try:
            detail = await _execute(action, node)
        except (HarnessError, OSError) as exc:
            report.results.append(ActionResult(action, "failed", error=str(exc)))
            failed.add(action.node)
            continue
        report.results.append(ActionResult(action, "ok", detail))
        if isinstance(detail, dict) and detail.get("reboot_required"):
            if action.node not in report.reboot_required:
                report.reboot_required.append(action.node)
    if dry_run:
        report.reboot_required = sorted({n.node for n in p.notes if n.kind == "reboot_required"})
    else:
        for note in p.notes:
            if note.kind == "reboot_required" and note.node not in report.reboot_required \
                    and note.node not in failed and "pending" in note.message:
                report.reboot_required.append(note.node)
    if reboot and not dry_run:
        for name in report.reboot_required:
            node = nodes.get(name)
            if node is None:
                continue
            try:
                if rebooter is not None:
                    await rebooter(name)
                else:
                    await node.reboot()
            except (HarnessError, OSError) as exc:
                report.notes.append(Note(name, f"reboot failed: {exc}", "warning"))
                continue
            await asyncio.sleep(0.5)
            if await wait_reachable(node, reboot_timeout):
                report.rebooted.append(name)
            else:
                report.notes.append(Note(name, f"not reachable {reboot_timeout:.0f} s after the "
                                         "reboot", "warning"))
    report.duration_s = time.monotonic() - start
    return report


async def plan_system(
    system: System,
    nodes: Mapping[str, Any],
    *,
    builder: ArtifactBuilder | None = None,
    artifacts: Mapping[tuple[str, str], AppArtifact] | None = None,
    prune: bool = False,
    addresses: Mapping[str, str] | None = None,
) -> tuple[Plan, dict[str, NodeRender], dict[tuple[str, str], AppArtifact],
           dict[str, LiveState]]:
    """Build (if needed), render, fetch the live state and plan in one call."""
    if artifacts is None:
        artifacts = (builder or ArtifactBuilder()).build_system(system)
    addr = dict(addresses) if addresses is not None else sim_address_plan(system)
    renders = render_system(system, addresses=addr, artifacts=artifacts)
    live = await fetch_live(system, nodes, renders)
    return (plan(system, live, renders=renders, artifacts=artifacts, prune=prune),
            renders, dict(artifacts), live)
