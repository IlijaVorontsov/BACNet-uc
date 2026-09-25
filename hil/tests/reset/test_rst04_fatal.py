"""RST-04 fatal error recovery (instrumented; hardware), per image.

Catalogue RST-04: after 'hil panic' the DUT is back (I-Am or CONNACK) within 20 s plus the
watchdog timeout, with no rig intervention. rstmon shows the internal reset pulse. Passes 5/5.
BACnet is xfail(strict) until FW-05.

The 4.4.2 default fatal handler halts (kernel/fatal.c): only a watchdog or a reboot in the
handler brings the DUT back. mqtt_tls feeds the IWDG through task_wdt with a 1 s hardware
fallback (FW-05 status), so its allowance is a few seconds.
"""

from __future__ import annotations

import pytest

from hilrig.console import Console
from hilrig.dutctl import DutLink
from hilrig.release import DutImage
from hilrig.stim import Stim

pytestmark = [pytest.mark.instrumented, pytest.mark.hil_only]

RUNS = 5
BACK_S = 20.0
WATCHDOG_S = {"mqtt": 5.0, "bacnet": 0.0}  # IWDG 2 s window + task_wdt fallback; BACnet: none


@pytest.mark.parametrize(
    "app_image",
    [
        pytest.param(
            "bacnet",
            marks=pytest.mark.xfail(
                strict=True, reason="FW-05: BACnet has no watchdog or fatal-handler reboot"
            ),
        ),
        "mqtt",
    ],
    indirect=True,
)
def test_rst04_fatal_error_recovery(
    app_image: DutImage, console: Console, dut_link: DutLink, stim: Stim
) -> None:
    deadline = BACK_S + WATCHDOG_S[app_image.app]
    for run in range(RUNS):
        before = stim.rstmon().n
        mark = dut_link.mark()
        console.send("hil panic")
        back = dut_link.wait_app(mark, deadline)  # no rig intervention in between
        after = stim.rstmon()
        assert after.n >= before + 1, (
            f"run {run}: no internal reset pulse on NRST (rstmon {before} -> {after.n})"
        )
        assert back <= deadline
