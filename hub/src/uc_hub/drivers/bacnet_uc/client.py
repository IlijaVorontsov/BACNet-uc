"""``NodeApi`` over SMP/UDP (port 1337).

One connected UDP socket per node. Requests are matched to responses by the
SMP sequence number, so several may be in flight (bounded by
``max_inflight``; Zephyr's UDP transport has a handful of buffers). A request
that gets no answer within ``timeout_s`` is resent with the same sequence
number, ``retries`` times, so a late answer to an earlier attempt still
counts. FS transfers are serialised because the node keeps a single
upload/download context.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import cbor2

from ...core.errors import (
    DeviceError,
    DeviceTimeout,
    InvalidRequest,
    NotFound,
    Unsupported,
)
from . import names
from .api import (
    APP_INSTALL,
    APP_LIST,
    APP_REMOVE,
    APP_START,
    APP_STATUS,
    APP_STOP,
    GROUP_FS,
    GROUP_OS,
    GROUP_UC_APP,
    GROUP_UC_IO,
    GROUP_UC_NODE,
    IO_CATALOG,
    IO_FORCE,
    IO_READ,
    IO_WRITE,
    NODE_IDENTIFY,
    NODE_INFO,
    NODE_OBJECTS,
    NODE_PROP_READ,
    NODE_PROP_WRITE,
    NODE_RELOAD,
    NodeApi,
)
from .smp import (
    FS_ERR_FILE_EMPTY,
    FS_ERR_FILE_OFFSET_NOT_VALID,
    FS_FILE,
    FS_HASH,
    HEADER_SIZE,
    OP_READ,
    OP_WRITE,
    OS_ECHO,
    OS_MCUMGR_PARAMS,
    OS_RESET,
    SmpFrame,
    SmpFrameError,
    decode_frame,
    encode_frame,
    error_code,
    group_name,
    raise_for_error,
    response_op,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 1337
DEFAULT_MTU = 1024
DEFAULT_TIMEOUT_S = 1.5
DEFAULT_RETRIES = 3
DEFAULT_MAX_INFLIGHT = 4
DEFAULT_OBJECTS_PAGE = 8

RELOAD_DOCS = frozenset({"device", "io", "apps", "all"})
_MIN_MTU = 64
_MAX_UPLOAD_RESYNCS = 3
_MAX_OBJECT_PAGES = 4096
_SHA256_EMPTY = hashlib.sha256(b"").digest()


def format_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """``host``, ``host:port`` or ``[v6]:port`` -> ``(host, port)``."""
    text = text.strip()
    if text.startswith("["):
        host, sep, rest = text[1:].partition("]")
        port_text = rest[1:] if rest.startswith(":") else ""
        if not sep or not host:
            raise InvalidRequest(f"bad address {text!r}")
    elif text.count(":") == 1:
        host, _, port_text = text.partition(":")
    else:
        host, port_text = text, ""
    if not host:
        raise InvalidRequest(f"bad address {text!r}")
    try:
        port = int(port_text) if port_text else default_port
    except ValueError as e:
        raise InvalidRequest(f"bad port in address {text!r}") from e
    if not 0 < port < 65536:
        raise InvalidRequest(f"bad port in address {text!r}")
    return host, port


def _uint(value: Any, what: str, maximum: int = 0xFFFFFFFF) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise InvalidRequest(f"{what} must be an integer 0..{maximum}, not {value!r}")
    return int(value)


def _param_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise InvalidRequest(f"app parameter values must be text or numbers, not {value!r}")


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, client: SmpNodeClient) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self._client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        logger.debug("%s: socket error: %s", self._client.address, exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._client._on_connection_lost(exc)


@dataclass(slots=True)
class _Pending:
    op: int
    group: int
    command: int
    future: asyncio.Future[SmpFrame]


class SmpNodeClient(NodeApi):
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = DEFAULT_RETRIES,
        mtu: int | None = None,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        objects_page: int = DEFAULT_OBJECTS_PAGE,
        name: str | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        if mtu is not None and not _MIN_MTU <= mtu <= 0xFFFF:
            raise ValueError(f"mtu must be {_MIN_MTU}..65535")
        if not 1 <= max_inflight <= 255:
            raise ValueError("max_inflight must be 1..255")
        if objects_page < 1:
            raise ValueError("objects_page must be >= 1")
        self.host = host
        self.port = port
        self.address = format_address(host, port)
        self.label = name or self.address
        self.timeout_s = float(timeout_s)
        self.retries = int(retries)
        self.objects_page = objects_page
        self._mtu = mtu
        self._inflight = asyncio.Semaphore(max_inflight)
        self._fs_lock = asyncio.Lock()
        self._endpoint_lock = asyncio.Lock()
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[int, _Pending] = {}
        self._seq = random.randrange(256)
        self._closed = False

    @classmethod
    def from_settings(
        cls, host: str, port: int, settings: Mapping[str, Any], *, name: str | None = None,
    ) -> SmpNodeClient:
        """Build a client from the ``drivers.bacnet_uc`` section of hub.yaml."""
        mtu = settings.get("mtu")
        return cls(
            host,
            port,
            timeout_s=float(settings.get("timeout_s", DEFAULT_TIMEOUT_S)),
            retries=int(settings.get("retries", DEFAULT_RETRIES)),
            mtu=int(mtu) if mtu is not None else None,
            max_inflight=int(settings.get("max_inflight", DEFAULT_MAX_INFLIGHT)),
            objects_page=int(settings.get("objects_page", DEFAULT_OBJECTS_PAGE)),
            name=name,
        )

    # -- transport ------------------------------------------------------------
    async def request(
        self, op: int, group: int, command: int, body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request and return the response map; error responses
        raise (see ``smp.error_for``)."""
        frame = await self._transact(op, group, command, body)
        return raise_for_error(frame.body, context=self._context(group, command))

    async def _transact(
        self, op: int, group: int, command: int, body: dict[str, Any] | None,
    ) -> SmpFrame:
        if self._closed:
            raise DeviceError(f"{self.label}: client is closed")
        async with self._inflight:
            transport = await self._ensure_transport()
            seq = self._next_seq()
            future: asyncio.Future[SmpFrame] = asyncio.get_running_loop().create_future()
            self._pending[seq] = _Pending(response_op(op), group, command, future)
            data = encode_frame(op, group, command, seq, body or {})
            attempts = self.retries + 1
            try:
                for attempt in range(1, attempts + 1):
                    try:
                        transport.sendto(data)
                    except OSError as e:
                        logger.debug("%s: send failed: %s", self.label, e)
                    done, _ = await asyncio.wait({future}, timeout=self.timeout_s)
                    if done:
                        return future.result()
                    logger.debug(
                        "no answer to %s (attempt %d/%d)",
                        self._context(group, command), attempt, attempts,
                    )
            finally:
                self._pending.pop(seq, None)
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # a failure set after the last wait must not warn
        raise DeviceTimeout(
            f"{self._context(group, command)}: no answer from {self.address} "
            f"after {attempts} attempt(s) of {self.timeout_s:g} s"
        )

    def _context(self, group: int, command: int) -> str:
        return f"{self.label}: {group_name(group)} cmd {command}"

    def _next_seq(self) -> int:
        for _ in range(256):
            self._seq = (self._seq + 1) & 0xFF
            if self._seq not in self._pending:
                return self._seq
        raise DeviceError(f"{self.label}: no free SMP sequence number")

    async def _ensure_transport(self) -> asyncio.DatagramTransport:
        transport = self._transport
        if transport is not None and not transport.is_closing():
            return transport
        async with self._endpoint_lock:
            if self._closed:
                raise DeviceError(f"{self.label}: client is closed")
            if self._transport is not None and not self._transport.is_closing():
                return self._transport
            loop = asyncio.get_running_loop()
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self), remote_addr=(self.host, self.port)
                )
            except OSError as e:
                raise DeviceTimeout(f"{self.label}: cannot reach {self.address}: {e}") from e
            self._transport = transport
            return transport

    def _on_datagram(self, data: bytes) -> None:
        try:
            frame = decode_frame(data)
        except SmpFrameError as e:
            logger.warning("%s: dropped malformed SMP frame: %s", self.label, e)
            return
        pending = self._pending.get(frame.seq)
        if pending is None or pending.future.done():
            logger.debug("%s: dropped late or unsolicited response seq %d", self.label, frame.seq)
            return
        if (frame.op, frame.group, frame.command) != (pending.op, pending.group, pending.command):
            logger.warning(
                "%s: response seq %d is op %d group %d cmd %d, expected op %d group %d cmd %d",
                self.label, frame.seq, frame.op, frame.group, frame.command,
                pending.op, pending.group, pending.command,
            )
            return
        pending.future.set_result(frame)

    def _on_connection_lost(self, exc: Exception | None) -> None:
        self._transport = None
        if exc is not None:
            logger.debug("%s: socket closed: %s", self.label, exc)
        self._fail_pending(f"{self.label}: socket closed")

    def _fail_pending(self, message: str) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(DeviceError(message))

    async def close(self) -> None:
        self._closed = True
        transport, self._transport = self._transport, None
        self._fail_pending(f"{self.label}: client closed")
        if transport is not None:
            transport.close()

    async def mtu(self) -> int:
        """Largest SMP frame the node accepts: ``mcumgr_params`` buf_size
        (capped at the UDP transport MTU), or 1024 when the node cannot say."""
        if self._mtu is None:
            try:
                rsp = await self.request(OP_READ, GROUP_OS, OS_MCUMGR_PARAMS)
            except (DeviceError, NotFound, Unsupported) as e:
                logger.debug("%s: mcumgr_params unavailable (%s), MTU %d", self.label, e, DEFAULT_MTU)
                self._mtu = DEFAULT_MTU
            else:
                size = rsp.get("buf_size")
                if isinstance(size, int) and not isinstance(size, bool) and size >= _MIN_MTU:
                    self._mtu = min(size, DEFAULT_MTU)
                else:
                    self._mtu = DEFAULT_MTU
        return self._mtu

    # -- OS / FS --------------------------------------------------------------
    async def echo(self, text: str) -> str:
        rsp = await self.request(OP_WRITE, GROUP_OS, OS_ECHO, {"d": text})
        reply = rsp.get("r")
        if not isinstance(reply, str):
            raise self._malformed("echo", rsp)
        return reply

    async def reset(self) -> None:
        await self.request(OP_WRITE, GROUP_OS, OS_RESET, {})

    async def file_upload(self, path: str, data: bytes) -> None:
        self._check_path(path)
        data = bytes(data)
        total = len(data)
        async with self._fs_lock:
            mtu = await self.mtu()
            off = 0
            resyncs = 0
            while True:
                body: dict[str, Any] = {"name": path, "off": off}
                if off == 0:
                    body["len"] = total
                # +2: a non-empty bstr header is up to 3 bytes, the empty one 1.
                room = mtu - HEADER_SIZE - len(cbor2.dumps({**body, "data": b""})) - 2
                if room < 1:
                    raise DeviceError(f"{self.label}: MTU {mtu} is too small to upload {path}")
                chunk = data[off : off + room]
                body["data"] = chunk
                frame = await self._transact(OP_WRITE, GROUP_FS, FS_FILE, body)
                rsp = frame.body
                resync = rsp.get("len")
                if (
                    error_code(rsp) == (GROUP_FS, FS_ERR_FILE_OFFSET_NOT_VALID)
                    and resyncs < _MAX_UPLOAD_RESYNCS
                    and isinstance(resync, int)
                    and 0 < resync <= total
                ):
                    # A retried chunk the node had already written, or a stale
                    # upload context: continue from the node's file length.
                    resyncs += 1
                    logger.debug("%s: upload %s resumes at %d", self.label, path, resync)
                    off = resync
                    continue
                raise_for_error(rsp, context=f"{self.label}: upload {path}")
                new_off = rsp.get("off")
                if isinstance(new_off, bool) or not isinstance(new_off, int) or not 0 <= new_off <= total:
                    raise self._malformed(f"upload {path}", rsp)
                if chunk and new_off <= off:
                    raise DeviceError(f"{self.label}: upload of {path} stalled at offset {off}")
                off = new_off
                if off >= total:
                    break
            await self._verify_upload(path, data)

    async def _verify_upload(self, path: str, data: bytes) -> None:
        try:
            digest = await self.file_sha256(path)
        except Unsupported:
            logger.info("%s: node has no sha256 file hash; %s not verified", self.label, path)
            return
        if digest != hashlib.sha256(data).digest():
            raise DeviceError(
                f"{self.label}: sha256 of {path} does not match the uploaded data", group=GROUP_FS
            )

    async def file_download(self, path: str) -> bytes:
        self._check_path(path)
        async with self._fs_lock:
            buf = bytearray()
            total = 0
            while True:
                off = len(buf)
                rsp = await self.request(OP_READ, GROUP_FS, FS_FILE, {"name": path, "off": off})
                data = rsp.get("data")
                if not isinstance(data, bytes) or rsp.get("off") != off:
                    raise self._malformed(f"download {path}", rsp)
                if off == 0:
                    size = rsp.get("len")
                    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                        raise self._malformed(f"download {path}", rsp)
                    total = size
                buf += data
                if len(buf) >= total:
                    return bytes(buf[:total])
                if not data:
                    raise DeviceError(f"{self.label}: download of {path} stalled at offset {off}")

    async def file_sha256(self, path: str) -> bytes | None:
        self._check_path(path)
        frame = await self._transact(OP_READ, GROUP_FS, FS_HASH, {"name": path, "type": "sha256"})
        rsp = frame.body
        if error_code(rsp) == (GROUP_FS, FS_ERR_FILE_EMPTY):
            return _SHA256_EMPTY
        try:
            raise_for_error(rsp, context=f"{self.label}: sha256 {path}")
        except NotFound:
            return None
        output = rsp.get("output")
        if not isinstance(output, bytes) or len(output) != 32:
            raise self._malformed(f"sha256 {path}", rsp)
        return output

    # -- uc_node ----------------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        return await self.request(OP_READ, GROUP_UC_NODE, NODE_INFO)

    async def reload(self, doc: str) -> bool:
        if doc not in RELOAD_DOCS:
            raise InvalidRequest(f"reload doc must be one of {sorted(RELOAD_DOCS)}, not {doc!r}")
        rsp = await self.request(OP_WRITE, GROUP_UC_NODE, NODE_RELOAD, {"doc": doc})
        return bool(rsp.get("reboot_required", False))

    async def objects(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(_MAX_OBJECT_PAGES):
            rsp = await self.request(
                OP_READ, GROUP_UC_NODE, NODE_OBJECTS, {"offset": offset, "count": self.objects_page}
            )
            page, total = rsp.get("objects"), rsp.get("total")
            if not isinstance(page, list) or isinstance(total, bool) or not isinstance(total, int):
                raise self._malformed("objects", rsp)
            out.extend(o for o in page if isinstance(o, dict))
            offset += len(page)
            if not page or offset >= total:
                return out
        raise DeviceError(f"{self.label}: object list did not end after {_MAX_OBJECT_PAGES} pages")

    async def prop_read(
        self, obj_type: str | int, instance: int, prop: str | int = "present-value",
        index: int | None = None,
    ) -> Any:
        body = self._prop_body(obj_type, instance, prop, index)
        rsp = await self.request(OP_READ, GROUP_UC_NODE, NODE_PROP_READ, body)
        if "value" not in rsp:
            raise self._malformed("prop_read", rsp)
        return rsp["value"]

    async def prop_write(
        self, obj_type: str | int, instance: int, value: Any,
        prop: str | int = "present-value", priority: int | None = None,
        index: int | None = None, lease_ms: int | None = None,
    ) -> None:
        body = self._prop_body(obj_type, instance, prop, index)
        body["value"] = value
        if priority is not None:
            body["priority"] = _uint(priority, "priority", 16)
            if priority < 1:
                raise InvalidRequest("priority must be 1..16")
        if lease_ms is not None:
            body["lease_ms"] = _uint(lease_ms, "lease_ms")
        await self.request(OP_WRITE, GROUP_UC_NODE, NODE_PROP_WRITE, body)

    async def identify(self, seconds: int = 30) -> None:
        await self.request(
            OP_WRITE, GROUP_UC_NODE, NODE_IDENTIFY, {"seconds": _uint(seconds, "seconds")}
        )

    @staticmethod
    def _prop_body(
        obj_type: str | int, instance: int, prop: str | int, index: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": names.wire_object_type(obj_type),
            "instance": _uint(instance, "instance", 0x3FFFFF),
            "prop": names.wire_property(prop),
        }
        if index is not None:
            if isinstance(index, bool) or not isinstance(index, int):
                raise InvalidRequest(f"index must be an integer, not {index!r}")
            body["index"] = index
        return body

    # -- uc_io ------------------------------------------------------------------
    async def io_catalog(self) -> dict[str, Any]:
        rsp = await self.request(OP_READ, GROUP_UC_IO, IO_CATALOG)
        if not isinstance(rsp.get("channels"), list):
            raise self._malformed("io catalog", rsp)
        return rsp

    async def io_read(self, name: str | None = None) -> dict[str, float]:
        rsp = await self.request(OP_READ, GROUP_UC_IO, IO_READ, {"name": name} if name else {})
        values = rsp.get("values")
        if not isinstance(values, dict):
            raise self._malformed("io read", rsp)
        try:
            return {str(k): float(v) for k, v in values.items()}
        except (TypeError, ValueError) as e:
            raise self._malformed("io read", rsp) from e

    async def io_write(self, name: str, value: float) -> None:
        await self.request(OP_WRITE, GROUP_UC_IO, IO_WRITE, {"name": name, "value": float(value)})

    async def io_force(
        self, name: str, value: float | None, lease_ms: int | None = None
    ) -> None:
        body: dict[str, Any] = {"name": name}
        if value is None:
            body["release"] = True
        else:
            body["value"] = float(value)
            if lease_ms is not None:
                body["lease_ms"] = _uint(lease_ms, "lease_ms")
        await self.request(OP_WRITE, GROUP_UC_IO, IO_FORCE, body)

    # -- uc_app -----------------------------------------------------------------
    async def app_list(self) -> list[dict[str, Any]]:
        rsp = await self.request(OP_READ, GROUP_UC_APP, APP_LIST)
        apps = rsp.get("apps")
        if not isinstance(apps, list):
            raise self._malformed("app list", rsp)
        return [a for a in apps if isinstance(a, dict)]

    async def app_install(self, manifest: dict[str, Any]) -> None:
        await self.request(OP_WRITE, GROUP_UC_APP, APP_INSTALL, self.wire_manifest(manifest))

    @staticmethod
    def wire_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
        """``<app manifest>`` as the node expects it: ``params`` a map of
        strings (apps.json's ``[{"key", "value"}]`` list is accepted too) and
        ``sha256`` 32 raw bytes (hex text is accepted)."""
        if not isinstance(manifest.get("name"), str) or not isinstance(manifest.get("file"), str):
            raise InvalidRequest("app manifest needs text fields 'name' and 'file'")
        body = dict(manifest)
        params = body.get("params")
        if params is not None:
            if isinstance(params, list):
                try:
                    params = {p["key"]: p["value"] for p in params}
                except (KeyError, TypeError) as e:
                    raise InvalidRequest("params list items need 'key' and 'value'") from e
            if not isinstance(params, Mapping):
                raise InvalidRequest("params must be a map")
            body["params"] = {str(k): _param_text(v) for k, v in params.items()}
        sha = body.get("sha256")
        if sha is not None:
            if isinstance(sha, str):
                try:
                    sha = bytes.fromhex(sha)
                except ValueError as e:
                    raise InvalidRequest("sha256 must be 64 hex digits") from e
            if not isinstance(sha, (bytes, bytearray)) or len(sha) != 32:
                raise InvalidRequest("sha256 must be 32 bytes")
            body["sha256"] = bytes(sha)
        return body

    async def app_start(self, name: str) -> None:
        await self.request(OP_WRITE, GROUP_UC_APP, APP_START, {"name": name})

    async def app_stop(self, name: str) -> None:
        await self.request(OP_WRITE, GROUP_UC_APP, APP_STOP, {"name": name})

    async def app_remove(self, name: str, delete_file: bool = False) -> None:
        body: dict[str, Any] = {"name": name}
        if delete_file:
            body["delete_file"] = True
        await self.request(OP_WRITE, GROUP_UC_APP, APP_REMOVE, body)

    async def app_status(self, name: str) -> dict[str, Any]:
        return await self.request(OP_READ, GROUP_UC_APP, APP_STATUS, {"name": name})

    # -- helpers ----------------------------------------------------------------
    def _malformed(self, what: str, rsp: Mapping[str, Any]) -> DeviceError:
        keys = ", ".join(sorted(rsp)) or "none"
        return DeviceError(f"{self.label}: malformed {what} response (keys: {keys})")

    @staticmethod
    def _check_path(path: str) -> None:
        if not isinstance(path, str) or not path.startswith("/") or "\0" in path:
            raise InvalidRequest(f"file paths must be absolute, not {path!r}")

    def __repr__(self) -> str:
        return f"SmpNodeClient({self.label!r}, {self.address!r})"
