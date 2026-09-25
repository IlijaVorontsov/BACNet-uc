"""MSTP-01 idle bias: with the bias network on and both terminators fitted, the idle bus sits at
>= +200 mV differential (510/510 ohm from 5 V: about 278 mV, v1 design 4.1).

D+ and D- are read through dividers by stimulus ADC channels. These taps are It2 hardware; the
test skips until bench.yml lists them among the stimulus channels.
"""

from __future__ import annotations

import pytest

from hilrig.bench import Bench
from hilrig.stim import Stim

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only]

TAPS = ("rs485_a", "rs485_b")  # stimulus ADC channels on D+ and D-
DIVIDER = 2.0  # bus volts per ADC volt on each tap
MIN_BIAS_MV = 200.0


def test_idle_bias(bench: Bench, stim: Stim) -> None:
    if bench.mstp is None or bench.stim is None or not set(TAPS) <= set(bench.stim.chans):
        pytest.skip(f"no MS/TP bus or no bus taps {TAPS} on this bench")
    a, b = (stim.adc(tap, 64).mv * DIVIDER for tap in TAPS)
    assert a - b >= MIN_BIAS_MV, f"idle differential {a - b:.0f} mV (D+ {a:.0f} mV, D- {b:.0f} mV)"
