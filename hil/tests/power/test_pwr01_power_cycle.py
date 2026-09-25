"""PWR-01 power cycle (release; hardware), per image.

Catalogue PWR-01: stim power cycle 2000: v3v3 < 300 mV within 200 ms of off and for the rest
of the off time. DUT back (DHCP + I-Am/CONNACK) within 20 s. No udev remove event for the DUT
ST-LINK. Passes --cycles/--cycles.

The 2000 ms cycle is split into ``power off``/``power on`` so the rail can be sampled while
off (``stim adc v3v3``). The ST-LINK stays on USB through D5 during an E5V cut; a udev remove
would re-enumerate it, which changes its USB device number, so an unchanged devnum means no
remove event happened.
"""

from __future__ import annotations

import pytest

from hilrig import flash as flash_mod
from hilrig.bench import Bench
from hilrig.dutctl import V3V3_OFF_MV, DutControl
from hilrig.release import DutImage

pytestmark = [pytest.mark.release, pytest.mark.hil_only, pytest.mark.cycles(60, 30)]

OFF_MS = 2000
RAIL_S = 0.2
BACK_S = 20.0


@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_pwr01_power_cycle(app_image: DutImage, dut_power: DutControl, bench: Bench, cycles: int) -> None:
    usb = flash_mod.usb_devnum_of(bench.dut.serial) if bench.dut.serial else None
    for cycle in range(cycles):
        report = dut_power.cycle(OFF_MS, timeout=BACK_S)
        late = [(t, mv) for t, mv in report.v3v3 if t >= RAIL_S]
        assert late and all(mv < V3V3_OFF_MV for _, mv in late), f"cycle {cycle}: v3v3 while off {late}"
        assert report.back_s is not None and report.back_s <= BACK_S, (
            f"cycle {cycle}: back after {report.back_s} s"
        )
        if usb is not None:
            assert flash_mod.usb_devnum_of(bench.dut.serial or "") == usb, (
                f"cycle {cycle}: the ST-LINK re-enumerated"
            )
