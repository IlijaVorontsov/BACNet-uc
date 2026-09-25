"""Leased live writes and forces through the policy, and what happens when
their leases end: relinquish, restore, release, and audited failures."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support.site import Hub, eventually, start_hub

from uc_hub.core.errors import DeviceError, InvalidRequest, PolicyDenied
from uc_hub.policy import LeaseManager
from uc_hub.runtime.live import LeaseReleaser, LiveControl
from uc_hub.sim.bacnet_ip_device import SimBacnetIpDevice

AO, BO = 1, 4


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now


@dataclass
class Live:
    hub: Hub
    clock: Clock
    leases: LeaseManager
    control: LiveControl
    ahu: SimBacnetIpDevice

    def expire(self, seconds: float) -> None:
        self.clock.now += seconds
        self.leases.wake()

    async def audit(self, action: str) -> list[dict[str, Any]]:
        return [e for e in await self.hub.services.store.list_audit() if e["action"] == action]


def ahu_site(address: str, instance: int) -> Any:
    def edit(doc: dict[str, Any]) -> None:
        doc["external_devices"] = [{"name": "ahu1-ctl", "protocol": "bacnet-ip", "address": address,
                                    "device_instance": instance}]
        doc["placement"]["ahu1-ctl"] = "plant"
        doc["policy"]["max_writes_per_minute"] = 6
    return edit


@pytest.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    async with SimBacnetIpDevice(drift=False, tick_s=0.05) as ahu:
        hub = await start_hub(tmp_path, edit=ahu_site(ahu.address, ahu.instance), hub={"drivers": {"bacnet_ip": {
            "interface": "127.0.0.1", "port": 0, "timeout_s": 0.3, "retries": 1, "poll_interval_s": 0.1}}})
        clock = Clock()
        releaser = LeaseReleaser(hub.site, hub.services.store)
        leases = LeaseManager(hub.services.store, releaser, clock=clock, on_event=releaser.on_event,
                              retry_base_s=0.01, release_timeout_s=2.0)
        await leases.start()
        try:
            yield Live(hub, clock, leases, LiveControl(hub.site, hub.services.policy, leases), ahu)
        finally:
            await leases.stop()
            await hub.close()


def slot(hub: Hub, node: str, obj: tuple[int, int], priority: int) -> Any:
    sim_obj = hub.nodes[node].objects[obj]
    assert sim_obj.priority is not None
    return sim_obj.priority[priority - 1]


async def test_a_commandable_write_is_relinquished_when_its_lease_ends(live: Live) -> None:
    hub = live.hub
    ref = hub.ref("r204-ctl/analog-output:1")
    result, lease = await live.control.write(ref, 55.0, lease_s=30, user="tech", run_id=None)
    assert result.ok and lease.priority == 12 and lease.target == {"point": str(ref)}
    assert slot(hub, "r204-ctl", (AO, 1), 12) == 55.0
    # The node holds the lease as well, for when the hub is gone.
    assert (AO, 1, 12) in hub.nodes["r204-ctl"]._leases
    live.expire(31)
    await eventually(lambda: slot(hub, "r204-ctl", (AO, 1), 12) is None, what="relinquished")
    await eventually(lambda: live.leases.active() == [], what="lease ended")
    (entry,) = await live.audit("lease.released")
    assert (entry["user"], entry["outcome"], entry["args"]["lease"]) == ("tech", "ok", lease.id)


async def test_a_write_without_priority_array_is_restored(live: Live) -> None:
    hub = live.hub
    mode = hub.ref("ahu1-ctl/multi-state-value:1")
    point = await hub.site.point(mode)
    assert (point.commandable, point.writable, point.datatype) == (False, True, "int")
    _, first = await live.control.write(mode, 3, lease_s=30, user="tech")
    assert first.priority is None and first.target == {"point": str(mode), "restore": 2}
    assert int(live.ahu.mode.presentValue) == 3
    # A second write keeps the value from before the first one.
    _, second = await live.control.write(mode, 4, lease_s=30, user="tech")
    assert second.target["restore"] == 2 and [x.id for x in live.leases.active()] == [second.id]
    assert int(live.ahu.mode.presentValue) == 4
    live.expire(31)
    await eventually(lambda: int(live.ahu.mode.presentValue) == 2, what="restored to 2")


async def test_a_force_is_released_when_its_lease_ends(live: Live) -> None:
    hub = live.hub
    channel = hub.nodes["r204-ctl"].channels["ai0"]
    lease, point = await live.control.force("r204-ctl", "ai0", 650, lease_s=30, user="tech")
    assert point is not None and str(point.ref) == "hq/r204-ctl/analog-input:1"
    assert lease.target == {"node": "r204-ctl", "channel": "ai0"}
    assert channel.forced == 650 and channel.force_until is not None  # lease_ms reached the node
    live.expire(31)
    await eventually(lambda: channel.forced is None, what="force released")


async def test_failed_releases_are_audited_and_retried(live: Live) -> None:
    hub = live.hub
    ref = hub.ref("r204-ctl/analog-output:1")
    await live.control.write(ref, 60.0, lease_s=30, user="tech")
    hub.nodes["r204-ctl"].online = False
    live.expire(31)
    await eventually(lambda: (lease := live.leases.active()) and lease[0].last_error, what="a failed attempt")
    for _ in range(100):
        failures = await live.audit("lease.release_failed")
        if failures:
            break
        await asyncio.sleep(0.02)
    assert failures[-1]["outcome"] == "error" and "attempt 1 failed" in failures[-1]["detail"]
    hub.nodes["r204-ctl"].online = True
    live.expire(120)
    await eventually(lambda: slot(hub, "r204-ctl", (AO, 1), 12) is None, what="relinquished on retry")


async def test_writes_and_forces_follow_the_policy(live: Live) -> None:
    hub, control = live.hub, live.control
    with pytest.raises(PolicyDenied, match="life-safety"):
        await control.write(hub.ref("r205-ctl/binary-output:1"), 1, user="tech")
    with pytest.raises(PolicyDenied, match="life-safety"):
        await control.force("r205-ctl", "do0", 1, user="tech")
    with pytest.raises(PolicyDenied, match="deny pattern"):
        await control.write(hub.ref("r205-ctl/analog-output:1"), 10.0, user="tech")
    with pytest.raises(PolicyDenied, match="reserved"):
        await control.write(hub.ref("r204-ctl/analog-output:1"), 10.0, priority=5, user="tech")
    with pytest.raises(PolicyDenied, match="outrank"):
        await control.write(hub.ref("r204-ctl/analog-output:1"), 10.0, priority=11, user="tech")
    with pytest.raises(PolicyDenied, match="not writable"):
        await control.write(hub.ref("r204-ctl/analog-input:1"), 10.0, user="tech")
    with pytest.raises(InvalidRequest):
        await control.write(hub.ref("r204-ctl/binary-output:1"), 2, user="tech")
    assert live.leases.active() == [] and slot(hub, "r205-ctl", (BO, 1), 12) is None
    for value in range(6):
        await control.write(hub.ref("r204-ctl/analog-output:1"), float(value), user="tech")
    with pytest.raises(PolicyDenied, match="rate limit"):
        await control.write(hub.ref("r204-ctl/analog-output:1"), 7.0, user="tech")
    assert len(live.leases.active()) == 1  # one slot, one lease


async def test_undo_before_the_lease_ends(live: Live) -> None:
    hub, control = live.hub, live.control
    ref = hub.ref("r204-ctl/analog-output:1")
    await control.write(ref, 20.0, user="tech")
    await control.relinquish(ref, 12, user="tech")
    assert slot(hub, "r204-ctl", (AO, 1), 12) is None and live.leases.active() == []
    await control.force("r204-ctl", "ao0", 80, user="tech")
    released = await control.release_force("r204-ctl", "ao0")
    assert released is not None and released.state == "released"
    assert hub.nodes["r204-ctl"].channels["ao0"].forced is None
    # A force the agent does not hold is released directly, still under the rules.
    hub.nodes["r204-ctl"].force("di0", 1)
    assert await control.release_force("r204-ctl", "di0") is None
    assert hub.nodes["r204-ctl"].channels["di0"].forced is None
    with pytest.raises(PolicyDenied, match="life-safety"):
        await control.release_force("r205-ctl", "do0")


async def test_unanswered_writes_keep_their_lease(live: Live) -> None:
    """The node may have applied a write whose answer was lost, so its lease
    stays until the undo reaches the node."""
    hub, control = live.hub, live.control
    ref = hub.ref("r204-ctl/analog-output:1")
    hub.nodes["r204-ctl"].online = False
    with pytest.raises(DeviceError):
        await control.write(ref, 5.0, user="tech")
    (lease,) = live.leases.active()
    assert lease.last_error
    hub.nodes["r204-ctl"].online = True
    live.expire(120)
    await eventually(lambda: live.leases.active() == [], what="undone once the node answers")


async def test_no_write_without_a_value_to_restore(live: Live) -> None:
    await live.ahu.stop()
    with pytest.raises(DeviceError, match="cannot be read"):
        await live.control.write(live.hub.ref("ahu1-ctl/multi-state-value:1"), 3, user="tech")
    assert live.leases.active() == []


async def test_forces_take_finite_values_only(live: Live) -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(InvalidRequest, match="finite"):
            await live.control.force("r204-ctl", "ao0", value, user="tech")
    assert live.leases.active() == [] and live.hub.nodes["r204-ctl"].channels["ao0"].forced is None
