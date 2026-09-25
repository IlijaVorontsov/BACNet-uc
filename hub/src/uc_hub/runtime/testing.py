"""Acceptance tests (``system.tests``) on the live site.

``LiveTestTarget`` is the ``manifest.testrun.TestTarget`` of the live site.
Test steps are agent actions: writes and forces go through ``LiveControl``,
so the policy applies (life-safety points, deny patterns, the agent
priority, the rate limit) and each one holds a lease. The runner undoes its
writes and forces when a test ends; ``close`` releases whatever a test left
(a write without a priority, a step that failed half-way).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..core.errors import DeviceError, HubError, PolicyDenied, Unsupported
from ..core.ids import PointRef
from ..core.types import ProtocolName, Quality, TestResult, Value
from ..manifest.testrun import PRESENT_VALUE, run_tests
from .live import LiveControl

logger = logging.getLogger(__name__)


class LiveTestTarget:
    def __init__(self, control: LiveControl, *, user: str, run_id: str | None = None) -> None:
        self.control = control
        self.site = control.site
        self.user = user
        self.run_id = run_id
        #: Leases this run holds: (point, priority) of writes, (node, channel) of forces.
        self._writes: set[tuple[PointRef, int | None]] = set()
        self._forces: set[tuple[str, str]] = set()

    async def read(self, point: PointRef, prop: str = PRESENT_VALUE) -> Value:
        if prop != PRESENT_VALUE:
            if self.site.device_spec(point.device).protocol is not ProtocolName.BACNET_UC:
                raise Unsupported(f"live tests read {prop} only on bacnet-uc nodes")
            parsed = point.bacnet
            if parsed is None:
                raise Unsupported(f"{point} is not a BACnet object")
            value: Value = await self.site.node(point.device).prop_read(parsed[0], parsed[1], prop)
            return value
        (reading,) = await self.site.read([point])
        if reading.quality not in (Quality.GOOD, Quality.STALE):
            raise DeviceError(f"{point}: {reading.error or reading.quality.value}")
        return reading.value

    async def write(self, point: PointRef, value: Value, prop: str = PRESENT_VALUE,
                    priority: int | None = None) -> None:
        if prop != PRESENT_VALUE:
            raise PolicyDenied(f"live tests write present values only; a {prop} write is permanent and "
                               "cannot be leased (change it through the manifest)")
        if value is None:
            await self.control.relinquish(point, priority, user=self.user, run_id=self.run_id)
            self._writes.discard((point, priority))
            return
        _, lease = await self.control.write(point, value, priority=priority, user=self.user, run_id=self.run_id)
        self._writes.add((point, lease.priority))

    async def force(self, node: str, channel: str, value: float | None) -> None:
        if value is None:
            await self.control.release_force(node, channel)
            self._forces.discard((node, channel))
            return
        await self.control.force(node, channel, value, user=self.user, run_id=self.run_id)
        self._forces.add((node, channel))

    async def close(self) -> list[str]:
        """Release every lease the tests still hold; returns what failed."""
        problems = []
        for node, channel in sorted(self._forces):
            try:
                await self.control.release_force(node, channel)
            except HubError as e:
                problems.append(f"release {node}/{channel}: {e}")
        for ref, priority in sorted(self._writes, key=lambda w: (str(w[0]), w[1] or 0)):
            try:
                await self.control.relinquish(ref, priority, user=self.user, run_id=self.run_id)
            except HubError as e:
                problems.append(f"relinquish {ref.device}/{ref.obj}: {e}")
        self._forces.clear()
        self._writes.clear()
        for problem in problems:
            logger.warning("live test cleanup: %s", problem)
        return problems


async def run_live_tests(control: LiveControl, tests: list[dict[str, Any]], *, user: str,
                         run_id: str | None = None, names: Iterable[str] | None = None) -> list[TestResult]:
    """Run manifest tests on the live site and clean up after them."""
    target = LiveTestTarget(control, user=user, run_id=run_id)
    try:
        results = await run_tests(tests, target, control.site.name, names=list(names) if names is not None else None)
    finally:
        problems = await target.close()
    if problems and results:
        last = results[-1]
        note = "cleanup after the tests failed: " + "; ".join(problems)
        last.detail = f"{last.detail}; {note}" if last.detail else note
        if last.status == "pass":
            last.status = "error"
    return results
