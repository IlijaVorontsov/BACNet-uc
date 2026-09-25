from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from uc_hub.core.errors import NotFound
from uc_hub.store import EventBus, Store


async def _run(store: Store) -> str:
    return (await store.create_run(title="t", created_by="dev"))["id"]


async def _take(sub: Any, n: int, wait_s: float = 5.0) -> list[dict[str, Any]]:
    async def collect() -> list[dict[str, Any]]:
        out = []
        async for event in sub:
            out.append(event)
            if len(out) == n:
                break
        return out

    return await asyncio.wait_for(collect(), wait_s)


async def test_append_numbers_persists_and_returns(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    first = await bus.append(rid, "run.state", state="running")
    second = await bus.append(rid, "message.delta", text="Hel")
    assert first == {"seq": 1, "run_id": rid, "ts": first["ts"], "type": "run.state", "state": "running"}
    assert second["seq"] == 2
    assert await store.list_events(rid) == [first, second]
    with pytest.raises(ValueError, match="reserved"):
        await bus.append(rid, "x", seq=5)
    with pytest.raises(NotFound):
        await bus.append("r_missing", "x")


async def test_replay_then_live(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    for i in range(5):
        await bus.append(rid, "message.delta", text=str(i))
    async with bus.subscribe(rid, after_seq=2) as sub:
        replayed = await _take(sub, 3)
        assert [e["seq"] for e in replayed] == [3, 4, 5]
        live = asyncio.create_task(_take(sub, 2))
        await asyncio.sleep(0.01)
        await bus.append(rid, "message.delta", text="5")
        await bus.append(rid, "message.done", text="done")
        events = await live
        assert [(e["seq"], e["type"]) for e in events] == [(6, "message.delta"), (7, "message.done")]
        assert sub.last_seq == 7
    assert bus.subscriber_count(rid) == 0


async def test_live_events_equal_replayed_ones(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    sub = bus.subscribe(rid)
    live = await bus.append(rid, "tool.result", call_id="c1", ok=True, data={"rows": (1, 2)}, summary="2 rows")
    assert (await _take(sub, 1))[0] == live == (await store.list_events(rid))[0]
    assert live["data"] == {"rows": [1, 2]}
    await sub.aclose()


async def test_no_gap_or_duplicate_under_concurrent_appends(store: Store) -> None:
    """Subscribers join at random points while producers append; each must see
    exactly seq after_seq+1 .. N once, in order."""
    bus = EventBus(store, page_size=7)
    rid = await _run(store)
    total = 300
    for i in range(40):
        await bus.append(rid, "message.delta", text=f"pre{i}")
    rng = random.Random(1)

    async def producer(k: int) -> None:
        for i in range(65):
            await bus.append(rid, "message.delta", text=f"{k}:{i}")
            if rng.random() < 0.3:
                await asyncio.sleep(0)

    async def consumer(after: int) -> list[int]:
        await asyncio.sleep(rng.random() * 0.01)
        async with bus.subscribe(rid, after_seq=after) as sub:
            return [e["seq"] for e in await _take(sub, total - after, wait_s=20)]

    afters = [0, 5, 17, 39, 40]
    results = await asyncio.gather(*(consumer(a) for a in afters), *(producer(k) for k in range(4)))
    for after, seqs in zip(afters, results[: len(afters)], strict=True):
        assert seqs == list(range(after + 1, total + 1)), f"after={after}"


async def test_slow_subscriber_overflows_and_recovers(store: Store) -> None:
    bus = EventBus(store, queue_size=4, page_size=3)
    rid = await _run(store)
    sub = bus.subscribe(rid)
    fast = bus.subscribe(rid)
    fast_seen: list[int] = []

    async def fast_reader() -> None:
        async for event in fast:
            fast_seen.append(event["seq"])
            if event["seq"] == 50:
                return

    reader = asyncio.create_task(fast_reader())
    # The slow subscriber does not read while 50 events are appended.
    for i in range(50):
        await bus.append(rid, "message.delta", text=str(i))
    await asyncio.wait_for(reader, 5)
    assert fast_seen == list(range(1, 51))
    assert sub.overflows >= 1
    got = [e["seq"] for e in await _take(sub, 50)]
    assert got == list(range(1, 51))
    # And it keeps working live afterwards.
    nxt = asyncio.create_task(_take(sub, 1))
    await asyncio.sleep(0.01)
    await bus.append(rid, "message.done", text="x")
    assert [e["seq"] for e in await nxt] == [51]
    await sub.aclose()
    await fast.aclose()


async def test_events_appended_behind_the_bus_are_recovered(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    sub = bus.subscribe(rid)
    await bus.append(rid, "a")
    assert [e["seq"] for e in await _take(sub, 1)] == [1]
    await store.append_event(rid, "b", {})  # persisted without fan-out
    await bus.append(rid, "c")
    assert [(e["seq"], e["type"]) for e in await _take(sub, 2)] == [(2, "b"), (3, "c")]
    await sub.aclose()


async def test_append_completes_when_caller_is_cancelled(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    sub = bus.subscribe(rid)
    task = asyncio.create_task(bus.append(rid, "x"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [e["seq"] for e in await _take(sub, 1)] == [1]
    await sub.aclose()


async def test_close_ends_subscriptions(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    sub = bus.subscribe(rid)
    waiter = asyncio.create_task(_take(sub, 1))
    await asyncio.sleep(0.01)
    bus.close()
    assert await waiter == []
    late = bus.subscribe(rid)
    assert await _take(late, 1) == []
    with pytest.raises(RuntimeError):
        await bus.append(rid, "x")
    assert bus.subscriber_count() == 0


async def test_dropped_subscriptions_are_forgotten(store: Store) -> None:
    bus = EventBus(store)
    rid = await _run(store)
    bus.subscribe(rid)
    kept = bus.subscribe(rid)
    import gc

    gc.collect()
    assert bus.subscriber_count(rid) == 1
    await bus.append(rid, "x")
    assert [e["seq"] for e in await _take(kept, 1)] == [1]
    await kept.aclose()
    assert bus.subscriber_count() == 0


async def test_untrusted_values_still_make_valid_json(store: Store) -> None:
    """Event text goes to browsers as it is: a NaN reading must not make it
    invalid JSON, and a lone surrogate (json.loads accepts "\\udcff" from an
    MQTT payload) must not make SQLite reject the event."""
    import json

    bus = EventBus(store)
    rid = await _run(store)
    name = json.loads('"R204 \\udcff"')
    live = await bus.append(rid, "tool.result", call_id="c1", ok=True, summary=name,
                            data={"readings": [{"value": float("nan")}, {"value": float("-inf")}, {"value": 1.5}]})
    assert live["data"]["readings"] == [{"value": None}, {"value": None}, {"value": 1.5}]
    assert live["summary"] == name
    assert (await store.list_events(rid)) == [live]
    json.dumps(live, allow_nan=False)
    handle = await store.put_result({"name": name, "v": float("nan")}, run_id=rid)
    assert await store.get_result(handle) == {"name": name, "v": None}
