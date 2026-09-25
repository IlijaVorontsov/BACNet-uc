"""R-02b DUT-side wiring walk over SMP (rig gate; BACnet sessions, valid on release images).

Catalogue R-02b: every wired di line: stim dout 0/1/z gives the expected SMP uc_io read value
at both levels. Every do line: SMP uc_io force 0/1 gives the matching stim din, then release.
ai0/ai3: stim dac 500/2500 mV read by uc_io read within +-50 mV; ai1/ai2/ai4/ai5 dividers
within +-5 %. Each line changes only its own channel: no stuck-at, short or swap. MQTT
sessions accept a pass less than 24 h old. Any failure is a rig fault.

uc_io values are logical (devicetree polarity applied): an ACTIVE_LOW di reads 1 when the
stimulus drives 0. Hi-Z leaves the DUT's pull, which reads inactive (0) on every di.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hilrig.bench import Bench, CatalogChannel, logical
from hilrig.release import DutImage
from hilrig.smp import Smp
from hilrig.stim import Stim

pytestmark = [pytest.mark.rig(gate=True), pytest.mark.hil_only]

SETTLE_S = 0.3  # > sample_ms 100 + debounce_ms 20 (docs/io.md) with margin
DIVIDERS_MV = {"ai1": 1650.0, "ai2": 1100.0, "ai4": 2200.0, "ai5": 767.0}
MAX_AGE_S = 24 * 3600


def state_file(bench: Bench, artifacts: Path) -> Path:
    """R-02b result shared with the bench's MQTT sessions: next to bench.yml (HIL.md 9)."""
    return (bench.path.parent if bench.path else artifacts) / "r02b-state.json"


def wired(bench: Bench, kind: str) -> list[CatalogChannel]:
    assert bench.stim is not None
    return [c for c in bench.catalog if c.kind == kind and not c.hil_io and c.name in bench.stim.chans]


def expected_di(chan: CatalogChannel, level: int | str) -> int:
    return 0 if level == "z" else logical(chan, int(level))


@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_r02b_dut_wiring(
    app_image: DutImage, request: pytest.FixtureRequest, bench: Bench, stim: Stim, artifacts: Path
) -> None:
    state = state_file(bench, artifacts)
    if app_image.app == "mqtt":  # no SMP on the MQTT image: accept a recent BACnet-session pass
        assert state.exists(), f"no R-02b pass recorded for this bench ({state}): run a BACnet session first"
        age = time.time() - json.loads(state.read_text())["passed_at"]
        assert age < MAX_AGE_S, f"last R-02b pass is {age / 3600:.1f} h old (limit 24 h)"
        return
    smp: Smp = request.getfixturevalue("smp")
    request.getfixturevalue("rig_config")
    walk_di(smp, stim, wired(bench, "di"))
    walk_do(smp, stim, wired(bench, "do"))
    walk_ai(smp, stim, bench)
    state.write_text(json.dumps({"passed_at": time.time(), "bench": bench.name}) + "\n")


def walk_di(smp: Smp, stim: Stim, chans: list[CatalogChannel]) -> None:
    names = [c.name for c in chans]
    for chan in chans:
        for level in (0, 1, "z"):
            stim.dout(chan.name, level)
            time.sleep(SETTLE_S)
            values = smp.io_read()
            want = dict.fromkeys(names, 0) | {chan.name: expected_di(chan, level)}
            got = {n: int(values[n]) for n in names}
            assert got == want, f"stim dout {chan.name} {level}: uc_io {got}, expected {want}"


def walk_do(smp: Smp, stim: Stim, chans: list[CatalogChannel]) -> None:
    baseline = {c.name: stim.din(c.name) for c in chans}
    for chan in chans:
        try:
            for value in (0, 1):
                smp.io_force(chan.name, value)
                time.sleep(SETTLE_S)
                got = {c.name: stim.din(c.name) for c in chans}
                want = baseline | {chan.name: logical(chan, value)}
                assert got == want, f"uc_io force {chan.name} {value}: stim din {got}, expected {want}"
        finally:
            smp.io_release(chan.name)
        time.sleep(SETTLE_S)
        baseline[chan.name] = stim.din(chan.name)


def walk_ai(smp: Smp, stim: Stim, bench: Bench) -> None:
    assert bench.stim is not None
    for chan, mv in DIVIDERS_MV.items():
        if chan in bench.stim.chans:
            read = smp.io_read(chan)[chan]
            assert abs(read - mv) <= 0.05 * mv, (
                f"{chan} divider: uc_io {read:.0f} mV, expected {mv:.0f} +-5 %"
            )
    sources = [c for c in ("ai0", "ai3") if c in bench.stim.chans]
    try:
        for chan in sources:
            others = {o: smp.io_read(o)[o] for o in sources if o != chan}
            for mv in (500, 2500):
                stim.dac(chan, mv)
                time.sleep(SETTLE_S)
                node = stim.adc(chan, 64).mv
                read = smp.io_read(chan)[chan]
                assert abs(read - node) <= 50, f"{chan}: dac {mv} mV, node {node:.0f} mV, uc_io {read:.0f} mV"
                for other, before in others.items():
                    after = smp.io_read(other)[other]
                    assert abs(after - before) <= 50, f"{chan} moved {other}: {before:.0f} -> {after:.0f} mV"
            stim.dac(chan, "z")
    finally:
        for chan in sources:
            stim.dac(chan, "z")
