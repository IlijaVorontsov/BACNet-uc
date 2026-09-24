"""Driver for BACnet-uc nodes (the manifest's ``system.nodes``), over SMP.

Every node gets one ``SmpNodeClient``. Points are the node's BACnet objects
(``uc_node objects``) except the device object and types that carry no
value; their origin comes from the object owner and the IO catalog binding.
Live values are polled: one task per node reads the watched points every
``poll_interval_s``, publishes the ones that changed and everything every
``refresh_s``, and probes nodes without watched points with ``uc_node info``
every ``heartbeat_s`` so online state stays current.

Settings (``drivers.bacnet_uc`` in hub.yaml, all optional): ``timeout_s``,
``retries``, ``mtu``, ``max_inflight``, ``objects_page`` (client);
``poll_interval_s`` (2), ``refresh_s`` (30), ``heartbeat_s`` (15),
``read_concurrency`` (8), ``units_cache_s`` (300), ``discover_broadcast``
("255.255.255.255:1337", a list, or empty to disable) and ``sim_addresses``
(node name -> "host:port" for ``transport: sim`` nodes).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import random
import socket
import time
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ...core.driver import Driver, DriverContext
from ...core.errors import (
    DeviceError,
    DeviceTimeout,
    HubError,
    InvalidRequest,
    NotFound,
    Unsupported,
)
from ...core.ids import (
    COMMANDABLE_TYPES,
    OBJECT_TYPES,
    OPTIONALLY_COMMANDABLE_TYPES,
    PointRef,
    bacnet_obj,
)
from ...core.types import (
    DeviceDescription,
    DeviceRecord,
    DiscoveredDevice,
    Point,
    PointKind,
    ProtocolName,
    Quality,
    Reading,
    Value,
    WriteResult,
)
from . import names
from .api import GROUP_UC_NODE, NODE_INFO, NodeApi
from .client import DEFAULT_PORT, SmpNodeClient, format_address, parse_address
from .smp import (
    OP_READ,
    OP_READ_RSP,
    SmpFrameError,
    decode_frame,
    encode_frame,
    error_code,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_REFRESH_S = 30.0
DEFAULT_HEARTBEAT_S = 15.0
DEFAULT_READ_CONCURRENCY = 8
DEFAULT_UNITS_CACHE_S = 300.0
DEFAULT_DISCOVER_BROADCAST = "255.255.255.255:1337"

#: Object types that become points (the device object does not).
POINT_TYPES = frozenset(t for t in OBJECT_TYPES if t != "device")
PRIORITY_TYPES = frozenset(COMMANDABLE_TYPES | OPTIONALLY_COMMANDABLE_TYPES)
_TRUE_WORDS = frozenset({"active", "on", "true", "1"})
_FALSE_WORDS = frozenset({"inactive", "off", "false", "0"})
_ANSWERED, _TIMED_OUT, _NOT_SENT = "answered", "timeout", "not-sent"


def point_kind(type_name: str) -> PointKind:
    if type_name.endswith("-input"):
        return PointKind.INPUT
    if type_name.endswith("-output"):
        return PointKind.OUTPUT
    return PointKind.VALUE


def normalize_value(type_name: str, value: Any) -> Value:
    """A node value as the hub's ``Value``: analog float, binary 0/1,
    multi-state int."""
    if value is None or isinstance(value, str):
        return value
    if type_name.startswith("analog-") and isinstance(value, (int, float)):
        return float(value)
    if type_name.startswith(("binary-", "multi-state-")) and isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (bool, int, float)):
        return value
    return str(value)


def wire_value(type_name: str, value: Value) -> Value:
    """A hub value in the datatype the object's present value expects;
    ``InvalidRequest`` when it cannot be one."""
    if value is None:
        return None
    number: float | None = None
    if isinstance(value, (bool, int, float)):
        number = float(value)
    elif isinstance(value, str):
        word = value.strip().lower()
        if type_name.startswith("binary-") and word in _TRUE_WORDS | _FALSE_WORDS:
            return 1 if word in _TRUE_WORDS else 0
        try:
            number = float(word)
        except ValueError:
            number = None
    if number is None or not math.isfinite(number):
        raise InvalidRequest(f"{value!r} is not a valid value for a {type_name}")
    if type_name.startswith("analog-"):
        return number
    if type_name.startswith("binary-"):
        if number not in (0.0, 1.0):
            raise InvalidRequest(f"{type_name} takes 0 or 1, not {value!r}")
        return int(number)
    if not number.is_integer() or number < 1:
        raise InvalidRequest(f"{type_name} takes a state number >= 1, not {value!r}")
    return int(number)


def _target(ref: PointRef) -> tuple[str, int]:
    target = ref.bacnet
    if target is None or target[0] not in POINT_TYPES:
        raise InvalidRequest(f"{ref} is not a BACnet-uc point (expected <type>:<instance>)")
    return target


@dataclass(eq=False)
class _Node:
    record: DeviceRecord
    spec: dict[str, Any]
    client: SmpNodeClient | None
    #: Why the node cannot be reached at all (no transport), else None.
    unavailable: str | None = None
    #: (object, owner, name) -> (monotonic time, units)
    units: dict[tuple[str, str, str], tuple[float, str | None]] = field(default_factory=dict)
    online: bool | None = None
    watched: set[PointRef] = field(default_factory=set)
    last: dict[PointRef, tuple[Value, Quality]] = field(default_factory=dict)
    poller: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    last_refresh: float = -math.inf
    last_heartbeat: float = -math.inf

    @property
    def name(self) -> str:
        return self.record.name


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, Any]]) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._queue.put_nowait((data, addr))

    def error_received(self, exc: Exception) -> None:
        logger.debug("discovery socket error: %s", exc)


class BacnetUcDriver(Driver):
    protocol = ProtocolName.BACNET_UC

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        s = ctx.settings
        self.poll_interval_s = float(s.get("poll_interval_s", DEFAULT_POLL_INTERVAL_S))
        self.refresh_s = float(s.get("refresh_s", DEFAULT_REFRESH_S))
        self.heartbeat_s = float(s.get("heartbeat_s", DEFAULT_HEARTBEAT_S))
        self.units_cache_s = float(s.get("units_cache_s", DEFAULT_UNITS_CACHE_S))
        self._read_limit = asyncio.Semaphore(int(s.get("read_concurrency", DEFAULT_READ_CONCURRENCY)))
        self._nodes: dict[str, _Node] = {}
        self._started = False

    # -- life cycle ---------------------------------------------------------------
    async def add_device(self, record: DeviceRecord, spec: dict[str, Any]) -> None:
        if record.name in self._nodes:
            await self.remove_device(record.name)
        record.managed = True
        client, reason = self._make_client(record.name, spec)
        if client is not None:
            record.address = client.address
        node = _Node(record, dict(spec), client, reason)
        self._nodes[record.name] = node
        if reason is not None:
            logger.warning("%s: %s", record.name, reason)
            self._set_online(node, False)
        if self._started:
            self._ensure_poller(node)

    def _make_client(self, name: str, spec: Mapping[str, Any]) -> tuple[SmpNodeClient | None, str | None]:
        """The node's client, or None and why the node cannot be reached."""
        transport = spec.get("transport")
        if not isinstance(transport, Mapping):
            return None, "the node has no transport"
        kind = transport.get("kind")
        host: Any = None
        port = DEFAULT_PORT
        if kind == "udp":
            host = transport.get("host")
            port = int(transport.get("port", DEFAULT_PORT))
            if not host:
                return None, "transport.host is missing"
        elif kind == "sim":
            address = (self.ctx.settings.get("sim_addresses") or {}).get(name)
            if address is not None:
                try:
                    host, port = self._sim_address(address)
                except InvalidRequest as e:
                    return None, f"bad simulation address: {e}"
            elif transport.get("ipv4"):
                host = transport["ipv4"]
            else:
                return None, "no address for the simulated node (settings sim_addresses)"
        elif kind == "serial":
            return None, (f"serial transport ({transport.get('device')}) is not implemented yet; "
                          "the node is treated as offline")
        else:
            return None, f"unknown transport kind {kind!r}"
        return SmpNodeClient.from_settings(str(host), port, self.ctx.settings, name=name), None

    @staticmethod
    def _sim_address(address: Any) -> tuple[str, int]:
        if isinstance(address, str):
            return parse_address(address)
        if isinstance(address, (tuple, list)) and len(address) == 2:
            return str(address[0]), int(address[1])
        if isinstance(address, Mapping) and "host" in address:
            return str(address["host"]), int(address.get("port", DEFAULT_PORT))
        raise InvalidRequest(f"{address!r} is not host:port")

    async def remove_device(self, name: str) -> None:
        node = self._nodes.pop(name, None)
        if node is not None:
            await self._shutdown(node)

    async def start(self) -> None:
        self._started = True
        for node in self._nodes.values():
            self._ensure_poller(node)

    async def stop(self) -> None:
        self._started = False
        await asyncio.gather(*(self._shutdown(node) for node in list(self._nodes.values())))

    async def _shutdown(self, node: _Node) -> None:
        task, node.poller = node.poller, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if node.client is not None:
            await node.client.close()
            node.client = None
            node.unavailable = "driver stopped"

    # -- access for the manifest engine ------------------------------------------------
    def node(self, name: str) -> NodeApi:
        """The SMP interface of a node, for plan/apply."""
        return self._client(self._node(name))

    def _node(self, name: str) -> _Node:
        node = self._nodes.get(name)
        if node is None:
            raise NotFound(f"unknown bacnet-uc device {name!r}")
        return node

    @staticmethod
    def _client(node: _Node) -> SmpNodeClient:
        if node.client is None:
            raise DeviceTimeout(f"{node.name}: {node.unavailable}")
        return node.client

    # -- inventory ---------------------------------------------------------------------
    async def discover(self, timeout_s: float = 3.0) -> list[DiscoveredDevice]:
        loop = asyncio.get_running_loop()
        targets = await self._discovery_targets()
        if not targets:
            return []
        queue: asyncio.Queue[tuple[bytes, Any]] = asyncio.Queue()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _DiscoveryProtocol(queue), local_addr=("0.0.0.0", 0), allow_broadcast=True
            )
        except OSError as e:
            raise DeviceError(f"cannot open the discovery socket: {e}") from e
        seq = random.randrange(256)
        frame = encode_frame(OP_READ, GROUP_UC_NODE, NODE_INFO, seq, {})
        found: dict[str, DiscoveredDevice] = {}

        def send() -> None:
            for addr in targets:
                try:
                    transport.sendto(frame, addr)
                except OSError as e:
                    logger.debug("discovery: cannot send to %s: %s", addr, e)

        try:
            send()
            deadline = loop.time() + timeout_s
            resend_at = deadline - timeout_s / 2  # once more, for lost datagrams
            while (now := loop.time()) < deadline:
                if resend_at and now >= resend_at:
                    resend_at = 0.0
                    send()
                try:
                    data, addr = await asyncio.wait_for(queue.get(), (resend_at or deadline) - now)
                except TimeoutError:
                    continue
                device = self._discovered(data, addr, seq)
                if device is not None:
                    found.setdefault(device.address, device)
        finally:
            transport.close()
        return list(found.values())

    async def _discovery_targets(self) -> list[tuple[str, int]]:
        setting = self.ctx.settings.get("discover_broadcast", DEFAULT_DISCOVER_BROADCAST)
        texts = [setting] if isinstance(setting, str) else list(setting or [])
        wanted: list[tuple[str, int]] = []
        for text in texts:
            if not text:
                continue
            try:
                wanted.append(parse_address(text))
            except InvalidRequest as e:
                logger.warning("discover_broadcast: %s", e)
        wanted += [(n.client.host, n.client.port) for n in self._nodes.values() if n.client]
        loop = asyncio.get_running_loop()
        resolved: list[tuple[str, int]] = []
        for host, port in wanted:
            try:
                infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
            except OSError as e:
                logger.debug("discovery: cannot resolve %s: %s", host, e)
                continue
            if not infos:
                continue
            addr = (str(infos[0][4][0]), int(infos[0][4][1]))
            if addr not in resolved:
                resolved.append(addr)
        return resolved

    def _discovered(self, data: bytes, addr: Any, seq: int) -> DiscoveredDevice | None:
        try:
            frame = decode_frame(data)
        except SmpFrameError:
            return None
        if (frame.op, frame.group, frame.command, frame.seq) != (OP_READ_RSP, GROUP_UC_NODE, NODE_INFO, seq):
            return None
        info = frame.body
        if error_code(info) is not None:
            return None
        device: Any = info.get("device")
        if not isinstance(device, dict):
            device = {}
        instance = device.get("instance")
        return DiscoveredDevice(
            protocol=ProtocolName.BACNET_UC,
            address=format_address(str(addr[0]), int(addr[1])),
            instance=instance if isinstance(instance, int) and not isinstance(instance, bool) else None,
            name=str(device.get("name", "")),
            model=str(info.get("board", "")),
            hwid=str(info.get("hwid") or ""),
            bacnet_uc=True,
            extra={k: info[k] for k in ("fw", "mac", "net") if k in info},
        )

    async def describe(self, device: str) -> DeviceDescription:
        node = self._node(device)
        client = self._client(node)
        try:
            info = await client.info()
            objects = await client.objects()
            catalog = await self._optional(client.io_catalog(), f"{device}: io catalog")
            apps = await self._optional(client.app_list(), f"{device}: app list")
        except DeviceTimeout:
            self._set_online(node, False)
            raise
        self._set_online(node, True)
        self._apply_info(node, info)
        channels = self._bound_channels(node, catalog)
        points = [p for p in (self._point(node, o, channels) for o in objects) if p is not None]
        await self._fill_units(node, client, points)
        return DeviceDescription(
            device=node.record, points=points, apps=apps or [],
            extra={"info": info, "io": catalog or {}},
        )

    @staticmethod
    async def _optional(call: Awaitable[T], what: str) -> T | None:
        try:
            return await call
        except (DeviceError, NotFound, Unsupported) as e:
            logger.info("%s unavailable: %s", what, e)
            return None

    @staticmethod
    def _bound_channels(node: _Node, catalog: Mapping[str, Any] | None) -> dict[tuple[str, int], str]:
        """(type name, instance) -> IO channel, from the catalog and, for
        firmware that does not report bindings, the manifest's io list."""
        out: dict[tuple[str, int], str] = {}
        for p in node.spec.get("io") or []:
            if isinstance(p, Mapping) and isinstance(p.get("type"), str) and isinstance(p.get("instance"), int):
                out[(p["type"], p["instance"])] = str(p.get("channel", ""))
        for ch in (catalog or {}).get("channels") or []:
            obj = ch.get("object") if isinstance(ch, Mapping) else None
            if not isinstance(obj, Mapping):
                continue
            obj_type: Any = obj.get("type")
            instance: Any = obj.get("instance")
            try:
                out[(names.object_type_name(obj_type), int(instance))] = str(ch.get("name"))
            except (InvalidRequest, TypeError, ValueError):
                continue
        return out

    def _point(self, node: _Node, obj: Mapping[str, Any], channels: Mapping[tuple[str, int], str]) -> Point | None:
        obj_type: Any = obj.get("type")
        try:
            type_name = names.object_type_name(obj_type)
        except InvalidRequest:
            return None
        instance = obj.get("instance")
        if type_name not in POINT_TYPES or isinstance(instance, bool) or not isinstance(instance, int):
            return None
        owner = str(obj.get("owner") or "")
        if owner == "io":
            channel = channels.get((type_name, instance))
            source = f"io:{channel}" if channel else "io"
        elif owner.startswith("app:"):
            source = owner
        else:
            source = "system"
        kind = point_kind(type_name)
        oid = bacnet_obj(type_name, instance)
        return Point(
            ref=PointRef(self.ctx.site, node.name, oid),
            name=str(obj.get("name") or oid),
            kind=kind,
            datatype="real" if type_name.startswith("analog-") else "enum",
            writable=kind is not PointKind.INPUT,
            commandable=type_name in PRIORITY_TYPES,
            source=source,
            space=node.record.space,
            meta={"type": OBJECT_TYPES[type_name], "instance": instance, "owner": owner},
        )

    async def _fill_units(self, node: _Node, client: SmpNodeClient, points: list[Point]) -> None:
        now = time.monotonic()
        offline = False

        async def one(point: Point) -> None:
            nonlocal offline
            key = (point.ref.obj, point.meta["owner"], point.name)
            cached = node.units.get(key)
            if cached is not None and now - cached[0] < self.units_cache_s:
                point.units = cached[1]
                return
            if offline:
                return
            try:
                raw = await client.prop_read(point.meta["type"], point.meta["instance"], names.PROP_UNITS)
            except DeviceTimeout:
                offline = True
                return
            except (DeviceError, NotFound, Unsupported, InvalidRequest) as e:
                logger.debug("%s: units of %s: %s", node.name, point.ref.obj, e)
                return
            point.units = names.units_name(raw)
            node.units[key] = (now, point.units)

        await self._gather(one(p) for p in points if p.datatype == "real")

    async def _gather(self, calls: Iterable[Awaitable[T]]) -> list[T]:
        async def limited(call: Awaitable[T]) -> T:
            async with self._read_limit:
                return await call

        return await asyncio.gather(*(limited(c) for c in calls))

    # -- values ------------------------------------------------------------------------
    async def read(self, refs: list[PointRef]) -> list[Reading]:
        readings = await self._read_many(refs)
        for reading in readings:
            self._publish(reading)
        return readings

    async def _read_many(self, refs: list[PointRef]) -> list[Reading]:
        results = await self._gather(self._read_one(ref) for ref in refs)
        answered: dict[str, bool] = {}
        for ref, (_, contact) in zip(refs, results, strict=True):
            if contact == _ANSWERED:
                answered[ref.device] = True
            elif contact == _TIMED_OUT:
                answered.setdefault(ref.device, False)
        for name, online in answered.items():
            node = self._nodes.get(name)
            if node is not None:
                self._set_online(node, online)
        return [reading for reading, _ in results]

    async def _read_one(self, ref: PointRef) -> tuple[Reading, str]:
        node = self._nodes.get(ref.device)
        if node is None:
            return Reading(ref, None, quality=Quality.FAULT,
                           error=f"unknown bacnet-uc device {ref.device!r}"), _NOT_SENT
        try:
            type_name, instance = _target(ref)
        except InvalidRequest as e:
            return Reading(ref, None, quality=Quality.FAULT, error=str(e)), _NOT_SENT
        if node.client is None:
            return Reading(ref, None, quality=Quality.OFFLINE, error=node.unavailable), _NOT_SENT
        try:
            value = await node.client.prop_read(type_name, instance)
        except DeviceTimeout as e:
            return Reading(ref, None, quality=Quality.OFFLINE, error=str(e)), _TIMED_OUT
        except (DeviceError, NotFound, Unsupported, InvalidRequest) as e:
            return Reading(ref, None, quality=Quality.FAULT, error=str(e)), _ANSWERED
        return Reading(ref, normalize_value(type_name, value)), _ANSWERED

    async def write(
        self, ref: PointRef, value: Value, priority: int | None, *, lease_ms: int | None = None,
    ) -> WriteResult:
        """Also takes ``lease_ms``: the node relinquishes the write by itself
        when it expires (firmware request B2; older firmware ignores it, so
        the hub keeps relinquishing on its own)."""
        node = self._node(ref.device)
        type_name, instance = _target(ref)
        payload = wire_value(type_name, value)
        try:
            client = self._client(node)
            await client.prop_write(type_name, instance, payload, priority=priority, lease_ms=lease_ms)
        except DeviceTimeout as e:
            if node.client is not None:
                self._set_online(node, False)
            return WriteResult(ref, False, value, priority, str(e))
        except (DeviceError, NotFound, Unsupported) as e:
            self._set_online(node, True)
            return WriteResult(ref, False, value, priority, str(e))
        self._set_online(node, True)
        node.wake.set()
        return WriteResult(ref, True, value, priority)

    async def relinquish(self, ref: PointRef, priority: int) -> WriteResult:
        return await self.write(ref, None, priority)

    async def priority_array(self, ref: PointRef) -> list[Value] | None:
        node = self._node(ref.device)
        type_name, instance = _target(ref)
        if type_name not in PRIORITY_TYPES:
            return None
        client = self._client(node)
        try:
            whole = await client.prop_read(type_name, instance, names.PROP_PRIORITY_ARRAY)
        except NotFound:
            await client.prop_read(type_name, instance)  # NotFound when the object is missing
            return None
        except (DeviceError, Unsupported) as e:
            logger.debug("%s: whole priority array unreadable (%s), reading slots", ref, e)
            whole = None
        if isinstance(whole, list) and len(whole) == 16:
            return [normalize_value(type_name, v) for v in whole]
        # The firmware decodes only the first element of an array read.
        slots = await asyncio.gather(
            *(client.prop_read(type_name, instance, names.PROP_PRIORITY_ARRAY, index=i)
              for i in range(1, 17)),
            return_exceptions=True,
        )
        for slot in slots:
            if isinstance(slot, NotFound):
                return None
        for slot in slots:
            if isinstance(slot, BaseException):
                raise slot
        return [normalize_value(type_name, v) for v in slots]

    async def watch(self, refs: list[PointRef]) -> None:
        for ref in refs:
            node = self._nodes.get(ref.device)
            if node is None:
                logger.warning("watch: %s is not a bacnet-uc device", ref.device)
                continue
            if ref not in node.watched:
                node.watched.add(ref)
                node.wake.set()
            self._ensure_poller(node)

    async def unwatch(self, refs: list[PointRef]) -> None:
        for ref in refs:
            node = self._nodes.get(ref.device)
            if node is not None:
                node.watched.discard(ref)
                node.last.pop(ref, None)

    # -- node features -----------------------------------------------------------------
    async def identify(self, device: str, seconds: int = 30) -> None:
        node = self._node(device)
        try:
            await self._client(node).identify(seconds)
        except Unsupported as e:
            raise Unsupported(
                f"{device}: this firmware cannot identify (no uc_node cmd 5); "
                f"find the board by its address {node.record.address} instead ({e})"
            ) from e

    async def force(
        self, device: str, channel: str, value: float | None, lease_ms: int | None = None,
    ) -> None:
        """Force an IO channel to a raw value (None releases the force)."""
        node = self._node(device)
        await self._client(node).io_force(channel, value, lease_ms)
        node.wake.set()

    # -- polling -----------------------------------------------------------------------
    def _ensure_poller(self, node: _Node) -> None:
        if node.client is None or (node.poller is not None and not node.poller.done()):
            return
        node.poller = asyncio.create_task(self._poll_loop(node), name=f"bacnet-uc-poll-{node.name}")

    async def _poll_loop(self, node: _Node) -> None:
        while True:
            try:
                await self._poll_once(node)
            except Exception:  # keep polling; the traceback is logged
                logger.exception("%s: polling failed", node.name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(node.wake.wait(), self.poll_interval_s)
            node.wake.clear()

    async def _poll_once(self, node: _Node) -> None:
        now = time.monotonic()
        refs = sorted(node.watched)
        if not refs:
            if now - node.last_heartbeat >= self.heartbeat_s:
                node.last_heartbeat = now
                await self._heartbeat(node)
            return
        if node.online is False and not await self._heartbeat(node):
            self._publish_changed(node, [
                Reading(ref, None, quality=Quality.OFFLINE, error=f"{node.name} is offline")
                for ref in refs
            ], full=False)
            return
        full = now - node.last_refresh >= self.refresh_s
        if full:
            node.last_refresh = now
        self._publish_changed(node, await self._read_many(refs), full)

    def _publish_changed(self, node: _Node, readings: list[Reading], full: bool) -> None:
        for reading in readings:
            state = (reading.value, reading.quality)
            if full or node.last.get(reading.ref) != state:
                node.last[reading.ref] = state
                self._publish(reading)

    async def _heartbeat(self, node: _Node) -> bool:
        client = node.client
        if client is None:
            return False
        try:
            info = await client.info()
        except DeviceTimeout:
            self._set_online(node, False)
            return False
        except HubError as e:
            logger.debug("%s: info failed: %s", node.name, e)
        else:
            self._apply_info(node, info)
        self._set_online(node, True)
        return True

    # -- state -------------------------------------------------------------------------
    @staticmethod
    def _apply_info(node: _Node, info: Mapping[str, Any]) -> None:
        record = node.record
        if isinstance(info.get("board"), str):
            record.model = info["board"]
        if isinstance(info.get("fw"), str):
            record.firmware = info["fw"]
        if isinstance(info.get("hwid"), str):
            record.hwid = info["hwid"]
        if isinstance(info.get("mac"), str):
            record.meta["mac"] = info["mac"]
        device = info.get("device")
        if isinstance(device, Mapping):
            instance = device.get("instance")
            if isinstance(instance, int) and not isinstance(instance, bool):
                record.instance = instance

    def _set_online(self, node: _Node, online: bool) -> None:
        if online:
            node.record.last_seen = time.time()
        if node.online is online:
            return
        node.online = online
        node.record.online = online
        logger.info("%s is %s", node.name, "online" if online else "offline")
        try:
            self.ctx.set_online(node.name, online)
        except Exception:  # a failing listener must not break device I/O
            logger.exception("set_online callback failed for %s", node.name)

    def _publish(self, reading: Reading) -> None:
        try:
            self.ctx.publish(reading)
        except Exception:  # a failing listener must not break device I/O
            logger.exception("publish callback failed for %s", reading.ref)
