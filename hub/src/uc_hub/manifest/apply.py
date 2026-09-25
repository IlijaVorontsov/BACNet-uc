"""Applying an approved plan, verifying it, and rolling a target back.

Targets are applied in stages of at most ``max_devices_per_stage`` nodes
(concurrently within a stage), then the gateway. Before a target is touched
its current configuration is backed up; after its changes ran, the node is
read back and compared with what was sent. The first failure stops the
apply: the rest of that target and every later stage are reported as not
run, and the caller can offer ``rollback_target`` with the saved backup.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..core.errors import HubError, InvalidRequest, NotFound, Unsupported
from ..core.types import Change, ChangeResult, Plan
from ..drivers.bacnet_uc.api import NodeApi
from . import nodedocs
from .nodedocs import CFG_APPS, CFG_DEVICE, CFG_IO
from .plan import GATEWAY, NodeResolver, document_bytes

logger = logging.getLogger(__name__)

#: Errors expected to fail one change. Any other exception fails the change
#: too (logged with its traceback): raising out of a stage would leave the
#: stage's other targets being changed in the background, unreported.
_CHANGE_ERRORS = (HubError, OSError, TimeoutError, ValueError, KeyError, TypeError)
_EMPTY_DOCS: dict[str, dict[str, Any]] = {
    CFG_IO: {"schema": 1, "points": []},
    CFG_APPS: {"schema": 1, "apps": []},
}


class GatewayApplier(Protocol):
    """The gateway side of a plan (bridges and the building model)."""

    async def apply_bridges(self, bridges: list[dict[str, Any]]) -> None:
        """Replace the running bridge set with ``bridges`` (``Bridge.to_dict`` form)."""

    async def apply_tags(self, tags: dict[str, list[str]], safety: dict[str, str]) -> None:
        """Replace the manifest tags and safety classes (full point ids)."""


@dataclass(slots=True)
class TargetBackup:
    """What a target looked like before a plan touched it."""

    target: str
    plan_id: str
    #: Node file path -> content; None when the file did not exist.
    files: dict[str, bytes | None] = field(default_factory=dict)
    #: Gateway state the plan replaces: "bridges", and "tags" with "safety".
    gateway: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "files": {p: None if d is None else base64.b64encode(d).decode("ascii") for p, d in self.files.items()},
            "gateway": self.gateway,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TargetBackup:
        return cls(
            target=str(data["target"]),
            plan_id=str(data["plan_id"]),
            files={str(p): None if d is None else base64.b64decode(d, validate=True)
                   for p, d in (data.get("files") or {}).items()},
            gateway=dict(data.get("gateway") or {}),
            created_at=float(data.get("created_at", 0.0)),
        )


class BackupStore(Protocol):
    async def save(self, backup: TargetBackup) -> None:
        """Persist ``backup``; raising aborts the target before any change."""


class MemoryBackupStore:
    """BackupStore kept in memory (tests, simulation)."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], TargetBackup] = {}

    async def save(self, backup: TargetBackup) -> None:
        self._items[(backup.plan_id, backup.target)] = backup

    def get(self, plan_id: str, target: str) -> TargetBackup | None:
        return self._items.get((plan_id, target))

    def all(self) -> list[TargetBackup]:
        return list(self._items.values())


@dataclass(slots=True)
class ApplyProgress:
    target: str
    #: A change id, ``<target>/backup`` or ``<target>/verify``.
    step: str
    state: Literal["running", "ok", "failed", "not-run"]
    detail: str = ""


ProgressCallback = Callable[[ApplyProgress], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]


async def apply_plan(
    plan: Plan,
    nodes: NodeResolver,
    gateway: GatewayApplier | None,
    backups: BackupStore,
    on_progress: ProgressCallback | None = None,
    max_devices_per_stage: int = 5,
    *,
    verify_timeout_s: float = 10.0,
    poll_interval_s: float = 0.25,
    change_timeout_s: float = 120.0,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[ChangeResult]:
    """Execute ``plan`` and return one result per change, in plan order, plus a
    ``<target>/verify`` result after each node and a ``<target>/backup``
    result where the backup failed. Raises ``InvalidRequest`` (before touching
    anything) for a plan with blocked targets or gateway changes without a
    ``gateway``."""
    blocked = getattr(plan, "blocked", None)
    if blocked:
        raise InvalidRequest(f"plan {plan.id} cannot be applied; unreachable while planning: "
                             + ", ".join(f"{t} ({r})" for t, r in blocked.items()))
    if gateway is None and any(c.target == GATEWAY for c in plan.changes):
        raise InvalidRequest(f"plan {plan.id} has gateway changes but no gateway applier was given")
    runner = _Runner(plan, nodes, gateway, backups, on_progress, verify_timeout_s, poll_interval_s,
                     change_timeout_s, sleep, clock)
    return await runner.run(max(1, max_devices_per_stage))


class _Runner:
    def __init__(self, plan: Plan, nodes: NodeResolver, gateway: GatewayApplier | None,
                 backups: BackupStore, on_progress: ProgressCallback | None, verify_timeout_s: float,
                 poll_interval_s: float, change_timeout_s: float, sleep: Sleep,
                 clock: Callable[[], float]) -> None:
        self.plan = plan
        self.nodes = nodes
        self.gateway = gateway
        self.backups = backups
        self.on_progress = on_progress
        self.verify_timeout_s = verify_timeout_s
        self.poll_interval_s = poll_interval_s
        self.change_timeout_s = change_timeout_s
        self.sleep = sleep
        self.clock = clock

    async def emit(self, target: str, step: str, state: Literal["running", "ok", "failed", "not-run"],
                   detail: str = "") -> None:
        if self.on_progress is None:
            return
        try:
            result = self.on_progress(ApplyProgress(target, step, state, detail))
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("apply progress callback failed")

    def log_failure(self, error: Exception, target: str, step: str, detail: str) -> None:
        if isinstance(error, _CHANGE_ERRORS):
            logger.warning("apply %s: %s %s failed: %s", self.plan.id, target, step, detail)
        else:
            logger.error("apply %s: %s %s failed unexpectedly", self.plan.id, target, step, exc_info=error)

    async def run(self, per_stage: int) -> list[ChangeResult]:
        by_target: dict[str, list[Change]] = {}
        for c in self.plan.changes:
            by_target.setdefault(c.target, []).append(c)
        node_targets = [t for t in by_target if t != GATEWAY]
        stages = [node_targets[i:i + per_stage] for i in range(0, len(node_targets), per_stage)]
        if GATEWAY in by_target:
            stages.append([GATEWAY])
        logger.info("apply %s: %d changes on %d targets in %d stages", self.plan.id, len(self.plan.changes),
                    len(by_target), len(stages))
        results: dict[str, list[ChangeResult]] = {}
        failed = False
        for stage in stages:
            if failed:
                for target in stage:
                    results[target] = await self.not_run(target, by_target[target], "an earlier stage failed")
                continue
            outcomes = await asyncio.gather(*(self.apply_target(t, by_target[t]) for t in stage))
            for target, (target_results, ok) in zip(stage, outcomes, strict=True):
                results[target] = target_results
                failed = failed or not ok
        return [r for target in by_target for r in results[target]]

    async def not_run(self, target: str, changes: list[Change], why: str) -> list[ChangeResult]:
        out = []
        for c in changes:
            out.append(ChangeResult(c.id, False, f"not run: {why}"))
            await self.emit(target, c.id, "not-run", why)
        return out

    async def apply_target(self, target: str, changes: list[Change]) -> tuple[list[ChangeResult], bool]:
        if target == GATEWAY:
            return await self.apply_gateway(changes)
        results: list[ChangeResult] = []
        step = f"{target}/backup"
        await self.emit(target, step, "running")
        try:
            api = self.nodes(target)
            backup = await _backup_node(api, self.plan.id, target, changes)
            await self.backups.save(backup)
        except Exception as e:
            detail = f"backup failed: {_describe(e)}"
            self.log_failure(e, target, "backup", _describe(e))
            await self.emit(target, step, "failed", detail)
            results.append(ChangeResult(step, False, detail))
            results.extend(await self.not_run(target, changes, "the backup failed"))
            return results, False
        await self.emit(target, step, "ok")
        for i, change in enumerate(changes):
            await self.emit(target, change.id, "running", change.summary)
            try:
                async with asyncio.timeout(self.change_timeout_s):
                    detail = await _execute(api, change)
            except Exception as e:
                detail = _describe(e)
                self.log_failure(e, target, change.id, detail)
                await self.emit(target, change.id, "failed", detail)
                results.append(ChangeResult(change.id, False, detail))
                results.extend(await self.not_run(target, changes[i + 1:], f"{change.id} failed"))
                return results, False
            await self.emit(target, change.id, "ok", detail)
            results.append(ChangeResult(change.id, True, detail))
        step = f"{target}/verify"
        await self.emit(target, step, "running")
        try:
            async with asyncio.timeout(self.change_timeout_s + self.verify_timeout_s):
                problems = await self.verify_node(api, changes)
        except Exception as e:
            problems = [f"verification could not read the node: {_describe(e)}"]
            self.log_failure(e, target, step, problems[0])
        ok = not problems
        detail = "; ".join(problems) if problems else "node matches the plan"
        await self.emit(target, step, "ok" if ok else "failed", detail)
        results.append(ChangeResult(step, ok, detail))
        return results, ok

    async def verify_node(self, api: NodeApi, changes: list[Change]) -> list[str]:
        problems: list[str] = []
        for c in changes:
            if c.kind == "upload-doc":
                path, content = c.payload["path"], c.payload["content"]
                normalize = nodedocs.normalize_device if c.payload["doc"] == "device" else nodedocs.normalize_io
                try:
                    stored = json.loads((await api.file_download(path)).decode("utf-8"))
                except NotFound:
                    stored = None
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stored = "invalid"
                if not isinstance(stored, dict) or normalize(stored) != normalize(content):
                    problems.append(f"{path} on the node differs from the uploaded document")
            elif c.kind == "install-app":
                manifest = c.payload["manifest"]
                if manifest.get("autostart", True):
                    problem = await self.wait_running(api, manifest["name"])
                    if problem:
                        problems.append(problem)
        removed = [c.payload["name"] for c in changes if c.kind == "remove-app"]
        if removed:
            present = {s.get("name") for s in await api.app_list() if isinstance(s, dict)}
            problems.extend(f"app {name} is still installed" for name in removed if name in present)
        return problems

    async def wait_running(self, api: NodeApi, name: str) -> str | None:
        deadline = self.clock() + self.verify_timeout_s
        while True:
            status = await api.app_status(name)
            state = status.get("state")
            if state == "running":
                return None
            if state == "failed":
                return f"app {name} failed: {status.get('last_error') or 'no error text'}"
            if self.clock() >= deadline:
                return f"app {name} is {state} after {self.verify_timeout_s:g} s, not running"
            await self.sleep(self.poll_interval_s)

    async def apply_gateway(self, changes: list[Change]) -> tuple[list[ChangeResult], bool]:
        assert self.gateway is not None
        results: list[ChangeResult] = []
        step = f"{GATEWAY}/backup"
        backup = TargetBackup(GATEWAY, self.plan.id)
        for c in changes:
            if c.kind == "tags":
                backup.gateway["tags"] = c.payload["before"]["tags"]
                backup.gateway["safety"] = c.payload["before"]["safety"]
            elif c.kind.startswith("bridge-"):
                backup.gateway["bridges"] = c.payload["before_bridges"]
        try:
            await self.backups.save(backup)
        except Exception as e:
            detail = f"backup failed: {_describe(e)}"
            self.log_failure(e, GATEWAY, "backup", _describe(e))
            await self.emit(GATEWAY, step, "failed", detail)
            results.append(ChangeResult(step, False, detail))
            results.extend(await self.not_run(GATEWAY, changes, "the backup failed"))
            return results, False
        bridges_applied_by: str | None = None
        for i, change in enumerate(changes):
            await self.emit(GATEWAY, change.id, "running", change.summary)
            try:
                async with asyncio.timeout(self.change_timeout_s):
                    if change.kind == "tags":
                        await self.gateway.apply_tags(change.payload["tags"], change.payload["safety"])
                        detail = "tags and safety classes applied"
                    elif change.kind.startswith("bridge-"):
                        if bridges_applied_by is None:
                            await self.gateway.apply_bridges(change.payload["bridges"])
                            bridges_applied_by = change.id
                            detail = f"bridge set applied ({len(change.payload['bridges'])} bridges)"
                        else:
                            detail = f"applied with {bridges_applied_by}"
                    else:
                        raise Unsupported(f"{change.kind} is not a gateway change")
            except Exception as e:
                detail = _describe(e)
                self.log_failure(e, GATEWAY, change.id, detail)
                await self.emit(GATEWAY, change.id, "failed", detail)
                results.append(ChangeResult(change.id, False, detail))
                results.extend(await self.not_run(GATEWAY, changes[i + 1:], f"{change.id} failed"))
                return results, False
            await self.emit(GATEWAY, change.id, "ok", detail)
            results.append(ChangeResult(change.id, True, detail))
        return results, True


async def _download(api: NodeApi, path: str) -> bytes | None:
    try:
        return await api.file_download(path)
    except NotFound:
        return None


async def _backup_node(api: NodeApi, plan_id: str, target: str, changes: list[Change]) -> TargetBackup:
    backup = TargetBackup(target, plan_id)
    for path in (CFG_DEVICE, CFG_IO, CFG_APPS):
        backup.files[path] = await _download(api, path)
    for c in changes:
        if c.kind == "upload-file":
            backup.files[c.payload["path"]] = await _download(api, c.payload["path"])
    return backup


async def _execute(api: NodeApi, change: Change) -> str:
    p = change.payload
    if change.kind == "upload-file":
        data = base64.b64decode(p["data"], validate=True)
        if hashlib.sha256(data).hexdigest() != p["sha256"]:
            raise ValueError(f"the planned content of {p['path']} does not match its sha256")
        await api.file_upload(p["path"], data)
        return f"uploaded {len(data)} bytes"
    if change.kind == "upload-doc":
        data = document_bytes(p["content"])
        await api.file_upload(p["path"], data)
        return f"uploaded {len(data)} bytes"
    if change.kind == "reload":
        reboot = await api.reload(p["doc"])
        return "reloaded; the node reports that a reboot is required" if reboot else "reloaded"
    if change.kind == "remove-app":
        await api.app_remove(p["name"])
        return "removed"
    if change.kind == "install-app":
        manifest = dict(p["manifest"])
        if isinstance(manifest.get("sha256"), str):
            manifest["sha256"] = bytes.fromhex(manifest["sha256"])
        await api.app_install(manifest)
        return "installed"
    raise Unsupported(f"{change.kind} is not a node change")


def _describe(error: BaseException) -> str:
    text = str(error)
    if isinstance(error, TimeoutError) and not text:
        text = "timed out"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


async def rollback_target(
    backup: TargetBackup,
    nodes: NodeResolver,
    gateway: GatewayApplier | None = None,
    *,
    timeout_s: float = 300.0,
) -> ChangeResult:
    """Restore a target from its backup: for a node, the overwritten app
    modules and the three configuration documents, then ``reload all``; for
    the gateway, the previous bridge set, tags and safety classes."""
    step = f"{backup.target}/rollback"
    notes: list[str] = []
    try:
        async with asyncio.timeout(timeout_s):
            if backup.target == GATEWAY:
                if gateway is None:
                    raise InvalidRequest("a gateway applier is needed to roll back the gateway")
                if "tags" in backup.gateway:
                    await gateway.apply_tags(backup.gateway["tags"], backup.gateway["safety"])
                    notes.append("tags restored")
                if "bridges" in backup.gateway:
                    await gateway.apply_bridges(backup.gateway["bridges"])
                    notes.append(f"{len(backup.gateway['bridges'])} bridges restored")
            else:
                notes.extend(await _restore_node(nodes(backup.target), backup))
    except (*_CHANGE_ERRORS, LookupError) as e:
        detail = _describe(e)
        logger.warning("rollback %s of plan %s failed: %s", backup.target, backup.plan_id, detail)
        return ChangeResult(step, False, detail)
    return ChangeResult(step, True, "; ".join(notes) or "nothing to restore")


async def _restore_node(api: NodeApi, backup: TargetBackup) -> list[str]:
    notes: list[str] = []
    docs = (CFG_DEVICE, CFG_IO, CFG_APPS)
    for path, data in backup.files.items():
        if path not in docs and data is not None:
            await api.file_upload(path, data)
            notes.append(f"{path} restored")
    for path in docs:
        data = backup.files.get(path)
        if data is None and path in _EMPTY_DOCS:
            data = document_bytes(_EMPTY_DOCS[path])
        if data is None:
            notes.append(f"{path} did not exist before and was left in place")
            continue
        await api.file_upload(path, data)
        notes.append(f"{path} restored")
    if await api.reload("all"):
        notes.append("the node reports that a reboot is required")
    return notes
