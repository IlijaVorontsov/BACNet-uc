"""The acceptance test runner."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from uc_hub.core.errors import DeviceError, DeviceTimeout, InvalidRequest
from uc_hub.core.ids import PointRef
from uc_hub.core.types import Value
from uc_hub.manifest import compare, run_tests

from .fakes import FakeClock, FakeTarget

COLD = {"name": "valve opens when cold", "steps": [
    {"force": {"node": "r204-ctl", "channel": "ai0", "value": 650}},
    {"expect": {"point": "r204-ctl/analog-input:1", "op": "approx", "value": 15.0, "tolerance": 0.001}},
    {"expect": {"point": "r204-ctl/analog-output:1", "op": "gt", "value": 50, "within_ms": 5000}},
    {"release": {"node": "r204-ctl", "channel": "ai0"}},
]}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def target(clock: FakeClock) -> FakeTarget:
    return FakeTarget(clock)


async def run(tests: list[dict[str, Any]], target: FakeTarget, clock: FakeClock, **kwargs: Any) -> list[Any]:
    return await run_tests(tests, target, "hq", clock=clock, sleep=clock.sleep, **kwargs)


def released(target: FakeTarget) -> list[tuple[Any, ...]]:
    return [e for e in target.log if e[0] == "force" and e[3] is None]


async def test_pass(target: FakeTarget, clock: FakeClock) -> None:
    (result,) = await run([COLD], target, clock)
    assert result.to_json() == {"name": "valve opens when cold", "status": "pass", "duration_ms": 1000,
                                "failed_step": None, "detail": "", "target": "live"}
    assert clock.sleeps == [0.2] * 5
    assert target.forces == {}
    assert released(target) == [("force", "r204-ctl", "ai0", None)]


async def test_fail_after_within_ms_releases_forces(target: FakeTarget, clock: FakeClock) -> None:
    target.valve_delay_s = 30
    (result,) = await run([COLD], target, clock, target_kind="sim")
    assert result.status == "fail" and result.failed_step == 2 and result.target == "sim"
    assert result.detail == "step 2 expect r204-ctl/analog-output:1 gt 50: got 0.0 after 5000 ms"
    assert result.duration_ms == 5000
    assert released(target) == [("force", "r204-ctl", "ai0", None)]
    assert target.forces == {}


async def test_prioritised_writes_are_relinquished(target: FakeTarget, clock: FakeClock) -> None:
    test = {"name": "setpoint", "steps": [
        {"write": {"point": "r204-ctl/analog-value:1", "value": 23.0, "priority": 12}},
        {"write": {"point": "hq/r204-ctl/analog-value:2", "property": "relinquish-default", "value": 5}},
        {"write": {"point": "r204-ctl/binary-output:1", "value": True, "priority": 14}},
        {"write": {"point": "r204-ctl/binary-output:1", "value": None, "priority": 14}},
        {"expect": {"point": "r204-ctl/analog-value:1", "op": "eq", "value": 22}},
    ]}
    (result,) = await run([test], target, clock)
    assert result.status == "fail" and result.failed_step == 4
    assert result.detail == "step 4 expect r204-ctl/analog-value:1 eq 22: got 23.0"
    writes = [e for e in target.log if e[0] == "write"]
    assert writes[-1] == ("write", "hq/r204-ctl/analog-value:1", None, "present-value", 12)
    assert writes.count(("write", "hq/r204-ctl/binary-output:1", None, "present-value", 14)) == 1
    assert target.priorities == {}


async def test_wait_and_property_reads(target: FakeTarget, clock: FakeClock) -> None:
    test = {"name": "wait", "steps": [
        {"wait": 1500},
        {"write": {"point": "r204-ctl/analog-value:5", "value": 4}},
        {"expect": {"point": "r204-ctl/analog-value:5", "property": "present-value", "op": "le", "value": 4}},
        {"expect": {"point": "r204-ctl/analog-value:6", "property": "description", "op": "ge", "value": 0}},
    ]}
    (result,) = await run([test], target, clock)
    assert result.status == "pass" and result.duration_ms == 1500
    assert clock.sleeps == [1.5]
    assert ("read", "hq/r204-ctl/analog-value:6", "description") in target.log


async def test_read_errors(target: FakeTarget, clock: FakeClock) -> None:
    target.fail_read = DeviceTimeout("r204-ctl: no answer")
    (result,) = await run([COLD], target, clock)
    assert result.status == "error" and result.failed_step == 1
    assert result.detail == ("step 1 expect r204-ctl/analog-input:1 approx 15.0: read failed: "
                             "DeviceTimeout: r204-ctl: no answer")
    assert target.forces == {}


async def test_step_errors(target: FakeTarget, clock: FakeClock) -> None:
    tests = [
        {"name": "malformed", "steps": [{"force": {"node": "r204-ctl"}}]},
        {"name": "unknown", "steps": [{"pause": 1}]},
        {"name": "two keys", "steps": [{"wait": 1, "force": {}}]},
        {"name": "bad point", "steps": [{"expect": {"point": "nope", "op": "eq", "value": 1}}]},
        {"name": "bad op", "steps": [{"expect": {"point": "r204-ctl/analog-value:1", "op": "near", "value": 1}}]},
    ]
    results = await run(tests, target, clock)
    assert [(r.status, r.failed_step) for r in results] == [("error", 0)] * 5
    assert [r.detail for r in results] == [
        "step 0 force: malformed step (KeyError: 'channel')",
        "step 0 pause: unknown step",
        "step 0: a step is a mapping with exactly one key",
        "step 0 expect: not a point id: 'nope' (expected site/device/object)",
        "step 0 expect: ValueError: unknown op 'near'",
    ]


async def test_cleanup_failure_is_an_error(target: FakeTarget, clock: FakeClock) -> None:
    target.fail_force_release = DeviceError("channel busy", rc=5, group=65)
    test = {"name": "stuck", "steps": COLD["steps"][:3]}
    (result,) = await run([test], target, clock)
    assert result.status == "error"
    assert result.failed_step is None
    assert result.detail == "cleanup failed: release r204-ctl/ai0: DeviceError: channel busy"


async def test_cancellation_still_cleans_up(target: FakeTarget, clock: FakeClock) -> None:
    started = asyncio.Event()
    original = target.read

    async def blocking_read(point: PointRef, prop: str = "present-value") -> Value:
        if point.obj == "analog-output:1":
            started.set()
            await asyncio.Event().wait()
        return await original(point, prop)

    target.read = blocking_read  # type: ignore[method-assign]
    test = {"name": "cancel", "steps": [
        {"write": {"point": "r204-ctl/analog-value:1", "value": 23.0, "priority": 12}},
        *COLD["steps"],
    ]}
    task = asyncio.create_task(run([test], target, clock))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.forces == {} and target.priorities == {}
    assert target.log[-2:] == [("force", "r204-ctl", "ai0", None),
                               ("write", "hq/r204-ctl/analog-value:1", None, "present-value", 12)]


async def test_lost_answers_are_still_undone(target: FakeTarget, clock: FakeClock) -> None:
    """The node applied the write and the force, but the answers were lost."""
    write, force = target.write, target.force

    async def lossy_write(point: PointRef, value: Value, prop: str = "present-value",
                          priority: int | None = None) -> None:
        await write(point, value, prop, priority)
        if value is not None:
            raise DeviceTimeout("answer lost")

    async def lossy_force(node: str, channel: str, value: float | None) -> None:
        await force(node, channel, value)
        if value is not None:
            raise TimeoutError

    target.write = lossy_write  # type: ignore[method-assign]
    target.force = lossy_force  # type: ignore[method-assign]
    results = await run([
        {"name": "write", "steps": [{"write": {"point": "r204-ctl/analog-value:1", "value": 23, "priority": 12}}]},
        {"name": "force", "steps": [{"force": {"node": "r204-ctl", "channel": "ai0", "value": 650}}]},
    ], target, clock)
    assert [(r.status, r.detail) for r in results] == [
        ("error", "step 0 write: DeviceTimeout: answer lost"), ("error", "step 0 force: TimeoutError: timed out")]
    assert target.priorities == {} and target.forces == {}


async def test_refused_steps_are_not_undone(target: FakeTarget, clock: FakeClock) -> None:
    force = target.force

    async def refusing_force(node: str, channel: str, value: float | None) -> None:
        if value == 1:
            raise DeviceError("channel busy", rc=5, group=65)
        await force(node, channel, value)

    target.force = refusing_force  # type: ignore[method-assign]
    test = {"name": "refused", "steps": [
        {"force": {"node": "r204-ctl", "channel": "ai0", "value": 650}},
        {"force": {"node": "r204-ctl", "channel": "ai0", "value": 1}},
    ]}
    refused = {"name": "refused alone", "steps": [{"force": {"node": "r204-ctl", "channel": "di0", "value": 1}}]}
    results = await run([test, refused], target, clock)
    assert [(r.status, r.detail) for r in results] == [
        ("error", "step 1 force: DeviceError: channel busy"), ("error", "step 0 force: DeviceError: channel busy")]
    assert released(target) == [("force", "r204-ctl", "ai0", None)]
    assert target.forces == {}


async def test_cancelled_write_is_relinquished(target: FakeTarget, clock: FakeClock) -> None:
    started = asyncio.Event()
    write = target.write

    async def hanging_write(point: PointRef, value: Value, prop: str = "present-value",
                            priority: int | None = None) -> None:
        await write(point, value, prop, priority)
        if value is not None:
            started.set()
            await asyncio.Event().wait()

    target.write = hanging_write  # type: ignore[method-assign]
    test = {"name": "cancel", "steps": [
        {"write": {"point": "r204-ctl/analog-value:1", "value": 23.0, "priority": 12}}]}
    task = asyncio.create_task(run([test], target, clock))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.priorities == {}


async def test_hung_cleanup_is_bounded(target: FakeTarget, clock: FakeClock) -> None:
    force = target.force

    async def release_hangs(node: str, channel: str, value: float | None) -> None:
        if value is None:
            await asyncio.sleep(10)
        await force(node, channel, value)

    target.force = release_hangs  # type: ignore[method-assign]
    test = {"name": "stuck", "steps": COLD["steps"][:1]}
    (result,) = await asyncio.wait_for(run([test], target, clock, cleanup_timeout_s=0.05), 5)
    assert (result.status, result.detail) == ("error", "cleanup failed: release r204-ctl/ai0: TimeoutError: timed out")


async def test_test_timeout(target: FakeTarget, clock: FakeClock) -> None:
    async def hanging_read(point: PointRef, prop: str = "present-value") -> Value:
        await asyncio.sleep(10)
        return 0

    target.read = hanging_read  # type: ignore[method-assign]
    (result,) = await run([COLD], target, clock, test_timeout_s=0.05)
    assert result.status == "error" and result.failed_step == 1
    assert result.detail == "step 1: the test did not finish within 0.05 s"
    assert target.forces == {}


async def test_selection_by_name(target: FakeTarget, clock: FakeClock) -> None:
    other = {"name": "noop", "steps": []}
    results = await run([COLD, other], target, clock, names=["noop"])
    assert [(r.name, r.status, r.duration_ms) for r in results] == [("noop", "pass", 0)]
    with pytest.raises(InvalidRequest, match="unknown test: missing"):
        await run([COLD], target, clock, names=["missing"])


async def test_several_tests_run_in_order(target: FakeTarget, clock: FakeClock) -> None:
    target.valve_delay_s = 0
    warm = {"name": "warm", "steps": [
        {"force": {"node": "r204-ctl", "channel": "ai0", "value": 780}},
        {"expect": {"point": "r204-ctl/analog-output:1", "op": "lt", "value": 5}},
    ]}
    results = await run([COLD, warm], target, clock)
    assert [(r.name, r.status) for r in results] == [("valve opens when cold", "pass"), ("warm", "pass")]
    assert target.forces == {}


@pytest.mark.parametrize(("actual", "op", "expected", "result"), [
    (1, "eq", 1.0, True), (1, "eq", True, True), (0, "eq", False, True), (True, "ne", 0, True),
    ("on", "eq", "on", True), ("on", "ne", "off", True), (None, "eq", 0, False), (None, "ne", 0, True),
    (21.4, "gt", 21, True), (21, "gt", 21, False), (21, "ge", 21, True), (20.9, "lt", 21, True),
    (21, "le", 21, True), ("21", "gt", 1, False), (None, "lt", 1, False),
    (21.004, "approx", 21, True), (21.02, "approx", 21, False), (float("nan"), "approx", 1, False),
])
def test_compare(actual: Any, op: str, expected: Any, result: bool) -> None:
    assert compare(actual, op, expected) is result


def test_compare_tolerance() -> None:
    assert compare(21.4, "approx", 21, tolerance=0.5)
    assert compare(20.6, "approx", 21, tolerance=-0.5)
    with pytest.raises(ValueError):
        compare(1, "between", 2)
