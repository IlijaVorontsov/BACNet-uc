"""RST-01 NRST reset (release; hardware), per image.

Catalogue RST-01: stim reset 10: LA nrst low for 10 ms + 2.3-3.9 ms RC (+-1 ms). rstmon count
+1. The DUT link drops and returns. I-Am (BACnet image) or CONNACK (MQTT image) within 20 s.
Passes 20/20. Instrumented variant: HIL-BOOT reset cause has the PIN bit.

NRST also resets the PHY (SB177), so the link renegotiates and the DUT asks for DHCP again.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hilrig import netns as nsmod
from hilrig.bench import Bench
from hilrig.dutctl import DutControl
from hilrig.la import LogicAnalyzer
from hilrig.netns import Topology
from hilrig.release import DutImage

pytestmark = [pytest.mark.release, pytest.mark.hil_only]

RUNS = 20
HOLD_MS = 10
LOW_MIN_S, LOW_MAX_S = (HOLD_MS + 2.3 - 1.0) / 1e3, (HOLD_MS + 3.9 + 1.0) / 1e3
BACK_S = 20.0
RESET_PIN = 0x1  # hwinfo RESET_PIN


class CarrierWatch:
    """Polls the DUT NIC's carrier in lan-a in the background: saw it drop, saw it return."""

    def __init__(self, netns: str, iface: str) -> None:
        self.netns, self.iface = netns, iface
        self.dropped = self.returned = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            up = nsmod.carrier(self.netns, self.iface)
            self.dropped |= not up
            self.returned |= self.dropped and up
            time.sleep(0.02)

    def __enter__(self) -> CarrierWatch:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_rst01_nrst_reset(
    app_image: DutImage,
    dut_reset: DutControl,
    la: LogicAnalyzer,
    netns: Topology,
    bench: Bench,
    artifacts: Path,
    request: pytest.FixtureRequest,
) -> None:
    assert bench.la is not None
    if "nrst" not in bench.la.channels:
        pytest.skip("bench.yml la.channels has no nrst")
    console = request.getfixturevalue("console") if app_image.instrumented else None
    for run in range(RUNS):
        since = console.mark() if console else 0
        mark = dut_reset.link.mark()
        with CarrierWatch(netns.ns("lan-a"), netns.capture_iface) as link:
            with la.acquire(0.5, artifacts / "la" / f"RST-01-{run}") as acq:
                time.sleep(0.1)
                report = dut_reset.reset(HOLD_MS, wait=False)
            back = dut_reset.link.wait_app(mark, BACK_S)
        assert acq.trace is not None
        lows = [(a, b) for a, b in acq.trace["nrst"].intervals(0) if a > 0 and b < acq.trace.duration_s]
        assert len(lows) == 1, f"run {run}: NRST low intervals {lows}"
        low_s = lows[0][1] - lows[0][0]
        assert LOW_MIN_S <= low_s <= LOW_MAX_S, f"run {run}: NRST low {low_s * 1e3:.2f} ms"
        assert report.rstmon_before is not None and report.rstmon_after == report.rstmon_before + 1
        assert link.dropped and link.returned, f"run {run}: DUT link drop/return not seen"
        assert back <= BACK_S
        if console is not None:
            _, m = console.wait_for(r"HIL-BOOT .*reset=0x([0-9a-fA-F]+)", 1.0, since)
            assert int(m[1], 16) & RESET_PIN, f"run {run}: reset cause {m[1]} lacks PIN"
