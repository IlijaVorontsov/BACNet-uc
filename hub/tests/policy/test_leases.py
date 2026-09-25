from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from uc_hub.core.errors import InvalidRequest, NotFound
from uc_hub.policy import Lease, LeaseManager
from uc_hub.store import Conflict, Store

POINT = {"point": "hq/r204-ctl/analog-output:1"}


class Releaser:
    """Records calls; fails the first ``failures`` calls, or hangs when asked."""

    def __init__(self, failures: int = 0, hang: bool = False) -> None:
        self.calls: list[tuple[str, float]] = []
        self.failures = failures
        self.hang = hang
        self.event = asyncio.Event()

    async def __call__(self, lease: Lease) -> None:
        self.calls.append((lease.id, time.monotonic()))
        self.event.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("device unreachable")

    def ids(self) -> list[str]:
        return [lid for lid, _ in self.calls]

    async def wait_calls(self, n: int, wait_s: float = 5.0) -> None:
        async def until() -> None:
            while len(self.calls) < n:
                self.event.clear()
                await self.event.wait()

        await asyncio.wait_for(until(), wait_s)


class Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, str]] = []

    async def __call__(self, event: str, lease: Lease, detail: str) -> None:
        self.items.append((event, lease.id, detail))

    def kinds(self, lease_id: str | None = None) -> list[str]:
        return [e for e, lid, _ in self.items if lease_id is None or lid == lease_id]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_790_290_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def releaser() -> Releaser:
    return Releaser()


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
async def make(store: Store, events: Events) -> AsyncIterator[Callable[..., Awaitable[LeaseManager]]]:
    managers: list[LeaseManager] = []

    async def factory(rel: Releaser, **kw: Any) -> LeaseManager:
        kw.setdefault("on_event", events)
        kw.setdefault("retry_base_s", 0.01)
        kw.setdefault("retry_max_s", 0.05)
        m = LeaseManager(store, rel, **kw)
        managers.append(m)
        await m.start()
        return m

    yield factory
    for m in managers:
        await m.stop()


async def _wait_until(cond: Callable[[], bool], wait_s: float = 5.0) -> None:
    deadline = time.monotonic() + wait_s
    while not cond():
        if time.monotonic() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.005)


async def test_expiry_calls_the_releaser_on_time(store: Store, make: Any, releaser: Releaser, events: Events) -> None:
    m = await make(releaser)
    t0 = time.monotonic()
    long = await m.grant("write", POINT | {"n": 1}, 12, 30, "dev", run_id="r_1")
    short = await m.grant("write", POINT, 12, 0.1, "dev", run_id="r_1")
    row = await store.get_lease(short.id)
    assert row is not None and row["state"] == "active" and row["target"] == POINT and row["priority"] == 12
    await releaser.wait_calls(1)
    elapsed = releaser.calls[0][1] - t0
    assert releaser.ids() == [short.id]
    assert 0.09 <= elapsed < 0.4, elapsed
    await _wait_until(lambda: m.get(short.id) is None)
    row = await store.get_lease(short.id)
    assert row is not None and row["state"] == "released" and row["attempts"] == 1
    assert row["released_at"] is not None and row["last_error"] is None
    assert [lease.id for lease in m.active()] == [long.id]
    assert events.kinds(short.id) == ["granted", "released"]
    assert events.items[-1][2] == "expired"


async def test_fake_clock_wakeups(store: Store, make: Any, releaser: Releaser) -> None:
    clock = FakeClock()
    m = await make(releaser, clock=clock)
    lease = await m.grant("force", {"node": "r204-ctl", "channel": "ai0"}, None, 300, "tech1")
    assert lease.expires_at == clock.now + 300
    clock.now += 299
    m.wake()
    await asyncio.sleep(0.02)
    assert releaser.calls == []
    clock.now += 1
    m.wake()
    await releaser.wait_calls(1)
    await _wait_until(lambda: not m.active())
    row = await store.get_lease(lease.id)
    assert row is not None and row["state"] == "released" and row["released_at"] == clock.now


async def test_stale_leases_are_released_at_start(store: Store, make: Any, releaser: Releaser,
                                                  events: Events) -> None:
    far = time.time() + 3600
    for lid in ("l_a", "l_b"):
        await store.insert_lease({"id": lid, "kind": "write", "target": POINT | {"id": lid}, "priority": 12,
                                  "expires_at": far, "created_by": "dev", "run_id": None, "created_at": 1.0})
    await store.insert_lease({"id": "l_done", "kind": "write", "target": POINT, "priority": 12, "expires_at": 1.0,
                              "created_by": "dev", "run_id": None, "created_at": 1.0, "state": "released"})
    m = await make(releaser)
    # start() returns after the first attempt of each stale lease.
    assert sorted(releaser.ids()) == ["l_a", "l_b"]
    assert m.active() == []
    for lid in ("l_a", "l_b"):
        row = await store.get_lease(lid)
        assert row is not None and row["state"] == "released"
    assert all(e == "released" and "hub stopped" in d for e, _, d in events.items)


async def test_failed_release_is_retried_with_backoff(store: Store, make: Any, events: Events) -> None:
    rel = Releaser(failures=2)
    m = await make(rel)
    lease = await m.grant("write", POINT, 12, 0.02, "dev")
    await rel.wait_calls(3)
    await _wait_until(lambda: m.get(lease.id) is None)
    row = await store.get_lease(lease.id)
    assert row is not None
    assert (row["state"], row["attempts"], row["last_error"]) == ("released", 3, None)
    assert events.kinds(lease.id) == ["granted", "release_failed", "release_failed", "released"]
    failed = [d for e, _, d in events.items if e == "release_failed"]
    assert "attempt 1 failed: device unreachable" in failed[0]
    # Backoff doubles: the gap before the third attempt is longer than before the second.
    t = [ts for _, ts in rel.calls]
    assert t[2] - t[1] >= 0.02 - 0.005 and t[1] - t[0] >= 0.01 - 0.005


async def test_release_gives_up_after_max_attempts(store: Store, make: Any, events: Events) -> None:
    rel = Releaser(failures=100)
    m = await make(rel, max_attempts=3)
    lease = await m.grant("write", POINT, 12, 0.01, "dev")
    await _wait_until(lambda: "failed" in events.kinds(lease.id))
    row = await store.get_lease(lease.id)
    assert row is not None
    assert (row["state"], row["attempts"], row["last_error"]) == ("failed", 3, "device unreachable")
    await asyncio.sleep(0.1)
    assert len(rel.calls) == 3
    assert m.active() == []


async def test_hanging_releaser_times_out(store: Store, make: Any, events: Events) -> None:
    rel = Releaser(hang=True)
    m = await make(rel, release_timeout_s=0.05, max_attempts=2)
    lease = await m.grant("write", POINT, 12, 0.01, "dev")
    await _wait_until(lambda: "failed" in events.kinds(lease.id))
    row = await store.get_lease(lease.id)
    assert row is not None and "timed out" in row["last_error"]


async def test_explicit_release(store: Store, make: Any, releaser: Releaser, events: Events) -> None:
    m = await make(releaser)
    lease = await m.grant("write", POINT, 12, 60, "dev")
    released = await m.release(lease.id, reason="run finished")
    assert released.state == "released" and released.attempts == 1
    assert releaser.ids() == [lease.id]
    assert events.items[-1] == ("released", lease.id, "run finished")
    again = await m.release(lease.id)
    assert again.state == "released" and len(releaser.calls) == 1
    with pytest.raises(NotFound):
        await m.release("l_missing")

    releaser.failures = 1
    other = await m.grant("write", POINT | {"n": 2}, 12, 60, "dev")
    result = await m.release(other.id)
    assert result.state == "active" and result.last_error == "device unreachable"
    await _wait_until(lambda: m.get(other.id) is None)  # the retry succeeds
    row = await store.get_lease(other.id)
    assert row is not None and row["state"] == "released" and row["attempts"] == 2


async def test_extend(store: Store, make: Any, releaser: Releaser) -> None:
    m = await make(releaser)
    lease = await m.grant("write", POINT, 12, 0.1, "dev")
    extended = await m.extend(lease.id, 30)
    assert extended.expires_at == pytest.approx(lease.expires_at + 30)
    row = await store.get_lease(lease.id)
    assert row is not None and row["expires_at"] == pytest.approx(extended.expires_at)
    await asyncio.sleep(0.2)
    assert releaser.calls == []
    for bad in (0, -1, float("nan"), float("inf"), True):
        with pytest.raises(InvalidRequest):
            await m.extend(lease.id, bad)
    await m.release(lease.id)
    with pytest.raises(Conflict):
        await m.extend(lease.id, 10)
    with pytest.raises(NotFound):
        await m.extend("l_missing", 10)


async def test_new_lease_on_the_same_slot_replaces_the_old(store: Store, make: Any, releaser: Releaser,
                                                           events: Events) -> None:
    m = await make(releaser)
    first = await m.grant("write", POINT, 12, 0.05, "dev")
    second = await m.grant("write", POINT, 12, 30, "dev")
    other_priority = await m.grant("write", POINT, 13, 30, "dev")
    assert {lease.id for lease in m.active()} == {second.id, other_priority.id}
    row = await store.get_lease(first.id)
    assert row is not None and row["state"] == "released"
    assert events.kinds(first.id) == ["granted", "superseded"]
    await asyncio.sleep(0.1)
    assert releaser.calls == []  # the old lease's expiry must not clear the new write


async def test_stop_cancels_and_next_start_releases(store: Store, events: Events) -> None:
    hanging = Releaser(hang=True)
    m = LeaseManager(store, hanging, on_event=events)
    await m.start()
    lease = await m.grant("write", POINT, 12, 0.01, "dev")
    await hanging.wait_calls(1)
    await m.stop()
    assert not m.running
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not [t for t in others if (t.get_name() or "").startswith("lease-")]
    row = await store.get_lease(lease.id)
    assert row is not None and row["state"] == "active"
    await m.stop()  # idempotent

    rel = Releaser()
    m2 = LeaseManager(store, rel)
    await m2.start()
    assert rel.ids() == [lease.id]
    await m2.stop()


async def test_argument_checks(store: Store, make: Any, releaser: Releaser) -> None:
    idle = LeaseManager(store, releaser)
    with pytest.raises(RuntimeError, match="not running"):
        await idle.grant("write", POINT, 12, 10, "dev")
    m = await make(releaser)
    with pytest.raises(InvalidRequest):
        await m.grant("hold", POINT, 12, 10, "dev")  # type: ignore[arg-type]
    for bad in (0, -3, float("nan"), float("inf")):
        with pytest.raises(InvalidRequest):
            await m.grant("write", POINT, 12, bad, "dev")
    assert await store.list_leases() == []


async def test_event_handler_errors_do_not_break_leases(store: Store, make: Any, releaser: Releaser) -> None:
    def broken(event: str, lease: Lease, detail: str) -> None:
        raise RuntimeError("audit down")

    m = await make(releaser, on_event=broken)
    lease = await m.grant("write", POINT, 12, 0.01, "dev")
    await _wait_until(lambda: m.get(lease.id) is None)
    assert releaser.ids() == [lease.id]


async def test_active_is_filtered_and_sorted(make: Any, releaser: Releaser) -> None:
    m = await make(releaser)
    a = await m.grant("write", POINT | {"n": 1}, 12, 50, "dev", run_id="r_1")
    b = await m.grant("write", POINT | {"n": 2}, 12, 20, "dev", run_id="r_2")
    c = await m.grant("force", {"node": "n", "channel": "do0"}, None, 30, "dev", run_id="r_1")
    assert [lease.id for lease in m.active()] == [b.id, c.id, a.id]
    assert [lease.id for lease in m.active("r_1")] == [c.id, a.id]
    snapshot = m.get(a.id)
    assert snapshot is not None
    snapshot.target["n"] = 99
    assert m.get(a.id).target["n"] == 1  # type: ignore[union-attr]
    assert Lease.from_json(a.to_json()) == a


async def test_extend_does_not_cancel_a_requested_release(store: Store, make: Any, events: Events) -> None:
    rel = Releaser(failures=1)
    m = await make(rel, retry_base_s=0.05, retry_max_s=0.05)
    lease = await m.grant("write", POINT, 12, 60, "dev")
    result = await m.release(lease.id, reason="run finished")
    assert result.state == "active" and result.last_error == "device unreachable"
    with pytest.raises(Conflict):
        await m.extend(lease.id, 30)
    await _wait_until(lambda: m.get(lease.id) is None, wait_s=2)  # the retry still happens soon
    row = await store.get_lease(lease.id)
    assert row is not None and row["state"] == "released" and row["attempts"] == 2


async def test_releaser_raising_cancelled_error_is_a_failed_attempt(store: Store, make: Any,
                                                                    events: Events) -> None:
    """A driver whose transport closes under a request may raise CancelledError
    without the manager being cancelled; that must back off, not spin."""

    class Cancelling(Releaser):
        async def __call__(self, lease: Lease) -> None:
            await super().__call__(lease)
            raise asyncio.CancelledError

    rel = Cancelling()
    m = await make(rel, max_attempts=3)
    lease = await m.grant("write", POINT, 12, 0.01, "dev")
    await _wait_until(lambda: "failed" in events.kinds(lease.id), wait_s=2)
    await asyncio.sleep(0.05)
    assert len(rel.calls) == 3
    row = await store.get_lease(lease.id)
    assert row is not None and (row["state"], row["attempts"]) == ("failed", 3)
    assert events.kinds(lease.id) == ["granted", "release_failed", "release_failed", "failed"]


async def test_concurrent_grants_on_one_slot_leave_one_lease(store: Store, make: Any, releaser: Releaser,
                                                             events: Events) -> None:
    m = await make(releaser)
    leases = await asyncio.gather(*(m.grant("write", POINT, 12, 30, "dev") for _ in range(3)))
    assert [lease.id for lease in m.active()] == [leases[-1].id]
    assert [row["id"] for row in await store.list_leases(state="active")] == [leases[-1].id]
    assert [e for e, _, _ in events.items].count("superseded") == 2
    assert releaser.calls == []


async def test_old_lease_retry_cannot_start_while_its_slot_is_regranted(store: Store, events: Events) -> None:
    """A failed release waiting for its retry must not run while grant stores
    the lease that takes over the slot: it would clear the new write."""
    rel = Releaser(failures=1)
    clock = FakeClock()
    m = LeaseManager(store, rel, clock=clock, on_event=events, retry_base_s=1.0)
    await m.start()
    try:
        old = await m.grant("write", POINT, 12, 10, "dev")
        clock.now += 10
        m.wake()
        await _wait_until(lambda: "release_failed" in events.kinds(old.id))
        insert = store.insert_lease

        async def slow_insert(row: Any) -> None:
            clock.now += 5  # the old lease's retry falls due while the new lease is stored
            m.wake()
            await asyncio.sleep(0.02)
            await insert(row)

        store.insert_lease = slow_insert  # type: ignore[method-assign]
        new = await m.grant("write", POINT, 12, 60, "dev")
        await asyncio.sleep(0.02)
        assert len(rel.calls) == 1
        assert [lease.id for lease in m.active()] == [new.id]
        assert events.kinds(old.id)[-1] == "superseded"
    finally:
        await m.stop()
