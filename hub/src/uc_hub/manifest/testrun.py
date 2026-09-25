"""Acceptance tests from ``system.tests``: force/release/write/wait/expect steps.

A test changes the live site (forces inputs, commands points), so whatever
it forced is released and whatever it wrote at a priority is relinquished
when it ends: on success, failure, error, timeout and cancellation alike.
Clock and sleep are injectable so tests of the runner need no real time.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from ..core.errors import DeviceTimeout, HubError, InvalidRequest
from ..core.ids import PointRef
from ..core.types import TestResult, Value

logger = logging.getLogger(__name__)

PRESENT_VALUE = "present-value"
RETRY_INTERVAL_S = 0.2
DEFAULT_TOLERANCE = 0.01
#: Each release/relinquish of the cleanup gets this long, so a node that
#: stopped answering cannot hang the test run.
CLEANUP_TIMEOUT_S = 30.0
_STEP_ERRORS = (HubError, OSError, TimeoutError, ValueError)
_K = TypeVar("_K")
#: Cleanup tasks that outlive a cancelled test run; kept referenced until done.
_CLEANUPS: set[asyncio.Task[list[str]]] = set()


class TestTarget(Protocol):
    """Where tests run: the live site or the simulation."""

    async def read(self, point: PointRef, prop: str = PRESENT_VALUE) -> Value: ...

    async def write(self, point: PointRef, value: Value, prop: str = PRESENT_VALUE,
                    priority: int | None = None) -> None:
        """``value`` None with a priority relinquishes that priority."""

    async def force(self, node: str, channel: str, value: float | None) -> None:
        """Force an IO channel of a bacnet-uc node; None releases the force."""


class _StepFailed(Exception):
    def __init__(self, status: Literal["fail", "error"], detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(slots=True)
class _Held:
    """What the running test must undo, in the order it was done."""

    forces: dict[tuple[str, str], None] = field(default_factory=dict)
    writes: dict[tuple[PointRef, str, int], None] = field(default_factory=dict)


async def run_tests(
    tests: list[dict[str, Any]],
    target: TestTarget,
    site: str,
    *,
    names: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retry_interval_s: float = RETRY_INTERVAL_S,
    test_timeout_s: float = 600.0,
    cleanup_timeout_s: float = CLEANUP_TIMEOUT_S,
    target_kind: Literal["live", "sim"] = "live",
) -> list[TestResult]:
    """Run ``tests`` (``system.tests`` entries) in order, or only those in ``names``."""
    if names is not None:
        known = {t.get("name") for t in tests}
        unknown = [n for n in names if n not in known]
        if unknown:
            raise InvalidRequest(f"unknown test{'s' if len(unknown) > 1 else ''}: {', '.join(unknown)}")
        tests = [t for t in tests if t.get("name") in names]
    runner = _Runner(target, site, clock, sleep, retry_interval_s, cleanup_timeout_s)
    results = []
    for test in tests:
        results.append(await runner.run(test, test_timeout_s, target_kind))
    return results


class _Runner:
    def __init__(self, target: TestTarget, site: str, clock: Callable[[], float],
                 sleep: Callable[[float], Awaitable[None]], retry_interval_s: float,
                 cleanup_timeout_s: float) -> None:
        self.target = target
        self.site = site
        self.clock = clock
        self.sleep = sleep
        self.retry_interval_s = retry_interval_s
        self.cleanup_timeout_s = cleanup_timeout_s

    async def run(self, test: dict[str, Any], timeout_s: float,
                  target_kind: Literal["live", "sim"]) -> TestResult:
        name = str(test.get("name", "?"))
        start = self.clock()
        held = _Held()
        status: Literal["pass", "fail", "error"] = "pass"
        failed_step: int | None = None
        detail = ""
        index = 0
        try:
            async with asyncio.timeout(timeout_s):
                for index, step in enumerate(test.get("steps") or []):
                    await self.step(index, step, held)
        except _StepFailed as e:
            status, failed_step, detail = e.status, index, e.detail
        except TimeoutError:
            status, failed_step = "error", index
            detail = f"step {index}: the test did not finish within {timeout_s:g} s"
        finally:
            problems = await _cleanup_shielded(self.target, held, self.cleanup_timeout_s)
        if problems:
            text = "cleanup failed: " + "; ".join(problems)
            detail = f"{detail}; {text}" if detail else text
            if status == "pass":
                status = "error"
        duration_ms = max(0, round((self.clock() - start) * 1000))
        if status != "pass":
            logger.info("test %r %s at step %s: %s", name, status, failed_step, detail)
        return TestResult(name=name, status=status, duration_ms=duration_ms, failed_step=failed_step,
                          detail=detail, target=target_kind)

    def point(self, text: Any, where: str) -> PointRef:
        try:
            return PointRef.parse(str(text), default_site=self.site)
        except ValueError as e:
            raise _StepFailed("error", f"{where}: {e}") from None

    async def step(self, index: int, step: Any, held: _Held) -> None:
        if not isinstance(step, dict) or len(step) != 1:
            raise _StepFailed("error", f"step {index}: a step is a mapping with exactly one key")
        kind, body = next(iter(step.items()))
        where = f"step {index} {kind}"
        try:
            if kind == "force":
                node, channel = str(body["node"]), str(body["channel"])
                value = float(body["value"])
                async with _holding(held.forces, (node, channel)):
                    await self.target.force(node, channel, value)
            elif kind == "release":
                node, channel = str(body["node"]), str(body["channel"])
                await self.target.force(node, channel, None)
                held.forces.pop((node, channel), None)
            elif kind == "write":
                await self.write(where, body, held)
            elif kind == "wait":
                await self.sleep(max(0, int(body)) / 1000)
            elif kind == "expect":
                await self.expect(where, body)
            else:
                raise _StepFailed("error", f"{where}: unknown step")
        except _StepFailed:
            raise
        except _STEP_ERRORS as e:
            raise _StepFailed("error", f"{where}: {_describe(e)}") from None
        except (KeyError, TypeError) as e:
            raise _StepFailed("error", f"{where}: malformed step ({_describe(e)})") from None

    async def write(self, where: str, body: dict[str, Any], held: _Held) -> None:
        ref = self.point(body["point"], where)
        prop = str(body.get("property", PRESENT_VALUE))
        value = body.get("value")
        priority = None if body.get("priority") is None else int(body["priority"])
        if priority is not None and value is not None:
            async with _holding(held.writes, (ref, prop, priority)):
                await self.target.write(ref, value, prop, priority)
            return
        await self.target.write(ref, value, prop, priority)
        if priority is not None:
            held.writes.pop((ref, prop, priority), None)

    async def expect(self, where: str, body: dict[str, Any]) -> None:
        ref = self.point(body["point"], where)
        prop = str(body.get("property", PRESENT_VALUE))
        op = str(body["op"])
        expected = body["value"]
        tolerance = float(body.get("tolerance", DEFAULT_TOLERANCE))
        within_ms = max(0, int(body.get("within_ms", 0)))
        deadline = self.clock() + within_ms / 1000
        what = f"{where} {ref.device}/{ref.obj}{'' if prop == PRESENT_VALUE else '.' + prop} {op} {expected!r}"
        while True:
            error: BaseException | None = None
            actual: Value = None
            try:
                actual = await self.target.read(ref, prop)
            except _STEP_ERRORS as e:
                error = e
            else:
                if compare(actual, op, expected, tolerance):
                    return
            if self.clock() >= deadline:
                break
            await self.sleep(self.retry_interval_s)
        waited = f" after {within_ms} ms" if within_ms else ""
        if error is not None:
            raise _StepFailed("error", f"{what}: read failed{waited}: {_describe(error)}")
        raise _StepFailed("fail", f"{what}: got {actual!r}{waited}")


@contextlib.asynccontextmanager
async def _holding(held: dict[_K, None], key: _K) -> AsyncIterator[None]:
    """Hold ``key`` for cleanup while the force or write is sent, not after:
    when the answer is lost (timeout, cancellation) the node may well have
    applied it. Only a refusal the target answered (any HubError except
    DeviceTimeout) proves nothing changed, and then an earlier hold stays."""
    was_held = key in held
    held[key] = None
    try:
        yield
    except HubError as e:
        if not was_held and not isinstance(e, DeviceTimeout):
            held.pop(key, None)
        raise


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def compare(actual: Value, op: str, expected: Any, tolerance: float = DEFAULT_TOLERANCE) -> bool:
    """``actual <op> expected``. Booleans compare as 0/1 (binary present
    values are 0/1 on the wire); ordering ops and approx need numbers."""
    a, b = _number(actual), _number(expected)
    if op in ("eq", "ne"):
        equal = a == b if a is not None and b is not None else actual == expected
        return equal if op == "eq" else not equal
    if a is None or b is None:
        return False
    if op == "gt":
        return a > b
    if op == "ge":
        return a >= b
    if op == "lt":
        return a < b
    if op == "le":
        return a <= b
    if op == "approx":
        return abs(a - b) <= abs(tolerance)
    raise ValueError(f"unknown op {op!r}")


async def _cleanup(target: TestTarget, held: _Held, timeout_s: float) -> list[str]:
    problems: list[str] = []
    for node, channel in reversed(list(held.forces)):
        try:
            async with asyncio.timeout(timeout_s):
                await target.force(node, channel, None)
        except _STEP_ERRORS as e:
            problems.append(f"release {node}/{channel}: {_describe(e)}")
    for ref, prop, priority in reversed(list(held.writes)):
        try:
            async with asyncio.timeout(timeout_s):
                await target.write(ref, None, prop, priority)
        except _STEP_ERRORS as e:
            problems.append(f"relinquish {ref.device}/{ref.obj} at priority {priority}: {_describe(e)}")
    for problem in problems:
        logger.warning("test cleanup: %s", problem)
    return problems


async def _cleanup_shielded(target: TestTarget, held: _Held, timeout_s: float) -> list[str]:
    """Run the cleanup so that cancelling the test run cannot interrupt it."""
    if not held.forces and not held.writes:
        return []
    task = asyncio.ensure_future(_cleanup(target, held, timeout_s))
    _CLEANUPS.add(task)
    task.add_done_callback(_CLEANUPS.discard)
    return await asyncio.shield(task)


def _describe(error: BaseException) -> str:
    text = str(error)
    if isinstance(error, TimeoutError) and not text:
        text = "timed out"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__
