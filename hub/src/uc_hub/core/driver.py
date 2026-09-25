"""Driver interface. One driver instance serves all devices of one protocol
on a site.

Life cycle: ``Driver(ctx)`` (per-driver settings from hub.yaml arrive in
``ctx.settings``) -> ``add_device`` for each device of the manifest ->
``start()`` -> calls from the site runtime (``add_device`` and
``remove_device`` also while running, when a manifest revision goes live) ->
``stop()``.

Live values: the driver pushes readings with ``ctx.publish`` for every point
passed to ``watch`` (COV, MQTT messages or polling, the driver's choice) and
whenever a ``read`` returns a fresh value. ``ctx.set_online`` reports device
reachability changes.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Callable

from .ids import PointRef
from .types import (
    DeviceDescription,
    DeviceRecord,
    DiscoveredDevice,
    ProtocolName,
    Reading,
    Value,
    WriteResult,
)


@dataclass
class DriverContext:
    site: str
    publish: Callable[[Reading], None]
    set_online: Callable[[str, bool], None] = lambda device, online: None
    #: Extra per-driver settings from the hub config (timeouts, broker, ...).
    settings: dict[str, Any] = field(default_factory=dict)


class Driver(abc.ABC):
    protocol: ProtocolName

    def __init__(self, ctx: DriverContext) -> None:
        self.ctx = ctx

    # -- life cycle ---------------------------------------------------------
    @abc.abstractmethod
    async def add_device(self, record: DeviceRecord, spec: dict[str, Any]) -> None:
        """Register a device from the site manifest. ``spec`` is its manifest
        entry (a ``system.nodes[]`` item for bacnet-uc, an
        ``external_devices[]`` item otherwise)."""

    async def remove_device(self, name: str) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    # -- inventory ----------------------------------------------------------
    @abc.abstractmethod
    async def discover(self, timeout_s: float = 3.0) -> list[DiscoveredDevice]:
        """Find devices on the network (manifest devices included)."""

    @abc.abstractmethod
    async def describe(self, device: str) -> DeviceDescription:
        """Current points (and apps) of a manifest device, read from the device."""

    # -- values -------------------------------------------------------------
    @abc.abstractmethod
    async def read(self, refs: list[PointRef]) -> list[Reading]:
        """Read present values. Never raises for a single bad point: that
        point's reading has quality FAULT/OFFLINE and ``error`` set."""

    @abc.abstractmethod
    async def write(self, ref: PointRef, value: Value, priority: int | None) -> WriteResult:
        """Write a present value (at ``priority`` for commandable points).
        Policy checks happen before this call; drivers do not repeat them."""

    async def relinquish(self, ref: PointRef, priority: int) -> WriteResult:
        """Write NULL at ``priority``."""
        return await self.write(ref, None, priority)

    async def watch(self, refs: list[PointRef]) -> None:
        """Start publishing live readings for these points (idempotent)."""
        return None

    async def unwatch(self, refs: list[PointRef]) -> None:
        return None

    # -- optional capabilities (raise errors.Unsupported) ---------------------
    async def identify(self, device: str, seconds: int = 30) -> None:
        from .errors import Unsupported

        raise Unsupported(f"{self.protocol.value} devices cannot identify")

    async def priority_array(self, ref: PointRef) -> list[Value] | None:
        """The 16 priority slots (None = NULL), or None if not commandable."""
        return None
