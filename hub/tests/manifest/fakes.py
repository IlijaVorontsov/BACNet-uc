"""In-memory stand-ins for a BACnet-uc node, the gateway and a test target."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Any

from uc_hub.core.errors import DeviceError, DeviceTimeout, NotFound, Unsupported
from uc_hub.core.ids import PointRef
from uc_hub.core.types import Value
from uc_hub.drivers.bacnet_uc.api import GROUP_UC_APP, NodeApi

WASM = b"\0asm\x01\0\0\0"
APPS_JSON = "/lfs/cfg/apps.json"


def wasm(tag: str) -> bytes:
    """A distinct minimal module per tag."""
    return WASM + tag.encode()


class FakeNode(NodeApi):
    """A BACnet-uc node's files and apps, with SMP semantics that matter to
    the manifest engine (apps.json kept by install/remove, reload of apps)."""

    def __init__(self, name: str = "r204-ctl", instance: int = 2041, *, bacnet_port: int = 47808,
                 sha_supported: bool = True) -> None:
        self.address = f"fake:{name}"
        self.name = name
        self.instance = instance
        self.bacnet_port = bacnet_port
        self.sha_supported = sha_supported
        self.files: dict[str, bytes] = {}
        self.apps: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.unreachable = False
        #: method name -> exception raised by the next call(s) of it
        self.fail: dict[str, Exception] = {}
        #: apps that go to state "failed" when started
        self.broken: set[str] = set()
        #: apps that stay in "starting"
        self.slow: set[str] = set()
        self.reboot_required = False
        #: file path -> content the node stores instead of an upload (to break verification)
        self.corrupt: dict[str, bytes] = {}

    # -- helpers ----------------------------------------------------------------------
    def _call(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))
        if self.unreachable:
            raise DeviceTimeout(f"{self.name}: no answer")
        if method in self.fail:
            raise self.fail[method]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def put_json(self, path: str, doc: Any) -> None:
        self.files[path] = json.dumps(doc).encode()

    def get_json(self, path: str) -> Any:
        return json.loads(self.files[path])

    def _state(self, name: str, autostart: bool) -> str:
        if not autostart:
            return "stopped"
        if name in self.broken:
            return "failed"
        return "starting" if name in self.slow else "running"

    def _write_apps_json(self) -> None:
        entries = []
        for app in self.apps.values():
            entry = {k: copy.deepcopy(app[k]) for k in ("name", "file", "autostart", "period_ms", "heap_kb",
                                                        "stack_kb", "perms")}
            entry["params"] = [{"key": k, "value": v} for k, v in app["params"].items()]
            if app.get("sha256"):
                entry["sha256"] = app["sha256"]
            entries.append(entry)
        self.put_json(APPS_JSON, {"schema": 1, "apps": entries})

    def install_direct(self, manifest: dict[str, Any], data: bytes, state: str | None = None) -> None:
        """Put an app on the node as if it had been installed earlier."""
        self.files[manifest["file"]] = data
        self._install(manifest)
        if state is not None:
            self.apps[manifest["name"]]["state"] = state

    def _install(self, manifest: dict[str, Any]) -> None:
        sha = manifest.get("sha256")
        if isinstance(sha, bytes):
            sha = sha.hex()
        autostart = manifest.get("autostart", True)
        self.apps[manifest["name"]] = {
            "name": manifest["name"], "file": manifest["file"], "autostart": autostart,
            "period_ms": manifest.get("period_ms", 1000), "heap_kb": manifest.get("heap_kb", 8),
            "stack_kb": manifest.get("stack_kb", 4), "perms": list(manifest.get("perms", [])),
            "params": dict(manifest.get("params", {})), "sha256": sha,
            "state": self._state(manifest["name"], autostart), "last_error": "",
        }
        if self.apps[manifest["name"]]["state"] == "failed":
            self.apps[manifest["name"]]["last_error"] = "trap: unreachable"
        self._write_apps_json()

    def _status(self, app: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": app["name"], "file": app["file"], "state": app["state"], "autostart": app["autostart"],
            "period_ms": app["period_ms"], "heap_kb": app["heap_kb"], "stack_kb": app["stack_kb"],
            "perms": list(app["perms"]), "ticks": 0, "events": 0, "errors": 0,
            "last_error": app["last_error"], "uptime_ms": 0,
        }

    # -- OS / FS -------------------------------------------------------------------------
    async def echo(self, text: str) -> str:
        self._call("echo", text)
        return text

    async def reset(self) -> None:
        self._call("reset")

    async def file_upload(self, path: str, data: bytes) -> None:
        self._call("file_upload", path)
        self.files[path] = self.corrupt.get(path, data)

    async def file_download(self, path: str) -> bytes:
        self._call("file_download", path)
        if path not in self.files:
            raise NotFound(f"{path}: no such file")
        return self.files[path]

    async def file_sha256(self, path: str) -> bytes | None:
        self._call("file_sha256", path)
        if not self.sha_supported:
            raise Unsupported("fs hash not supported")
        data = self.files.get(path)
        return hashlib.sha256(data).digest() if data is not None else None

    # -- uc_node ---------------------------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        self._call("info")
        return {
            "fw": "0.1.0", "board": "native_sim", "api": 65536,
            "device": {"instance": self.instance, "name": self.name},
            "net": {"ipv4": "127.0.0.1", "bacnet_port": self.bacnet_port},
            "apps": {"installed": len(self.apps), "running": 0},
        }

    async def reload(self, doc: str) -> bool:
        self._call("reload", doc)
        if doc in ("apps", "all") and APPS_JSON in self.files:
            entries = self.get_json(APPS_JSON)["apps"]
            self.apps = {}
            for e in entries:
                manifest = dict(e)
                manifest["params"] = {p["key"]: p["value"] for p in e.get("params", [])}
                self._install(manifest)
        return self.reboot_required

    async def objects(self) -> list[dict[str, Any]]:
        self._call("objects")
        return []

    async def prop_read(self, obj_type: str | int, instance: int, prop: str | int = "present-value",
                        index: int | None = None) -> Any:
        self._call("prop_read", obj_type, instance, prop)
        return 0.0

    async def prop_write(self, obj_type: str | int, instance: int, value: Any,
                         prop: str | int = "present-value", priority: int | None = None,
                         index: int | None = None, lease_ms: int | None = None) -> None:
        self._call("prop_write", obj_type, instance, value, prop, priority)

    async def identify(self, seconds: int = 30) -> None:
        self._call("identify", seconds)

    # -- uc_io -------------------------------------------------------------------------------
    async def io_catalog(self) -> dict[str, Any]:
        self._call("io_catalog")
        return {"board": "native_sim", "channels": []}

    async def io_read(self, name: str | None = None) -> dict[str, float]:
        self._call("io_read", name)
        return {}

    async def io_write(self, name: str, value: float) -> None:
        self._call("io_write", name, value)

    async def io_force(self, name: str, value: float | None, lease_ms: int | None = None) -> None:
        self._call("io_force", name, value)

    # -- uc_app --------------------------------------------------------------------------------
    async def app_list(self) -> list[dict[str, Any]]:
        self._call("app_list")
        return [self._status(a) for a in self.apps.values()]

    async def app_install(self, manifest: dict[str, Any]) -> None:
        self._call("app_install", copy.deepcopy(manifest))
        data = self.files.get(manifest["file"])
        if data is None:
            raise DeviceError(f"{manifest['file']} not found", rc=3, group=GROUP_UC_APP)
        sha = manifest.get("sha256")
        if sha is not None and hashlib.sha256(data).digest() != sha:
            raise DeviceError("sha256 mismatch", rc=9, group=GROUP_UC_APP)
        current = self.apps.get(manifest["name"])
        if current is not None and current["state"] == "running" and not manifest.get("restart"):
            raise DeviceError(f"app {manifest['name']} is running", rc=8, group=GROUP_UC_APP)
        self._install(manifest)

    async def app_start(self, name: str) -> None:
        self._call("app_start", name)

    async def app_stop(self, name: str) -> None:
        self._call("app_stop", name)

    async def app_remove(self, name: str, delete_file: bool = False) -> None:
        self._call("app_remove", name)
        if name not in self.apps:
            raise DeviceError(f"no app {name}", rc=3, group=GROUP_UC_APP)
        del self.apps[name]
        self._write_apps_json()

    async def app_status(self, name: str) -> dict[str, Any]:
        self._call("app_status", name)
        if name not in self.apps:
            raise DeviceError(f"no app {name}", rc=3, group=GROUP_UC_APP)
        app = self.apps[name]
        if app["state"] == "starting" and name not in self.slow:
            app["state"] = "running"
        return self._status(app)


class Nodes:
    """NodeResolver over a dict of fakes."""

    def __init__(self, *nodes: FakeNode) -> None:
        self.nodes = {n.name: n for n in nodes}

    def __call__(self, name: str) -> NodeApi:
        return self.nodes[name]


class FakeGateway:
    def __init__(self) -> None:
        self.bridges: list[dict[str, Any]] = []
        self.tags: dict[str, list[str]] = {}
        self.safety: dict[str, str] = {}
        self.calls: list[str] = []
        self.fail: Exception | None = None

    async def apply_bridges(self, bridges: list[dict[str, Any]]) -> None:
        self.calls.append("bridges")
        if self.fail is not None:
            raise self.fail
        self.bridges = copy.deepcopy(bridges)

    async def apply_tags(self, tags: dict[str, list[str]], safety: dict[str, str]) -> None:
        self.calls.append("tags")
        self.tags = copy.deepcopy(tags)
        self.safety = dict(safety)


class FakeClock:
    """Manual monotonic time; ``sleep`` advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class FakeTarget:
    """A room: ai0 raw mV -> analog-input:1 (scale 0.1, offset -50); the
    "valve" analog-output:1 follows the temperature once ``respond`` is set."""

    def __init__(self, clock: FakeClock, site: str = "hq") -> None:
        self.clock = clock
        self.site = site
        self.values: dict[tuple[str, str], Value] = {}
        self.forces: dict[tuple[str, str], float] = {}
        self.priorities: dict[tuple[str, str, int], Value] = {}
        self.log: list[tuple[Any, ...]] = []
        self.fail_read: Exception | None = None
        self.fail_force_release: Exception | None = None
        #: seconds after a cold force before the valve opens
        self.valve_delay_s = 1.0
        self._cold_since: float | None = None

    async def read(self, point: PointRef, prop: str = "present-value") -> Value:
        self.log.append(("read", str(point), prop))
        if self.fail_read is not None:
            raise self.fail_read
        key = (point.device, point.obj)
        if key == ("r204-ctl", "analog-input:1") and (point.device, "ai0") in self.forces:
            return self.forces[(point.device, "ai0")] * 0.1 - 50
        if key == ("r204-ctl", "analog-output:1"):
            cold = self._cold_since is not None and self.clock.now - self._cold_since >= self.valve_delay_s
            return 80.0 if cold else 0.0
        commanded = [v for (d, o, _), v in sorted(self.priorities.items(), key=lambda kv: kv[0][2])
                     if (d, o) == key]
        return commanded[0] if commanded else self.values.get(key, 0.0)

    async def write(self, point: PointRef, value: Value, prop: str = "present-value",
                    priority: int | None = None) -> None:
        self.log.append(("write", str(point), value, prop, priority))
        if priority is None:
            self.values[(point.device, point.obj)] = value
        elif value is None:
            self.priorities.pop((point.device, point.obj, priority), None)
        else:
            self.priorities[(point.device, point.obj, priority)] = value

    async def force(self, node: str, channel: str, value: float | None) -> None:
        self.log.append(("force", node, channel, value))
        if value is None:
            if self.fail_force_release is not None:
                raise self.fail_force_release
            self.forces.pop((node, channel), None)
            self._cold_since = None
        else:
            self.forces[(node, channel)] = value
            if value * 0.1 - 50 < 18:
                self._cold_since = self.clock.now
