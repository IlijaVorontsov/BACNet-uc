"""The demo site against simulated nodes over real SMP: plan, apply, re-plan,
rollback and the acceptance tests. The in-memory FakeNode only mirrors what
the engine expects; this checks it against the simulator and the SMP client."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from uc_hub.core.ids import PointRef
from uc_hub.core.types import Value
from uc_hub.manifest import (
    MemoryBackupStore,
    SiteManifest,
    apply_plan,
    compute_plan,
    directory_resolver,
    load_site,
    rollback_target,
    run_tests,
)

from .conftest import UC_LINK
from .fakes import FakeGateway
from .test_load import EXAMPLES

sim = pytest.importorskip("uc_hub.sim")
client_module = pytest.importorskip("uc_hub.drivers.bacnet_uc.client")


@dataclass
class Farm:
    site: SiteManifest
    net: Any
    clock: Any
    clients: dict[str, Any]
    uc_link: Path

    def nodes(self, name: str) -> Any:
        return self.clients[name]

    async def sleep(self, seconds: float) -> None:
        self.net.advance(seconds, 0.1)
        await asyncio.sleep(0)

    async def plan(self, desired: SiteManifest, live: SiteManifest | None, revision: int) -> Any:
        return await compute_plan(desired, live, self.nodes, directory_resolver(EXAMPLES), self.uc_link,
                                  revision, revision - 1)

    async def apply(self, plan: Any, backups: MemoryBackupStore | None = None) -> None:
        results = await apply_plan(plan, self.nodes, FakeGateway(), backups or MemoryBackupStore(),
                                   sleep=self.sleep, clock=self.clock)
        assert all(r.ok for r in results), [r for r in results if not r.ok]


class NodeTarget:
    """TestTarget over the nodes' SMP clients."""

    def __init__(self, clients: dict[str, Any]) -> None:
        self.clients = clients

    async def read(self, point: PointRef, prop: str = "present-value") -> Value:
        obj_type, instance = _bacnet(point)
        value: Value = await self.clients[point.device].prop_read(obj_type, instance, prop)
        return value

    async def write(self, point: PointRef, value: Value, prop: str = "present-value",
                    priority: int | None = None) -> None:
        obj_type, instance = _bacnet(point)
        await self.clients[point.device].prop_write(obj_type, instance, value, prop, priority)

    async def force(self, node: str, channel: str, value: float | None) -> None:
        await self.clients[node].io_force(channel, value)


def _bacnet(point: PointRef) -> tuple[str, int]:
    parsed = point.bacnet
    assert parsed is not None, point
    return parsed


@pytest.fixture
async def farm(tmp_path: Path) -> AsyncIterator[Farm]:
    site = load_site(EXAMPLES / "site.yaml")
    uc_link = tmp_path / "uc-link.wasm"
    uc_link.write_bytes(UC_LINK)
    clock = sim.ManualClock()
    async with sim.SimNetwork(clock) as net:
        _, sims = await sim.start_sim_nodes(
            [{"name": name, "instance": node["device"]["instance"]} for name, node in site.nodes.items()],
            network=net,
        )
        clients = {s.name: client_module.SmpNodeClient("127.0.0.1", s.port, timeout_s=0.5, retries=2, name=s.name)
                   for s in sims}
        try:
            yield Farm(site, net, clock, clients, uc_link)
        finally:
            for c in clients.values():
                await c.close()


async def test_apply_converges_and_rolls_back(farm: Farm) -> None:
    site = farm.site
    plan = await farm.plan(site, None, 1)
    assert plan.blocked == {}
    await farm.apply(plan)
    again = await farm.plan(site, site, 2)
    assert again.changes == [] and again.blocked == {}

    doc = site.to_dict()
    doc["system"]["apps"][0]["params"]["setpoint"] = 19
    doc["system"]["links"] = []
    changed = SiteManifest.from_dict(doc)
    update = await farm.plan(changed, site, 3)
    assert [(c.kind, c.summary) for c in update.changes] == [
        ("remove-app", "remove app link"),
        ("install-app", "update app thermostat: params.setpoint changed (restart)"),
    ]
    backups = MemoryBackupStore()
    await farm.apply(update, backups)
    assert (await farm.plan(changed, changed, 4)).changes == []

    backup = backups.get(update.id, "r204-ctl")
    assert backup is not None
    assert (await rollback_target(backup, farm.nodes)).ok
    assert (await farm.plan(site, changed, 5)).changes == []
    states = {a["name"]: a["state"] for a in await farm.clients["r204-ctl"].app_list()}
    assert states == {"thermostat": "running", "link": "running"}


async def test_demo_acceptance_tests_pass_on_the_simulator(farm: Farm) -> None:
    await farm.apply(await farm.plan(farm.site, None, 1))
    await farm.sleep(2)
    results = await run_tests(farm.site.tests, NodeTarget(farm.clients), farm.site.name,
                              clock=farm.clock, sleep=farm.sleep, target_kind="sim")
    assert [(r.name, r.status, r.detail) for r in results] == [
        (t["name"], "pass", "") for t in farm.site.tests]
    for client in farm.clients.values():
        assert not any(ch["forced"] for ch in (await client.io_catalog())["channels"])
