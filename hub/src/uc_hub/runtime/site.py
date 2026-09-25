"""The running site: drivers, devices, the point model and live values.

``SiteRuntime`` registers the devices of the live manifest with their
drivers (``system.nodes`` -> bacnet-uc, ``external_devices`` -> bacnet-ip or
mqtt), applies ``placement``, and describes every device in the background,
retrying with exponential backoff until it answers (a device that comes
online is retried at once). The point model is what the drivers describe,
overlaid with the manifest: tags are added, the safety class is the stricter
of the driver's and the manifest's, and the space is the device's placement.

Readings arrive through ``DriverContext.publish``. Each one goes to the
``LiveHub`` (latest value and live fan-out), to a bounded in-memory history
per point (``point_history``) and to the listeners (gateway bridges). Points
listed in the manifest's ``tags`` are watched all the time so they have a
history; other points are watched while someone holds them
(``ensure_watched`` / ``release_watched`` are reference counted).

``reload`` switches to a new manifest revision: removed devices leave their
driver, added or changed ones are (re-)registered and described again, and
the overlays follow the new tags, safety classes and placement.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.driver import Driver, DriverContext
from ..core.errors import DeviceError, HubError, InvalidRequest, NotFound, Unsupported
from ..core.ids import PointRef
from ..core.types import (
    DeviceDescription,
    DeviceRecord,
    Point,
    PointKind,
    ProtocolName,
    Quality,
    Reading,
    SafetyClass,
    Value,
    WriteResult,
)
from ..drivers.bacnet_ip import BacnetIpDriver
from ..drivers.bacnet_uc import BacnetUcDriver
from ..drivers.bacnet_uc.api import NodeApi
from ..drivers.mqtt import MqttDriver
from ..manifest.load import DeviceSpec, SiteManifest
from ..store.events import LiveHub

logger = logging.getLogger(__name__)

DriverFactory = Callable[[DriverContext], Driver]
Listener = Callable[[Reading], None]
#: (ts, value, quality) of one recorded reading.
Sample = tuple[float, Value, Quality]

DEFAULT_FACTORIES: dict[ProtocolName, DriverFactory] = {
    ProtocolName.BACNET_UC: BacnetUcDriver,
    ProtocolName.BACNET_IP: BacnetIpDriver,
    ProtocolName.MQTT: MqttDriver,
}
_SAFETY_ORDER = (SafetyClass.NORMAL, SafetyClass.CRITICAL, SafetyClass.LIFE_SAFETY)
_BACNET = frozenset({ProtocolName.BACNET_UC, ProtocolName.BACNET_IP})


@dataclass(eq=False)
class _Device:
    spec: DeviceSpec
    #: Shared with the driver, which keeps online, address, model... current.
    record: DeviceRecord
    description: DeviceDescription | None = None
    #: The driver took the device (an invalid spec is refused).
    registered: bool = False
    #: Why the device could not be registered or described, None after a success.
    error: str | None = None
    #: obj -> point with the manifest overlay.
    points: dict[str, Point] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: A describe was requested while one was running.
    stale: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def protocol(self) -> ProtocolName:
        return self.spec.protocol


class SiteRuntime:
    def __init__(
        self,
        manifest: SiteManifest,
        live: LiveHub,
        *,
        driver_settings: Mapping[ProtocolName, Mapping[str, Any]] | None = None,
        configured: Iterable[ProtocolName] = (),
        factories: Mapping[ProtocolName, DriverFactory] | None = None,
        history_limit: int = 2000,
        history_max_age_s: float = 24 * 3600.0,
        describe_timeout_s: float = 60.0,
        describe_concurrency: int = 4,
        retry_min_s: float = 1.0,
        retry_max_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``configured`` protocols get a driver even without devices (for
        discovery); the others only when the manifest uses them."""
        self.manifest = manifest
        self.live = live
        self.drivers: dict[ProtocolName, Driver] = {}
        #: Count of the draft's pending changes, for ``summary`` (set by the services).
        self.pending_changes: Callable[[], int] = lambda: 0
        self._settings = {p: dict(s) for p, s in (driver_settings or {}).items()}
        self._configured = set(configured)
        self._factories = dict(factories or DEFAULT_FACTORIES)
        self._history_limit = history_limit
        self._history_max_age_s = history_max_age_s
        self._describe_timeout_s = describe_timeout_s
        self._describe_limit = asyncio.Semaphore(describe_concurrency)
        self._retry_min_s = retry_min_s
        self._retry_max_s = retry_max_s
        self._clock = clock
        self._devices: dict[str, _Device] = {}
        #: Full point id -> tags / safety class; the manifest's, or what the
        #: gateway applied while a plan runs (``apply_tags``).
        self._tags: dict[str, list[str]] = manifest.tags
        self._safety: dict[str, SafetyClass] = manifest.safety
        self._points: dict[PointRef, Point] = {}
        self._text: dict[PointRef, str] = {}
        self._history: dict[PointRef, deque[Sample]] = {}
        self._watch: Counter[PointRef] = Counter()
        self._trended: set[PointRef] = set()
        self._listeners: list[Listener] = []
        #: Last discovery per protocol: DiscoveredDevice JSON with "known" and "device".
        self._discovered: dict[ProtocolName, list[dict[str, Any]]] = {}
        self._started = False

    @property
    def name(self) -> str:
        return self.manifest.name

    # -- life cycle ---------------------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for protocol in self._wanted_protocols(self.manifest):
            self._make_driver(protocol)
        for spec in self.manifest.devices.values():
            await self._add(spec)
        for protocol, driver in list(self.drivers.items()):
            await self._start_driver(protocol, driver)
        for dev in self._devices.values():
            self._schedule_describe(dev)
        await self._set_trended(self._tagged(self.manifest))

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        tasks = [d.task for d in self._devices.values() if d.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for protocol, driver in self.drivers.items():
            try:
                await driver.stop()
            except Exception:
                logger.exception("stopping the %s driver failed", protocol.value)

    def _wanted_protocols(self, manifest: SiteManifest) -> list[ProtocolName]:
        used = {d.protocol for d in manifest.devices.values()}
        return [p for p in ProtocolName if p in used or p in self._configured]

    def _make_driver(self, protocol: ProtocolName) -> Driver:
        driver = self._factories[protocol](DriverContext(
            site=self.name, publish=self._publish, set_online=self._set_online,
            settings=dict(self._settings.get(protocol, {})),
        ))
        self.drivers[protocol] = driver
        return driver

    async def _start_driver(self, protocol: ProtocolName, driver: Driver) -> None:
        try:
            await driver.start()
        except (HubError, OSError) as e:
            # One broken transport must not take the other protocols down.
            logger.error("the %s driver did not start: %s", protocol.value, e)

    async def _add(self, spec: DeviceSpec) -> None:
        record = DeviceRecord(
            site=self.name, name=spec.name, protocol=spec.protocol, address=_address(spec),
            managed=spec.managed, instance=self.manifest.device_instance(spec.name), space=spec.space,
        )
        dev = _Device(spec, record)
        self._devices[spec.name] = dev
        try:
            await self.driver(spec.protocol).add_device(record, dict(spec.spec))
        except HubError as e:
            dev.error = f"not registered with the {spec.protocol.value} driver: {e}"
            logger.error("%s: %s", spec.name, dev.error)
            return
        dev.registered = True
        watched = [ref for ref in self._watch if ref.device == spec.name]
        if watched:
            await self._driver_watch(watched)

    async def _remove(self, name: str) -> None:
        dev = self._devices.pop(name)
        if dev.task is not None:
            dev.task.cancel()
            await asyncio.gather(dev.task, return_exceptions=True)
        driver = self.drivers.get(dev.protocol)
        if driver is not None:
            try:
                await driver.remove_device(name)
            except HubError as e:
                logger.warning("%s: removing it from the %s driver failed: %s", name, dev.protocol.value, e)
        for ref in [r for r in self._points if r.device == name]:
            del self._points[ref]
            self._text.pop(ref, None)
        for ref in [r for r in self._history if r.device == name]:
            del self._history[ref]
        self.live.forget_device(name)

    # -- devices ------------------------------------------------------------------------
    def driver(self, protocol: ProtocolName) -> Driver:
        driver = self.drivers.get(protocol)
        if driver is None:
            raise Unsupported(f"no {protocol.value} driver runs on this hub (configure drivers."
                              f"{protocol.value.replace('-', '_')} in hub.yaml)")
        return driver

    @property
    def bacnet_uc(self) -> BacnetUcDriver:
        driver = self.driver(ProtocolName.BACNET_UC)
        assert isinstance(driver, BacnetUcDriver)
        return driver

    def node(self, name: str) -> NodeApi:
        """SMP interface of a bacnet-uc node (the manifest engine's resolver)."""
        return self.bacnet_uc.node(name)

    def _device(self, name: str) -> _Device:
        dev = self._devices.get(name)
        if dev is None:
            raise NotFound(f"no device {name!r} in the site manifest")
        return dev

    def devices(self) -> list[DeviceRecord]:
        return [d.record for d in self._devices.values()]

    def device(self, name: str) -> DeviceRecord:
        return self._device(name).record

    def device_spec(self, name: str) -> DeviceSpec:
        return self._device(name).spec

    def description(self, name: str) -> DeviceDescription | None:
        """The last description, with the manifest overlay on its points."""
        dev = self._device(name)
        if dev.description is None:
            return None
        return dataclasses.replace(dev.description, points=list(dev.points.values()))

    async def describe(self, name: str) -> DeviceDescription:
        """Describe the device now (the driver asks it), then return
        ``description``. Raises what the driver raised."""
        dev = self._device(name)
        await self._describe(dev)
        description = self.description(name)
        assert description is not None
        return description

    def refresh(self, names: Iterable[str]) -> None:
        """Describe these devices again in the background (e.g. after apply
        changed their apps or IO)."""
        for name in names:
            dev = self._devices.get(name)
            if dev is not None:
                self._schedule_describe(dev)

    def _schedule_describe(self, dev: _Device) -> None:
        if not dev.registered:
            return
        dev.stale = True
        if dev.task is None or dev.task.done():
            dev.task = asyncio.create_task(self._describe_loop(dev), name=f"describe-{dev.name}")
        else:
            dev.wake.set()

    async def _describe_loop(self, dev: _Device) -> None:
        delay = self._retry_min_s
        while True:
            dev.stale = False
            try:
                await self._describe(dev)
            except Exception:
                dev.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(dev.wake.wait(), delay)
                delay = min(delay * 2, self._retry_max_s)
                continue
            if not dev.stale:
                return
            delay = self._retry_min_s

    async def _describe(self, dev: _Device) -> None:
        async with dev.lock, self._describe_limit:
            try:
                async with asyncio.timeout(self._describe_timeout_s):
                    description = await self.driver(dev.protocol).describe(dev.name)
            except Exception as e:
                dev.error = _describe_error(e)
                log = logger.exception if not isinstance(e, (HubError, TimeoutError, OSError)) else logger.info
                log("%s: describe failed: %s", dev.name, dev.error)
                raise
            dev.description = description
            dev.error = None
            self._overlay(dev)

    def _overlay(self, dev: _Device) -> None:
        for ref in [r for r in self._points if r.device == dev.name]:
            del self._points[ref]
            self._text.pop(ref, None)
        dev.points = {}
        if dev.description is None:
            return
        tags, safety = self._tags, self._safety
        spaces = {s.id: s.name for s in self.manifest.spaces}
        for p in dev.description.points:
            pid = str(p.ref)
            point = dataclasses.replace(
                p,
                tags=list(dict.fromkeys([*p.tags, *tags.get(pid, [])])),
                safety=max(p.safety, safety.get(pid, SafetyClass.NORMAL), key=_SAFETY_ORDER.index),
                space=dev.record.space,
                meta=dict(p.meta),
            )
            dev.points[p.ref.obj] = point
            self._points[p.ref] = point
            self._text[p.ref] = " ".join([
                pid, point.name, point.description, " ".join(point.tags), point.source, dev.name,
                dev.record.space or "", spaces.get(dev.record.space or "", ""),
            ]).lower()

    # -- points -------------------------------------------------------------------------
    def point_ref(self, text: str) -> PointRef:
        """A full or device-relative point id of this site."""
        try:
            ref = PointRef.parse(text, default_site=self.name)
        except ValueError as e:
            raise InvalidRequest(str(e)) from None
        if ref.site != self.name:
            raise InvalidRequest(f"{text}: this hub runs site {self.name!r}, not {ref.site!r}")
        return ref

    def points(self, device: str | None = None) -> list[Point]:
        if device is not None:
            return list(self._device(device).points.values())
        return list(self._points.values())

    def known_point(self, ref: PointRef) -> Point | None:
        """The point from the model, without asking the device."""
        return self._points.get(ref)

    async def point(self, ref: PointRef) -> Point:
        """The point with its overlay; a device that was not described yet is
        described first. Raises ``NotFound`` for unknown points."""
        dev = self._device(ref.device)
        if dev.description is None:
            await self._describe(dev)
        point = dev.points.get(ref.obj)
        if point is None:
            raise NotFound(f"{ref.device} has no point {ref.obj!r}")
        return point

    async def channel_point(self, node: str, channel: str) -> Point | None:
        """The point an IO channel of a bacnet-uc node feeds or drives: from
        the node's IO catalog binding, else from the manifest's io list (so
        the safety rules hold while the node cannot be described). None when
        nothing is bound to the channel; ``NotFound`` when the node's
        catalog has no such channel, ``DeviceError`` when neither the node
        nor the manifest tells what the channel drives."""
        dev = self._device(node)
        if dev.protocol is not ProtocolName.BACNET_UC:
            raise InvalidRequest(f"{node} is a {dev.protocol.value} device; only bacnet-uc nodes have IO channels")
        if dev.description is None:
            with contextlib.suppress(HubError, TimeoutError, OSError):
                await self._describe(dev)
        catalog = (dev.description.extra.get("io") or {}).get("channels") if dev.description else None
        if catalog and channel not in {c.get("name") for c in catalog if isinstance(c, Mapping)}:
            raise NotFound(f"{node} has no IO channel {channel!r}")
        for point in dev.points.values():
            if point.source == f"io:{channel}":
                return point
        for entry in dev.spec.spec.get("io") or []:
            if entry.get("channel") == channel:
                ref = PointRef(self.name, node, f"{entry['type']}:{entry['instance']}")
                return self._points.get(ref) or Point(
                    ref=ref, name=str(entry.get("name") or ref.obj), kind=_kind(entry["type"]),
                    safety=self._safety.get(str(ref), SafetyClass.NORMAL), source=f"io:{channel}",
                    space=dev.record.space,
                )
        if dev.description is None:
            raise DeviceError(f"{node} cannot be described ({dev.error}) and the manifest does not bind "
                              f"{channel}, so what the channel drives is unknown")
        return None

    def search(
        self,
        query: str = "",
        *,
        space: str | None = None,
        device: str | None = None,
        protocol: str | None = None,
        tag: str | None = None,
        kind: str | None = None,
        writable: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[Point]]:
        """Points whose id, name, description, tags, source, device or space
        contain every word of ``query`` (case-insensitive), best matches
        first. ``space`` includes its child spaces. Returns (total, page)."""
        spaces = self._spaces_within(space)
        words = query.lower().split()
        wanted_tag = tag.lower() if tag else None
        hits: list[tuple[int, str, Point]] = []
        for ref, point in self._points.items():
            dev = self._devices[ref.device]
            if spaces is not None and dev.record.space not in spaces:
                continue
            if device is not None and ref.device != device:
                continue
            if protocol is not None and dev.protocol.value != protocol:
                continue
            if wanted_tag is not None and wanted_tag not in (t.lower() for t in point.tags):
                continue
            if kind is not None and point.kind.value != kind:
                continue
            if writable is not None and point.writable != writable:
                continue
            text = self._text[ref]
            if not all(w in text for w in words):
                continue
            hits.append((_score(words, ref, point), str(ref), point))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return len(hits), [h[2] for h in hits[offset:offset + limit]]

    def search_devices(
        self, query: str = "", *, space: str | None = None, device: str | None = None,
        protocol: str | None = None,
    ) -> list[DeviceRecord]:
        spaces = self._spaces_within(space)
        names = {s.id: s.name for s in self.manifest.spaces}
        words = query.lower().split()
        out = []
        for dev in self._devices.values():
            rec = dev.record
            if spaces is not None and rec.space not in spaces:
                continue
            if (device is not None and dev.name != device) or (protocol is not None and dev.protocol.value != protocol):
                continue
            text = " ".join([dev.name, dev.protocol.value, rec.address, rec.model, rec.space or "",
                             names.get(rec.space or "", ""), str(dev.spec.spec.get("equipment", "")),
                             str(dev.spec.spec.get("description", ""))]).lower()
            if all(w in text for w in words):
                out.append(rec)
        return out

    def _spaces_within(self, space: str | None) -> set[str] | None:
        if space is None:
            return None
        if space not in {s.id for s in self.manifest.spaces}:
            raise InvalidRequest(f"unknown space {space!r}")
        return self.manifest.spaces_within(space)

    # -- values -------------------------------------------------------------------------
    async def read(self, refs: list[PointRef]) -> list[Reading]:
        """Present values in ``refs`` order; never raises for a bad point."""
        out: list[Reading | None] = [None] * len(refs)
        groups: dict[ProtocolName, list[int]] = {}
        for i, ref in enumerate(refs):
            dev = self._devices.get(ref.device) if ref.site == self.name else None
            if dev is None or dev.protocol not in self.drivers:
                why = "no such device in the site manifest" if dev is None else f"no {dev.protocol.value} driver"
                out[i] = Reading(ref, None, self._clock(), Quality.FAULT, error=why)
            else:
                groups.setdefault(dev.protocol, []).append(i)

        async def one(protocol: ProtocolName, indexes: list[int]) -> None:
            batch = [refs[i] for i in indexes]
            try:
                readings = await self.drivers[protocol].read(batch)
            except Exception as e:  # drivers should not raise here; keep the other protocols' values
                logger.exception("%s read failed", protocol.value)
                readings = [Reading(r, None, self._clock(), Quality.FAULT, error=str(e)) for r in batch]
            for i, reading in zip(indexes, readings, strict=True):
                out[i] = reading

        await asyncio.gather(*(one(p, ix) for p, ix in groups.items()))
        return [r for r in out if r is not None]

    def latest(self, ref: PointRef) -> Reading | None:
        return self.live.latest(ref)

    def history(self, ref: PointRef, since: float) -> list[Sample]:
        """Recorded readings of a point with ``ts >= since``, oldest first."""
        samples = self._history.get(ref)
        return [s for s in samples if s[0] >= since] if samples else []

    def retained_only(self, ref: PointRef) -> bool:
        """True while an MQTT point only has a retained value (see ``MqttDriver.retained_only``)."""
        driver = self.drivers.get(ProtocolName.MQTT)
        return isinstance(driver, MqttDriver) and driver.retained_only(ref)

    async def write(self, ref: PointRef, value: Value, priority: int | None, *,
                    lease_ms: int | None = None) -> WriteResult:
        """Write through the driver. Policy checks are the caller's job.
        ``lease_ms`` goes to BACnet-uc nodes, which then relinquish by
        themselves when the hub is gone (firmware request B2)."""
        driver = self.driver(self._device(ref.device).protocol)
        if lease_ms is not None and isinstance(driver, BacnetUcDriver):
            return await driver.write(ref, value, priority, lease_ms=lease_ms)
        return await driver.write(ref, value, priority)

    async def relinquish(self, ref: PointRef, priority: int) -> WriteResult:
        return await self.driver(self._device(ref.device).protocol).relinquish(ref, priority)

    async def force(self, node: str, channel: str, value: float | None, *, lease_ms: int | None = None) -> None:
        dev = self._device(node)
        if dev.protocol is not ProtocolName.BACNET_UC:
            raise InvalidRequest(f"{node} is a {dev.protocol.value} device; only bacnet-uc nodes have IO channels")
        await self.bacnet_uc.force(node, channel, value, lease_ms)

    async def identify(self, device: str, seconds: int) -> None:
        await self.driver(self._device(device).protocol).identify(device, seconds)

    async def priority_array(self, ref: PointRef) -> list[Value] | None:
        return await self.driver(self._device(ref.device).protocol).priority_array(ref)

    def add_listener(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    def _publish(self, reading: Reading) -> None:
        self.live.publish(reading)
        if reading.ref.device in self._devices:
            samples = self._history.get(reading.ref)
            if samples is None:
                samples = self._history[reading.ref] = deque(maxlen=self._history_limit)
            if not samples or reading.ts >= samples[-1][0]:
                samples.append((reading.ts, reading.value, reading.quality))
                horizon = reading.ts - self._history_max_age_s
                while samples and samples[0][0] < horizon:
                    samples.popleft()
        for listener in list(self._listeners):
            try:
                listener(reading)
            except Exception:
                logger.exception("reading listener failed for %s", reading.ref)

    def _set_online(self, name: str, online: bool) -> None:
        dev = self._devices.get(name)
        if dev is not None and online and dev.description is None and dev.task is not None:
            dev.wake.set()

    # -- watching -------------------------------------------------------------------------
    async def ensure_watched(self, refs: Iterable[PointRef]) -> None:
        """Hold live updates for these points (reference counted). A point it
        refuses (``NotFound``) counts nothing; otherwise the counts are taken
        before the drivers are asked, so a caller cancelled meanwhile holds
        them and must release them."""
        refs = list(refs)
        for ref in refs:
            if ref.site != self.name:
                raise NotFound(f"{ref} is not a point of site {self.name}")
            self._device(ref.device)
        new = []
        for ref in refs:
            self._watch[ref] += 1
            if self._watch[ref] == 1:
                new.append(ref)
        await self._driver_watch(new)

    async def release_watched(self, refs: Iterable[PointRef]) -> None:
        gone = []
        for ref in refs:
            count = self._watch.get(ref, 0)
            if count <= 1:
                self._watch.pop(ref, None)
                if count == 1:
                    gone.append(ref)
            else:
                self._watch[ref] = count - 1
        await self._driver_unwatch(gone)

    def watch_count(self, ref: PointRef) -> int:
        return self._watch.get(ref, 0)

    async def _driver_watch(self, refs: list[PointRef]) -> None:
        for protocol, batch in self._by_protocol(refs).items():
            try:
                await self.drivers[protocol].watch(batch)
            except HubError as e:
                logger.warning("watching %d %s points failed: %s", len(batch), protocol.value, e)

    async def _driver_unwatch(self, refs: list[PointRef]) -> None:
        for protocol, batch in self._by_protocol(refs).items():
            try:
                await self.drivers[protocol].unwatch(batch)
            except HubError as e:
                logger.warning("unwatching %d %s points failed: %s", len(batch), protocol.value, e)

    def _by_protocol(self, refs: list[PointRef]) -> dict[ProtocolName, list[PointRef]]:
        out: dict[ProtocolName, list[PointRef]] = {}
        for ref in refs:
            dev = self._devices.get(ref.device)
            if dev is not None and dev.protocol in self.drivers:
                out.setdefault(dev.protocol, []).append(ref)
        return out

    def _tagged(self, manifest: SiteManifest) -> set[PointRef]:
        devices = manifest.devices
        return {ref for ref in map(manifest.point_ref, manifest.tags) if ref.device in devices}

    async def _set_trended(self, wanted: set[PointRef]) -> None:
        added, removed = wanted - self._trended, self._trended - wanted
        self._trended = set(wanted)
        await self.release_watched(removed)
        await self.ensure_watched(added)

    # -- discovery ------------------------------------------------------------------------
    async def discover(self, protocol: ProtocolName | None = None, timeout_s: float = 3.0) -> dict[str, Any]:
        """Sweep the network: ``{"devices": [DiscoveredDevice JSON + "known"
        and "device" (its manifest name)], "errors": {protocol: text}}``.
        Raises when every sweep failed."""
        protocols = [protocol] if protocol is not None else list(self.drivers)
        drivers = [(p, self.driver(p)) for p in protocols]
        results = await asyncio.gather(*(d.discover(timeout_s) for _, d in drivers), return_exceptions=True)
        devices: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for (p, _), result in zip(drivers, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                errors[p.value] = _describe_error(result)
                logger.warning("%s discovery failed: %s", p.value, errors[p.value])
                continue
            found = []
            for d in result:
                name = self._match(d.protocol, d.address, d.instance, d.hwid, d.extra.get("device"))
                found.append({**d.to_json(), "known": name is not None, "device": name})
            self._discovered[p] = found
            devices.extend(found)
        if errors and len(errors) == len(drivers):
            raise HubError("discovery failed: " + "; ".join(f"{p}: {e}" for p, e in errors.items()))
        return {"devices": devices, "errors": errors}

    def _match(self, protocol: ProtocolName, address: str, instance: int | None, hwid: str,
               named: Any) -> str | None:
        if isinstance(named, str) and named in self._devices:
            return named
        for dev in self._devices.values():
            rec = dev.record
            same_family = dev.protocol == protocol or {dev.protocol, protocol} <= _BACNET
            if not same_family:
                continue
            if address and address == rec.address and dev.protocol == protocol:
                return dev.name
            if instance is not None and protocol in _BACNET and instance == rec.instance:
                return dev.name
            if hwid and hwid == rec.hwid:
                return dev.name
        return None

    # -- overviews ------------------------------------------------------------------------
    def summary(self) -> dict[str, int]:
        """The ``Site.summary`` counts of the API."""
        return {
            "devices": len(self._devices),
            "online": sum(1 for d in self._devices.values() if d.record.online),
            "points": len(self._points),
            "unassigned": sum(1 for found in self._discovered.values() for d in found if not d["known"]),
            "pending_changes": self.pending_changes(),
        }

    def overview(self) -> dict[str, Any]:
        """``summary`` plus what the agent needs to orient itself: devices
        per protocol and per space, and the devices that need attention."""
        per_space: dict[str | None, list[_Device]] = {}
        for dev in self._devices.values():
            per_space.setdefault(dev.record.space, []).append(dev)
        protocols = Counter(d.protocol.value for d in self._devices.values())
        return {
            "site": self.name,
            "description": self.manifest.description,
            "summary": self.summary(),
            "protocols": dict(sorted(protocols.items())),
            "spaces": [
                {"id": s.id, "name": s.name, "parent": s.parent, "devices": len(per_space.get(s.id, [])),
                 "online": sum(1 for d in per_space.get(s.id, []) if d.record.online),
                 "points": sum(len(d.points) for d in per_space.get(s.id, []))}
                for s in self.manifest.spaces
            ],
            "unplaced": [d.name for d in per_space.get(None, [])],
            "offline": [d.name for d in self._devices.values() if not d.record.online],
            "not_described": {d.name: d.error for d in self._devices.values() if d.error is not None},
        }

    def site_json(self) -> dict[str, Any]:
        """The API ``Site``."""
        return {
            "name": self.name,
            "description": self.manifest.description,
            "spaces": [{"id": s.id, "name": s.name, "parent": s.parent} for s in self.manifest.spaces],
            "devices": [d.record.to_json(len(d.points)) for d in self._devices.values()],
            "summary": self.summary(),
        }

    def tree(self, space: str | None = None) -> dict[str, Any]:
        """Spaces nested by parent with their devices and counts (each
        space's counts include its children); devices without placement are
        listed as ``unplaced`` at the root."""
        spaces = self.manifest.spaces
        known = {s.id for s in spaces}
        if space is not None and space not in known:
            raise NotFound(f"unknown space {space!r}")
        placed: dict[str, list[_Device]] = {}
        for dev in self._devices.values():
            if dev.record.space is not None:
                placed.setdefault(dev.record.space, []).append(dev)
        children: dict[str | None, list[Any]] = {}
        for s in spaces:
            children.setdefault(s.parent, []).append(s)

        def build(s: Any) -> dict[str, Any]:
            kids = [build(c) for c in children.get(s.id, [])]
            devs = placed.get(s.id, [])
            return {
                "id": s.id,
                "name": s.name,
                "devices": [{"name": d.name, "protocol": d.protocol.value, "online": d.record.online,
                             "points": len(d.points), "equipment": d.spec.spec.get("equipment")} for d in devs],
                "device_count": len(devs) + sum(k["device_count"] for k in kids),
                "online": sum(1 for d in devs if d.record.online) + sum(k["online"] for k in kids),
                "points": sum(len(d.points) for d in devs) + sum(k["points"] for k in kids),
                "children": kids,
            }

        if space is not None:
            return {"spaces": [build(next(s for s in spaces if s.id == space))]}
        return {
            "spaces": [build(s) for s in children.get(None, [])],
            "unplaced": [d.name for d in self._devices.values() if d.record.space is None],
        }

    # -- manifest revisions ---------------------------------------------------------------
    def apply_tags(self, tags: Mapping[str, list[str]], safety: Mapping[str, SafetyClass | str]) -> None:
        """Replace the tags and safety classes of the overlay (full point ids)."""
        self._tags = {k: list(v) for k, v in tags.items()}
        self._safety = {k: SafetyClass(v) for k, v in safety.items()}
        for dev in self._devices.values():
            self._overlay(dev)

    async def reload(self, manifest: SiteManifest) -> None:
        """Switch to a new live manifest revision of the same site."""
        if manifest.name != self.name:
            raise InvalidRequest(f"the site is {self.name!r}; a manifest of {manifest.name!r} cannot be loaded")
        old = self.manifest.devices
        new = manifest.devices
        self.manifest = manifest
        self._tags, self._safety = manifest.tags, manifest.safety
        for name in [n for n in old if n not in new or old[n].protocol != new[n].protocol]:
            await self._remove(name)
        for protocol in self._wanted_protocols(manifest):
            if protocol not in self.drivers:
                driver = self._make_driver(protocol)
                if self._started:
                    await self._start_driver(protocol, driver)
        changed: list[_Device] = []
        for name, spec in new.items():
            dev = self._devices.get(name)
            if dev is None or dev.spec.spec != spec.spec:
                if dev is not None and dev.task is not None:
                    dev.task.cancel()
                    await asyncio.gather(dev.task, return_exceptions=True)
                await self._add(spec)
                changed.append(self._devices[name])
            else:
                dev.spec = spec
                dev.record.space = spec.space
        for dev in self._devices.values():
            self._overlay(dev)
        if self._started:
            for dev in changed:
                self._schedule_describe(dev)
        await self._set_trended(self._tagged(manifest))
        logger.info("site %s reloaded: %d devices (%d new or changed)", self.name, len(self._devices), len(changed))


def _address(spec: DeviceSpec) -> str:
    s = spec.spec
    if spec.protocol is ProtocolName.BACNET_UC:
        transport = s.get("transport") or {}
        host = transport.get("host") or transport.get("ipv4")
        return f"{host}:{transport.get('port', 1337)}" if host else str(transport.get("kind", ""))
    if spec.protocol is ProtocolName.BACNET_IP:
        return str(s.get("address", ""))
    if s.get("profile", "mqtt_tls") == "mqtt_tls":
        return f"{s.get('topic_root', 'bacnet-uc')}/{s.get('client_id', '')}"
    return str(s.get("topic", ""))


def _kind(type_name: str) -> PointKind:
    if type_name.endswith("-input"):
        return PointKind.INPUT
    if type_name.endswith("-output"):
        return PointKind.OUTPUT
    return PointKind.VALUE


def _score(words: list[str], ref: PointRef, point: Point) -> int:
    name = point.name.lower()
    tags = " ".join(point.tags).lower()
    score = sum(2 for w in words if w in name) + sum(1 for w in words if w in tags)
    if words and " ".join(words) in (str(ref).lower(), f"{ref.device}/{ref.obj}".lower()):
        score += 10
    return score


def _describe_error(error: BaseException) -> str:
    text = str(error)
    if isinstance(error, TimeoutError) and not text:
        return "timed out"
    return text or type(error).__name__

