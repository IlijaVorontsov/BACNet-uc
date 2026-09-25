"""Planning: the changes that bring the live site to a desired manifest.

Per BACnet-uc node the planner downloads ``device.json``, ``io.json`` and
``apps.json``, lists the apps and hashes their modules, and compares them
semantically with the manifest (``nodedocs.normalize_*``). The gateway
target covers bridges, tags and safety classes, compared with the live
manifest. Nothing is written anywhere: a plan is a proposal that a human
approves and ``apply.apply_plan`` executes.

Order of the changes of one node (``apply`` relies on it): upload-file,
upload-doc device, upload-doc io, reload, remove-app, install-app. Change ids
are ``c1``, ``c2``, ... over the whole plan, nodes in manifest order and the
gateway last, so the same inputs always give the same plan.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import HubError, InvalidRequest, NotFound, Unsupported
from ..core.types import Change, ChangeKind, Plan, SafetyClass
from ..drivers.bacnet_uc.api import NodeApi
from . import nodedocs
from .diff import canonical_json, json_diff, text_diff
from .load import SiteManifest
from .nodedocs import CFG_APPS, CFG_DEVICE, CFG_IO, DesiredApp
from .validate import is_relative_inside

logger = logging.getLogger(__name__)

GATEWAY = "gateway"
#: Resolves a node name to its management connection.
NodeResolver = Callable[[str], NodeApi]
#: Reads an app module given its path relative to the manifest directory.
FileResolver = Callable[[str], bytes]

MAX_MODULE_BYTES = 1024 * 1024
WASM_MAGIC = b"\0asm"
RUNNING_STATES = frozenset({"running", "starting"})
LIFE_SAFETY = SafetyClass.LIFE_SAFETY.value
#: Documents whose change needs a reboot to take effect (management-protocol.md, reload).
REBOOT_FIELDS = ("device.instance", "bacnet.udp_port", "network")
_SUMMARY_PATHS = 6


def directory_resolver(base_dir: Path, max_bytes: int = MAX_MODULE_BYTES) -> FileResolver:
    """A FileResolver for regular files below ``base_dir``; paths that leave
    it (``..``, absolute paths, symlinks pointing outside) are refused, and
    so are FIFOs and devices, whose reads could block the worker thread."""
    base = base_dir.resolve()

    def read(relative: str) -> bytes:
        if not is_relative_inside(relative):
            raise InvalidRequest(f"{relative!r} is not a relative path inside the manifest directory")
        path = (base / relative).resolve()
        if not path.is_relative_to(base):
            raise InvalidRequest(f"{relative!r} resolves outside the manifest directory")
        try:
            st = path.stat()
        except FileNotFoundError:
            raise NotFound(f"{relative} does not exist") from None
        if not stat.S_ISREG(st.st_mode):
            raise InvalidRequest(f"{relative} is not a regular file")
        if st.st_size > max_bytes:
            raise InvalidRequest(f"{relative} is {st.st_size} bytes; the limit is {max_bytes}")
        return path.read_bytes()

    return read


def document_bytes(doc: dict[str, Any]) -> bytes:
    """The bytes uploaded for a JSON document (compact, UTF-8)."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def compute_plan(
    desired: SiteManifest,
    live: SiteManifest | None,
    nodes: NodeResolver,
    files: FileResolver,
    uc_link_wasm: Path | str | None,
    revision: int,
    base_revision: int,
    *,
    plan_id: str | None = None,
    concurrency: int = 4,
    node_timeout_s: float = 60.0,
) -> Plan:
    """Diff ``desired`` against the live nodes and the ``live`` manifest.

    ``files`` reads app modules named in the manifest; ``uc_link_wasm`` is the
    stock uc-link module (links are skipped with a warning without it).
    Unreachable nodes get a warning and no changes, and are recorded in
    ``Plan.blocked``; ``apply_plan`` refuses such a plan.
    """
    plan = Plan(id=plan_id or f"p{revision}", revision=revision, base_revision=base_revision)
    link_module = await _read_link_module(uc_link_wasm, plan.warnings) if desired.links else None
    limit = asyncio.Semaphore(max(1, concurrency))

    async def run(name: str) -> _NodeResult:
        async with limit:
            planner = _NodePlanner(name, desired, nodes, files, link_module)
            return await planner.run(node_timeout_s)

    names = list(desired.nodes)
    results = await asyncio.gather(*(run(name) for name in names))
    changes: list[Change] = []
    for name, result in zip(names, results, strict=True):
        plan.warnings.extend(result.warnings)
        if result.blocked is not None:
            plan.blocked[name] = result.blocked
        else:
            changes.extend(result.changes)
    if live is not None:
        for name in live.nodes:
            if name not in desired.nodes:
                plan.warnings.append(f"{name}: no longer in the manifest; its configuration is left unchanged")
    changes.extend(_plan_gateway(desired, live, plan.warnings))
    for i, change in enumerate(changes, start=1):
        change.id = f"c{i}"
    plan.changes = changes
    return plan


async def _read_link_module(path: Path | str | None, warnings: list[str]) -> bytes | None:
    if path is None:
        return None
    try:
        data = await asyncio.to_thread(Path(path).read_bytes)
    except OSError as e:
        warnings.append(f"uc_link_wasm {path}: cannot be read ({e.strerror or e}); links are skipped")
        return None
    if not data.startswith(WASM_MAGIC):
        warnings.append(f"uc_link_wasm {path} is not a WebAssembly module; links are skipped")
        return None
    return data


@dataclass(slots=True)
class _NodeResult:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: str | None = None


@dataclass(slots=True)
class _LiveNode:
    info: dict[str, Any]
    raw: dict[str, bytes | None]
    statuses: dict[str, dict[str, Any]]


class _NodePlanner:
    def __init__(self, name: str, desired: SiteManifest, nodes: NodeResolver, files: FileResolver,
                 link_module: bytes | None) -> None:
        self.name = name
        self.desired = desired
        self.nodes = nodes
        self.files = files
        self.link_module = link_module
        self.result = _NodeResult()
        self._sha_cache: dict[str, str | None] = {}

    def warn(self, text: str) -> None:
        self.result.warnings.append(f"{self.name}: {text}")

    def change(self, kind: ChangeKind, summary: str, diff: str = "", **payload: Any) -> Change:
        return Change(id="", target=self.name, kind=kind, summary=summary, diff=diff, payload=payload)

    async def run(self, timeout_s: float) -> _NodeResult:
        try:
            api = self.nodes(self.name)
        except (KeyError, HubError) as e:
            return self._blocked(f"no management connection ({e})")
        try:
            async with asyncio.timeout(timeout_s):
                live = await self._read_live(api)
                await self._plan(api, live)
        except TimeoutError:
            return self._blocked(f"planning did not finish within {timeout_s:g} s")
        except HubError as e:
            return self._blocked(f"{type(e).__name__}: {e}")
        except Exception as e:
            # Whatever one node answers must not take the other nodes' plans down.
            logger.exception("plan: %s: unexpected error", self.name)
            return self._blocked(f"unexpected {type(e).__name__}: {e}")
        return self.result

    def _blocked(self, reason: str) -> _NodeResult:
        logger.warning("plan: %s unreachable: %s", self.name, reason)
        self.result.changes.clear()
        self.result.blocked = reason
        self.warn(f"unreachable: {reason}; no changes planned for it, and the plan cannot be applied "
                  "until it answers")
        return self.result

    async def _read_live(self, api: NodeApi) -> _LiveNode:
        info = await api.info()
        raw: dict[str, bytes | None] = {}
        for path in (CFG_DEVICE, CFG_IO, CFG_APPS):
            try:
                raw[path] = await api.file_download(path)
            except NotFound:
                raw[path] = None
        statuses = {
            str(s["name"]): s for s in await api.app_list() if isinstance(s, dict) and "name" in s
        }
        return _LiveNode(info if isinstance(info, dict) else {}, raw, statuses)

    def _live_doc(self, live: _LiveNode, path: str) -> tuple[dict[str, Any] | None, str | None]:
        """(parsed document, raw text when it is not a JSON object)."""
        raw = live.raw.get(path)
        if raw is None:
            return None, None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            doc = None
        if isinstance(doc, dict):
            return doc, None
        self.warn(f"{path} on the node is not a JSON object; it will be replaced")
        return None, raw.decode("utf-8", "replace")

    async def _plan(self, api: NodeApi, live: _LiveNode) -> None:
        uploads, installs, removes = await self._plan_apps(api, live)
        docs: list[Change] = []
        reloads: list[Change] = []
        device = self._plan_device(live)
        if device is not None:
            docs.append(device[0])
            reloads.append(device[1])
        io = self._plan_io(live)
        if io is not None:
            docs.append(io)
            reloads.append(self.change("reload", "reload io", doc="io", reboot_expected=False))
        self.result.changes = [*uploads, *docs, *reloads, *removes, *installs]

    # -- device.json -------------------------------------------------------------------
    def _plan_device(self, live: _LiveNode) -> tuple[Change, Change] | None:
        have, invalid = self._live_doc(live, CFG_DEVICE)
        want = nodedocs.merge_device_doc(self.desired.device_json(self.name), have)
        if have is not None and nodedocs.normalize_device(have) == nodedocs.normalize_device(want):
            self._check_running(live.info, want)
            return None
        before = have if have is not None else self._running_device(live.info)
        reboot = [
            f for f in REBOOT_FIELDS
            if _get(nodedocs.normalize_device(before), f) != _get(nodedocs.normalize_device(want), f)
        ]
        if invalid is not None:
            summary = "device.json: replaced (the node's copy is not valid JSON)"
            diff = text_diff(invalid, canonical_json(want), "device.json")
        elif have is None:
            summary = f"device.json: new (instance {want['device']['instance']}, {want['device']['name']!r})"
            diff = json_diff(None, want, "device.json")
        else:
            paths = nodedocs.changed_paths(nodedocs.normalize_device(have), nodedocs.normalize_device(want))
            summary = f"device.json: {_list(paths)} changed"
            diff = json_diff(have, want, "device.json")
        suffix = f" (reboot required: {', '.join(reboot)})" if reboot else ""
        if reboot:
            self.warn(f"device.json changes {', '.join(reboot)}; this takes effect after the node reboots, "
                      "which apply does not do")
        upload = self.change("upload-doc", summary + suffix, diff, doc="device", path=CFG_DEVICE, content=want)
        reload = self.change("reload", "reload device" + suffix, doc="device", reboot_expected=bool(reboot))
        return upload, reload

    def _check_running(self, info: dict[str, Any], want: dict[str, Any]) -> None:
        """A device.json that matches but is not in effect yet (an earlier apply
        changed the instance or port and the node was not rebooted)."""
        running = self._running_device(info)
        stored = nodedocs.normalize_device(want)
        stale = [f for f in ("device.instance", "bacnet.udp_port")
                 if _get(running, f) is not None and _get(running, f) != _get(stored, f)]
        if stale:
            self.warn(f"runs {', '.join(f'{f} {_get(running, f)}' for f in stale)}, but its device.json has "
                      f"{', '.join(str(_get(stored, f)) for f in stale)}; reboot the node to apply it")

    @staticmethod
    def _running_device(info: dict[str, Any]) -> dict[str, Any]:
        """What a node without device.json runs, from ``<node info>``."""
        out: dict[str, Any] = {}
        device = info.get("device")
        if isinstance(device, dict) and isinstance(device.get("instance"), int):
            out["device"] = {"instance": device["instance"], "name": str(device.get("name", ""))}
        net = info.get("net")
        if isinstance(net, dict) and isinstance(net.get("bacnet_port"), int):
            out["bacnet"] = {"udp_port": net["bacnet_port"]}
        return out

    # -- io.json -------------------------------------------------------------------
    def _plan_io(self, live: _LiveNode) -> Change | None:
        have, invalid = self._live_doc(live, CFG_IO)
        want = self.desired.io_json(self.name)
        want_points = nodedocs.normalize_io(want)["points"]
        after = {p["channel"]: p for p in want_points}
        if have is not None:
            have_points = nodedocs.normalize_io(have)["points"]
            if have_points == want_points:
                return None
            before = {nodedocs.io_point_key(p): p for p in have_points}
            added = [c for c in after if c not in before]
            removed = [c for c in before if c not in after]
            changed = [c for c in after if c in before and before[c] != after[c]]
            parts = [f"{sign}{len(items)} ({', '.join(items)})"
                     for sign, items in (("+", added), ("-", removed), ("~", changed)) if items]
            summary = f"io.json: {' '.join(parts) or 'duplicate channels removed'}"
            diff = json_diff(have, want, "io.json")
        else:
            summary = f"io.json: new, {len(after)} point{'s' if len(after) != 1 else ''}"
            diff = (text_diff(invalid, canonical_json(want), "io.json") if invalid is not None
                    else json_diff(None, want, "io.json"))
        return self.change("upload-doc", summary, diff, doc="io", path=CFG_IO, content=want)

    # -- apps -----------------------------------------------------------------------
    def _live_apps(self, live: _LiveNode) -> dict[str, dict[str, Any]]:
        doc, _ = self._live_doc(live, CFG_APPS)
        entries = (doc or {}).get("apps")
        return {
            str(e["name"]): e for e in entries if isinstance(e, dict) and "name" in e
        } if isinstance(entries, list) else {}

    async def _module(self, app: DesiredApp) -> bytes | None:
        if app.link:
            return self.link_module
        if app.wasm is None:
            self.warn(f"app {app.name}: building from C source ({app.source}) is not available yet; skipped")
            return None
        try:
            data = await asyncio.to_thread(self.files, app.wasm)
        except (HubError, OSError) as e:
            self.warn(f"app {app.name}: cannot read {app.wasm} ({e}); skipped")
            return None
        if not app.file.endswith(".aot") and not data.startswith(WASM_MAGIC):
            self.warn(f"app {app.name}: {app.wasm} is not a WebAssembly module; skipped")
            return None
        return data

    async def _remote_sha(self, api: NodeApi, path: str, entries: dict[str, dict[str, Any]]) -> str | None:
        if path in self._sha_cache:
            return self._sha_cache[path]
        try:
            digest = await api.file_sha256(path)
            value = digest.hex() if digest else None
        except Unsupported:
            recorded = [e.get("sha256") for e in entries.values() if e.get("file") == path]
            value = next((s.lower() for s in recorded if isinstance(s, str)), None)
        self._sha_cache[path] = value
        return value

    async def _plan_apps(self, api: NodeApi, live: _LiveNode) -> tuple[list[Change], list[Change], list[Change]]:
        entries = self._live_apps(live)
        uploads: dict[str, Change] = {}
        installs: list[Change] = []
        desired = self.desired.apps(self.name)
        skipped_links = 0
        for app in desired:
            data = await self._module(app)
            if data is None:
                if app.link:
                    skipped_links += len(app.link_indexes)
                continue
            sha = hashlib.sha256(data).hexdigest()
            remote = await self._remote_sha(api, app.file, entries)
            new_module = remote != sha
            if new_module and app.file not in uploads:
                origin = "stock uc-link" if app.link else app.wasm
                uploads[app.file] = self.change(
                    "upload-file",
                    f"upload {app.file} ({len(data)} bytes) for app {app.name}",
                    f"{app.file}: sha256 {remote or '(none)'} -> {sha}\n",
                    path=app.file, sha256=sha, size=len(data), source=origin,
                    data=base64.b64encode(data).decode("ascii"),
                )
            install = self._plan_install(app, sha, new_module, entries.get(app.name),
                                         live.statuses.get(app.name))
            if install is not None:
                installs.append(install)
        if skipped_links:
            self.warn(f"{skipped_links} link{'s' if skipped_links != 1 else ''} skipped: uc_link_wasm is not "
                      "configured (drivers.bacnet_uc.uc_link_wasm)")
        wanted = {app.name for app in desired}
        removes = [
            self.change("remove-app", f"remove app {name}", json_diff(nodedocs.normalize_app(
                entries.get(name) or live.statuses[name]), None, f"apps/{name}"), name=name)
            for name in sorted((set(entries) | set(live.statuses)) - wanted)
        ]
        return list(uploads.values()), installs, removes

    def _plan_install(self, app: DesiredApp, sha: str, new_module: bool, entry: dict[str, Any] | None,
                      status: dict[str, Any] | None) -> Change | None:
        manifest = {**app.manifest, "sha256": sha}
        want = nodedocs.normalize_app(manifest)
        current = entry or status
        if current is None:
            summary = f"install app {app.name}"
            diff = json_diff(None, want, f"apps/{app.name}")
        else:
            have = nodedocs.normalize_app(current)
            if have["sha256"] is None and not new_module:
                have["sha256"] = sha
            paths = [p for p in nodedocs.changed_paths(have, want) if p != "sha256"]
            if not paths and not new_module:
                if status is not None and status.get("state") == "failed" and want["autostart"]:
                    self.warn(f"app {app.name} is failed ({status.get('last_error') or 'no error text'}); "
                              "the plan does not change it")
                return None
            what = [_list(paths) + " changed"] if paths else []
            if new_module:
                what.append("new module")
            summary = f"update app {app.name}: {', '.join(what)}"
            diff = json_diff(have, want, f"apps/{app.name}")
            if status is not None and status.get("state") in RUNNING_STATES:
                manifest["restart"] = True
                summary += " (restart)"
        if not app.link:
            for perm in ("io", "bacnet.remote"):
                if perm in want["perms"]:
                    self.warn(f"app {app.name} requests permission {perm}")
        return self.change("install-app", summary, diff, manifest=manifest)


# -- gateway ------------------------------------------------------------------------
def _plan_gateway(desired: SiteManifest, live: SiteManifest | None, warnings: list[str]) -> list[Change]:
    changes: list[Change] = []
    want_tags, want_safety = desired.tags, {k: v.value for k, v in desired.safety.items()}
    have_tags = live.tags if live is not None else {}
    have_safety = {k: v.value for k, v in live.safety.items()} if live is not None else {}
    if _tag_form(want_tags) != _tag_form(have_tags) or want_safety != have_safety:
        tag_points = sorted(k for k in set(want_tags) | set(have_tags)
                            if sorted(set(want_tags.get(k, []))) != sorted(set(have_tags.get(k, []))))
        safety_points = sorted(k for k in set(want_safety) | set(have_safety)
                               if want_safety.get(k) != have_safety.get(k))
        parts = [f"{what}: {len(points)} point{'s' if len(points) != 1 else ''}"
                 for what, points in (("tags", tag_points), ("safety", safety_points)) if points]
        # Only a site admin may add or remove a life-safety mark (DESIGN.md section 10,
        # rule 1): the approval gate reads payload["life_safety"].
        life_safety = {
            "added": [k for k in safety_points if want_safety.get(k) == LIFE_SAFETY],
            "removed": [k for k in safety_points if have_safety.get(k) == LIFE_SAFETY],
        }
        for verb, points in life_safety.items():
            if points:
                warnings.append(f"{GATEWAY}: life-safety mark {verb} for {', '.join(points)}; "
                                "only a site admin may approve this")
        changes.append(Change(
            id="", target=GATEWAY, kind="tags", summary=", ".join(parts),
            diff=json_diff({"safety": have_safety, "tags": have_tags},
                           {"safety": want_safety, "tags": want_tags}, "tags.json"),
            payload={"tags": want_tags, "safety": want_safety,
                     "before": {"tags": have_tags, "safety": have_safety}, "life_safety": life_safety},
        ))
    want = {str(b.dest): b.to_dict() for b in desired.bridges}
    have = {str(b.dest): b.to_dict() for b in live.bridges} if live is not None else {}
    common = {"bridges": list(want.values()), "before_bridges": list(have.values())}
    for dest in sorted(set(have) - set(want)):
        b = have[dest]
        changes.append(Change(
            id="", target=GATEWAY, kind="bridge-remove", summary=f"remove bridge {b['from']} -> {dest}",
            diff=json_diff(b, None, f"bridges/{dest}"), payload={"bridge": b, **common}))
    for dest in sorted(set(have) & set(want)):
        if have[dest] != want[dest]:
            paths = nodedocs.changed_paths(have[dest], want[dest])
            changes.append(Change(
                id="", target=GATEWAY, kind="bridge-update",
                summary=f"update bridge {want[dest]['from']} -> {dest}: {_list(paths)} changed",
                diff=json_diff(have[dest], want[dest], f"bridges/{dest}"),
                payload={"bridge": want[dest], "before": have[dest], **common}))
    for dest in sorted(set(want) - set(have)):
        b = want[dest]
        changes.append(Change(
            id="", target=GATEWAY, kind="bridge-add", summary=f"add bridge {b['from']} -> {dest}",
            diff=json_diff(None, b, f"bridges/{dest}"), payload={"bridge": b, **common}))
    return changes


def _tag_form(tags: dict[str, list[str]]) -> dict[str, list[str]]:
    return {k: sorted(set(v)) for k, v in tags.items() if v}


def _get(doc: dict[str, Any], dotted: str) -> Any:
    current: Any = doc
    for key in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _list(items: list[str]) -> str:
    if len(items) <= _SUMMARY_PATHS:
        return ", ".join(items)
    return ", ".join(items[:_SUMMARY_PATHS]) + f" and {len(items) - _SUMMARY_PATHS} more"
