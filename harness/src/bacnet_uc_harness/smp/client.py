# SPDX-License-Identifier: Apache-2.0
"""SMP v2 client with the standard groups used by the harness and the
BACnet-uc custom groups (docs/management-protocol.md).

Every request is retried with the same sequence number when no response
arrives (``retries`` times, default 3); :class:`HarnessTimeout` is raised
afterwards. Error responses raise :class:`SmpError`:

- SMP v2: ``{"err": {"group": g, "rc": n}}`` with ``n != 0``,
- legacy: ``{"rc": n}`` with ``n != 0`` (``mcumgr_err_t``).
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any

from bacnet_uc_harness.bacnet import enums as bn_enums
from bacnet_uc_harness.errors import HarnessError, HarnessTimeout, ReloadError, SmpError
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.codec import decode_frame, encode_frame
from bacnet_uc_harness.smp.transport import SerialTransport, SmpTransport, UdpTransport

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int], Any] | Callable[[int, int], Awaitable[Any]]

# Bytes kept free below the MTU on uploads, in addition to the exact CBOR
# overhead (headroom for transports that count framing into the MTU).
_UPLOAD_MARGIN = 16


async def _report(progress: ProgressCallback | None, done: int, total: int) -> None:
    if progress is None:
        return
    result = progress(done, total)
    if inspect.isawaitable(result):
        await result


def _obj_type(value: str | int) -> int:
    try:
        return bn_enums.object_type_number(value)
    except ValueError as exc:
        raise HarnessError(str(exc)) from None


def _prop(value: str | int) -> int:
    try:
        return bn_enums.property_number(value)
    except ValueError as exc:
        raise HarnessError(str(exc)) from None


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Convert an apps.json-style entry into the ``uc_app install`` request map.

    ``params`` as a list of ``{"key", "value"}`` becomes a map with string
    values; ``sha256`` as hex text becomes 32 bytes. Other keys are passed on
    unchanged.
    """
    req = dict(manifest)
    params = req.get("params")
    if isinstance(params, list):
        req["params"] = {str(p["key"]): str(p["value"]) for p in params}
    elif isinstance(params, dict):
        req["params"] = {str(k): str(v) for k, v in params.items()}
    sha = req.get("sha256")
    if isinstance(sha, str):
        try:
            req["sha256"] = bytes.fromhex(sha)
        except ValueError:
            raise HarnessError(f"sha256 is not hex: {sha!r}") from None
    return req


class SmpClient:
    """Asynchronous SMP client over one transport (one request at a time)."""

    def __init__(self, transport: SmpTransport, timeout: float = 5.0, retries: int = 3) -> None:
        self.transport = transport
        self.timeout = timeout
        self.retries = retries
        self._seq = random.randrange(256)
        self._max_frame: int | None = None

    def __repr__(self) -> str:
        return f"SmpClient({self.transport!r})"

    async def __aenter__(self) -> SmpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.transport.close()

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFF
        return seq

    # --- core ------------------------------------------------------------------------

    async def request(
        self,
        op: int,
        group: int,
        cmd: int,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
        *,
        legacy_rc_is_error: bool = True,
        retries: int | None = None,
    ) -> dict[str, Any]:
        """Send one request and return the response map. ``retries`` overrides
        the client's number of retransmissions (0: send once).

        Raises:
            SmpError: the node answered with a non-zero rc.
            HarnessTimeout: no response after ``retries`` retransmissions.
            HarnessError: malformed or mismatching response.
        """
        seq = self._next_seq()
        frame = encode_frame(op, group, cmd, seq, payload)
        t = self.timeout if timeout is None else timeout
        last: HarnessTimeout | None = None
        raw: bytes | None = None
        tries = (self.retries if retries is None else retries) + 1
        for attempt in range(tries):
            try:
                raw = await self.transport.request(frame, seq, t)
                break
            except HarnessTimeout as exc:
                last = exc
                log.debug(
                    "SMP %s/%d seq %d: timeout (attempt %d)",
                    g.group_name(group),
                    cmd,
                    seq,
                    attempt + 1,
                )
        if raw is None:
            raise HarnessTimeout(
                f"SMP {g.group_name(group)}/{cmd}: no response after {tries} attempt(s)"
                f" ({last})"
            )
        hdr, rsp = decode_frame(raw)
        if hdr.op != op + 1 or hdr.group != group or hdr.cmd != cmd:
            raise HarnessError(
                f"unexpected SMP response op={hdr.op} group={hdr.group} cmd={hdr.cmd} "
                f"for op={op} group={group} cmd={cmd}"
            )
        err = rsp.get("err")
        if isinstance(err, dict):
            rc = int(err.get("rc", 0))
            if rc != 0:
                raise SmpError(int(err.get("group", group)), rc, response=rsp)
        rc = rsp.get("rc")
        if legacy_rc_is_error and isinstance(rc, int) and not isinstance(rc, bool) and rc != 0:
            raise SmpError(None, rc, response=rsp)
        return rsp

    async def read(
        self,
        group: int,
        cmd: int,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.request(g.OP_READ, group, cmd, payload, timeout)

    async def write(
        self,
        group: int,
        cmd: int,
        payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.request(g.OP_WRITE, group, cmd, payload, timeout)

    async def max_frame(self) -> int:
        """Largest request frame: the transport MTU, limited by the node's
        ``buf_size`` from ``mcumgr_params`` when that command is available."""
        if self._max_frame is None:
            mtu = self.transport.mtu
            try:
                params = await self.mcumgr_params()
                buf = int(params.get("buf_size", 0))
                if buf > 0:
                    mtu = min(mtu, buf)
            except SmpError:
                pass
            self._max_frame = mtu
        return self._max_frame

    # --- OS group -----------------------------------------------------------------------

    async def os_echo(self, text: str) -> str:
        rsp = await self.write(g.GROUP_OS, g.OS_ECHO, {"d": text})
        return str(rsp.get("r", ""))

    async def os_reset(self, force: bool = False) -> None:
        await self.write(g.GROUP_OS, g.OS_RESET, {"force": True} if force else {})

    async def os_info(self, fmt: str = "a") -> str:
        rsp = await self.read(g.GROUP_OS, g.OS_INFO, {"format": fmt})
        return str(rsp.get("output", ""))

    async def mcumgr_params(self) -> dict[str, Any]:
        return await self.read(g.GROUP_OS, g.OS_MCUMGR_PARAMS)

    # --- file system group ---------------------------------------------------------------

    def _chunk_size(self, max_frame: int, template: dict[str, Any], group: int, cmd: int) -> int:
        overhead = len(encode_frame(g.OP_WRITE, group, cmd, 0, template))
        # the empty "data" bstr header (1 byte) grows to 3 bytes for chunks < 64 KiB
        size = max_frame - overhead - 2 - _UPLOAD_MARGIN
        if size < 16:
            raise HarnessError(f"MTU {max_frame} too small for an upload")
        return size

    async def fs_upload(
        self, path: str, data: bytes, progress: ProgressCallback | None = None, *,
        verify: bool = True, attempts: int = 3,
    ) -> None:
        """Upload ``data`` to ``path`` (FS group ``file`` write, chunked).

        Zephyr's fs_mgmt keeps one upload open between chunks, and a chunk
        whose response was lost is sent again (same sequence number):

        - at an offset > 0 the node answers ``FILE_OFFSET_NOT_VALID`` with the
          length it has; when that is the end of this chunk the upload goes on
          from there;
        - the first chunk of a multi-chunk upload is never sent twice: at
          offset 0 the node truncates the file but keeps its upload offset,
          so a repeated first chunk would be written twice.

        Any other surprise (no answer to the first chunk, a reply offset other
        than offset + chunk length) closes the upload (``fs_close``) and starts
        it again from offset 0. With ``verify`` the SHA-256 of the file on the
        node is compared with ``data`` at the end (a mismatch is another
        attempt; nodes that cannot hash are trusted).

        Raises:
            HarnessError: the file is still not right after ``attempts``
                attempts.
            SmpError: the node rejected the upload (e.g. unknown mount point).
            HarnessTimeout: no answer to a later chunk despite the retries.
        """
        data = bytes(data)
        total = len(data)
        max_frame = await self.max_frame()
        template = {"off": total, "data": b"", "len": total, "name": path}
        chunk = self._chunk_size(max_frame, template, g.GROUP_FS, g.FS_FILE)
        why = ""
        for attempt in range(max(1, attempts)):
            if attempt:
                log.info("fs upload %s: %s; starting again", path, why)
                try:
                    await self.fs_close()
                except (SmpError, HarnessTimeout):
                    pass
            problem = await self._fs_upload_pass(path, data, chunk, progress)
            if problem is None and verify and total:
                problem = await self._fs_verify(path, data)
            if problem is None:
                return
            why = problem
        raise HarnessError(f"fs upload {path}: {why} (gave up after {max(1, attempts)} "
                           "attempts)")

    async def _fs_upload_pass(self, path: str, data: bytes, chunk: int,
                              progress: ProgressCallback | None) -> str | None:
        """One upload from offset 0; the reason to start again, or None."""
        total = len(data)
        off = 0
        resumed = False
        while True:
            piece = data[off : off + chunk]
            req: dict[str, Any] = {"off": off, "data": piece, "name": path}
            if off == 0:
                req["len"] = total
            # a repeated first chunk of a longer file is written twice
            once = off == 0 and len(piece) < total
            try:
                rsp = await self.request(g.OP_WRITE, g.GROUP_FS, g.FS_FILE, req,
                                         retries=0 if once else None)
                new_off = rsp.get("off")
                if not isinstance(new_off, int):
                    raise HarnessError(f"fs upload {path}: response without offset: {rsp}")
                if new_off != off + len(piece):
                    return (f"the node answered offset {new_off} to {len(piece)} bytes at "
                            f"offset {off}")
            except HarnessTimeout:
                if not once:
                    raise
                return "no response to the first chunk"
            except SmpError as exc:
                have = exc.response.get("len")
                if exc.group != g.GROUP_FS or exc.rc != g.FS_RC_FILE_OFFSET_NOT_VALID:
                    raise
                if off == 0 or not isinstance(have, int) or not off < have <= off + len(piece):
                    return f"the node has {have} bytes where {off} were expected"
                # the chunk was written, only its response was lost; the node
                # closed the upload and continues the file at its length
                new_off = have
                resumed = True
            off = new_off
            await _report(progress, min(off, total), total)
            if off >= total:
                break
        if resumed:
            # a continued upload has no length: the node keeps the file open
            try:
                await self.fs_close()
            except SmpError:
                pass
        return None

    async def _fs_verify(self, path: str, data: bytes) -> str | None:
        try:
            got = await self.fs_hash(path, "sha256")
        except SmpError as exc:
            if exc.rc_name in ("CHECKSUM_HASH_NOT_FOUND", "ENOTSUP", "NOT_SUPPORTED"):
                return None  # the node cannot hash: trust the offsets
            raise
        want = hashlib.sha256(data).digest()
        if got != want:
            return f"SHA-256 of the file on the node is {got.hex()}, expected {want.hex()}"
        return None

    async def fs_download(self, path: str, progress: ProgressCallback | None = None, *,
                          fresh: bool = True) -> bytes:
        """Download ``path`` (FS group ``file`` read).

        Zephyr's FS group keeps the file of a download open between requests
        (until ``CONFIG_MCUMGR_GRP_FS_FILE_AUTOMATIC_IDLE_CLOSE_TIME``) and a new
        download of the same file continues on that handle with the length
        it had at the first open: a file that grows (a log file) would come
        back truncated. With ``fresh`` (default) the handle is closed first.
        """
        if fresh:
            try:
                await self.fs_close()
            except SmpError:
                pass  # close command not supported: nothing to reset
        rsp = await self.read(g.GROUP_FS, g.FS_FILE, {"name": path, "off": 0})
        total = rsp.get("len")
        if not isinstance(total, int):
            raise HarnessError(f"fs download {path}: first response without length")
        buf = bytearray(rsp.get("data", b""))
        await _report(progress, len(buf), total)
        while len(buf) < total:
            rsp = await self.read(g.GROUP_FS, g.FS_FILE, {"name": path, "off": len(buf)})
            if rsp.get("off", len(buf)) != len(buf):
                raise HarnessError(f"fs download {path}: offset mismatch")
            chunk = rsp.get("data", b"")
            if not chunk:
                raise HarnessError(f"fs download {path}: empty chunk at offset {len(buf)}")
            buf += chunk
            await _report(progress, len(buf), total)
        return bytes(buf[:total])

    async def fs_status(self, path: str) -> int:
        """Size of ``path`` in bytes."""
        rsp = await self.read(g.GROUP_FS, g.FS_STATUS, {"name": path})
        return int(rsp["len"])

    async def fs_hash(self, path: str, type: str = "sha256") -> bytes:
        """Hash/checksum of ``path`` (``"sha256"`` or ``"crc32"``; a numeric
        crc32 is returned as 4 big-endian bytes)."""
        rsp = await self.read(g.GROUP_FS, g.FS_HASH_CHECKSUM, {"name": path, "type": type})
        out = rsp.get("output")
        if isinstance(out, int):
            return out.to_bytes(4, "big")
        if not isinstance(out, bytes | bytearray):
            raise HarnessError(f"fs hash {path}: unexpected output {out!r}")
        return bytes(out)

    async def fs_close(self) -> None:
        """Close a file left open by an interrupted upload/download."""
        await self.write(g.GROUP_FS, g.FS_OPENED_FILE, {})

    # --- shell group ------------------------------------------------------------------------

    async def shell_exec(self, argv: list[str]) -> tuple[int, str]:
        """Run a shell command line; returns ``(return code, output)``."""
        rsp = await self.request(
            g.OP_WRITE,
            g.GROUP_SHELL,
            g.SHELL_EXEC,
            {"argv": list(argv)},
            legacy_rc_is_error=False,
        )
        ret = rsp.get("ret", rsp.get("rc", 0))
        return int(ret), str(rsp.get("o", ""))

    # --- image group --------------------------------------------------------------------------

    async def img_state(self) -> list[dict[str, Any]]:
        rsp = await self.read(g.GROUP_IMG, g.IMG_STATE)
        return list(rsp.get("images", []))

    async def img_upload(
        self, data: bytes, image: int = 0, progress: ProgressCallback | None = None
    ) -> None:
        """Upload a signed MCUboot image to the secondary slot."""
        data = bytes(data)
        total = len(data)
        sha = hashlib.sha256(data).digest()
        max_frame = await self.max_frame()
        template = {"image": image, "len": total, "off": total, "sha": sha, "data": b""}
        chunk = self._chunk_size(max_frame, template, g.GROUP_IMG, g.IMG_UPLOAD)
        off = 0
        stalls = 0
        while off < total or total == 0:
            piece = data[off : off + chunk]
            req: dict[str, Any] = {"off": off, "data": piece}
            if off == 0:
                req.update({"image": image, "len": total, "sha": sha})
            rsp = await self.write(g.GROUP_IMG, g.IMG_UPLOAD, req)
            new_off = rsp.get("off")
            if not isinstance(new_off, int):
                raise HarnessError(f"image upload: response without offset: {rsp}")
            stalls = stalls + 1 if new_off <= off else 0
            if stalls > 3:
                raise HarnessError(f"image upload: offset does not advance ({new_off})")
            off = new_off
            await _report(progress, min(off, total), total)
            if total == 0:
                break
        if off != total:
            raise HarnessError(f"image upload: node reports offset {off}, expected {total}")

    async def img_test(self, hash: bytes) -> list[dict[str, Any]]:
        """Mark the image with ``hash`` for a test boot."""
        rsp = await self.write(g.GROUP_IMG, g.IMG_STATE, {"hash": bytes(hash), "confirm": False})
        return list(rsp.get("images", []))

    async def img_confirm(self, hash: bytes | None = None) -> list[dict[str, Any]]:
        """Confirm the running image (``hash`` None) or make ``hash`` permanent."""
        req: dict[str, Any] = {"confirm": True}
        if hash is not None:
            req["hash"] = bytes(hash)
        rsp = await self.write(g.GROUP_IMG, g.IMG_STATE, req)
        return list(rsp.get("images", []))

    async def img_erase(self, slot: int | None = None) -> None:
        await self.write(g.GROUP_IMG, g.IMG_ERASE, {} if slot is None else {"slot": slot})

    # --- uc_app (64) ------------------------------------------------------------------------

    async def app_list(self) -> list[dict[str, Any]]:
        """Status of all installed applications (follows ``total`` paging)."""
        rsp = await self.read(g.GROUP_UC_APP, g.UC_APP_LIST, {})
        apps = list(rsp.get("apps", []))
        total = rsp.get("total")
        while isinstance(total, int) and len(apps) < total:
            more = await self.read(g.GROUP_UC_APP, g.UC_APP_LIST, {"offset": len(apps)})
            page = list(more.get("apps", []))
            if not page:
                break
            apps += page
        return apps

    async def app_install(self, manifest: dict[str, Any]) -> None:
        """Install (and autostart) an application; see :func:`normalize_manifest`."""
        await self.write(g.GROUP_UC_APP, g.UC_APP_INSTALL, normalize_manifest(manifest))

    async def app_start(self, name: str) -> None:
        await self.write(g.GROUP_UC_APP, g.UC_APP_START, {"name": name})

    async def app_stop(self, name: str) -> None:
        await self.write(g.GROUP_UC_APP, g.UC_APP_STOP, {"name": name})

    async def app_remove(self, name: str, delete_file: bool = False) -> None:
        req: dict[str, Any] = {"name": name}
        if delete_file:
            req["delete_file"] = True
        await self.write(g.GROUP_UC_APP, g.UC_APP_REMOVE, req)

    async def app_status(self, name: str) -> dict[str, Any]:
        return await self.read(g.GROUP_UC_APP, g.UC_APP_STATUS, {"name": name})

    # --- uc_io (65) --------------------------------------------------------------------------

    async def io_catalog(self) -> dict[str, Any]:
        """``{"board": str, "channels": [...]}`` (follows ``total`` paging)."""
        rsp = await self.read(g.GROUP_UC_IO, g.UC_IO_CATALOG, {})
        channels = list(rsp.get("channels", []))
        total = rsp.get("total")
        while isinstance(total, int) and len(channels) < total:
            more = await self.read(g.GROUP_UC_IO, g.UC_IO_CATALOG, {"offset": len(channels)})
            page = list(more.get("channels", []))
            if not page:
                break
            channels += page
        return {**rsp, "channels": channels}

    async def io_read(self, name: str | None = None) -> dict[str, float]:
        rsp = await self.read(g.GROUP_UC_IO, g.UC_IO_READ, {} if name is None else {"name": name})
        return {str(k): float(v) for k, v in dict(rsp.get("values", {})).items()}

    async def io_write(self, name: str, value: float) -> None:
        await self.write(g.GROUP_UC_IO, g.UC_IO_WRITE, {"name": name, "value": float(value)})

    async def io_force(self, name: str, value: float) -> None:
        await self.write(g.GROUP_UC_IO, g.UC_IO_FORCE, {"name": name, "value": float(value)})

    async def io_release(self, name: str) -> None:
        await self.write(g.GROUP_UC_IO, g.UC_IO_FORCE, {"name": name, "release": True})

    # --- uc_node (66) ------------------------------------------------------------------------

    async def node_info(self) -> dict[str, Any]:
        return await self.read(g.GROUP_UC_NODE, g.UC_NODE_INFO, {})

    async def node_reload(self, doc: str = "all") -> bool:
        """Re-read a configuration document; returns ``reboot_required``.

        Raises:
            ReloadError: a document failed (with ``"all"`` the others were
                applied); it carries the node's ``reboot_required``.
        """
        try:
            rsp = await self.write(g.GROUP_UC_NODE, g.UC_NODE_RELOAD, {"doc": doc})
        except SmpError as exc:
            if exc.group == g.GROUP_UC_NODE:
                raise ReloadError(doc, exc) from exc
            raise
        return bool(rsp.get("reboot_required", False))

    async def node_objects(self, offset: int = 0, count: int | None = None) -> dict[str, Any]:
        """One page of the local object list: ``{"total": n, "objects": [...]}``."""
        req: dict[str, Any] = {"offset": offset}
        if count is not None:
            req["count"] = count
        return await self.read(g.GROUP_UC_NODE, g.UC_NODE_OBJECTS, req)

    async def node_objects_all(self) -> list[dict[str, Any]]:
        """All local objects (follows the paging of ``node_objects``)."""
        objects: list[dict[str, Any]] = []
        while True:
            page = await self.node_objects(offset=len(objects))
            items = list(page.get("objects", []))
            objects += items
            if not items or len(objects) >= int(page.get("total", 0)):
                return objects

    async def prop_read(
        self,
        obj_type: str | int,
        instance: int,
        prop: str | int = "present-value",
        index: int | None = None,
    ) -> Any:
        """Read a property of a local object through SMP (``uc_node prop_read``)."""
        req: dict[str, Any] = {
            "type": _obj_type(obj_type),
            "instance": int(instance),
            "prop": _prop(prop),
        }
        if index is not None:
            req["index"] = int(index)
        rsp = await self.read(g.GROUP_UC_NODE, g.UC_NODE_PROP_READ, req)
        return rsp.get("value")

    async def prop_write(
        self,
        obj_type: str | int,
        instance: int,
        prop: str | int,
        value: Any,
        priority: int | None = None,
        index: int | None = None,
    ) -> None:
        """Write a property of a local object through SMP (``uc_node prop_write``)."""
        req: dict[str, Any] = {
            "type": _obj_type(obj_type),
            "instance": int(instance),
            "prop": _prop(prop),
            "value": value,
        }
        if priority is not None:
            req["priority"] = int(priority)
        if index is not None:
            req["index"] = int(index)
        await self.write(g.GROUP_UC_NODE, g.UC_NODE_PROP_WRITE, req)


async def connect_udp(host: str, port: int = 1337, timeout: float = 5.0) -> SmpClient:
    """SMP client over UDP (the socket is opened immediately)."""
    transport = UdpTransport(host, port)
    await transport.open()
    return SmpClient(transport, timeout=timeout)


async def connect_serial(device: str, baud: int = 115200) -> SmpClient:
    """SMP client over the shell console UART (needs pyserial)."""
    transport = SerialTransport(device, baud)
    await transport.open()
    return SmpClient(transport)
