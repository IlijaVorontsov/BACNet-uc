"""R-04 Ethernet capture pipeline on the DUT's wire (rig gate; SIL and HIL).

Catalogue R-04: sentinels are present in the pcapng and absent from rows(). A known Who-Is
from sim1 is captured and decoded field by field. dumpcap reports 0 dropped packets.

The capture fixture only returns after a readiness sentinel was captured.
"""

from __future__ import annotations

import socket

import pytest

from hilrig import netns as nsmod
from hilrig.capture import SENTINEL_PORT, Capture, sentinel_count
from hilrig.netns import HOSTS, Topology

pytestmark = pytest.mark.rig(gate=True)

# BVLC Original-Broadcast-NPDU, NPDU (no routing), Who-Is for instances 4194000..4194001:
# nobody answers, so the capture holds exactly this request.
WHO_IS = bytes.fromhex("810b0010010010080b3ffed01b3ffed1")


@pytest.mark.capture("udp port 47808")
def test_r04_capture_pipeline(capture: Capture, netns: Topology) -> None:
    with nsmod.enter(netns.ns("sim1")):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((HOSTS["sim1"].ip, 47808))
        sock.sendto(WHO_IS, (nsmod.BROADCAST_A, 47808))
    capture.stop()
    assert sentinel_count(capture.path) >= 1, "the readiness barrier left no sentinel in the file"
    assert capture.rows(f"udp.dstport == {SENTINEL_PORT}") == [], "sentinels must be excluded from analysis"
    # ip.src: a BBMD on the wire may legitimately forward the broadcast to foreign devices
    rows = capture.rows(
        f"bacapp.unconfirmed_service == 8 && ip.src == {HOSTS['sim1'].ip}",
        "ip.src",
        "ip.dst",
        "bvlc.function",
        "bacapp.who_is.low_limit",
        "bacapp.who_is.high_limit",
    )
    assert [
        (
            r["ip.src"],
            r["ip.dst"],
            r["bvlc.function"],
            r["bacapp.who_is.low_limit"],
            r["bacapp.who_is.high_limit"],
        )
        for r in rows
    ] == [(HOSTS["sim1"].ip, nsmod.BROADCAST_A, "0x0b", "4194000", "4194001")]
    assert capture.dropped() == 0, f"dumpcap dropped packets (see {capture.log})"
