"""IO-01 BO write reaches the pin (release; hardware).

Catalogue IO-01: with do3 bound to BO:3: WP active at priority 8 gives stim din do3 = 1. NULL
at priority 8 gives Relinquish_Default. Priority 1 overrides priority 8. 50/50 correct within
a 1 s functional deadline. The latency bound (sample_ms + 5 ms) is asserted in IO-03.

rig_config binds do3 to BO:3 (golden io.json). do3 is ACTIVE_HIGH: pin level = logical value.
"""

from __future__ import annotations

import time

import pytest

from hilrig.bacnet import Bacnet
from hilrig.bench import Bench, channel, logical
from hilrig.release import DutImage
from hilrig.rigconfig import RigConfig
from hilrig.stim import Stim

pytestmark = [pytest.mark.release, pytest.mark.hil_only]

RUNS = 50
DEADLINE_S = 1.0
ENUMERATED, NULL = 9, 0
ACTIVE, INACTIVE = 1, 0


@pytest.mark.parametrize("app_image", ["bacnet"], indirect=True)
def test_io01_bo_write_reaches_the_pin(
    app_image: DutImage, rig_config: RigConfig, bacnet: Bacnet, stim: Stim, bench: Bench
) -> None:
    inst = bench.dut.bacnet_instance
    do3 = channel(bench.dut.board, "do3")

    def write(priority: int, value: int | None) -> None:
        tag = NULL if value is None else ENUMERATED
        bacnet.write(
            inst,
            "binary-output",
            3,
            "present-value",
            0 if value is None else value,
            tag=tag,
            priority=priority,
        )

    def pin(level: int, what: str) -> None:
        deadline = time.monotonic() + DEADLINE_S
        while stim.din("do3") != logical(do3, level):
            assert time.monotonic() < deadline, f"{what}: do3 not {level} within 1 s"

    default = ACTIVE if bacnet.read(inst, "binary-output", 3, "relinquish-default") == "active" else INACTIVE
    correct = 0
    try:
        for run in range(RUNS):
            write(8, ACTIVE)
            pin(ACTIVE, f"run {run}: active at priority 8")
            write(8, None)
            pin(default, f"run {run}: NULL at priority 8 -> Relinquish_Default")
            write(8, ACTIVE)
            write(1, INACTIVE)
            pin(INACTIVE, f"run {run}: priority 1 inactive over priority 8 active")
            write(1, None)
            pin(ACTIVE, f"run {run}: priority 1 relinquished, priority 8 active again")
            write(8, None)
            pin(default, f"run {run}: all relinquished")
            correct += 1
    finally:
        write(1, None)
        write(8, None)
    assert correct == RUNS
