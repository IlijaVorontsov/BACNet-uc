"""MSTP-11 Tno_token and Tslot: after a lost token the DUT (TS = its MAC) claims the bus with a
Poll-For-Master after 500 + 10*TS .. 500 + 10*(TS+1) ms of silence."""

from __future__ import annotations

import pytest

from hilrig import mstp
from hilrig.mstp import FrameType
from hilrig.stim import Stim
from mstp_bus import DUT_MAC, CaptureBus, HostNode

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only, pytest.mark.timing]

BUSY_S = 2.0  # longer than the DUT's boot, so it is IDLE on the bus when the silence starts
TOLERANCE_S = 1e-3  # catalogue: +-1 ms


def test_first_pfm_after_silence(capture_bus: CaptureBus, stim: Stim, host_node: HostNode) -> None:
    """Reset the DUT, keep the bus busy with tokens to an absent station, then go silent."""

    def lose_token() -> None:
        stim.reset()
        host_node.keep_bus_busy(BUSY_S)

    view = capture_bus(BUSY_S + 2.0, lose_token)
    assert view.tx_frames, "the DUT never claimed the token"
    claim = view.tx_frames[0]
    assert claim.ok and claim.ftype == FrameType.POLL_FOR_MASTER and claim.dst == DUT_MAC + 1, str(claim)
    check = mstp.no_token_window(view.bus_octets, view.tx_frames, DUT_MAC, view.limits, TOLERANCE_S)
    assert len(check.samples) == 1 and check.ok, str(check)
