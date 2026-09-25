"""Gateway bridges and the gateway side of a plan.

A bridge copies a source point (typically an MQTT sensor) to a BACnet
destination: ``value * scale + offset`` is written at the bridge priority,
under the same rules as an agent write (``Policy.check_point_write`` without
counting toward the agent's rate limit: life-safety points, deny patterns
and priorities above the agent's are refused). A value is written when it
changes and again every half ``max_age_s``; BACnet-uc nodes also get
``lease_ms = max_age_s``, so they relinquish by themselves when the gateway
is gone.

When the source is older than ``max_age_s``, not of good quality, or only
known from a retained MQTT message (the broker's copy of unknown age; points
with ``trust_retained`` excepted), the destination is relinquished at the
bridge priority; so it is when the policy stops admitting the bridge's
value (a later revision denies or marks the destination). Non-commandable destinations have nothing to relinquish and
keep their last value; ``status`` shows them as stale.

``Gateway`` is the ``manifest.apply.GatewayApplier``: ``apply_bridges``
replaces the running set, ``apply_tags`` the tags and safety classes of the
point model and the policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..core.errors import HubError
from ..core.ids import PointRef
from ..core.types import Quality, Reading, SafetyClass, Value
from ..manifest.load import Bridge
from ..policy import Policy
from .site import SiteRuntime

logger = logging.getLogger(__name__)

#: Shortest and longest pause between two checks of a bridge's source.
MIN_TICK_S = 0.05
MAX_TICK_S = 5.0
RELINQUISH_TIMEOUT_S = 10.0


@dataclass(eq=False)
class _Running:
    bridge: Bridge
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    #: The priority our value sits at on the destination, None when nothing
    #: of ours is there (or the destination is not commandable).
    holding: int | None = None
    written: Value = None
    written_at: float = -math.inf
    state: str = "waiting"
    detail: str = "no value from the source yet"

    @property
    def tick_s(self) -> float:
        return min(MAX_TICK_S, max(MIN_TICK_S, self.bridge.max_age_s / 4))

    def to_json(self) -> dict[str, Any]:
        return {**self.bridge.to_dict(), "state": self.state, "detail": self.detail,
                "value": self.written, "holding": self.holding}


class Bridges:
    def __init__(self, site: SiteRuntime, policy: Policy, *, clock: Callable[[], float] = time.time) -> None:
        self.site = site
        self.policy = policy
        self._clock = clock
        self._running: dict[PointRef, _Running] = {}
        self._listening = False

    async def start(self, bridges: list[Bridge]) -> None:
        if not self._listening:
            self.site.add_listener(self._on_reading)
            self._listening = True
        await self.replace(bridges)

    async def stop(self) -> None:
        """Stop every bridge and relinquish what it wrote: without the
        gateway nothing keeps the values fresh."""
        await self.replace([])
        if self._listening:
            self.site.remove_listener(self._on_reading)
            self._listening = False

    def status(self) -> list[dict[str, Any]]:
        return [r.to_json() for r in self._running.values()]

    async def replace(self, bridges: list[Bridge]) -> None:
        """Run exactly ``bridges`` (one per destination, as the manifest
        validator guarantees). A removed bridge, or one whose priority
        changed, has its destination relinquished; an unchanged or updated
        bridge keeps its value in place."""
        wanted = {b.dest: b for b in bridges}
        started: list[Bridge] = []
        for dest, running in list(self._running.items()):
            new = wanted.get(dest)
            if new == running.bridge:
                continue
            await self._stop(running, relinquish=new is None or new.priority != running.bridge.priority)
            del self._running[dest]
            await self.site.release_watched([running.bridge.source])
            if new is not None and running.holding == new.priority:
                self._start(new, holding=running.holding, written=running.written)
                started.append(new)
        for dest, bridge in wanted.items():
            if dest not in self._running:
                self._start(bridge)
                started.append(bridge)
        for bridge in started:
            try:
                await self.site.ensure_watched([bridge.source])
            except HubError as e:
                logger.warning("bridge source %s cannot be watched: %s", bridge.source, e)

    def _start(self, bridge: Bridge, *, holding: int | None = None, written: Value = None) -> None:
        running = _Running(bridge, holding=holding, written=written)
        running.task = asyncio.create_task(self._run(running), name=f"bridge-{bridge.dest}")
        self._running[bridge.dest] = running

    async def _stop(self, running: _Running, *, relinquish: bool) -> None:
        if running.task is not None:
            running.task.cancel()
            await asyncio.gather(running.task, return_exceptions=True)
        if relinquish and running.holding is not None:
            try:
                async with asyncio.timeout(RELINQUISH_TIMEOUT_S):
                    await self._relinquish(running, "the bridge was removed or stopped")
            except (HubError, TimeoutError) as e:
                logger.warning("bridge %s -> %s: could not relinquish the destination: %s",
                               running.bridge.source, running.bridge.dest, e or "timed out")

    def _on_reading(self, reading: Reading) -> None:
        for running in self._running.values():
            if running.bridge.source == reading.ref:
                running.wake.set()

    async def _run(self, running: _Running) -> None:
        while True:
            running.wake.clear()
            try:
                await self._step(running)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bad bridge must not stop the others; it retries on the next tick
                detail = str(e) or type(e).__name__
                if (running.state, running.detail) != ("error", detail):
                    log = logger.warning if isinstance(e, HubError) else logger.exception
                    log("bridge %s -> %s: %s", running.bridge.source, running.bridge.dest, detail)
                running.state, running.detail = "error", detail
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(running.wake.wait(), running.tick_s)

    async def _step(self, running: _Running) -> None:
        bridge = running.bridge
        now = self._clock()
        reading = self.site.latest(bridge.source)
        stale = self._stale(bridge, reading, now)
        if stale is not None:
            if running.holding is not None:
                await self._relinquish(running, stale)
            running.state, running.detail = "stale", stale
            return
        assert reading is not None
        point = await self.site.point(bridge.dest)
        try:
            value = _convert(reading.value, bridge, point.datatype)
            refresh = now - running.written_at >= bridge.max_age_s / 2
            if value == running.written and (running.holding is not None or not point.commandable) and not refresh:
                return
            priority = self.policy.check_point_write(point, bridge.priority, value, admit=False)
        except HubError:
            # The rules no longer admit our value there (a deny pattern or safety mark of a newer
            # revision, a value out of range): take back what we hold instead of leaving it stale.
            if running.holding is not None:
                await self._relinquish(running, "the policy refuses the bridge's value")
            raise
        lease_ms = bridge.max_age_s * 1000 if priority is not None else None
        result = await self.site.write(bridge.dest, value, priority, lease_ms=lease_ms)
        if not result.ok:
            running.state, running.detail = "error", f"write failed: {result.error}"
            logger.warning("bridge %s -> %s: %s", bridge.source, bridge.dest, running.detail)
            return
        running.holding, running.written, running.written_at = priority, value, now
        running.state, running.detail = "active", f"wrote {value!r}" + (f" at priority {priority}" if priority else "")

    def _stale(self, bridge: Bridge, reading: Reading | None, now: float) -> str | None:
        """Why the source value must not be copied, or None."""
        if reading is None:
            return "no value from the source yet"
        if reading.quality is not Quality.GOOD:
            return f"source quality is {reading.quality.value}" + (f" ({reading.error})" if reading.error else "")
        if now - reading.ts > bridge.max_age_s:
            return f"source value is {now - reading.ts:.0f} s old (max_age_s {bridge.max_age_s})"
        if self.site.retained_only(bridge.source):
            return "source only has a retained MQTT value; waiting for a live message"
        if not isinstance(reading.value, (int, float)):  # booleans count as 0/1
            return f"source value {reading.value!r} is not a number"
        return None

    async def _relinquish(self, running: _Running, why: str) -> None:
        assert running.holding is not None
        result = await self.site.relinquish(running.bridge.dest, running.holding)
        if not result.ok:
            raise HubError(f"relinquishing {running.bridge.dest} at priority {running.holding} failed: {result.error}")
        logger.info("bridge %s -> %s: relinquished (%s)", running.bridge.source, running.bridge.dest, why)
        running.holding, running.written, running.written_at = None, None, -math.inf


def _convert(value: Value, bridge: Bridge, datatype: str) -> Value:
    number = float(value) * bridge.scale + bridge.offset  # type: ignore[arg-type]
    if not math.isfinite(number):
        raise HubError(f"{value!r} * {bridge.scale} + {bridge.offset} is not a finite number")
    return round(number) if datatype in ("int", "enum") else number


class Gateway:
    """``manifest.apply.GatewayApplier`` over the running hub."""

    def __init__(self, site: SiteRuntime, policy: Policy, bridges: Bridges) -> None:
        self.site = site
        self.policy = policy
        self.bridges = bridges

    async def apply_bridges(self, bridges: list[dict[str, Any]]) -> None:
        await self.bridges.replace([parse_bridge(b, self.site.name) for b in bridges])

    async def apply_tags(self, tags: dict[str, list[str]], safety: dict[str, str]) -> None:
        self.policy.configure(self.policy.settings, safety)
        self.site.apply_tags(tags, {k: SafetyClass(v) for k, v in safety.items()})


def parse_bridge(entry: Mapping[str, Any], site: str) -> Bridge:
    """A ``Bridge.to_dict`` entry (defaults applied) back into a ``Bridge``."""
    return Bridge(
        source=PointRef.parse(str(entry["from"]), default_site=site),
        dest=PointRef.parse(str(entry["to"]), default_site=site),
        max_age_s=int(entry["max_age_s"]),
        scale=float(entry["scale"]),
        offset=float(entry["offset"]),
        priority=int(entry["priority"]),
    )
