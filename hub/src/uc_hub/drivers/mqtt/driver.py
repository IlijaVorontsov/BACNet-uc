"""Driver for MQTT devices: ``external_devices`` entries with ``protocol: mqtt``.

One broker connection (``BrokerLink``) serves every device of the site; it
reconnects with back-off and restores the subscriptions by itself. Devices
push their values, so the driver keeps the last sample of every point and
``read`` answers from that cache: quality ``stale`` when a periodic value is
older than ``stale_after_s``, ``offline`` when the point was never seen, the
device is offline or the broker link is down. Every decoded message is
published at once with ``ctx.publish``, so ``watch`` has nothing to add. The
profiles (``mqtt_tls``, ``generic-json``) are in ``profiles``.

A value that arrived as a retained message is the broker's stored copy, of
unknown age; ``retained_only`` tells consumers that must not act on such a
value (gateway bridges) until the device sends a live one.

Settings (``drivers.mqtt`` in hub.yaml, all optional): ``host``, ``port``
(8883 with ``tls``, else 1883), ``tls`` {``ca``, ``cert``, ``key``,
``insecure``}, ``username``, ``password`` or ``password_env``, ``client_id``
("uc-hub"), ``keepalive_s`` (30), ``timeout_s`` (10), ``reconnect_min_s``
(0.5), ``reconnect_max_s`` (30), ``stale_after_s`` (300),
``command_timeout_s`` (5) and ``max_payload_bytes`` (262144).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from typing import Any

import aiomqtt

from ...core.driver import Driver, DriverContext
from ...core.errors import DeviceError, DeviceTimeout, InvalidRequest, NotFound, Unsupported
from ...core.ids import PointRef
from ...core.types import (
    DeviceDescription,
    DeviceRecord,
    DiscoveredDevice,
    ProtocolName,
    Quality,
    Reading,
    Value,
    WriteResult,
)
from .connection import BrokerLink, BrokerSettings, positive_number, topic_matches
from .profiles import MqttTlsProfile, Profile, json_object, make_profile, short_text

logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_S = 300.0
DEFAULT_COMMAND_TIMEOUT_S = 5.0
DEFAULT_MAX_PAYLOAD_BYTES = 256 * 1024
#: Discovery keeps at most this many devices, whatever the broker holds.
MAX_DISCOVERED = 1000
_DISCOVERY_FILTERS = ("+/+/info", "+/+/status")


class MqttDriver(Driver):
    protocol = ProtocolName.MQTT

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        settings = ctx.settings or {}
        self.broker = BrokerSettings.from_settings(settings)
        self.stale_after_s = positive_number(settings, "stale_after_s", DEFAULT_STALE_AFTER_S)
        self.command_timeout_s = positive_number(
            settings, "command_timeout_s", DEFAULT_COMMAND_TIMEOUT_S)
        self.max_payload_bytes = int(positive_number(
            settings, "max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES, integer=True))
        self._devices: dict[str, Profile] = {}
        self._exact: dict[str, list[Profile]] = {}
        self._wild: dict[str, list[Profile]] = {}
        self._link = BrokerLink(
            self.broker, on_message=self._on_message, on_disconnect=self._on_link_down,
        )

    @property
    def connected(self) -> bool:
        """True while the broker connection is up."""
        return self._link.connected

    async def wait_connected(self, timeout_s: float) -> bool:
        return await self._link.wait_connected(timeout_s)

    # -- life cycle -----------------------------------------------------------
    async def add_device(self, record: DeviceRecord, spec: dict[str, Any]) -> None:
        """Adding a name again replaces the device; an invalid spec raises
        InvalidRequest and leaves the current entry alone."""
        profile = make_profile(self.ctx.site, record, spec)
        if record.name in self._devices:
            await self.remove_device(record.name)
        self._devices[record.name] = profile
        for f in profile.filters:
            self._routes(f).setdefault(f, []).append(profile)
        await self._link.subscribe(profile.filters)

    async def remove_device(self, name: str) -> None:
        profile = self._devices.pop(name, None)
        if profile is None:
            return
        for f in profile.filters:
            routes = self._routes(f)
            remaining = [p for p in routes.get(f, []) if p is not profile]
            if remaining:
                routes[f] = remaining
            else:
                routes.pop(f, None)
        profile.close(f"{name} was removed")
        await self._link.unsubscribe(profile.filters)

    async def start(self) -> None:
        """Connect in the background; devices come online as their retained
        status arrives."""
        await self._link.start()

    async def stop(self) -> None:
        await self._link.stop()
        for profile in self._devices.values():
            profile.close("MQTT driver stopped")

    # -- inventory ------------------------------------------------------------
    async def discover(self, timeout_s: float = 3.0) -> list[DiscoveredDevice]:
        """Devices that follow the mqtt_tls topic scheme, from the retained
        ``+/+/info`` and ``+/+/status`` messages. Uses a separate short-lived
        session so the driver's own subscriptions stay untouched."""
        client = self.broker.client(
            client_id=f"{self.broker.client_id}-discover-{secrets.token_hex(3)}")
        found: dict[str, dict[str, Any]] = {}
        try:
            async with client:
                await client.subscribe([(f, 0) for f in _DISCOVERY_FILTERS])
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(timeout_s):
                        async for message in client.messages:
                            payload = message.payload
                            # Retained info comes from any device on the broker:
                            # the same size limit as for live messages applies.
                            if isinstance(payload, (bytes, bytearray)) \
                                    and len(payload) <= self.max_payload_bytes:
                                _collect(message.topic.value, bytes(payload), found)
        except aiomqtt.MqttError as e:
            raise DeviceError(f"MQTT discovery: cannot use broker {self.broker.url}: {e}") from e
        known = {p.prefix: name for name, p in self._devices.items()
                 if isinstance(p, MqttTlsProfile)}
        devices = []
        for prefix, seen in sorted(found.items()):
            root, _, client_id = prefix.rpartition("/")
            info = seen.get("info")
            extra: dict[str, Any] = {
                "profile": MqttTlsProfile.name,
                "topic_root": root,
                "client_id": client_id,
                "status": seen.get("status"),
                "info": info,
            }
            if prefix in known:
                extra["device"] = known[prefix]
            devices.append(DiscoveredDevice(
                protocol=ProtocolName.MQTT,
                address=prefix,
                name=client_id,
                model=short_text(info.get("board")) if info else "",
                hwid=short_text(info.get("hwid")) if info else "",
                extra=extra,
            ))
        return devices

    async def describe(self, device: str) -> DeviceDescription:
        profile = self._profile(device)
        return DeviceDescription(
            device=profile.record, points=profile.points(), apps=[], extra=profile.extra(),
        )

    # -- values ---------------------------------------------------------------
    async def read(self, refs: list[PointRef]) -> list[Reading]:
        now = time.time()
        up = self._link.connected
        out = []
        for ref in refs:
            profile = self._devices.get(ref.device) if ref.site == self.ctx.site else None
            if profile is None:
                out.append(Reading(ref, None, now, Quality.FAULT,
                                   error=f"unknown mqtt device {ref.device!r}"))
            else:
                out.append(profile.reading(ref.obj, now, self.stale_after_s, up))
        return out

    async def write(self, ref: PointRef, value: Value, priority: int | None) -> WriteResult:
        """MQTT points have no priority array: ``priority`` is ignored and the
        value stays until something else overwrites it."""
        profile = self._profile(ref.device)
        spec = profile.spec(ref.obj)
        if spec is None:
            raise NotFound(f"{ref}: no such point")
        if not spec.writable:
            raise InvalidRequest(f"{ref} is read-only")
        if value is None:
            raise InvalidRequest(f"{ref} has no priority array: write a value to change it")
        try:
            written = await profile.write(ref.obj, value, self._send, self.command_timeout_s)
        except (DeviceError, DeviceTimeout, Unsupported) as e:
            return WriteResult(ref, False, value, None, str(e))
        return WriteResult(ref, True, written, None)

    async def relinquish(self, ref: PointRef, priority: int) -> WriteResult:
        return WriteResult(ref, False, None, priority,
                           f"{ref} has no priority array; write the value to restore instead")

    def retained_only(self, ref: PointRef) -> bool:
        """True while the point's value is a retained message the broker
        replayed (a stored copy of unknown age) and no live message has
        updated it since the hub connected. Points that declare retained
        values current (``trust_retained``, the mqtt_tls status) never are."""
        profile = self._devices.get(ref.device) if ref.site == self.ctx.site else None
        if profile is None:
            return False
        spec = profile.spec(ref.obj)
        return spec is not None and not spec.trust_retained and ref.obj in profile.retained

    # -- commands -------------------------------------------------------------
    async def command(
        self, device: str, cmd: str, arg: str | None = None, timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Send a command to an mqtt_tls device (``ping``, ``led``, ...) and
        return its reply from the event topic."""
        profile = self._mqtt_tls(device)
        return await profile.command(self._send, cmd, arg, timeout_s or self.command_timeout_s)

    async def identify(self, device: str, seconds: int = 30) -> None:
        profile = self._mqtt_tls(device)
        await profile.identify(self._send, seconds, self.command_timeout_s)

    # -- internals --------------------------------------------------------------
    def _profile(self, name: str) -> Profile:
        profile = self._devices.get(name)
        if profile is None:
            raise NotFound(f"unknown mqtt device {name!r}")
        return profile

    def _mqtt_tls(self, name: str) -> MqttTlsProfile:
        profile = self._profile(name)
        if not isinstance(profile, MqttTlsProfile):
            raise Unsupported(f"{name} is a {profile.name} device, which takes no commands")
        return profile

    def _routes(self, filter_: str) -> dict[str, list[Profile]]:
        return self._wild if ("+" in filter_ or "#" in filter_) else self._exact

    async def _send(self, topic: str, payload: bytes) -> None:
        await self._link.publish(topic, payload, qos=1)

    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if len(payload) > self.max_payload_bytes:
            logger.warning("ignoring a %d-byte message on %s (max_payload_bytes is %d)",
                           len(payload), topic, self.max_payload_bytes)
            return
        profiles = self._exact.get(topic, [])
        if self._wild:
            profiles = profiles + [p for f, ps in self._wild.items()
                                   if topic_matches(f, topic) for p in ps]
        now = time.time()
        for profile in profiles:
            changed = profile.handle(topic, payload, now)
            if retained:
                profile.retained.update(changed)
            else:
                profile.retained.difference_update(changed)
            if profile.online:
                profile.record.last_seen = now
            if profile.online is not None and profile.online != profile.record.online:
                self._set_online(profile, profile.online)
                if not profile.online:
                    changed = profile.objs()
            for obj in changed:
                self._emit(profile.reading(obj, now, self.stale_after_s, True))

    def _on_link_down(self) -> None:
        now = time.time()
        for profile in self._devices.values():
            profile.link_lost()
            if profile.record.online:
                self._set_online(profile, False)
            for obj in profile.objs():
                self._emit(profile.reading(obj, now, self.stale_after_s, False))

    def _set_online(self, profile: Profile, online: bool) -> None:
        profile.record.online = online
        try:
            self.ctx.set_online(profile.record.name, online)
        except Exception:
            logger.exception("set_online(%s) failed", profile.record.name)

    def _emit(self, reading: Reading) -> None:
        try:
            self.ctx.publish(reading)
        except Exception:
            logger.exception("publishing %s failed", reading.ref)


def _collect(topic: str, payload: bytes, found: dict[str, dict[str, Any]]) -> None:
    prefix, _, leaf = topic.rpartition("/")
    if prefix.count("/") != 1 or leaf not in ("info", "status"):
        return
    if prefix not in found and len(found) >= MAX_DISCOVERED:
        return
    value: Any
    if leaf == "info":
        value = json_object(payload)
    else:
        value = payload.decode("utf-8", "replace").strip().lower()
        if value not in ("online", "offline"):
            value = None
    if value is not None:
        found.setdefault(prefix, {})[leaf] = value
