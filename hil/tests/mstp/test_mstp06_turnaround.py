"""MSTP-06 Tturnaround: the DUT waits 40 bit times after another node's last octet before it
enables its driver."""

from __future__ import annotations

import pytest

from hilrig import mstp
from mstp_bus import BusView

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only, pytest.mark.timing]


def test_turnaround(ring: BusView) -> None:
    """Every DUT DE rise is >= 40 bit times after the end of the last foreign stop bit."""
    check = mstp.turnaround(ring.de, ring.bus_octets, ring.limits)
    assert check.ok, str(check)
    # the ring alternates between host and DUT, so most DE rises follow a host frame
    assert len(check.samples) >= len(ring.tx_frames) // 2, str(check)
