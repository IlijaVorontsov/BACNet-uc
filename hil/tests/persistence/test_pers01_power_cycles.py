"""PERS-01 configuration survives power cycles (release; hardware, and SIL on native_sim).

Catalogue PERS-01: after device.json name/location and io.json are pushed, then --cycles power
cycles (10 local, 20 nightly, 50 weekly): Object_Name and Location unchanged, io points
present, never a 'formatting /lfs' line, I-Am within 20 s of each power-on.

'formatting /lfs' is the one console line used as a release-tier criterion (FW-03). In SIL a
power cycle restarts the native_sim process, whose /lfs lives in its --flash file. The golden
documents are pushed back afterwards.
"""

from __future__ import annotations

import copy
import time

import pytest

from hilrig.bacnet import Bacnet
from hilrig.bench import Bench
from hilrig.console import Console
from hilrig.dutctl import DutControl
from hilrig.release import DutImage
from hilrig.rigconfig import RigConfig

pytestmark = [pytest.mark.release, pytest.mark.cycles(60, 30)]

OFF_MS = 2000
IAM_LIMIT_S = 20.0


@pytest.mark.parametrize("app_image", ["bacnet"], indirect=True)
def test_pers01_configuration_survives_power_cycles(
    app_image: DutImage,
    rig_config: RigConfig,
    dut_power: DutControl,
    bacnet: Bacnet,
    console: Console,
    bench: Bench,
    cycles: int,
) -> None:
    inst = bench.dut.bacnet_instance
    name, location = f"hil-pers-{int(time.time())}", "PERS-01 persistence"
    device = copy.deepcopy(rig_config.docs["device"])
    device["device"].update(name=name, location=location)
    points = [(p["type"], p["instance"], p["name"]) for p in rig_config.docs["io"]["points"]]
    since = console.mark()
    try:
        rig_config.apply(device=device)
        for cycle in range(cycles):
            report = dut_power.cycle(OFF_MS, timeout=IAM_LIMIT_S)
            assert report.back_s is not None and report.back_s <= IAM_LIMIT_S, (
                f"cycle {cycle}: no I-Am in 20 s"
            )
            assert bacnet.read(inst, "device", inst, "object-name") == f'"{name}"', (
                f"cycle {cycle}: Object_Name"
            )
            assert bacnet.read(inst, "device", inst, "location") == f'"{location}"', (
                f"cycle {cycle}: Location"
            )
            for obj_type, instance, point_name in points:
                got = bacnet.read(inst, obj_type, instance, "object-name")
                assert got == f'"{point_name}"', f"cycle {cycle}: {obj_type} {instance} is {got}"
            formatted = console.lines(since, pattern="formatting /lfs")
            assert not formatted, f"cycle {cycle}: /lfs was reformatted: {formatted[0].text}"
    finally:
        rig_config.apply()
