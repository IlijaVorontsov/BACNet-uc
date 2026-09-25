"""R-02a stimulus-side wiring and LA map (rig gate, every session, any image).

Catalogue R-02a: with the DUT powered (any image): stim adc reads the ai1/ai2/ai4/ai5
dividers at 1650/1100/2200/767 mV +-5 %. stim dac ai0/ai3 500 and 2500 mV read back by stim
adc on the same node within +-50 mV. 'stim lat sync 1 lb 1 10' succeeds (loopback). Each
stimulus-driven LA signal (di1, sync) toggles on its bench.yml channel and nowhere else. The
FX2 60 s capture has the expected sample count. Any failure marks the session as a rig fault.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from hilrig.bench import Bench
from hilrig.la import LogicAnalyzer
from hilrig.stim import Stim

pytestmark = [pytest.mark.rig(gate=True), pytest.mark.hil_only]

# docs/hil/pin-tables.md 1.1: distinct divider values, so swapped wiring shows up
DIVIDERS_MV = {"ai1": 1650.0, "ai2": 1100.0, "ai4": 2200.0, "ai5": 767.0}
DAC_NODES = ("ai0", "ai3")
DI1_PULSE_US, SYNC_PULSE_US = 100_000, 50_000
COINCIDENT_S = 1e-3  # "nowhere else": no other channel toggles within 1 ms of these edges


def wired(bench: Bench, *chans: str) -> list[str]:
    assert bench.stim is not None
    missing = [c for c in chans if c not in bench.stim.chans]
    if missing:
        pytest.skip(f"bench.yml stim.chans lacks {missing}")
    return list(chans)


def test_r02a_divider_levels(stim: Stim, bench: Bench) -> None:
    for chan in wired(bench, *DIVIDERS_MV):
        mv = stim.adc(chan, 64).mv
        assert abs(mv - DIVIDERS_MV[chan]) <= 0.05 * DIVIDERS_MV[chan], f"{chan}: {mv:.0f} mV"


def test_r02a_dac_read_back_on_the_same_node(stim: Stim, bench: Bench) -> None:
    for chan in wired(bench, *DAC_NODES):
        for mv in (500, 2500):
            stim.dac(chan, mv)
            read = stim.adc(chan, 64).mv
            assert abs(read - mv) <= 50, f"{chan}: dac {mv} mV, adc {read:.0f} mV"
        stim.dac(chan, "z")


def test_r02a_sync_loopback(stim: Stim, bench: Bench) -> None:
    wired(bench, "sync", "lb")
    assert stim.lat("sync", 1, "lb", 1, 10).lat_ns > 0  # raises StimTimeout without the jumper
    stim.dout("sync", 0)


def test_r02a_la_signals_on_their_channels(
    stim: Stim, bench: Bench, la: LogicAnalyzer, artifacts: Path
) -> None:
    assert bench.la is not None
    names = wired(bench, "di1", "sync")
    missing = [n for n in names if n not in bench.la.channels]
    if missing:
        pytest.skip(f"bench.yml la.channels lacks {missing}")
    with la.acquire(3.0, artifacts / "la" / "R-02a") as acq:
        time.sleep(0.8)
        stim.pulse("di1", DI1_PULSE_US, active=0)  # di1 idles Hi-Z (DUT pull-up): a low pulse
        time.sleep(0.9)
        stim.pulse("sync", SYNC_PULSE_US, active=1)
    trace = acq.trace
    assert trace is not None
    di1, sync = trace["di1"].toggles, trace["sync"].toggles
    assert len(di1) == 2 and abs((di1[1] - di1[0]) - DI1_PULSE_US / 1e6) < 2e-3, f"di1 toggles {di1}"
    assert len(sync) == 2 and abs((sync[1] - sync[0]) - SYNC_PULSE_US / 1e6) < 2e-3, f"sync toggles {sync}"
    assert di1[0] < sync[0], "di1 and sync channels swapped"
    edges = np.concatenate([di1, sync])
    for name, signal in trace.signals.items():
        if name in names:
            continue
        near = [t for t in signal.toggles if np.min(np.abs(edges - t)) < COINCIDENT_S]
        assert not near, f"LA channel {name} toggles with a stimulus edge at {near}: miswired probe"


def test_r02a_fx2_sample_count(bench: Bench, la: LogicAnalyzer, artifacts: Path) -> None:
    """A 60 s capture must hold 60 s of samples: an FX2 on a shared hub drops them."""
    with la.acquire(60.0, artifacts / "la" / "R-02a-60s") as acq:
        pass
    trace = acq.trace
    assert trace is not None
    count = round(trace.duration_s * trace.samplerate)
    expected = 60 * trace.samplerate
    # sigrok stops after the requested samples; the last USB transfer may add up to 0.1 %
    assert expected <= count <= expected * 1.001, f"{count} samples, expected {expected}"
