# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: fake nodes, SMP and BACnet clients connected to them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness.bacnet.client import BacnetClient
from bacnet_uc_harness.smp.client import SmpClient, connect_udp
from bacnet_uc_harness.testing import FakeNode

REPO_ROOT = Path(__file__).resolve().parents[2]

# Smallest valid WebAssembly module: magic + version 1, no sections.
WASM_EMPTY = b"\x00asm\x01\x00\x00\x00"


@pytest.fixture
def repo_root() -> Path:
    """Root of the BACnet-uc repository (schemas/, docs/, firmware/, ...)."""
    return REPO_ROOT


@pytest.fixture
def wasm_module() -> bytes:
    """A module the fake node accepts (valid magic and version)."""
    return WASM_EMPTY + b"\x00" * 24


@pytest.fixture
async def fake_node() -> AsyncIterator[FakeNode]:
    """A started :class:`FakeNode` (device 1001) on 127.0.0.1, ephemeral ports."""
    node = FakeNode()
    await node.start()
    try:
        yield node
    finally:
        await node.stop()


@pytest.fixture
async def make_fake_node() -> AsyncIterator[Callable[..., Awaitable[FakeNode]]]:
    """Factory for additional started fake nodes: ``await make_fake_node(1002)``."""
    nodes: list[FakeNode] = []

    async def make(device_instance: int = 1001, **kwargs: Any) -> FakeNode:
        node = FakeNode(device_instance, f"fake-{device_instance}", **kwargs)
        await node.start()
        nodes.append(node)
        return node

    try:
        yield make
    finally:
        for node in nodes:
            await node.stop()


@pytest.fixture
async def smp_client(fake_node: FakeNode) -> AsyncIterator[SmpClient]:
    """SMP client connected to :func:`fake_node` over UDP (1 s timeout)."""
    client = await connect_udp(fake_node.host, fake_node.smp_port, timeout=1.0)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def bacnet_client() -> AsyncIterator[BacnetClient]:
    """BACnet/IP client on 127.0.0.1 (ephemeral port, 1 s APDU timeout, 1 retry)."""
    client = BacnetClient(local_host="127.0.0.1", apdu_timeout=1.0, retries=1)
    await client.start()
    try:
        yield client
    finally:
        await client.close()
