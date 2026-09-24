from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from uc_hub.drivers.bacnet_uc.client import SmpNodeClient
from uc_hub.sim import EXAMPLE_IO, ManualClock, SimNetwork, SimNode


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
async def net(clock: ManualClock) -> AsyncIterator[SimNetwork]:
    async with SimNetwork(clock) as network:
        yield network


@pytest.fixture
async def node(net: SimNetwork) -> SimNode:
    sim = SimNode(name="r204-ctl", instance=2041, io=EXAMPLE_IO, network=net)
    await sim.start()
    return sim


def make_client(sim: SimNode, **kwargs: object) -> SmpNodeClient:
    options: dict[str, object] = {"timeout_s": 0.5, "retries": 1, "name": sim.name}
    options.update(kwargs)
    return SmpNodeClient("127.0.0.1", sim.port, **options)  # type: ignore[arg-type]


@pytest.fixture
async def client(node: SimNode) -> AsyncIterator[SmpNodeClient]:
    c = make_client(node)
    yield c
    await c.close()
