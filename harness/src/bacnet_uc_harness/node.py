# SPDX-License-Identifier: Apache-2.0
"""High-level operations on one BACnet-uc node.

A :class:`Node` owns (lazily) an :class:`~bacnet_uc_harness.smp.client.SmpClient`
for the management interface and uses a
:class:`~bacnet_uc_harness.bacnet.client.BacnetClient` (its own or a shared
one) for BACnet/IP. Configuration documents and application modules are
compared by SHA-256 before uploading, so repeated deployments only transfer
what changed.

Configuration documents are *staged* (docs/management-protocol.md, "Staged
documents"): :meth:`Node.push_config` uploads ``/lfs/cfg/<doc>.json.new`` and
sends ``uc_node reload``; the firmware validates the staged file and renames
it over the active document in one LittleFS commit, or deletes it and keeps
the running configuration (rc ``INVALID``). An interrupted upload therefore
never leaves a half-written active document behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from bacnet_uc_harness.bacnet import codec as bn_codec
from bacnet_uc_harness.bacnet import enums
from bacnet_uc_harness.bacnet.client import BacnetClient, parse_address
from bacnet_uc_harness.errors import BacnetError, HarnessError, HarnessTimeout, SmpError
from bacnet_uc_harness.manifest import ManifestError, apps_using_objects, validate_document
from bacnet_uc_harness.render import (
    APPS_DIR,
    DOC_NAMES,
    LOG_DIR,
    STAGED_SUFFIX,
    doc_bytes,
    doc_path,
)
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.client import (
    SmpClient,
    connect_serial,
    connect_udp,
    normalize_manifest,
)
from bacnet_uc_harness.smp.codec import encode_frame

NOT_FOUND_RC = frozenset({"NOT_FOUND", "FILE_NOT_FOUND", "ENOENT"})
_LOG_NAME = re.compile(r"^log\.(\d{4})$")
LOG_PROBE_MAX = 64


def is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, SmpError) and exc.rc_name in NOT_FOUND_RC


def jsonable(value: Any) -> Any:
    """JSON-friendly form of a BACnet or SMP value."""
    return bn_codec.to_jsonable(value)


def parse_object(obj: str | tuple[str | int, int]) -> tuple[int, int]:
    try:
        return enums.parse_object_ref(obj)
    except ValueError as exc:
        raise HarnessError(str(exc)) from None


@dataclass(frozen=True)
class SmpTarget:
    """Where a node's SMP server is: UDP ``host:port`` or a serial console."""

    kind: Literal["udp", "serial"]
    host: str | None = None
    port: int = 1337
    device: str | None = None
    baud: int = 115200

    @classmethod
    def parse(cls, spec: str | SmpTarget | Mapping[str, Any]) -> SmpTarget:
        """``udp:<host>[:<port>]``, ``serial:<device>[:<baud>]``, ``<host>[:<port>]``,
        ``/dev/...`` or an inventory-style mapping."""
        if isinstance(spec, SmpTarget):
            return spec
        if isinstance(spec, Mapping):
            kind = spec.get("transport", spec.get("kind", "udp"))
            if kind == "serial":
                return cls("serial", device=str(spec["device"]), baud=int(spec.get("baud", 115200)))
            return cls("udp", host=str(spec["host"]), port=int(spec.get("port", 1337)))
        text = str(spec).strip()
        if text.startswith("serial:") or text.startswith("/dev/") or text.upper().startswith(
                "COM"):
            rest = text.removeprefix("serial:")
            dev, sep, baud = rest.rpartition(":")
            if sep and baud.isdigit():
                return cls("serial", device=dev, baud=int(baud))
            return cls("serial", device=rest)
        rest = text.removeprefix("udp:").removeprefix("sim:")
        host, sep, port = rest.rpartition(":")
        if sep and port.isdigit():
            return cls("udp", host=host, port=int(port))
        return cls("udp", host=rest)

    def __str__(self) -> str:
        if self.kind == "serial":
            return f"serial:{self.device}:{self.baud}"
        return f"udp:{self.host}:{self.port}"


class Node:
    """A BACnet-uc node reachable over SMP (and BACnet/IP).

    Args:
        name: node name (inventory / manifest).
        smp_target: see :meth:`SmpTarget.parse`.
        bacnet_address: ``host[:port]`` of the node's BACnet/IP interface;
            ``None`` disables the BACnet operations (SMP fallbacks remain).
        smp_retries: SMP retransmissions per request (default: the client's).
        bacnet: shared BACnet client; a private one (ephemeral port) is
            created on first use otherwise.
    """

    def __init__(self, name: str, smp_target: str | SmpTarget | Mapping[str, Any],
                 bacnet_address: str | tuple[str, int] | None = None, *,
                 board: str | None = None, smp_timeout: float = 5.0,
                 smp_retries: int | None = None,
                 bacnet: BacnetClient | None = None) -> None:
        self.name = name
        self.target = SmpTarget.parse(smp_target)
        self.bacnet_address: tuple[str, int] | None = (
            parse_address(bacnet_address) if bacnet_address else None)
        self.board = board
        self.smp_timeout = smp_timeout
        self.smp_retries = smp_retries
        self._smp: SmpClient | None = None
        self._bacnet = bacnet
        self._own_bacnet = bacnet is None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"Node({self.name!r}, {self.target}, bacnet={self.bacnet_address})"

    # --- connections --------------------------------------------------------------------

    async def connect(self) -> Node:
        await self.smp()
        return self

    async def close(self) -> None:
        if self._smp is not None:
            await self._smp.close()
            self._smp = None
        if self._bacnet is not None and self._own_bacnet:
            await self._bacnet.close()
            self._bacnet = None

    async def __aenter__(self) -> Node:
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def smp(self) -> SmpClient:
        if self._smp is None:
            if self.target.kind == "serial":
                self._smp = await connect_serial(str(self.target.device), self.target.baud)
            else:
                self._smp = await connect_udp(str(self.target.host), self.target.port,
                                              timeout=self.smp_timeout)
            if self.smp_retries is not None:
                self._smp.retries = self.smp_retries
        return self._smp

    async def bacnet_client(self) -> BacnetClient:
        if self._bacnet is None:
            self._bacnet = BacnetClient()
            self._own_bacnet = True
        await self._bacnet.start()
        return self._bacnet

    def _require_bacnet(self) -> tuple[str, int]:
        if self.bacnet_address is None:
            raise HarnessError(f"node {self.name!r} has no BACnet/IP address configured")
        return self.bacnet_address

    # --- node --------------------------------------------------------------------------------

    async def info(self) -> dict[str, Any]:
        return await (await self.smp()).node_info()

    async def echo(self, text: str = "ping") -> str:
        return await (await self.smp()).os_echo(text)

    async def reboot(self) -> None:
        await (await self.smp()).os_reset()

    async def shell(self, argv: list[str]) -> tuple[int, str]:
        return await (await self.smp()).shell_exec(argv)

    async def objects(self) -> list[dict[str, Any]]:
        smp = await self.smp()
        return [{k: jsonable(v) for k, v in o.items()} for o in await smp.node_objects_all()]

    # --- files -------------------------------------------------------------------------------

    async def file_sha256(self, path: str) -> str | None:
        """Hex SHA-256 of a file on the node, ``None`` if it does not exist."""
        smp = await self.smp()
        try:
            return (await smp.fs_hash(path, "sha256")).hex()
        except SmpError as exc:
            if is_not_found(exc):
                return None
            if exc.rc_name == "FILE_EMPTY":
                return hashlib.sha256(b"").hexdigest()
            raise

    async def download(self, path: str) -> bytes | None:
        smp = await self.smp()
        try:
            return await smp.fs_download(path)
        except SmpError as exc:
            if is_not_found(exc):
                return None
            raise

    async def upload(self, path: str, data: bytes, *, force: bool = False) -> dict[str, Any]:
        """Upload unless the node already has identical content."""
        sha = hashlib.sha256(data).hexdigest()
        if not force and await self.file_sha256(path) == sha:
            return {"path": path, "uploaded": False, "sha256": sha, "size": len(data)}
        async with self._lock:
            await (await self.smp()).fs_upload(path, data)
        return {"path": path, "uploaded": True, "sha256": sha, "size": len(data)}

    # --- configuration --------------------------------------------------------------------

    async def config_sha256(self, doc: str) -> str | None:
        return await self.file_sha256(doc_path(doc))

    async def get_config(self, doc: str) -> dict[str, Any] | None:
        """Parsed ``/lfs/cfg/<doc>.json``; ``None`` if the node has none (it runs
        on defaults then)."""
        raw = await self.download(doc_path(doc))
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError(f"{self.name}: {doc_path(doc)} is not valid JSON: {exc}") from exc

    async def reload(self, doc: str = "all") -> bool:
        """``uc_node reload``; returns ``reboot_required``."""
        if doc not in (*DOC_NAMES, "all"):
            raise HarnessError(f"unknown document {doc!r}")
        return await (await self.smp()).node_reload(doc)

    async def push_config(self, doc_name: str, doc: Mapping[str, Any], *, reload: bool = True,
                          force: bool = False) -> dict[str, Any]:
        """Validate, stage and activate a configuration document.

        Unless ``force`` is set nothing is sent when the node's active
        document already has the same SHA-256. Otherwise the document is
        uploaded to ``/lfs/cfg/<doc>.json.new`` and, with ``reload``, activated
        with ``uc_node reload``; the active document's hash is then checked.
        Without ``reload`` the staged file waits for the next reload or boot.

        Returns ``{"doc", "path", "staged_path", "uploaded", "sha256", "size",
        "reloaded", "activated", "reboot_required"}``.

        Raises:
            ManifestError: ``doc`` violates ``schemas/<doc_name>.schema.json``.
            HarnessError: the node rejected the staged document (it deleted
                it and kept its configuration; :class:`SmpError` with rc
                ``INVALID``), applying it failed (other rc; the document is
                active), or the node did not activate it (firmware without
                staged documents).
        """
        issues = validate_document(doc_name, dict(doc))
        if issues:
            raise ManifestError(issues, f"({doc_name}.json for node {self.name!r})")
        data = doc_bytes(doc)
        sha = hashlib.sha256(data).hexdigest()
        path = doc_path(doc_name)
        staged = path + STAGED_SUFFIX
        out: dict[str, Any] = {"doc": doc_name, "path": path, "staged_path": staged,
                               "uploaded": False, "sha256": sha, "size": len(data),
                               "reloaded": False, "activated": False, "reboot_required": False}
        if not force and await self.file_sha256(path) == sha:
            out["activated"] = True
            return out
        async with self._lock:
            await (await self.smp()).fs_upload(staged, data)
        out["uploaded"] = True
        if not reload:
            return out
        try:
            out["reboot_required"] = await self.reload(doc_name)
        except SmpError as exc:
            if exc.rc_name == "INVALID":
                raise SmpError(exc.group, exc.rc, response=exc.response,
                               message=f"{self.name}: {doc_name}.json rejected by the node "
                               f"(rc {exc.rc_name}: staged document deleted, configuration "
                               "unchanged; see read_logs)") from exc
            raise
        out["reloaded"] = True
        active = await self.file_sha256(path)
        if active != sha:
            raise HarnessError(
                f"{self.name}: {doc_name}.json was not activated (active document "
                f"{active or 'missing'}, expected {sha}); does the firmware support staged "
                f"documents ({staged})?")
        out["activated"] = True
        return out

    # --- applications -------------------------------------------------------------------------

    async def list_apps(self) -> list[dict[str, Any]]:
        return await (await self.smp()).app_list()

    async def app_status(self, name: str) -> dict[str, Any] | None:
        try:
            return await (await self.smp()).app_status(name)
        except SmpError as exc:
            if is_not_found(exc):
                return None
            raise

    async def start_app(self, name: str) -> dict[str, Any]:
        await (await self.smp()).app_start(name)
        return await self.app_status(name) or {"name": name}

    async def restart_app(self, name: str) -> dict[str, Any]:
        """Stop (when running) and start an app: it re-initialises, e.g.
        re-subscribes and rewrites its outputs."""
        smp = await self.smp()
        status = await self.app_status(name)
        if status is None:
            raise HarnessError(f"{self.name}: app {name!r} is not installed")
        if status.get("state") in ("running", "starting"):
            await smp.app_stop(name)
        await smp.app_start(name)
        return await self.app_status(name) or {"name": name}

    async def stop_app(self, name: str) -> dict[str, Any]:
        await (await self.smp()).app_stop(name)
        return await self.app_status(name) or {"name": name}

    async def remove_app(self, name: str, delete_file: bool = True) -> dict[str, Any]:
        await (await self.smp()).app_remove(name, delete_file=delete_file)
        return {"name": name, "removed": True, "delete_file": delete_file}

    async def deploy_app(
        self,
        name: str,
        data: bytes,
        *,
        aot: bool | None = None,
        autostart: bool = True,
        period_ms: int = 1000,
        heap_kb: int = 8,
        stack_kb: int = 4,
        perms: list[str] | None = None,
        params: Mapping[str, Any] | list[dict[str, str]] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Upload a module to ``/lfs/apps/<name>.wasm`` (``.aot``) unless the node
        already has it (SHA-256), then install it with its manifest (``restart``
        replaces a running instance).

        A manifest whose ``uc_app install`` request would not fit one SMP
        request (many or long ``params``) is written into ``apps.json``
        instead (staged, ``uc_node reload apps``), as
        docs/management-protocol.md recommends; ``"installed_via"`` tells
        which way was used.
        """
        data = bytes(data)
        if aot is None:
            aot = data[:4] == b"\x00aot"
        if data[:4] not in (b"\x00asm", b"\x00aot"):
            raise HarnessError("module is neither WebAssembly (\\0asm) nor AOT (\\0aot)")
        path = f"{APPS_DIR}/{name}.{'aot' if aot else 'wasm'}"
        sha = hashlib.sha256(data).hexdigest()
        status = await self.app_status(name)
        same_file = await self.file_sha256(path) == sha
        stopped = False
        if not same_file or force:
            if status is not None and status.get("state") in ("running", "starting"):
                await (await self.smp()).app_stop(name)
                stopped = True
            async with self._lock:
                await (await self.smp()).fs_upload(path, data)
        manifest: dict[str, Any] = {
            "name": name, "file": path, "autostart": autostart, "period_ms": period_ms,
            "heap_kb": heap_kb, "stack_kb": stack_kb, "sha256": sha,
            "restart": True,
        }
        if perms is not None:
            manifest["perms"] = list(perms)
        if params:
            manifest["params"] = params
        smp = await self.smp()
        request = encode_frame(g.OP_WRITE, g.GROUP_UC_APP, g.UC_APP_INSTALL, 0,
                               normalize_manifest(manifest))
        if len(request) > await smp.max_frame():
            await self._install_via_apps_json(manifest)
            via = "apps.json"
        else:
            await smp.app_install(manifest)
            via = "install"
        return {"name": name, "file": path, "sha256": sha, "size": len(data),
                "uploaded": not same_file or force, "stopped_for_upload": stopped,
                "installed_via": via, "status": await self.app_status(name)}

    async def _install_via_apps_json(self, manifest: Mapping[str, Any]) -> None:
        """Add or replace the entry in apps.json and reload it; then make sure
        the app runs (autostart) with the new module."""
        name = str(manifest["name"])
        entry = {k: v for k, v in manifest.items() if k != "restart"}
        params = entry.get("params")
        if isinstance(params, Mapping):
            entry["params"] = [{"key": str(k), "value": str(v)} for k, v in params.items()]
        doc = await self.get_config("apps") or {"schema": 1, "apps": []}
        apps = [dict(e) for e in doc.get("apps", []) if isinstance(e, Mapping)]
        names = [e.get("name") for e in apps]
        if name in names:
            apps[names.index(name)] = entry
        else:
            apps.append(entry)
        status = await self.app_status(name)
        if status is not None and status.get("state") in ("running", "starting"):
            await (await self.smp()).app_stop(name)  # the reload starts it again
        await self.push_config("apps", {"schema": 1, "apps": apps}, force=True)
        status = await self.app_status(name)
        if entry.get("autostart", True) and status is not None and \
                status.get("state") not in ("running", "starting"):
            await (await self.smp()).app_start(name)

    async def restart_io_dependents(self) -> list[str]:
        """Restart the running apps that read or write one of the node's IO
        objects (stock apps and uc-link instances recognised from apps.json):
        call after an ``io.json`` reload re-created those objects. Returns the
        restarted app names."""
        try:
            apps_doc = await self.get_config("apps") or {}
            io_keys = set()
            for o in await self.objects():
                if o.get("owner") == "io":
                    io_keys.add(parse_object((str(o["type"]), int(o["instance"]))))
            own = (await self.info()).get("device", {}).get("instance")
        except (HarnessError, KeyError, TypeError, ValueError):
            return []
        names = apps_using_objects(apps_doc.get("apps", []), io_keys, own)
        running = {a.get("name"): a.get("state") for a in await self.list_apps()}
        out = []
        for name in names:
            if running.get(name) in ("running", "starting"):
                await self.restart_app(name)
                out.append(name)
        return out

    async def install_app(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Install an apps.json-style entry whose file is already on the node."""
        manifest = dict(entry)
        manifest["restart"] = True
        await (await self.smp()).app_install(manifest)
        return {"name": manifest["name"], "status": await self.app_status(manifest["name"])}

    # --- logs ------------------------------------------------------------------------------------

    async def log_files(self) -> list[str]:
        """Log files ``/lfs/log/log.NNNN``, oldest first."""
        names: list[str] = []
        try:
            rc, out = await self.shell(["fs", "ls", LOG_DIR])
            if rc == 0:
                names = [ln.strip() for ln in out.splitlines() if _LOG_NAME.match(ln.strip())]
        except (SmpError, HarnessTimeout):
            names = []
        if not names:
            smp = await self.smp()
            misses = 0
            for i in range(LOG_PROBE_MAX):
                name = f"log.{i:04d}"
                try:
                    await smp.fs_status(f"{LOG_DIR}/{name}")
                    names.append(name)
                    misses = 0
                except SmpError as exc:
                    if not is_not_found(exc):
                        raise
                    misses += 1
                    if names and misses > 8:
                        break
        nums = sorted(int(_LOG_NAME.match(n).group(1)) for n in names)  # type: ignore[union-attr]
        if nums and nums[-1] - nums[0] > 5000:  # numbering wrapped at 9999
            nums = sorted(nums, key=lambda n: n + 10000 if n < 5000 else n)
        return [f"{LOG_DIR}/log.{n:04d}" for n in nums]

    async def logs(self, lines: int = 200, grep: str | None = None) -> dict[str, Any]:
        """The last ``lines`` log lines (optionally only lines matching the
        regular expression ``grep``), newest last."""
        files = await self.log_files()
        pattern = re.compile(grep) if grep else None
        collected: list[str] = []
        read: list[str] = []
        for path in reversed(files):
            raw = await self.download(path)
            if raw is None:
                continue
            read.append(path)
            chunk = raw.decode("utf-8", errors="replace").splitlines()
            if pattern is not None:
                chunk = [ln for ln in chunk if pattern.search(ln)]
            collected = chunk + collected
            if len(collected) >= lines:
                break
        return {"node": self.name, "files": files, "read": list(reversed(read)),
                "lines": collected[-lines:] if lines > 0 else collected}

    # --- IO ---------------------------------------------------------------------------------------

    async def io_catalog(self) -> dict[str, Any]:
        return await (await self.smp()).io_catalog()

    async def io_read(self, name: str | None = None) -> dict[str, float]:
        return await (await self.smp()).io_read(name)

    async def io_write(self, name: str, value: float) -> None:
        await (await self.smp()).io_write(name, value)

    async def io_force(self, name: str, value: float) -> None:
        await (await self.smp()).io_force(name, value)

    async def io_release(self, name: str) -> None:
        await (await self.smp()).io_release(name)

    # --- properties -------------------------------------------------------------------------------

    async def prop_read(self, obj: str | tuple[str | int, int], prop: str | int = "present-value",
                        index: int | None = None) -> Any:
        """Read a local property through SMP (``uc_node prop_read``).

        Arrays and lists without ``index`` come back whole; one that does not
        fit a response (rc ``LIMIT``) is read element by element."""
        t, i = parse_object(obj)
        smp = await self.smp()
        try:
            return jsonable(await smp.prop_read(t, i, prop, index))
        except SmpError as exc:
            if exc.rc_name != "LIMIT" or index is not None:
                raise
        count = await smp.prop_read(t, i, prop, 0)
        if not isinstance(count, int):
            raise HarnessError(f"{self.name}: array size of {prop} is not a number: {count!r}")
        return [jsonable(await smp.prop_read(t, i, prop, k)) for k in range(1, count + 1)]

    async def prop_write(self, obj: str | tuple[str | int, int], prop: str | int, value: Any,
                         priority: int | None = None, index: int | None = None) -> None:
        t, i = parse_object(obj)
        await (await self.smp()).prop_write(t, i, prop, value, priority, index)

    async def bacnet_read(self, obj: str | tuple[str | int, int],
                          prop: str | int = "present-value", index: int | None = None) -> Any:
        """ReadProperty over BACnet/IP."""
        t, i = parse_object(obj)
        client = await self.bacnet_client()
        return jsonable(await client.read_property(self._require_bacnet(), t, i, prop, index))

    async def bacnet_write(self, obj: str | tuple[str | int, int], prop: str | int, value: Any,
                           priority: int | None = None, index: int | None = None) -> None:
        """WriteProperty over BACnet/IP (``None`` relinquishes at ``priority``)."""
        t, i = parse_object(obj)
        client = await self.bacnet_client()
        await client.write_property(self._require_bacnet(), t, i, prop, value, priority, index)

    async def read_point(self, obj: str | tuple[str | int, int],
                         prop: str | int = "present-value") -> tuple[Any, str]:
        """Read via BACnet/IP, falling back to SMP when BACnet is unavailable
        (no address, timeout). Returns ``(value, "bacnet" | "smp")``."""
        if self.bacnet_address is not None:
            try:
                return await self.bacnet_read(obj, prop), "bacnet"
            except BacnetError:
                raise
            except HarnessError:
                pass
        return await self.prop_read(obj, prop), "smp"

    async def write_point(self, obj: str | tuple[str | int, int], prop: str | int, value: Any,
                          priority: int | None = None) -> str:
        """Write via BACnet/IP with SMP fallback; returns the path used."""
        if self.bacnet_address is not None:
            try:
                await self.bacnet_write(obj, prop, value, priority)
                return "bacnet"
            except BacnetError:
                raise
            except HarnessError:
                pass
        await self.prop_write(obj, prop, value, priority)
        return "smp"
