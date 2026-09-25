"""MSTP-04 DE lead, MSTP-05 Tpostdrive, MSTP-07 Tframe_gap: how the DUT drives each frame."""

from __future__ import annotations

import pytest

from hilrig import mstp
from mstp_bus import BusView

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only, pytest.mark.timing]

DE_ASSERT_BITS = 16 / 16  # de-assert-time = <16> in the HIL overlay (v1 design D7)


def test_de_lead(ring: BusView) -> None:
    """MSTP-04: DE never rises after the start bit; lead = 16/16 bit +- (1/16 bit + 2 samples)."""
    timing = mstp.de_timing(ring.de, ring.tx_octets, ring.limits)
    assert timing.lead.ok, str(timing.lead)
    assert not timing.tx_outside_de, f"sent with DE low: {timing.tx_outside_de[:5]}"
    assert not timing.idle_de, f"DE high without data: {timing.idle_de[:5]}"
    expected = DE_ASSERT_BITS * ring.limits.bit_s
    tolerance = ring.limits.bit_s / 16 + 2 / ring.trace.samplerate
    off = [s for s in timing.lead.samples if abs(s.value_s - expected) > tolerance]
    assert not off, f"lead not {expected * 1e6:.2f} us +- {tolerance * 1e6:.2f} us: {off[:5]}"


def test_postdrive(ring: BusView) -> None:
    """MSTP-05: 0 <= DE fall - end of the last stop bit <= Tpostdrive (15 bit times)."""
    timing = mstp.de_timing(ring.de, ring.tx_octets, ring.limits)
    assert timing.postdrive.ok, str(timing.postdrive)


def test_frame_gap(ring: BusView) -> None:
    """MSTP-07: no idle time above Tframe_gap (20 bit times) inside a DUT frame."""
    assert all(f.ok for f in ring.tx_frames), [str(f) for f in ring.tx_frames if not f.ok][:5]
    check = mstp.frame_gap(ring.tx_frames, ring.limits)
    assert check.ok, str(check)
