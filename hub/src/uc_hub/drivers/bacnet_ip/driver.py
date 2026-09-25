"""Driver for third-party BACnet/IP devices (``external_devices`` entries with
``protocol: bacnet-ip``), on bacpypes3.

The hub is a BACnet device of its own (instance ``device_instance``, name
``device_name``) with one UDP socket. Manifest entries give ``address``
(``host[:port]``, IPv4, default port 47808), an optional ``device_instance``
(otherwise learnt from the I-Am to a directed Who-Is) and an optional
``points`` allow-list (``[{obj, name?, units?}]``); without it every
point object of the ``object-list`` is mapped, up to ``max_objects``. Points
and value conversion are described in ``mapping``.

Reads use ReadPropertyMultiple in batches of at most ``rpm_max_properties``
property references. A device that rejects the service is read with
ReadProperty from then on; an aborted batch (typically a response the device
cannot segment) is halved until it fits, and the smaller size is kept for the
device. The object list is read whole and, when that fails, element by
element.

Live values: every watched point is subscribed with SubscribeCOV (lifetime
``cov_lifetime_s``, renewed at 80 % of it). Points whose subscription the
device refuses are polled every ``poll_interval_s``, and all watched points
are re-read every ``refresh_s``, which also notices a device that went
silent. A request without an answer marks the device offline; its watched
points then read OFFLINE until the device answers again, when the
subscriptions are renewed.

Settings (``drivers.bacnet_ip`` in hub.yaml, all optional): ``interface``
(``0.0.0.0``; ``a.b.c.d/nn`` also sets the broadcast address), ``port``
(47808), ``broadcast`` (``host[:port]`` for global Who-Is; defaults to
255.255.255.255 on 0.0.0.0 and the subnet broadcast for ``/nn``, ``null``
disables it), ``discover_targets`` (addresses that also get a directed
Who-Is), ``device_instance`` (4194000), ``device_name`` (uc-hub),
``vendor_identifier`` (999), ``timeout_s`` (2.0) and ``retries`` (1) per
request, ``poll_interval_s`` (5), ``refresh_s`` (60), ``cov`` (true),
``cov_lifetime_s`` (300), ``cov_confirmed`` (false), ``cov_process_id`` (1),
``max_objects`` (256), ``rpm_max_properties`` (64), ``device_concurrency``
(2 requests in flight per device).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from bacpypes3.apdu import ErrorRejectAbortNack, IAmRequest, SubscribeCOVRequest, WhoIsRequest
from bacpypes3.basetypes import ErrorType, PropertyIdentifier, PropertyReference, PropertyValue, StatusFlags
from bacpypes3.errors import AbortException, RejectException
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address, GlobalBroadcast, IPv4Address
from bacpypes3.primitivedata import ObjectIdentifier

from ... import __version__
from ...core.driver import Driver, DriverContext
from ...core.errors import DeviceError, DeviceTimeout, HubError, InvalidRequest, NotFound
from ...core.ids import PointRef
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
from .mapping import (
    MAX_INSTANCE,
    POINT_TYPES,
    WILDCARD_DEVICE,
    TypeInfo,
    clean_text,
    obj_text,
    object_identifier,
    parse_point_obj,
    present_value_type,
    priority_slot,
    to_bacnet,
    to_value,
    units_name,
)
from .stack import (
    DEFAULT_PORT,
    ApplicationClosed,
    BacnetError,
    HubApplication,
    bacnet_error,
    format_address,
    open_application,
    parse_host_port,
    to_address,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

PV = "present-value"
STATUS_FLAGS = "status-flags"
PRIORITY_ARRAY = "priority-array"
_PV_ID = int(PropertyIdentifier(PV))
_FLAGS_ID = int(PropertyIdentifier(STATUS_FLAGS))
DEVICE_PROPS = (
    "object-identifier",
    "object-name",
    "vendor-name",
    "vendor-identifier",
    "model-name",
    "firmware-revision",
    "application-software-version",
    "location",
    "description",
    "protocol-revision",
    "max-apdu-length-accepted",
    "segmentation-supported",
)
#: Share of a COV lifetime after which the subscription is renewed.
RENEW_FRACTION = 0.8
#: Parallel ReadProperty requests when the object list is read element by element.
_INDEX_WINDOW = 8
#: Elements read at most that way, whatever the list's length says.
_INDEX_LIMIT = 4096
#: What bacpypes3 raises for an answer it cannot decode.
_DECODE_ERRORS = (ValueError, TypeError, AttributeError, IndexError, KeyError, RejectException, AbortException)
#: Discovered devices whose names are read; the rest only get instance and address.
_PEEK_LIMIT = 64
_FAULT_TEXT = "the device reports a fault (status-flags)"
#: Abort and reject reasons of a ReadPropertyMultiple that asked for too much
#: at once; other failures of a batch are about one of its properties.
_TOO_BIG = frozenset({
    "buffer-overflow", "segmentation-not-supported", "window-size-out-of-range",
    "application-exceeded-reply-time", "out-of-resources", "tsm-timeout", "apdu-too-long",
    "server-timeout", "too-many-arguments",
})


class NotStarted(HubError):
    """The driver is not running (not started yet, or stopped meanwhile)."""


@dataclass(frozen=True, slots=True)
class _PropError:
    """A property the device answered with an error (per-property)."""

    message: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class _Settings:
    interface: str
    port: int
    broadcast: tuple[str, int] | None
    discover_targets: tuple[tuple[str, int], ...]
    device_instance: int
    device_name: str
    vendor_identifier: int
    timeout_s: float
    retries: int
    poll_interval_s: float
    refresh_s: float
    cov: bool
    cov_lifetime_s: int
    cov_confirmed: bool
    cov_process_id: int
    max_objects: int
    rpm_max_properties: int
    device_concurrency: int

    @classmethod
    def parse(cls, s: Mapping[str, Any]) -> _Settings:
        try:
            iface = ipaddress.IPv4Interface(str(s.get("interface", "0.0.0.0")))
        except ValueError as e:
            raise InvalidRequest(f"bacnet_ip.interface: {e}") from None
        port = _int(s, "port", DEFAULT_PORT, 0, 65535)
        if "broadcast" in s:
            raw = s.get("broadcast")
            broadcast = parse_host_port(str(raw), port) if raw else None
        elif port == 0:
            broadcast = None  # an ephemeral port has no peers listening on it
        elif iface.ip == ipaddress.IPv4Address("0.0.0.0"):
            broadcast = ("255.255.255.255", port)
        elif iface.network.prefixlen < 31:
            broadcast = (str(iface.network.broadcast_address), port)
        else:
            broadcast = None
        targets = s.get("discover_targets") or []
        if isinstance(targets, str) or not isinstance(targets, Iterable):
            raise InvalidRequest("bacnet_ip.discover_targets must be a list of host[:port]")
        return cls(
            interface=str(iface.ip),
            port=port,
            broadcast=broadcast,
            discover_targets=tuple(parse_host_port(str(t), DEFAULT_PORT) for t in targets),
            device_instance=_int(s, "device_instance", 4194000, 0, MAX_INSTANCE),
            device_name=str(s.get("device_name", "uc-hub")) or "uc-hub",
            vendor_identifier=_int(s, "vendor_identifier", 999, 0, 65535),
            timeout_s=_float(s, "timeout_s", 2.0, 60.0),
            retries=_int(s, "retries", 1, 0, 10),
            poll_interval_s=_float(s, "poll_interval_s", 5.0, 86400.0),
            refresh_s=_float(s, "refresh_s", 60.0, 86400.0),
            cov=bool(s.get("cov", True)),
            cov_lifetime_s=_int(s, "cov_lifetime_s", 300, 1, 86400),
            cov_confirmed=bool(s.get("cov_confirmed", False)),
            cov_process_id=_int(s, "cov_process_id", 1, 0, 2**32 - 1),
            max_objects=_int(s, "max_objects", 256, 1, 65535),
            rpm_max_properties=_int(s, "rpm_max_properties", 64, 1, 1024),
            device_concurrency=_int(s, "device_concurrency", 2, 1, 64),
        )


def _int(s: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = s.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidRequest(f"bacnet_ip.{key} must be an integer in {low}..{high}, not {value!r}")
    return int(value)


def _float(s: Mapping[str, Any], key: str, default: float, high: float) -> float:
    value = s.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= high:
        raise InvalidRequest(f"bacnet_ip.{key} must be a number in (0, {high:g}], not {value!r}")
    return float(value)


@dataclass(eq=False)
class _Device:
    record: DeviceRecord
    #: "host:port"; the key COV notifications and I-Ams are matched by.
    key: str
    address: IPv4Address | None
    #: Why the device cannot be reached at all (bad address), else None.
    unavailable: str | None
    #: obj -> manifest entry, None when every object is mapped.
    allow: dict[str, dict[str, Any]] | None
    limit: asyncio.Semaphore
    max_props: int
    rpm: bool = True
    online: bool | None = None
    #: Points of the last describe(), by object id.
    points: dict[str, Point] = field(default_factory=dict)
    watched: dict[str, PointRef] = field(default_factory=dict)
    #: Subscribed objects -> loop time the subscription is renewed at.
    cov: dict[str, float] = field(default_factory=dict)
    #: Watched objects the device refused COV for.
    polled: set[str] = field(default_factory=set)
    last: dict[str, tuple[Value, Quality]] = field(default_factory=dict)
    next_poll: float = 0.0
    next_refresh: float = 0.0
    monitor: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def name(self) -> str:
        return self.record.name


class BacnetIpDriver(Driver):
    protocol = ProtocolName.BACNET_IP

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self.settings = _Settings.parse(ctx.settings)
        s = self.settings
        #: bacpypes3 gives up after (retries + 1) * timeout_s; this bounds a stuck request.
        self._deadline_s = s.timeout_s * (s.retries + 1) + 1.0
        self._devices: dict[str, _Device] = {}
        self._by_key: dict[str, _Device] = {}
        self._app: HubApplication | None = None

    @property
    def local_address(self) -> tuple[str, int] | None:
        """(host, port) the hub's BACnet/IP socket is bound to, once started."""
        return self._app.bound if self._app is not None else None

    # -- life cycle ---------------------------------------------------------------
    async def add_device(self, record: DeviceRecord, spec: dict[str, Any]) -> None:
        if record.name in self._devices:
            await self.remove_device(record.name)
        record.protocol = ProtocolName.BACNET_IP
        record.managed = False
        address: IPv4Address | None = None
        key, reason = "", None
        try:
            host, port = parse_host_port(spec.get("address", ""))
        except InvalidRequest as e:
            reason = f"bad address: {e}"
        else:
            key = f"{host}:{port}"
            address = to_address(host, port)
            record.address = key
        instance = spec.get("device_instance")
        if isinstance(instance, int) and not isinstance(instance, bool) and 0 <= instance <= MAX_INSTANCE:
            record.instance = instance
        elif instance is not None:
            logger.warning("%s: ignoring device_instance %r", record.name, instance)
        dev = _Device(
            record=record,
            key=key,
            address=address,
            unavailable=reason,
            allow=self._allow_list(record.name, spec.get("points")),
            limit=asyncio.Semaphore(self.settings.device_concurrency),
            max_props=self.settings.rpm_max_properties,
        )
        self._devices[record.name] = dev
        if key:
            other = self._by_key.get(key)
            if other is not None:
                logger.warning("%s and %s have the same address %s", other.name, record.name, key)
            self._by_key[key] = dev
        if reason is not None:
            logger.warning("%s: %s", record.name, reason)
            self._set_online(dev, False)

    @staticmethod
    def _allow_list(name: str, points: Any) -> dict[str, dict[str, Any]] | None:
        if points is None:
            return None
        allow: dict[str, dict[str, Any]] = {}
        for entry in points if isinstance(points, list) else []:
            obj = entry.get("obj") if isinstance(entry, Mapping) else None
            try:
                parse_point_obj(str(obj))
            except InvalidRequest as e:
                logger.warning("%s: skipping allow-list entry %r: %s", name, entry, e)
                continue
            allow[str(obj)] = dict(entry)
        return allow

    async def remove_device(self, name: str) -> None:
        dev = self._devices.pop(name, None)
        if dev is None:
            return
        if self._by_key.get(dev.key) is dev:
            del self._by_key[dev.key]
        await self._stop_monitor(dev)
        await self._cancel_subscriptions([dev])

    async def start(self) -> None:
        if self._app is not None:
            return
        s = self.settings
        device_object = DeviceObject(
            objectIdentifier=("device", s.device_instance),
            objectName=s.device_name,
            vendorIdentifier=s.vendor_identifier,
            vendorName="uc-hub",
            modelName="uc-hub",
            firmwareRevision=__version__,
            applicationSoftwareVersion=__version__,
            maxApduLengthAccepted=1476,
            apduTimeout=int(s.timeout_s * 1000),
            numberOfApduRetries=s.retries,
        )
        try:
            app = await open_application(
                HubApplication, [device_object], s.interface, s.port, broadcast=s.broadcast
            )
        except OSError as e:
            raise DeviceError(f"cannot open BACnet/IP on {s.interface}:{s.port}: {e}") from e
        app.cov_handler = self._on_cov
        self._app = app
        logger.info("BACnet/IP device %d on %s:%d", s.device_instance, *app.bound)
        for dev in self._devices.values():
            self._ensure_monitor(dev)

    async def stop(self) -> None:
        devices = list(self._devices.values())
        await asyncio.gather(*(self._stop_monitor(dev) for dev in devices))
        await self._cancel_subscriptions(devices)
        app, self._app = self._app, None
        if app is not None:
            app.cov_handler = None
            app.close()

    # -- inventory ----------------------------------------------------------------
    async def discover(self, timeout_s: float = 3.0) -> list[DiscoveredDevice]:
        app = self._require_app()
        found: dict[tuple[str, int], IAmRequest] = {}

        def on_i_am(apdu: IAmRequest) -> None:
            instance = int(apdu.iAmDeviceIdentifier[1])
            if instance != self.settings.device_instance:
                found[(format_address(apdu.pduSource), instance)] = apdu

        app.i_am_listeners.add(on_i_am)
        try:
            for target in self._who_is_targets():
                try:
                    await app.send_unconfirmed(WhoIsRequest(destination=target))
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("discovery: Who-Is to %s failed: %s", target, e)
            await asyncio.sleep(timeout_s)
        finally:
            app.i_am_listeners.discard(on_i_am)

        items = sorted(found.items(), key=lambda kv: (kv[0][1], kv[0][0]))
        peek_s = min(1.0, timeout_s)
        names = await asyncio.gather(
            *(self._peek(app, apdu.pduSource, instance, peek_s) for (_, instance), apdu in items[:_PEEK_LIMIT])
        )
        out: list[DiscoveredDevice] = []
        for index, ((key, instance), apdu) in enumerate(items):
            info = names[index] if index < len(names) else {}
            dev = self._by_key.get(key)
            if dev is not None and dev.record.instance in (None, instance):
                dev.record.instance = instance
                self._set_online(dev, True)
            out.append(DiscoveredDevice(
                protocol=ProtocolName.BACNET_IP,
                address=key,
                instance=instance,
                name=info.get("object-name", ""),
                model=info.get("model-name", ""),
                extra={
                    "vendor_id": int(apdu.vendorID),
                    "vendor": info.get("vendor-name", ""),
                    "max_apdu": int(apdu.maxAPDULengthAccepted),
                    "segmentation": str(apdu.segmentationSupported),
                },
            ))
        return out

    def _who_is_targets(self) -> list[Address]:
        targets: dict[str, Address] = {}
        if self.settings.broadcast is not None:
            targets["*"] = GlobalBroadcast()
        for dev in self._devices.values():
            if dev.address is not None:
                targets[dev.key] = dev.address
        for host, port in self.settings.discover_targets:
            targets.setdefault(f"{host}:{port}", to_address(host, port))
        return list(targets.values())

    @staticmethod
    async def _peek(app: HubApplication, address: Address, instance: int, timeout_s: float) -> dict[str, str]:
        """Name, model and vendor of a discovered device, or what of them
        could be read within ``timeout_s``."""
        device = ObjectIdentifier(("device", instance))
        props = ["object-name", "model-name", "vendor-name"]
        out: dict[str, str] = {}
        try:
            async with asyncio.timeout(timeout_s):
                try:
                    rows = await app.read_property_multiple(address, [device, props])
                except ErrorRejectAbortNack:
                    out["object-name"] = clean_text(await app.read_property(address, device, "object-name"))
                    return out
        except (ErrorRejectAbortNack, TimeoutError) as e:
            logger.debug("discovery: no names from %s: %s", address, e)
            return out
        except Exception as e:  # undecodable answers only cost the names
            logger.debug("discovery: unreadable names from %s: %s", address, e)
            return out
        for _, prop, _, value in rows or []:
            if value is not None and not isinstance(value, ErrorType):
                out[str(prop)] = clean_text(value)
        return out

    async def describe(self, device: str) -> DeviceDescription:
        dev = self._device(device)
        self._require_app()
        record = dev.record
        if record.instance is None:
            record.instance = await self._learn_instance(dev)
        asked = f"device:{record.instance if record.instance is not None else WILDCARD_DEVICE}"
        info = {prop: value for (_, prop), value in
                (await self._read_props(dev, [(asked, p) for p in DEVICE_PROPS])).items()}
        oid = info["object-identifier"]
        if isinstance(oid, _PropError):
            raise DeviceError(f"{dev.name}: cannot read {asked}: {oid.message}")
        record.instance = int(oid[1])

        def text(prop: str) -> str:
            value = info.get(prop)
            return "" if value is None or isinstance(value, _PropError) else clean_text(value)

        def number(prop: str) -> int | None:
            value = info.get(prop)
            return None if value is None or isinstance(value, _PropError) else int(value)

        objs, total, skipped, truncated = await self._point_objects(dev, record.instance)
        pairs: list[tuple[str, str]] = []
        for obj in objs:
            pairs.extend((obj, p) for p in self._describe_props(POINT_TYPES[parse_point_obj(obj)[0]]))
        values = await self._read_props(dev, pairs)
        commandable = await self._commandable(dev, objs, values)

        points: list[Point] = []
        missing: list[str] = []
        now = time.time()
        for obj in objs:
            type_name, obj_instance = parse_point_obj(obj)
            type_info = POINT_TYPES[type_name]
            entry = (dev.allow or {}).get(obj, {})

            def known(name: str, obj: str = obj) -> Any:
                value = values.get((obj, name))
                return None if isinstance(value, _PropError) else value

            pv_raw = values.get((obj, PV))
            if known("object-name") is None and isinstance(pv_raw, _PropError):
                missing.append(obj)
            ref = PointRef(self.ctx.site, dev.name, obj)
            points.append(Point(
                ref=ref,
                name=clean_text(entry.get("name")) or clean_text(known("object-name")) or obj,
                kind=type_info.kind,
                datatype=type_info.datatype,
                units=clean_text(entry.get("units")) or units_name(known("units")),
                writable=type_info.kind is not PointKind.INPUT,
                commandable=obj in commandable,
                description=clean_text(known("description")),
                source=f"bacnet-ip:{dev.key}",
                space=record.space,
                meta={"type": type_name, "instance": obj_instance},
            ))
            reading = self._reading(ref, type_name, pv_raw, None, now)
            dev.last[obj] = (reading.value, reading.quality)
            self._publish(reading)
        dev.points = {p.ref.obj: p for p in points}

        record.model = text("model-name")
        record.firmware = text("firmware-revision") or text("application-software-version")
        record.meta["vendor"] = text("vendor-name")
        extra: dict[str, Any] = {
            "vendor": text("vendor-name"),
            "model": record.model,
            "firmware": text("firmware-revision"),
            "application_software": text("application-software-version"),
            "location": text("location"),
            "description": text("description"),
            "object_count": total,
            "skipped_objects": dict(skipped),
            "truncated": truncated,
            "max_objects": self.settings.max_objects,
            "read_property_multiple": dev.rpm,
        }
        for prop, key in (("vendor-identifier", "vendor_id"), ("protocol-revision", "protocol_revision"),
                          ("max-apdu-length-accepted", "max_apdu")):
            if (value := number(prop)) is not None:
                extra[key] = value
        if segmentation := text("segmentation-supported"):
            extra["segmentation"] = segmentation
        if missing:
            extra["missing_objects"] = missing
        return DeviceDescription(device=record, points=points, apps=[], extra=extra)

    async def _learn_instance(self, dev: _Device) -> int | None:
        """The device instance from the I-Am to a directed Who-Is. The
        wildcard instance would do in one request, but not every device
        honours it (bacpypes3-based ones do not)."""
        app = self._require_app()
        address = self._address(dev)
        answer: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        def on_i_am(apdu: IAmRequest) -> None:
            if format_address(apdu.pduSource) == dev.key and not answer.done():
                answer.set_result(int(apdu.iAmDeviceIdentifier[1]))

        app.i_am_listeners.add(on_i_am)
        try:
            await app.send_unconfirmed(WhoIsRequest(destination=address))
            async with asyncio.timeout(self.settings.timeout_s):
                return await answer
        except TimeoutError:
            logger.info("%s: no I-Am to a directed Who-Is; trying the wildcard device instance", dev.name)
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("%s: Who-Is failed: %s", dev.name, e)
        finally:
            app.i_am_listeners.discard(on_i_am)
        return None

    @staticmethod
    def _describe_props(info: TypeInfo) -> tuple[str, ...]:
        props = ["object-name", "description", PV]
        if info.units:
            props.append("units")
        if info.priority == "maybe":
            props.append(f"{PRIORITY_ARRAY}[0]")
        return tuple(props)

    async def _commandable(
        self, dev: _Device, objs: list[str], values: Mapping[tuple[str, str], Any]
    ) -> set[str]:
        """Objects with a priority array. Value objects have one only when the
        vendor made them commandable; the size read (index 0) decides, and a
        device that refuses the index is asked for the whole array."""
        out: set[str] = set()
        unsure: list[str] = []
        for obj in objs:
            priority = POINT_TYPES[parse_point_obj(obj)[0]].priority
            if priority == "always":
                out.add(obj)
            elif priority == "maybe":
                size = values.get((obj, f"{PRIORITY_ARRAY}[0]"))
                if not isinstance(size, _PropError):
                    out.add(obj)
                elif size.reason not in ("unknown-property", "unknown-object"):
                    unsure.append(obj)
        if unsure:
            whole = await self._read_props(dev, [(obj, PRIORITY_ARRAY) for obj in unsure])
            out.update(obj for obj in unsure if not isinstance(whole.get((obj, PRIORITY_ARRAY)), _PropError))
        return out

    async def _point_objects(self, dev: _Device, instance: int) -> tuple[list[str], int, Counter[str], bool]:
        """(point objects, object-list length, skipped types, truncated)."""
        if dev.allow is not None:
            return list(dev.allow), len(dev.allow), Counter(), False
        app = self._require_app()
        address = self._address(dev)
        oid = ObjectIdentifier(("device", instance))
        try:
            ids: list[Any] = list(await self._call(
                dev, lambda: app.read_property(address, oid, "object-list"), track=False
            ))
            total = len(ids)
        except (BacnetError, DeviceTimeout) as e:
            logger.info("%s: object-list unreadable as a whole (%s); reading it by index", dev.name, e)
            ids, total = await self._object_list_by_index(dev, oid)
        objs: list[str] = []
        skipped: Counter[str] = Counter()
        truncated = False
        for raw in ids:
            obj = obj_text(raw)
            type_name = obj.split(":")[0] if obj else "proprietary"
            if obj is None or type_name not in POINT_TYPES:
                if type_name != "device":
                    skipped[type_name] += 1
            elif len(objs) < self.settings.max_objects:
                objs.append(obj)
            else:
                truncated = True
        return objs, total, skipped, truncated or len(ids) < total

    async def _object_list_by_index(self, dev: _Device, oid: ObjectIdentifier) -> tuple[list[Any], int]:
        """Object identifiers read one by one, until more than ``max_objects``
        point objects are known; and the length of the list."""
        app = self._require_app()
        address = self._address(dev)
        count = int(await self._call(dev, lambda: app.read_property(address, oid, "object-list", array_index=0)))
        if count > _INDEX_LIMIT:
            logger.warning("%s: object-list has %d entries; reading the first %d", dev.name, count, _INDEX_LIMIT)

        async def element(index: int) -> Any:
            try:
                return await self._call(dev, lambda: app.read_property(address, oid, "object-list", array_index=index))
            except BacnetError as e:
                logger.debug("%s: object-list[%d]: %s", dev.name, index, e)
                return None

        ids: list[Any] = []
        points = 0
        last = min(count, _INDEX_LIMIT)
        for start in range(1, last + 1, _INDEX_WINDOW):
            window = range(start, min(last, start + _INDEX_WINDOW - 1) + 1)
            for raw in await _all(element(i) for i in window):
                if raw is None:
                    continue
                ids.append(raw)
                obj = obj_text(raw)
                if obj is not None and obj.split(":")[0] in POINT_TYPES:
                    points += 1
            if points > self.settings.max_objects:
                break
        return ids, count

    # -- values -------------------------------------------------------------------
    async def read(self, refs: list[PointRef]) -> list[Reading]:
        now = time.time()
        readings: list[Reading | None] = [None] * len(refs)
        groups: dict[str, list[int]] = {}
        for i, ref in enumerate(refs):
            dev = self._devices.get(ref.device)
            problem = f"unknown bacnet-ip device {ref.device!r}" if dev is None else self._point_problem(dev, ref.obj)
            if problem is not None:
                readings[i] = Reading(ref, None, now, Quality.FAULT, error=problem)
            else:
                groups.setdefault(ref.device, []).append(i)

        async def one_device(name: str, indexes: list[int]) -> None:
            dev = self._devices[name]
            objs = list(dict.fromkeys(refs[i].obj for i in indexes))
            results = await self._read_objects(dev, objs)
            for i in indexes:
                readings[i] = _rebind(results[refs[i].obj], refs[i])

        await asyncio.gather(*(one_device(name, indexes) for name, indexes in groups.items()))
        out = [r for r in readings if r is not None]
        for reading in out:
            self._publish(reading)
        return out

    async def _read_objects(self, dev: _Device, objs: Sequence[str]) -> dict[str, Reading]:
        """Present values of ``objs``; OFFLINE readings when the device does not answer."""
        now = time.time()
        if self._app is None or dev.address is None:
            error = "the bacnet-ip driver is not started" if self._app is None else dev.unavailable
            return {obj: Reading(self._ref(dev, obj), None, now, Quality.OFFLINE, error=error) for obj in objs}
        try:
            return await self._read_objects_raw(dev, objs)
        except (DeviceTimeout, NotStarted) as e:
            return {obj: Reading(self._ref(dev, obj), None, now, Quality.OFFLINE, error=str(e)) for obj in objs}

    async def _read_objects_raw(self, dev: _Device, objs: Sequence[str]) -> dict[str, Reading]:
        """Like ``_read_objects`` but raises ``DeviceTimeout``."""
        props = (PV, STATUS_FLAGS) if dev.rpm else (PV,)
        values = await self._read_props(dev, [(obj, p) for obj in objs for p in props])
        now = time.time()
        return {
            obj: self._reading(self._ref(dev, obj), parse_point_obj(obj)[0], values.get((obj, PV)),
                               values.get((obj, STATUS_FLAGS)), now)
            for obj in objs
        }

    @staticmethod
    def _reading(ref: PointRef, type_name: str, raw: Any, flags: Any, ts: float) -> Reading:
        if raw is None:
            return Reading(ref, None, ts, Quality.FAULT, error="no present value in the answer")
        if isinstance(raw, _PropError):
            return Reading(ref, None, ts, Quality.FAULT, error=raw.message)
        try:
            value = to_value(type_name, raw)
        except (TypeError, ValueError) as e:
            return Reading(ref, None, ts, Quality.FAULT, error=f"unusable present value: {e}")
        if _fault_flag(flags):
            return Reading(ref, value, ts, Quality.FAULT, error=_FAULT_TEXT)
        return Reading(ref, value, ts)

    async def write(self, ref: PointRef, value: Value, priority: int | None) -> WriteResult:
        dev = self._device(ref.device)
        problem = self._point_problem(dev, ref.obj)
        if problem is not None:
            raise NotFound(f"{ref}: {problem}")
        type_name, instance = parse_point_obj(ref.obj)
        if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int)
                                     or not 1 <= priority <= 16):
            raise InvalidRequest(f"priority must be 1..16, not {priority!r}")
        payload = to_bacnet(type_name, value)
        point = dev.points.get(ref.obj)
        if point is not None and not point.commandable:
            if value is None:
                raise InvalidRequest(f"{ref} has no priority array; there is nothing to relinquish")
            priority = None
        elif value is None and priority is None:
            priority = 16
        app = self._require_app()
        oid = object_identifier(type_name, instance)
        try:
            address = self._address(dev)
            await self._call(dev, lambda: app.write_property(address, oid, PV, payload, priority=priority))
        except (DeviceError, DeviceTimeout) as e:
            return WriteResult(ref, False, value, priority, str(e))
        if ref.obj in dev.watched:
            dev.next_poll = 0.0
            dev.wake.set()
        return WriteResult(ref, True, value, priority)

    async def priority_array(self, ref: PointRef) -> list[Value] | None:
        dev = self._device(ref.device)
        problem = self._point_problem(dev, ref.obj)
        if problem is not None:
            raise NotFound(f"{ref}: {problem}")
        type_name, instance = parse_point_obj(ref.obj)
        if POINT_TYPES[type_name].priority == "never":
            return None
        app = self._require_app()
        address = self._address(dev)
        oid = object_identifier(type_name, instance)
        try:
            raw = await self._call(dev, lambda: app.read_property(address, oid, PRIORITY_ARRAY))
        except BacnetError as e:
            if e.reason == "unknown-property":
                return None
            raise
        slots = [priority_slot(type_name, slot) for slot in raw]
        if len(slots) != 16:
            raise DeviceError(f"{ref}: the priority array has {len(slots)} slots, not 16")
        return slots

    async def watch(self, refs: list[PointRef]) -> None:
        touched: dict[str, _Device] = {}
        for ref in refs:
            dev = self._devices.get(ref.device)
            problem = f"unknown bacnet-ip device {ref.device!r}" if dev is None else self._point_problem(dev, ref.obj)
            if problem is not None:
                self._publish(Reading(ref, None, quality=Quality.FAULT, error=problem))
                continue
            assert dev is not None
            if dev.address is None:
                self._publish(Reading(ref, None, quality=Quality.OFFLINE, error=dev.unavailable))
            if ref.obj not in dev.watched:
                dev.watched[ref.obj] = ref
                touched[dev.name] = dev
        for dev in touched.values():
            dev.next_refresh = 0.0
            dev.wake.set()
            self._ensure_monitor(dev)

    async def unwatch(self, refs: list[PointRef]) -> None:
        dropped: dict[str, list[str]] = {}
        for ref in refs:
            dev = self._devices.get(ref.device)
            if dev is not None and dev.watched.pop(ref.obj, None) is not None:
                dev.polled.discard(ref.obj)
                dev.last.pop(ref.obj, None)
                dropped.setdefault(dev.name, []).append(ref.obj)
        for name, objs in dropped.items():
            dev = self._devices[name]
            if not dev.watched:
                await self._stop_monitor(dev)
            cancel = [obj for obj in objs if dev.cov.pop(obj, None) is not None]
            if cancel and self._app is not None:
                await _quietly(self._unsubscribe(dev, obj) for obj in cancel)

    # -- live values --------------------------------------------------------------
    def _ensure_monitor(self, dev: _Device) -> None:
        if self._app is None or not dev.watched or dev.address is None:
            return
        if dev.monitor is None or dev.monitor.done():
            dev.monitor = asyncio.create_task(self._monitor(dev), name=f"bacnet-ip:{dev.name}")

    async def _stop_monitor(self, dev: _Device) -> None:
        task, dev.monitor = dev.monitor, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _monitor(self, dev: _Device) -> None:
        loop = asyncio.get_running_loop()
        while dev.watched:
            dev.wake.clear()
            delay = self.settings.poll_interval_s
            try:
                delay = await self._monitor_pass(dev, loop)
            except DeviceTimeout as e:
                self._went_offline(dev, str(e))
            except HubError as e:
                logger.warning("%s: live values: %s", dev.name, e)
            except Exception:  # keep the device live whatever went wrong this pass
                logger.exception("%s: live values failed", dev.name)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await dev.wake.wait()

    async def _monitor_pass(self, dev: _Device, loop: asyncio.AbstractEventLoop) -> float:
        """One round of subscribing and reading; returns the time until the next."""
        s = self.settings
        objs = list(dev.watched)
        if s.cov:
            now = loop.time()
            await _all(self._subscribe(dev, obj, loop) for obj in objs
                       if obj not in dev.polled and now >= dev.cov.get(obj, 0.0))
        polled = [obj for obj in objs if not s.cov or obj in dev.polled]
        now = loop.time()
        due: list[str] = []
        full = now >= dev.next_refresh
        if full:
            due = objs
            dev.next_refresh = now + s.refresh_s
            dev.next_poll = now + s.poll_interval_s
        elif polled and now >= dev.next_poll:
            due = polled
            dev.next_poll = now + s.poll_interval_s
        if due:
            results = await self._read_objects_raw(dev, due)
            self._publish_changed(dev, results.values(), full)
        deadlines = [dev.next_refresh, *dev.cov.values()]
        if polled:
            deadlines.append(dev.next_poll)
        return max(0.01, min(deadlines) - loop.time())

    def _went_offline(self, dev: _Device, error: str) -> None:
        dev.cov.clear()
        dev.next_refresh = 0.0
        now = time.time()
        self._publish_changed(
            dev, (Reading(ref, None, now, Quality.OFFLINE, error=error) for ref in dev.watched.values()), False
        )

    def _publish_changed(self, dev: _Device, readings: Iterable[Reading], full: bool) -> None:
        for reading in readings:
            obj = reading.ref.obj
            if obj not in dev.watched:
                continue
            state = (reading.value, reading.quality)
            if full or dev.last.get(obj) != state:
                dev.last[obj] = state
                self._publish(reading)

    async def _subscribe(self, dev: _Device, obj: str, loop: asyncio.AbstractEventLoop) -> None:
        s = self.settings
        app = self._require_app()
        request = SubscribeCOVRequest(
            subscriberProcessIdentifier=s.cov_process_id,
            monitoredObjectIdentifier=object_identifier(*parse_point_obj(obj)),
            issueConfirmedNotifications=s.cov_confirmed,
            lifetime=s.cov_lifetime_s,
            destination=self._address(dev),
        )
        # Recorded before the request goes out: when the monitor is cancelled
        # (unwatch, remove_device, stop) while the device is already creating
        # the subscription, the cancellation that follows still covers it
        # instead of leaving it in the device's subscription table.
        dev.cov[obj] = loop.time() + s.cov_lifetime_s * RENEW_FRACTION
        try:
            await self._call(dev, lambda: app.request(request))
        except BacnetError as e:
            logger.info("%s: no COV for %s (%s); polling it every %.1f s", dev.name, obj, e, s.poll_interval_s)
            dev.cov.pop(obj, None)
            dev.polled.add(obj)
            dev.next_poll = 0.0
            return
        except Exception:
            dev.cov.pop(obj, None)
            raise
        if obj in dev.watched:
            dev.cov[obj] = loop.time() + s.cov_lifetime_s * RENEW_FRACTION
        else:
            await self._unsubscribe(dev, obj)

    async def _unsubscribe(self, dev: _Device, obj: str) -> None:
        app = self._app
        if app is None or dev.address is None:
            return
        request = SubscribeCOVRequest(
            subscriberProcessIdentifier=self.settings.cov_process_id,
            monitoredObjectIdentifier=object_identifier(*parse_point_obj(obj)),
            destination=dev.address,
        )
        try:
            await self._call(dev, lambda: app.request(request), track=False)
        except (DeviceError, DeviceTimeout) as e:
            logger.debug("%s: cancelling the COV subscription of %s: %s", dev.name, obj, e)

    async def _cancel_subscriptions(self, devices: Sequence[_Device]) -> None:
        """Best effort, bounded: a silent device must not hold up a stop."""
        pending = [(dev, obj) for dev in devices for obj in dev.cov]
        for dev in devices:
            dev.cov.clear()
        if pending and self._app is not None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(1.0, self._deadline_s)):
                    await _quietly(self._unsubscribe(dev, obj) for dev, obj in pending)

    def _on_cov(self, source: Address, process_id: int, oid: ObjectIdentifier, values: list[PropertyValue]) -> bool:
        if process_id != self.settings.cov_process_id:
            return False
        dev = self._by_key.get(format_address(source))
        obj = obj_text(oid)
        if dev is None or obj is None or obj not in dev.watched:
            return False
        type_name, _ = parse_point_obj(obj)
        raw: Any = None
        flags: Any = None
        try:
            for item in values:
                prop = int(item.propertyIdentifier)
                if prop == _PV_ID:
                    raw = item.value.cast_out(present_value_type(type_name), null=True)
                elif prop == _FLAGS_ID:
                    flags = item.value.cast_out(StatusFlags)
        except Exception as e:  # a garbled value is a fault on that point, not a crash
            raw = _PropError(f"undecodable COV notification: {e}")
        if raw is None:
            return True
        self._set_online(dev, True)
        reading = self._reading(dev.watched[obj], type_name, raw, flags, time.time())
        dev.last[obj] = (reading.value, reading.quality)
        self._publish(reading)
        return True

    # -- requests -----------------------------------------------------------------
    async def _read_props(self, dev: _Device, pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        """Values of (object, property) pairs: bacpypes3 values, or ``_PropError``
        for properties the device refused. Raises ``DeviceTimeout``."""
        out: dict[tuple[str, str], Any] = {}
        i = 0
        while i < len(pairs) and dev.rpm:
            batch = pairs[i:i + dev.max_props]
            try:
                out.update(await self._rpm(dev, batch))
            except BacnetError as e:
                if e.kind == "reject" and e.reason == "unrecognized-service":
                    logger.info("%s does not support ReadPropertyMultiple; using ReadProperty", dev.name)
                    dev.rpm = False
                    break
                if e.kind in ("abort", "reject") and e.reason in _TOO_BIG and len(batch) > 1:
                    dev.max_props = max(1, len(batch) // 2)
                    logger.info("%s: %s; reading %d properties per request", dev.name, e, dev.max_props)
                    continue
                if e.kind != "error" and len(batch) > 1:
                    # One property the device or bacpypes3 chokes on (an
                    # undecodable value, say): halve this batch only, so the
                    # rest of the device keeps its batch size.
                    logger.debug("%s: %s; splitting a batch of %d", dev.name, e, len(batch))
                    half = len(batch) // 2
                    out.update(await self._read_props(dev, batch[:half]))
                    out.update(await self._read_props(dev, batch[half:]))
                else:
                    out.update(await self._rp_batch(dev, batch))
            i += len(batch)
        if i < len(pairs):
            out.update(await self._rp_batch(dev, pairs[i:]))
        return out

    async def _rpm(self, dev: _Device, batch: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        app = self._require_app()
        address = self._address(dev)
        params: list[Any] = []
        last_obj = None
        for obj, prop in batch:
            if obj != last_obj:
                params.append(_object_id(obj))
                params.append([])
                last_obj = obj
            params[-1].append(_property_ref(prop))
        rows = await self._call(dev, lambda: app.read_property_multiple(address, params))
        if not isinstance(rows, list):
            raise BacnetError(f"{dev.name}: unexpected ReadPropertyMultiple answer", "decode", "")
        asked = {obj for obj, _ in batch}
        wildcard = f"device:{WILDCARD_DEVICE}"
        out: dict[tuple[str, str], Any] = {}
        for oid, prop, index, value in rows:
            obj = obj_text(oid) or str(oid)
            if obj not in asked and obj.startswith("device:") and wildcard in asked:
                obj = wildcard  # the device answers the wildcard with its own id
            key = (obj, str(prop) if index is None else f"{prop}[{index}]")
            if isinstance(value, ErrorType):
                code = str(value.errorCode)
                out[key] = _PropError(f"{value.errorClass}: {code}", code)
            elif value is None:
                out[key] = _PropError("property not supported by the hub's decoder")
            else:
                out[key] = value
        for pair in batch:
            out.setdefault(pair, _PropError("missing from the answer"))
        return out

    async def _rp_batch(self, dev: _Device, pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        app = self._require_app()
        address = self._address(dev)

        async def one(obj: str, prop: str) -> tuple[tuple[str, str], Any]:
            name, _, index = prop.partition("[")
            array_index = int(index[:-1]) if index else None
            oid = _object_id(obj)
            try:
                value = await self._call(
                    dev, lambda: app.read_property(address, oid, name, array_index=array_index)
                )
            except BacnetError as e:
                return (obj, prop), _PropError(str(e), e.reason)
            return (obj, prop), value

        return dict(await _all(one(obj, prop) for obj, prop in pairs))

    async def _call(self, dev: _Device, request: Callable[[], Awaitable[T]], *, track: bool = True) -> T:
        """Run one bacpypes3 request against ``dev``: bounded in time and
        concurrency, errors as hub errors, online state tracked."""
        error: DeviceError | DeviceTimeout
        async with dev.limit:
            try:
                async with asyncio.timeout(self._deadline_s):
                    result = await request()
            except TimeoutError:
                error = DeviceTimeout(f"{dev.name} did not answer")
            except ErrorRejectAbortNack as e:
                error = bacnet_error(dev.name, e)
            except ApplicationClosed:
                raise NotStarted(f"the bacnet-ip driver stopped before {dev.name} answered") from None
            except _DECODE_ERRORS as e:
                logger.debug("%s: undecodable answer", dev.name, exc_info=True)
                error = BacnetError(f"{dev.name}: undecodable answer: {e}", "decode", type(e).__name__)
            else:
                if track:
                    self._set_online(dev, True)
                return result
        if track:
            self._set_online(dev, not isinstance(error, DeviceTimeout))
        raise error

    # -- state --------------------------------------------------------------------
    def _require_app(self) -> HubApplication:
        if self._app is None:
            raise NotStarted("the bacnet-ip driver is not started")
        return self._app

    def _device(self, name: str) -> _Device:
        dev = self._devices.get(name)
        if dev is None:
            raise NotFound(f"unknown bacnet-ip device {name!r}")
        return dev

    @staticmethod
    def _address(dev: _Device) -> IPv4Address:
        if dev.address is None:
            raise DeviceTimeout(f"{dev.name}: {dev.unavailable}")
        return dev.address

    def _ref(self, dev: _Device, obj: str) -> PointRef:
        return dev.watched.get(obj) or PointRef(self.ctx.site, dev.name, obj)

    @staticmethod
    def _point_problem(dev: _Device, obj: str) -> str | None:
        try:
            parse_point_obj(obj)
        except InvalidRequest as e:
            return str(e)
        if dev.allow is not None and obj not in dev.allow:
            return f"{obj} is not in the point allow-list of {dev.name}"
        return None

    def _set_online(self, dev: _Device, online: bool) -> None:
        if online:
            dev.record.last_seen = time.time()
        if dev.online is online:
            return
        dev.online = online
        dev.record.online = online
        logger.info("%s is %s", dev.name, "online" if online else "offline")
        try:
            self.ctx.set_online(dev.name, online)
        except Exception:  # a failing listener must not break device I/O
            logger.exception("set_online callback failed for %s", dev.name)

    def _publish(self, reading: Reading) -> None:
        try:
            self.ctx.publish(reading)
        except Exception:  # a failing listener must not break device I/O
            logger.exception("publish callback failed for %s", reading.ref)


def _rebind(reading: Reading, ref: PointRef) -> Reading:
    if reading.ref == ref:
        return reading
    return Reading(ref, reading.value, reading.ts, reading.quality, reading.priority, reading.error)


def _fault_flag(flags: Any) -> bool:
    try:
        return flags is not None and not isinstance(flags, _PropError) and bool(flags[1])
    except (TypeError, IndexError):
        return False


def _object_id(obj: str) -> ObjectIdentifier:
    if obj.startswith("device:"):
        return ObjectIdentifier(("device", int(obj.split(":")[1])))
    return object_identifier(*parse_point_obj(obj))


def _property_ref(prop: str) -> PropertyReference | PropertyIdentifier:
    name, _, index = prop.partition("[")
    if index:
        return PropertyReference(propertyIdentifier=name, propertyArrayIndex=int(index[:-1]))
    return PropertyIdentifier(name)


async def _quietly(coros: Iterable[Coroutine[Any, Any, None]]) -> None:
    await asyncio.gather(*coros, return_exceptions=True)


async def _all(coros: Iterable[Coroutine[Any, Any, T]]) -> list[T]:
    """Run ``coros`` together; the first hub error (a ``DeviceTimeout``, or
    ``NotStarted`` when the driver stops) cancels the rest, whose answers no
    longer matter, and is raised as itself."""
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(c) for c in coros]
    except* HubError as eg:
        raise eg.exceptions[0] from None
    return [t.result() for t in tasks]
