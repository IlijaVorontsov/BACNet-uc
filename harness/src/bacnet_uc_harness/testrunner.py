# SPDX-License-Identifier: Apache-2.0
"""Run the ``tests`` of a system manifest against live nodes.

Steps (``schemas/system.schema.json``):

``force``   ``uc_io force`` a channel (inputs: the value the IO scan sees;
            outputs: driven regardless of the object)
``release`` release a forced channel
``write``   WriteProperty on ``<node>/<type>:<instance>`` (BACnet/IP, SMP
            ``prop_write`` fallback); ``value: null`` relinquishes ``priority``
``wait``    sleep milliseconds
``expect``  read the property until ``<value> <op> <expected>`` holds or
            ``within_ms`` elapsed (BACnet/IP, SMP ``prop_read`` fallback)

Comparison: binary values are compared as numbers (``active``/``true`` = 1,
``inactive``/``false`` = 0); ``eq``/``ne`` on numbers allow a relative error
of 1e-6 (REAL is single precision); ``approx`` uses ``tolerance``
(absolute, default 0.01).

Channels forced by a test and not released are released at its end.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.manifest import System, TestSpec

_TRUE = {"active", "true", "on"}
_FALSE = {"inactive", "false", "off"}
EQ_REL_TOL = 1e-6


def to_number(value: Any) -> float | None:
    """Numeric form of a value (bools and binary state names included)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return 1.0
        if text in _FALSE:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return None
    return None


def compare(actual: Any, op: str, expected: Any, tolerance: float = 0.01) -> bool:
    """Evaluate ``actual <op> expected``."""
    a, e = to_number(actual), to_number(expected)
    if a is None or e is None:
        if op == "eq":
            return str(actual) == str(expected)
        if op == "ne":
            return str(actual) != str(expected)
        return False
    if op == "eq":
        return math.isclose(a, e, rel_tol=EQ_REL_TOL, abs_tol=1e-9)
    if op == "ne":
        return not math.isclose(a, e, rel_tol=EQ_REL_TOL, abs_tol=1e-9)
    if op == "gt":
        return a > e
    if op == "ge":
        return a >= e
    if op == "lt":
        return a < e
    if op == "le":
        return a <= e
    if op == "approx":
        return abs(a - e) <= tolerance
    raise HarnessError(f"unknown comparison {op!r}")


@dataclass
class StepResult:
    index: int
    kind: str
    ok: bool
    message: str
    value: Any = None
    attempts: int = 1
    elapsed_ms: int = 0
    via: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"index": self.index, "kind": self.kind, "ok": self.ok, "message": self.message,
               "elapsed_ms": self.elapsed_ms}
        if self.value is not None:
            out["value"] = self.value
        if self.kind == "expect":
            out["attempts"] = self.attempts
        if self.via:
            out["via"] = self.via
        return out


@dataclass
class TestResult:
    __test__ = False

    name: str
    ok: bool
    steps: list[StepResult] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "duration_ms": self.duration_ms,
                "error": self.error, "steps": [s.to_dict() for s in self.steps]}


def _point(ref: str) -> tuple[str, str]:
    node, _, obj = ref.partition("/")
    return node, obj


class TestRunner:
    """Executes tests; ``nodes`` maps node names to :class:`~bacnet_uc_harness.node.Node`
    (or objects with the same ``io_force``/``io_release``/``read_point``/
    ``write_point`` methods)."""

    __test__ = False

    def __init__(self, system: System, nodes: Mapping[str, Any], *,
                 poll_interval: float = 0.2, stop_on_failure: bool = True) -> None:
        self.system = system
        self.nodes = nodes
        self.poll_interval = poll_interval
        self.stop_on_failure = stop_on_failure

    def _node(self, name: str) -> Any:
        try:
            return self.nodes[name]
        except KeyError:
            raise HarnessError(f"no connection to node {name!r}") from None

    async def _read(self, point: str, prop: str) -> tuple[Any, str]:
        node, obj = _point(point)
        n = self._node(node)
        if hasattr(n, "read_point"):
            return await n.read_point(obj, prop)
        return await n.prop_read(obj, prop), "smp"

    async def _step(self, index: int, step: Mapping[str, Any],
                    forced: set[tuple[str, str]]) -> StepResult:
        kind, body = next(iter(step.items()))
        t0 = time.monotonic()

        def done(ok: bool, msg: str, **kw: Any) -> StepResult:
            return StepResult(index, kind, ok, msg,
                              elapsed_ms=int((time.monotonic() - t0) * 1000), **kw)

        if kind == "wait":
            await asyncio.sleep(int(body) / 1000)
            return done(True, f"waited {int(body)} ms")
        if kind == "force":
            await self._node(body["node"]).io_force(body["channel"], float(body["value"]))
            forced.add((body["node"], body["channel"]))
            return done(True, f"forced {body['node']}/{body['channel']} = {body['value']}")
        if kind == "release":
            await self._node(body["node"]).io_release(body["channel"])
            forced.discard((body["node"], body["channel"]))
            return done(True, f"released {body['node']}/{body['channel']}")
        if kind == "write":
            node, obj = _point(body["point"])
            prop = body.get("property", "present-value")
            n = self._node(node)
            if hasattr(n, "write_point"):
                via = await n.write_point(obj, prop, body["value"], body.get("priority"))
            else:
                await n.prop_write(obj, prop, body["value"], body.get("priority"))
                via = "smp"
            prio = f" @{body['priority']}" if body.get("priority") else ""
            return done(True, f"wrote {body['point']}.{prop} = {body['value']}{prio}", via=via)
        if kind == "expect":
            prop = body.get("property", "present-value")
            op = body["op"]
            expected = body["value"]
            tol = float(body.get("tolerance", 0.01))
            within = int(body.get("within_ms", 0)) / 1000
            deadline = time.monotonic() + within
            attempts = 0
            value: Any = None
            via = None
            last_err: str | None = None
            while True:
                attempts += 1
                try:
                    value, via = await self._read(body["point"], prop)
                    last_err = None
                    if compare(value, op, expected, tol):
                        return done(True, f"{body['point']}.{prop} = {value!r} {op} "
                                    f"{expected!r}", value=value, attempts=attempts, via=via)
                except HarnessError as exc:
                    last_err = str(exc)
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(self.poll_interval, max(0.0, deadline -
                                                                time.monotonic())))
            msg = (f"{body['point']}.{prop}: read failed: {last_err}" if last_err else
                   f"{body['point']}.{prop} = {value!r}, expected {op} {expected!r}")
            if within:
                msg += f" (within {int(within * 1000)} ms)"
            return done(False, msg, value=value, attempts=attempts, via=via)
        raise HarnessError(f"unknown test step {kind!r}")

    async def run_test(self, test: TestSpec) -> TestResult:
        t0 = time.monotonic()
        result = TestResult(test.name, True)
        forced: set[tuple[str, str]] = set()
        try:
            for i, step in enumerate(test.steps):
                try:
                    sr = await self._step(i, step, forced)
                except HarnessError as exc:
                    kind = next(iter(step))
                    sr = StepResult(i, kind, False, f"{type(exc).__name__}: {exc}")
                result.steps.append(sr)
                if not sr.ok:
                    result.ok = False
                    if self.stop_on_failure:
                        break
        finally:
            for node, channel in sorted(forced):
                try:
                    await self._node(node).io_release(channel)
                except HarnessError as exc:
                    result.error = f"cleanup: release {node}/{channel} failed: {exc}"
        result.duration_ms = int((time.monotonic() - t0) * 1000)
        return result

    async def run(self, names: Sequence[str] | None = None) -> list[TestResult]:
        tests = self.system.tests if not names else [self.system.test(n) for n in names]
        return [await self.run_test(t) for t in tests]


async def run_tests(system: System, nodes: Mapping[str, Any], names: Sequence[str] | None = None,
                    *, poll_interval: float = 0.2, stop_on_failure: bool = True
                    ) -> list[TestResult]:
    """Run the manifest tests (all, or the ones named) sequentially."""
    runner = TestRunner(system, nodes, poll_interval=poll_interval,
                        stop_on_failure=stop_on_failure)
    return await runner.run(names)


def summary(results: Sequence[TestResult]) -> dict[str, Any]:
    passed = sum(1 for r in results if r.ok)
    return {"ok": passed == len(results), "passed": passed, "failed": len(results) - passed,
            "tests": [r.to_dict() for r in results]}
