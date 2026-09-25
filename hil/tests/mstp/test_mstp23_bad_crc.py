"""MSTP-23 bad-CRC handling: the DUT ignores frames with a bad header or data CRC and still
answers the next valid poll within Tusage_delay.

The host node injects the frames while it holds the token, so they never collide with the DUT.
The DUT's bad_crc / receive_invalid counters are checked once the firmware exposes MS/TP
statistics; until then the evidence is the bus itself.
"""

from __future__ import annotations

import pytest

from hilrig import mstp
from hilrig.mstp import FrameType, RxError
from mstp_bus import DUT_MAC, HOST_MAC, CaptureBus, HostNode

pytestmark = [pytest.mark.mstp, pytest.mark.hil_only, pytest.mark.timing]

PER_TOKEN = 4  # bad frames of each kind per token hold (each resets the DUT's silence timer)
TOKENS = 5
LISTEN_S = 0.030  # > Tusage_delay: time for a (wrong) answer to show up


def test_bad_crc_frames_ignored(capture_bus: CaptureBus, host_node: HostNode) -> None:
    holds = 0

    def inject(node: HostNode) -> None:
        nonlocal holds
        if holds == TOKENS:
            return
        holds += 1
        for _ in range(PER_TOKEN):
            node.send(mstp.build_frame(FrameType.POLL_FOR_MASTER, DUT_MAC, HOST_MAC, bad_header_crc=True))
            node.listen(LISTEN_S)
            node.send(
                mstp.build_frame(
                    FrameType.TEST_REQUEST, DUT_MAC, HOST_MAC, bytes(range(32)), bad_data_crc=True
                )
            )
            node.listen(LISTEN_S)
        node.send(mstp.build_frame(FrameType.POLL_FOR_MASTER, DUT_MAC, HOST_MAC))
        node.listen(LISTEN_S)

    duration = TOKENS * (2 * PER_TOKEN + 1) * LISTEN_S + 2.0
    view = capture_bus(duration + 1.0, lambda: host_node.serve(duration, on_token=inject))
    bad = [f for f in view.rx_frames if f.error in (RxError.BAD_HEADER_CRC, RxError.BAD_DATA_CRC)]
    assert len(bad) == 2 * PER_TOKEN * TOKENS, f"LA saw {len(bad)} bad frames"
    answered = [
        (str(request), str(answer))
        for request, answer in mstp.first_responses(view.rx_frames, view.tx_frames)
        if request.error and answer is not None
    ]
    assert not answered, answered[:5]
    usage = mstp.usage_delay(view.rx_frames, view.tx_frames, DUT_MAC, view.limits)
    assert usage.ok, str(usage)
