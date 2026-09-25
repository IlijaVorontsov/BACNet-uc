"""MSTP-08 Tusage_delay and token passing: the DUT uses every token and poll addressed to it
within 15 ms, answers polls, and passes the token on to the host node."""

from __future__ import annotations

import pytest

from hilrig import mstp
from hilrig.mstp import FrameType
from mstp_bus import DUT_MAC, HOST_MAC, BusView

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only, pytest.mark.timing]


def test_usage_delay(ring: BusView) -> None:
    """Token or Poll-For-Master to the DUT -> its first octet <= Tusage_delay; p99 reported."""
    check = mstp.usage_delay(ring.rx_frames, ring.tx_frames, DUT_MAC, ring.limits)
    assert check.ok, str(check)
    print(f"Tusage_delay p99 = {check.percentile(99) * 1e3:.3f} ms over {len(check.samples)} tokens/polls")


def test_token_reaches_host(ring: BusView) -> None:
    """The DUT found the host by polling, answered its polls and hands it the token."""
    assert any(f.ftype == FrameType.TOKEN and f.dst == HOST_MAC for f in ring.tx_frames), (
        "no token to the host"
    )
    unanswered = [
        str(request)
        for request, answer in mstp.first_responses(ring.rx_frames, ring.tx_frames)
        if request.ok and request.ftype == FrameType.POLL_FOR_MASTER and request.dst == DUT_MAC
        and (answer is None or answer.ftype != FrameType.REPLY_TO_POLL_FOR_MASTER)
    ]  # fmt: skip
    assert not unanswered, unanswered[:5]
