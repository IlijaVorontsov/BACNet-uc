"""Run event bus and live point values.

``EventBus`` persists every run event before anyone sees it, so a browser
that reconnects (``Last-Event-ID``) replays exactly what it missed from the
store and then continues live. Producers never wait for consumers: each
subscriber has a bounded queue, and a subscriber that falls behind drops its
queue and re-reads the store from its last delivered ``seq``.

``LiveHub`` fans out point readings. They are not persisted: a slow viewer
only needs the newest values, so its queue drops the oldest readings.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import deque
from collections.abc import Iterable
from typing import Any

from ..core.ids import PointRef
from ..core.types import Reading
from .db import EVENT_KEYS, Store

logger = logging.getLogger(__name__)

#: A reading at most this much older than the cached one lost a race with it
#: and is dropped. Readings older still mean the wall clock stepped back:
#: dropping those would freeze the value until the clock caught up.
REORDER_WINDOW_S = 60.0


class RunSubscription:
    """Events of one run with ``seq > after_seq``: the persisted backlog, then
    live events, in ``seq`` order without gaps or duplicates.

    Iterate with ``async for``; ``aclose()`` (or ``async with``) unregisters.
    The yielded dicts are shared between subscribers: treat them as read-only.
    """

    def __init__(self, bus: EventBus, run_id: str, after_seq: int, queue_size: int, page_size: int) -> None:
        self.run_id = run_id
        #: ``seq`` of the last event yielded (the SSE ``id``).
        self.last_seq = max(0, after_seq)
        #: How often the live queue overflowed and the store was re-read.
        self.overflows = 0
        self._bus = bus
        self._queue: deque[dict[str, Any]] = deque()
        self._queue_size = queue_size
        self._page_size = page_size
        self._backlog: deque[dict[str, Any]] = deque()
        self._replay = True
        self._overflowed = False
        self._closed = False
        self._wakeup = asyncio.Event()

    def _push(self, event: dict[str, Any]) -> None:
        if self._closed or self._overflowed:
            return
        if len(self._queue) >= self._queue_size:
            # Everything dropped here is in the store; __anext__ re-reads it.
            self._queue.clear()
            self._overflowed = True
            self.overflows += 1
            logger.debug("run %s: subscriber fell behind, re-reading from seq %d", self.run_id, self.last_seq)
        else:
            self._queue.append(event)
        self._wakeup.set()

    def _close(self) -> None:
        self._closed = True
        self._queue.clear()
        self._backlog.clear()
        self._wakeup.set()

    def __aiter__(self) -> RunSubscription:
        return self

    async def __anext__(self) -> dict[str, Any]:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._backlog:
                event = self._backlog.popleft()
                self.last_seq = event["seq"]
                return event
            if self._overflowed:
                self._overflowed = False
                self._replay = True
            if self._replay:
                # Read after the reset above: whatever the queue dropped was
                # persisted before, so this read includes it.
                page = await self._bus.store.list_events(self.run_id, after_seq=self.last_seq, limit=self._page_size)
                self._replay = len(page) == self._page_size
                self._backlog.extend(page)
                continue
            if self._queue:
                event = self._queue.popleft()
                seq = event["seq"]
                if seq <= self.last_seq:
                    continue  # already replayed from the store
                if seq != self.last_seq + 1:
                    # Appended without the bus (or lost); the store has it.
                    self._replay = True
                    continue
                self.last_seq = seq
                return event
            self._wakeup.clear()
            await self._wakeup.wait()

    async def aclose(self) -> None:
        self._bus._unregister(self)
        self._close()

    async def __aenter__(self) -> RunSubscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class EventBus:
    """Numbered, persisted run events with replay-then-live subscriptions."""

    def __init__(self, store: Store, *, queue_size: int = 1000, page_size: int = 500) -> None:
        if queue_size < 1 or page_size < 1:
            raise ValueError("queue_size and page_size must be positive")
        self.store = store
        self._queue_size = queue_size
        self._page_size = page_size
        # Held across persist and fan-out so subscribers receive events in seq order.
        self._lock = asyncio.Lock()
        self._subs: dict[str, weakref.WeakSet[RunSubscription]] = {}
        self._closed = False

    async def append(self, run_id: str, event_type: str, /, **fields: Any) -> dict[str, Any]:
        """Persist an event with the run's next ``seq`` and deliver it to the
        run's subscribers. Returns the event (``seq``, ``run_id``, ``ts``,
        ``type`` and ``fields``). Completes even if the caller is cancelled,
        so an event is never stored without being delivered."""
        if self._closed:
            raise RuntimeError("the event bus is closed")
        clash = EVENT_KEYS & fields.keys()
        if clash:
            raise ValueError(f"event fields may not use the reserved keys {sorted(clash)}")
        return await asyncio.shield(self._append(run_id, event_type, fields))

    async def _append(self, run_id: str, event_type: str, fields: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            event = await self.store.append_event(run_id, event_type, fields)
            subs = self._subs.get(run_id)
            if subs:
                for sub in list(subs):
                    sub._push(event)
            elif subs is not None:
                del self._subs[run_id]  # every subscriber was garbage-collected
            return event

    def subscribe(self, run_id: str, after_seq: int = 0) -> RunSubscription:
        """Subscribe to a run's events with ``seq > after_seq``. Registration
        happens here, before the first store read, which is what makes the
        switch from replay to live gap-free."""
        sub = RunSubscription(self, run_id, after_seq, self._queue_size, self._page_size)
        if self._closed:
            sub._close()
        else:
            self._subs.setdefault(run_id, weakref.WeakSet()).add(sub)
        return sub

    def subscriber_count(self, run_id: str | None = None) -> int:
        if run_id is not None:
            return len(self._subs.get(run_id, ()))
        return sum(len(s) for s in self._subs.values())

    def _unregister(self, sub: RunSubscription) -> None:
        subs = self._subs.get(sub.run_id)
        if subs is not None:
            subs.discard(sub)
            if not subs:
                del self._subs[sub.run_id]

    def close(self) -> None:
        """End every subscription (hub shutdown); later appends fail."""
        self._closed = True
        for subs in self._subs.values():
            for sub in list(subs):
                sub._close()
        self._subs.clear()


def _matches(ref: PointRef, points: frozenset[str] | None, devices: frozenset[str] | None) -> bool:
    """No filter matches everything; otherwise the point id or its device must be listed."""
    if points is None and devices is None:
        return True
    return (points is not None and str(ref) in points) or (devices is not None and ref.device in devices)


class LiveSubscription:
    """Readings that match a filter: first the cached values (when asked
    for), then live ones. When the consumer is slower than the drivers, the
    oldest queued readings are dropped (counted in ``dropped``)."""

    def __init__(
        self,
        hub: LiveHub,
        points: frozenset[str] | None,
        devices: frozenset[str] | None,
        queue_size: int,
        initial: list[Reading],
    ) -> None:
        self.points = points
        self.devices = devices
        self.dropped = 0
        self._hub = hub
        self._initial = deque(initial)
        self._queue: deque[Reading] = deque(maxlen=queue_size)
        self._closed = False
        self._wakeup = asyncio.Event()

    def matches(self, ref: PointRef) -> bool:
        return _matches(ref, self.points, self.devices)

    def _push(self, reading: Reading) -> None:
        if self._closed:
            return
        if len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(reading)
        self._wakeup.set()

    def _close(self) -> None:
        self._closed = True
        self._initial.clear()
        self._queue.clear()
        self._wakeup.set()

    def __aiter__(self) -> LiveSubscription:
        return self

    async def __anext__(self) -> Reading:
        while True:
            if self._closed:
                raise StopAsyncIteration
            if self._initial:
                return self._initial.popleft()
            if self._queue:
                return self._queue.popleft()
            self._wakeup.clear()
            await self._wakeup.wait()

    async def aclose(self) -> None:
        self._hub._subs.discard(self)
        self._close()

    async def __aenter__(self) -> LiveSubscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


class LiveHub:
    """Latest value per point and fan-out of live readings (not persisted).

    ``publish`` is synchronous so it can be the drivers' ``ctx.publish``.
    """

    def __init__(self, *, site: str | None = None, queue_size: int = 256) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        #: Lets filters use site-relative point ids.
        self.site = site
        self._queue_size = queue_size
        self._latest: dict[str, Reading] = {}
        self._subs: weakref.WeakSet[LiveSubscription] = weakref.WeakSet()
        self._closed = False

    def publish(self, reading: Reading) -> None:
        key = str(reading.ref)
        cached = self._latest.get(key)
        if cached is not None and cached.ts - REORDER_WINDOW_S <= reading.ts < cached.ts:
            return  # an older poll result arriving after a newer COV/MQTT value
        self._latest[key] = reading
        for sub in list(self._subs):
            if sub.matches(reading.ref):
                sub._push(reading)

    def latest(self, point: PointRef | str) -> Reading | None:
        return self._latest.get(self._key(point))

    def snapshot(
        self, points: Iterable[PointRef | str] | None = None, devices: Iterable[str] | None = None
    ) -> list[Reading]:
        """Cached readings matching the filter, ordered by point id."""
        pts, devs = self._filter(points, devices)
        return [r for _, r in sorted(self._latest.items()) if _matches(r.ref, pts, devs)]

    def subscribe(
        self,
        points: Iterable[PointRef | str] | None = None,
        devices: Iterable[str] | None = None,
        *,
        initial: bool = True,
        queue_size: int | None = None,
    ) -> LiveSubscription:
        """Readings of the given points and/or devices (None and None = all).
        With ``initial`` the current cached values come first."""
        pts, devs = self._filter(points, devices)
        size = self._queue_size if queue_size is None else queue_size
        if size < 1:
            raise ValueError("queue_size must be positive")
        sub = LiveSubscription(self, pts, devs, size, [])
        if self._closed:
            sub._close()
            return sub
        if initial:
            sub._initial.extend(self.snapshot(pts, devs))
        self._subs.add(sub)
        return sub

    def forget_device(self, device: str) -> None:
        """Drop cached values of a device that left the manifest."""
        for key in [k for k, r in self._latest.items() if r.ref.device == device]:
            del self._latest[key]

    def subscriber_count(self) -> int:
        return len(self._subs)

    def close(self) -> None:
        self._closed = True
        for sub in list(self._subs):
            sub._close()
        self._subs = weakref.WeakSet()

    def _key(self, point: PointRef | str) -> str:
        if isinstance(point, PointRef):
            return str(point)
        return str(PointRef.parse(point, default_site=self.site))

    def _filter(
        self, points: Iterable[PointRef | str] | None, devices: Iterable[str] | None
    ) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        pts = None if points is None else frozenset(self._key(p) for p in points)
        devs = None if devices is None else frozenset(devices)
        return pts, devs
