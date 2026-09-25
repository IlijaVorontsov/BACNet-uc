"""IO-04 BI edge to Present_Value and debounce (release; hardware).

Catalogue IO-04: with di1 bound to BI:1 (debounce 20 ms): stim dout di1 0 gives PV active and
z gives inactive, 50/50 within a 1 s functional deadline. A 5 ms pulse (0/z) never changes PV
(20 trials).

di1 is ACTIVE_LOW with a pull-up (a dry contact): driving 0 is active, Hi-Z inactive.
rig_config binds di1 to BI:1 with debounce_ms 20 (golden io.json). After each 5 ms pulse the
PV is sampled for longer than sample + debounce, so a change would be seen.
"""

from __future__ import annotations

import time

import pytest

from hilrig.bacnet import Bacnet
from hilrig.bench import Bench, channel, drive_level
from hilrig.release import DutImage
from hilrig.rigconfig import DEBOUNCE_MS, SAMPLE_MS, RigConfig
from hilrig.stim import Stim

pytestmark = [pytest.mark.release, pytest.mark.hil_only]

RUNS, PULSE_RUNS = 50, 20
DEADLINE_S = 1.0
PULSE_US = 5000
WATCH_S = 3 * (SAMPLE_MS + DEBOUNCE_MS) / 1000


@pytest.mark.parametrize("app_image", ["bacnet"], indirect=True)
def test_io04_bi_edge_and_debounce(
    app_image: DutImage, rig_config: RigConfig, bacnet: Bacnet, stim: Stim, bench: Bench
) -> None:
    inst = bench.dut.bacnet_instance
    di1 = channel(bench.dut.board, "di1")

    def pv() -> str:
        return bacnet.read(inst, "binary-input", 1, "present-value")

    def wait_pv(value: str, what: str) -> None:
        deadline = time.monotonic() + DEADLINE_S
        while pv() != value:
            assert time.monotonic() < deadline, f"{what}: PV not {value} within 1 s"

    for run in range(RUNS):
        stim.dout("di1", drive_level(di1, True))
        wait_pv("active", f"run {run}: di1 active")
        stim.dout("di1", drive_level(di1, False))
        wait_pv("inactive", f"run {run}: di1 released")
    active_level = drive_level(di1, True)
    assert active_level in (0, 1)
    for run in range(PULSE_RUNS):
        stim.pulse("di1", PULSE_US, active=active_level)  # idle Hi-Z, 5 ms active: below the debounce
        end = time.monotonic() + WATCH_S
        while time.monotonic() < end:
            assert pv() == "inactive", f"pulse {run}: a 5 ms pulse changed PV"
