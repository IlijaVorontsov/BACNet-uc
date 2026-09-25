from __future__ import annotations

import asyncio

import pytest

from uc_hub.core.ids import PointRef
from uc_hub.core.types import Quality, Reading
from uc_hub.store import LiveHub, LiveSubscription

T1 = PointRef("hq", "r204-ctl", "analog-input:1")
T2 = PointRef("hq", "r204-ctl", "analog-output:1")
CO2 = PointRef("hq", "r204-co2", "co2")


def reading(ref: PointRef, value: float, ts: float) -> Reading:
    return Reading(ref, value, ts=ts)


async def drain(sub: LiveSubscription, n: int) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []

    async def collect() -> None:
        async for r in sub:
            out.append((str(r.ref), r.value))  # type: ignore[arg-type]
            if len(out) == n:
                return

    await asyncio.wait_for(collect(), 2)
    return out


async def test_filters_by_point_and_device() -> None:
    hub = LiveHub(site="hq")
    by_point = hub.subscribe(points=["r204-ctl/analog-input:1"])
    by_device = hub.subscribe(devices=["r204-co2"])
    both = hub.subscribe(points=[T2], devices=["r204-co2"])
    everything = hub.subscribe()
    nothing = hub.subscribe(points=[])
    hub.publish(reading(T1, 21.0, 1))
    hub.publish(reading(T2, 40.0, 1))
    hub.publish(reading(CO2, 650.0, 1))
    assert await drain(by_point, 1) == [(str(T1), 21.0)]
    assert await drain(by_device, 1) == [(str(CO2), 650.0)]
    assert await drain(both, 2) == [(str(T2), 40.0), (str(CO2), 650.0)]
    assert [v for _, v in await drain(everything, 3)] == [21.0, 40.0, 650.0]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(nothing), 0.05)
    assert hub.subscriber_count() == 5
    for sub in (by_point, by_device, both, everything, nothing):
        await sub.aclose()
    assert hub.subscriber_count() == 0


async def test_latest_cache_and_initial_values() -> None:
    hub = LiveHub(site="hq")
    hub.publish(reading(T1, 21.0, 10))
    hub.publish(reading(CO2, 600.0, 10))
    hub.publish(reading(T1, 21.5, 11))
    hub.publish(reading(T1, 19.0, 5))  # an older poll result is ignored
    latest = hub.latest("r204-ctl/analog-input:1")
    assert latest is not None and latest.value == 21.5
    assert hub.latest(CO2) is not None and hub.latest(T2) is None
    assert [r.value for r in hub.snapshot()] == [600.0, 21.5]
    assert [r.value for r in hub.snapshot(devices=["r204-ctl"])] == [21.5]

    async with hub.subscribe(points=[T1, CO2]) as sub:
        assert await drain(sub, 2) == [(str(CO2), 600.0), (str(T1), 21.5)]
        hub.publish(Reading(T1, None, ts=12, quality=Quality.OFFLINE, error="timeout"))
        r = await asyncio.wait_for(anext(sub), 1)
        assert r.quality is Quality.OFFLINE
    without = hub.subscribe(points=[T1], initial=False)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(without), 0.05)
    await without.aclose()

    hub.forget_device("r204-ctl")
    assert hub.latest(T1) is None and hub.latest(CO2) is not None
    with pytest.raises(ValueError):
        hub.latest("not a point")


async def test_slow_subscriber_drops_oldest() -> None:
    hub = LiveHub(queue_size=3)
    sub = hub.subscribe(points=[T1])
    other = hub.subscribe(points=[T1], queue_size=100)
    for i in range(10):
        hub.publish(reading(T1, float(i), i))
    assert sub.dropped == 7
    assert [v for _, v in await drain(sub, 3)] == [7.0, 8.0, 9.0]
    assert [v for _, v in await drain(other, 10)] == [float(i) for i in range(10)]
    assert other.dropped == 0
    hub.publish(reading(T1, 10.0, 10))
    assert await drain(sub, 1) == [(str(T1), 10.0)]


async def test_close_ends_live_subscriptions() -> None:
    hub = LiveHub()
    sub = hub.subscribe()
    waiter = asyncio.create_task(drain(sub, 1))
    await asyncio.sleep(0.01)
    hub.close()
    assert await waiter == []
    assert hub.subscriber_count() == 0
    with pytest.raises(StopAsyncIteration):
        await anext(hub.subscribe())


async def test_wall_clock_step_back_does_not_freeze_values() -> None:
    """Drivers stamp readings with the hub's wall clock. After it steps back
    (NTP), newer readings carry older stamps; dropping them all would freeze
    the cache, and hide an offline device, until the clock catches up."""
    hub = LiveHub()
    sub = hub.subscribe(points=[T1], initial=False)
    hub.publish(reading(T1, 1.0, 10_000))
    hub.publish(reading(T1, 2.0, 9_995))  # a poll answer that arrived after a newer COV value
    latest = hub.latest(T1)
    assert latest is not None and latest.value == 1.0
    hub.publish(Reading(T1, None, ts=6_400, quality=Quality.OFFLINE, error="timeout"))  # clock stepped back 1 h
    latest = hub.latest(T1)
    assert latest is not None and latest.quality is Quality.OFFLINE
    assert await drain(sub, 2) == [(str(T1), 1.0), (str(T1), None)]  # type: ignore[list-item]
    await sub.aclose()
