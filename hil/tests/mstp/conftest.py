"""Fixtures for the MS/TP bench-bus tests, on top of the rig fixtures.

The rig conftest provides ``bench`` (hilrig.bench.Bench), ``stim`` (hilrig.stim.Stim), ``la``
(the bench's logic analyzer) and ``artifacts`` (a directory). Here:

- ``mstp_bench`` skips unless bench.yml has an ``mstp`` section and the LA channels needed;
- ``host_node`` is the host's RS-485 adapter acting as MS/TP master 7 (FTDI USB-RS485-WE,
  automatic TXDEN), so the DUT has a ring peer without RS-485 commands on the stimulus board;
- ``capture_bus`` runs an action while the LA records and returns the decoded capture;
- ``ring`` is such a capture of normal token passing between the DUT and the host node.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import serial

from hilrig import mstp
from hilrig.bench import Bench
from hilrig.la import LogicAnalyzer
from mstp_bus import LA_CHANNELS, BusView, CaptureBus, HostNode

RING_S = 2.0  # how long the ring runs in a ``ring`` capture


@pytest.fixture
def mstp_bench(bench: Bench) -> Bench:
    """The bench, if it has an MS/TP bus and the LA channels these tests need."""
    if bench.mstp is None or bench.la is None:
        pytest.skip("bench.yml has no mstp/la section")
    missing = [c for c in LA_CHANNELS if c not in bench.la.channels]
    if missing:
        pytest.skip(f"bench.yml la.channels lacks {missing}")
    return bench


@pytest.fixture
def host_node(mstp_bench: Bench) -> Iterator[HostNode]:
    assert mstp_bench.mstp is not None
    with serial.Serial(mstp_bench.mstp.ftdi, mstp_bench.mstp.baud, timeout=0.001) as port:
        yield HostNode(port, mstp_bench.mstp.baud)


@pytest.fixture
def capture_bus(
    mstp_bench: Bench, la: LogicAnalyzer, artifacts: Path, request: pytest.FixtureRequest
) -> CaptureBus:
    """``capture_bus(seconds, action)``: record while ``action()`` runs; frames also go to a pcap."""

    def run(duration_s: float, action: Callable[[], None]) -> BusView:
        assert mstp_bench.mstp is not None
        workdir = artifacts / "la" / re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
        with la.acquire(duration_s, workdir) as acq:
            action()
        assert acq.trace is not None
        view = BusView(acq.trace, mstp_bench.mstp.baud)
        mstp.write_pcap(workdir / "mstp.pcap", mstp.parse_frames(view.bus_octets, view.baud))
        return view

    return run


@pytest.fixture
def ring(capture_bus: CaptureBus, host_node: HostNode) -> BusView:
    """A capture of normal token passing between the DUT and the host node."""
    view = capture_bus(RING_S + 1.0, lambda: host_node.serve(RING_S))
    if len(view.tx_frames) < 10:
        pytest.fail(f"DUT sent {len(view.tx_frames)} frames in {RING_S}s: no ring (wiring, baud, MAC?)")
    return view
