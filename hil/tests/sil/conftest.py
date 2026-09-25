"""SIL rig self-validation: bacserv stands in for the DUT at 192.0.2.10 (netns dut on br-a).

These tests prove the rig (topology, capture, tools, broker, PKI) before any hardware
exists. They need the stand-in bench (hil/host/bench-sil.yml, ``pytest --sil``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from hilrig.bacnet import Bacnet, BacServ
from hilrig.bench import Bench
from hilrig.netns import Topology


@pytest.fixture(scope="session")
def standin(bench: Bench, netns: Topology, bacnet: Bacnet, artifacts: Path) -> Iterator[BacServ]:
    """bacserv with the bench's device instance in netns dut, also acting as a BBMD."""
    if bench.dut.board != "bacserv":
        pytest.skip("SIL rig self-validation needs the bacserv stand-in bench (pytest --sil)")
    with BacServ(
        netns.ns("dut"),
        "dut0",
        bench.dut.bacnet_instance,
        "HIL standin",
        log=artifacts / "standin" / "bacserv.log",
        tools=bacnet.tools,
    ) as server:
        yield server
