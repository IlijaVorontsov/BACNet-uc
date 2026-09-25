"""Leases: every live write or IO force the agent makes ends at a known time.

A lease records what to undo (relinquish a priority slot, release a force).
It is persisted before the caller touches the device. The manager wakes up
at the earliest expiry and calls the releaser, retrying failed releases with
exponential backoff. Leases still active in the store when the manager starts
were left by a hub that stopped without releasing them, so they are released
at once (DESIGN.md section 10, rule 3).
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..core.errors import InvalidRequest, NotFound
from ..store.db import Conflict, Store, new_id

logger = logging.getLogger(__name__)

LeaseKind = Literal["write", "force"]
LeaseState = Literal["active", "released", "failed"]


@dataclass(slots=True)
class Lease:
    id: str
    kind: LeaseKind
    #: What to undo, for the releaser: ``{"point": "<site/device/obj>"}`` for a
    #: write, ``{"node": ..., "channel": ...}`` for an IO force.
    target: dict[str, Any]
    #: Priority slot of a write to a commandable point.
    priority: int | None
    expires_at: float
    created_by: str
    run_id: str | None
    created_at: float
    state: LeaseState = "active"
    #: Release attempts made so far (the successful one included).
    attempts: int = 0
    released_at: float | None = None
    last_error: str | None = None

    @property
    def slot(self) -> str:
        """What the lease holds; a new lease on the same slot replaces the old one."""
        return json.dumps([self.kind, self.target, self.priority], sort_keys=True, default=str)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "priority": self.priority,
            "expires_at": self.expires_at,
            "created_by": self.created_by,
            "run_id": self.run_id,
            "state": self.state,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "released_at": self.released_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Lease:
        return cls(
            id=str(data["id"]),
            kind=data["kind"],
            target=dict(data["target"]),
            priority=data.get("priority"),
            expires_at=float(data["expires_at"]),
            created_by=str(data["created_by"]),
            run_id=data.get("run_id"),
            created_at=float(data["created_at"]),
            state=data.get("state", "active"),
            attempts=int(data.get("attempts", 0)),
            released_at=data.get("released_at"),
            last_error=data.get("last_error"),
        )


#: Undoes a lease on the device; raising (or hanging past the timeout) means
#: the release failed and will be retried.
Releaser = Callable[[Lease], Awaitable[None]]
#: ``(event, lease, detail)``; events: granted, extended, released,
#: superseded, release_failed, failed. May be sync or async.
LeaseEventHandler = Callable[[str, Lease, str], Awaitable[None] | None]

_STARTUP = "left active when the hub stopped"


class LeaseManager:
    def __init__(
        self,
        store: Store,
        releaser: Releaser,
        *,
        clock: Callable[[], float] = time.time,
        on_event: LeaseEventHandler | None = None,
        release_timeout_s: float = 15.0,
        retry_base_s: float = 1.0,
        retry_max_s: float = 60.0,
        max_attempts: int | None = 10,
        max_sleep_s: float = 60.0,
    ) -> None:
        """``clock`` is wall-clock seconds (expiry times are persisted). With a
        fake clock, call ``wake()`` after moving it. ``max_attempts`` None
        retries forever; otherwise the lease ends ``failed``. ``max_sleep_s``
        bounds how long a wall-clock jump can delay an expiry."""
        self._store = store
        self._releaser = releaser
        self._clock = clock
        self._on_event = on_event
        self._release_timeout_s = release_timeout_s
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        self._max_attempts = max_attempts
        self._max_sleep_s = max_sleep_s
        #: Active leases, and when each one's next release attempt is due.
        self._leases: dict[str, Lease] = {}
        self._due: dict[str, float] = {}
        self._why: dict[str, str] = {}
        self._inflight: dict[str, asyncio.Task[None]] = {}
        #: Serializes grants, so two grants of one slot cannot both stay active.
        self._grant_lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- life cycle ---------------------------------------------------------------------
    async def start(self) -> None:
        """Release every lease the store still has active, then run the
        expiry loop. Returns once each stale lease had its first attempt."""
        if self._task is not None:
            return
        stale = [Lease.from_json(r) for r in await self._store.list_leases(state="active", limit=10_000)]
        now = self._clock()
        for lease in stale:
            self._leases[lease.id] = lease
            self._due[lease.id] = now
            self._why[lease.id] = _STARTUP
        tasks = [self._start_release(lease.id) for lease in stale]
        self._task = asyncio.create_task(self._loop(), name="lease-expiry")
        if tasks:
            logger.warning("releasing %d lease(s) %s", len(tasks), _STARTUP)
            await asyncio.wait(tasks)

    async def stop(self) -> None:
        """Cancel the loop and in-flight releases. Active leases stay active in
        the store and are released by the next ``start``."""
        task, self._task = self._task, None
        pending = [t for t in (task, *self._inflight.values()) if t is not None]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()
        self._leases.clear()
        self._due.clear()
        self._why.clear()

    @property
    def running(self) -> bool:
        return self._task is not None

    def wake(self) -> None:
        """Re-evaluate due times now (after moving a fake clock)."""
        self._wakeup.set()

    # -- API --------------------------------------------------------------------------------
    async def grant(
        self,
        kind: LeaseKind,
        target: Mapping[str, Any],
        priority: int | None,
        duration_s: float,
        user: str,
        run_id: str | None = None,
    ) -> Lease:
        """Record a lease before the caller writes or forces. An active lease
        on the same slot is replaced (released without calling the releaser,
        since the caller's new write now owns the slot)."""
        self._require_running()
        if kind not in ("write", "force"):
            raise InvalidRequest(f"lease kind must be write or force, got {kind!r}")
        _check_seconds(duration_s)
        async with self._grant_lock:
            return await self._grant(kind, copy.deepcopy(dict(target)), priority, duration_s, user, run_id)

    async def _grant(
        self,
        kind: LeaseKind,
        target: dict[str, Any],
        priority: int | None,
        duration_s: float,
        user: str,
        run_id: str | None,
    ) -> Lease:
        now = self._clock()
        lease = Lease(
            id=new_id("l"), kind=kind, target=target, priority=priority,
            expires_at=now + duration_s, created_by=user, run_id=run_id, created_at=now,
        )
        same_slot = [old for old in self._leases.values() if old.slot == lease.slot]
        # A release of this slot still in flight would clear the value the caller is about to write.
        releasing = [self._inflight[old.id] for old in same_slot if old.id in self._inflight]
        if releasing:
            _, still = await asyncio.wait(releasing, timeout=self._release_timeout_s)
            if still:
                logger.warning("%s %s: a release of this slot is still running and may clear the new value",
                               kind, target)
            now = self._clock()
            lease.created_at, lease.expires_at = now, now + duration_s
        # Parked, a failed release of the slot cannot be retried while the new lease is stored.
        parked = {old.id: self._due.pop(old.id) for old in same_slot
                  if old.id in self._due and old.id not in self._inflight}
        try:
            await self._store.insert_lease(lease.to_json())
        except BaseException:
            for lease_id, at in parked.items():
                if lease_id in self._leases:
                    self._due.setdefault(lease_id, at)
            self._wakeup.set()
            raise
        for old in same_slot:
            if old.id in parked and old.id in self._leases and old.id not in self._inflight:
                await self._finish(old, "released", event="superseded", detail=f"replaced by lease {lease.id}")
        self._leases[lease.id] = lease
        self._due[lease.id] = lease.expires_at
        self._wakeup.set()
        await self._emit("granted", lease, f"until {lease.expires_at:.3f}")
        return copy.deepcopy(lease)

    async def release(self, lease_id: str, *, reason: str = "released on request") -> Lease:
        """Release now and wait for the attempt. A failed attempt leaves the
        lease active (retried with backoff) with ``last_error`` set. Releasing
        a lease that already ended returns it unchanged."""
        self._require_running()
        lease = self._leases.get(lease_id)
        if lease is None:
            row = await self._store.get_lease(lease_id)
            if row is None:
                raise NotFound(f"lease {lease_id} not found")
            return Lease.from_json(row)
        task = self._inflight.get(lease_id)
        if task is None:
            self._why[lease_id] = reason
            task = self._start_release(lease_id)
        await asyncio.wait([task])
        return copy.deepcopy(lease)

    async def extend(self, lease_id: str, seconds: float) -> Lease:
        """Push the expiry back by ``seconds`` (clamp it with
        ``Policy.clamp_lease`` first). Only a lease that has not expired and
        whose release was not requested can be extended."""
        self._require_running()
        _check_seconds(seconds)
        lease = self._leases.get(lease_id)
        if lease is None:
            if await self._store.get_lease(lease_id) is None:
                raise NotFound(f"lease {lease_id} not found")
            raise Conflict(f"lease {lease_id} is no longer active")
        # _why holds every lease whose release started, including one waiting for its retry.
        if lease_id in self._why or lease.expires_at <= self._clock():
            raise Conflict(f"lease {lease_id} has expired or is being released")
        before = lease.expires_at
        # In memory first, so the loop cannot start the release while the store write is pending.
        lease.expires_at = before + seconds
        self._due[lease_id] = lease.expires_at
        try:
            await self._store.update_lease(lease_id, expires_at=lease.expires_at)
        except BaseException:
            lease.expires_at = before
            if lease_id in self._leases and lease_id not in self._why:
                self._due[lease_id] = before
            raise
        self._wakeup.set()
        await self._emit("extended", lease, f"until {lease.expires_at:.3f}")
        return copy.deepcopy(lease)

    def get(self, lease_id: str) -> Lease | None:
        """An active lease, or None."""
        lease = self._leases.get(lease_id)
        return None if lease is None else copy.deepcopy(lease)

    def active(self, run_id: str | None = None) -> list[Lease]:
        """Active leases, soonest expiry first."""
        leases = [lease for lease in self._leases.values() if run_id is None or lease.run_id == run_id]
        return [copy.deepcopy(lease) for lease in sorted(leases, key=lambda lease: lease.expires_at)]

    # -- internals ----------------------------------------------------------------------------
    def _require_running(self) -> None:
        if self._task is None:
            raise RuntimeError("the lease manager is not running")

    async def _loop(self) -> None:
        while True:
            self._wakeup.clear()
            try:
                timeout = self._start_due()
            except Exception:
                # A dead loop would leave every value in place; keep going.
                logger.exception("lease expiry check failed; retrying in 1 s")
                timeout = 1.0
            try:
                async with asyncio.timeout(timeout):
                    await self._wakeup.wait()
            except TimeoutError:
                pass

    def _start_due(self) -> float | None:
        """Start the releases that are due; seconds until the next one (None: none pending)."""
        now = self._clock()
        next_at: float | None = None
        for lease_id, at in list(self._due.items()):
            if lease_id in self._inflight:
                continue
            if at <= now:
                self._start_release(lease_id)
            elif next_at is None or at < next_at:
                next_at = at
        return None if next_at is None else min(next_at - now, self._max_sleep_s)

    def _start_release(self, lease_id: str) -> asyncio.Task[None]:
        lease = self._leases[lease_id]
        reason = self._why.setdefault(lease_id, "expired")
        task = asyncio.create_task(self._attempt(lease, reason), name=f"lease-release-{lease_id}")
        self._inflight[lease_id] = task
        task.add_done_callback(lambda t: self._release_done(lease_id, t))
        return task

    def _release_done(self, lease_id: str, task: asyncio.Task[None]) -> None:
        if self._inflight.get(lease_id) is task:
            del self._inflight[lease_id]
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("lease %s: release task crashed", lease_id, exc_info=error)
        self._wakeup.set()

    async def _attempt(self, lease: Lease, reason: str) -> None:
        lease.attempts += 1
        try:
            async with asyncio.timeout(self._release_timeout_s):
                await self._releaser(copy.deepcopy(lease))
        except TimeoutError:
            await self._attempt_failed(lease, f"release timed out after {self._release_timeout_s:g} s")
            return
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # stop()
            # Raised inside the driver (its transport closed under the request): without
            # this the lease stays due and the loop restarts the release at once, forever.
            await self._attempt_failed(lease, "the release was cancelled by the driver")
            return
        except Exception as e:  # driver code: any failure is retried, never fatal to the loop
            await self._attempt_failed(lease, str(e) or type(e).__name__)
            return
        lease.last_error = None
        await self._finish(lease, "released", event="released", detail=reason)

    async def _attempt_failed(self, lease: Lease, error: str) -> None:
        lease.last_error = error
        if self._max_attempts is not None and lease.attempts >= self._max_attempts:
            logger.error("lease %s (%s %s): giving up after %d release attempts: %s",
                         lease.id, lease.kind, lease.target, lease.attempts, error)
            await self._finish(lease, "failed", event="failed",
                               detail=f"gave up after {lease.attempts} attempts: {error}")
            return
        delay = min(self._retry_max_s, self._retry_base_s * 2 ** min(lease.attempts - 1, 30))
        self._due[lease.id] = self._clock() + delay
        logger.warning("lease %s: release attempt %d failed (%s); retrying in %.3g s",
                       lease.id, lease.attempts, error, delay)
        try:
            await self._store.update_lease(lease.id, attempts=lease.attempts, last_error=error)
        except Exception:
            logger.exception("lease %s: could not record the failed attempt", lease.id)
        await self._emit("release_failed", lease, f"attempt {lease.attempts} failed: {error}; "
                                                  f"retrying in {delay:.3g} s")

    async def _finish(self, lease: Lease, state: LeaseState, *, event: str, detail: str) -> None:
        lease.state = state
        if state == "released":
            lease.released_at = self._clock()
        self._leases.pop(lease.id, None)
        self._due.pop(lease.id, None)
        self._why.pop(lease.id, None)
        try:
            await self._store.update_lease(lease.id, state=state, attempts=lease.attempts,
                                           released_at=lease.released_at, last_error=lease.last_error)
        except Exception:
            # Still active in the store: the next start releases it again (releases are idempotent).
            logger.exception("lease %s: could not record state %s", lease.id, state)
        await self._emit(event, lease, detail)

    async def _emit(self, event: str, lease: Lease, detail: str) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(event, copy.deepcopy(lease), detail)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("lease %s: %s handler failed", lease.id, event)


def _check_seconds(seconds: float) -> None:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) \
            or seconds <= 0:
        raise InvalidRequest(f"lease duration must be a positive number of seconds, got {seconds!r}")
