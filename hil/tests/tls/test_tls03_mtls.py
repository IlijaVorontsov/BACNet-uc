"""TLS-03 mutual TLS matrix (release; hardware and SIL), per TLS version.

Catalogue TLS-03: mTLS image with a valid client cert: CONNACK 0. mTLS image with a rogue
client cert, and the plain image (no client cert): no session (TLS 1.2 handshake failure, or
TLS 1.3 alert on first read with no CONNACK). The DUT backs off (>= 1 s), does not reboot, and
recovers with a valid broker config. Both failure forms are accepted.

The refusing broker is the TLS front on 192.0.2.2 (D33) with ``client_ca``: the test CA
accepts the committed DUT certificate; the rogue CA makes that same certificate a rogue one
from the broker's point of view (one mTLS image serves both cases). Recovery: the front then
accepts again, and the DUT's own back-off brings it back (no reset).
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from typing import Literal

import pytest

from hilrig.bench import Bench
from hilrig.capture import Capture, connection_attempts
from hilrig.dutctl import DutControl, DutLink
from hilrig.netns import SVC2_IP
from hilrig.pki import Pki
from hilrig.release import DutImage
from hilrig.services import TlsFront

pytestmark = pytest.mark.release

CONNACK_LIMIT_S = 20.0
REFUSED_S = 20.0  # observe the refused attempts this long
RECOVER_S = 90.0  # the DUT's back-off tops out at 60 s (+ jitter)
BACKOFF_MIN_S = 1.0


def attempts(capture: Capture, dut: str) -> list[float]:
    """Connection attempts to the front: the first SYN of each source port (resent SYNs aside)."""
    syn = f"tcp.flags.syn == 1 && tcp.flags.ack == 0 && ip.src == {dut} && ip.dst == {SVC2_IP}"
    return connection_attempts(capture.rows(syn, "tcp.srcport"))


@pytest.mark.capture("tcp port 8883")
@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
@pytest.mark.parametrize("version", ["1.3", "1.2"])
@pytest.mark.parametrize("case", ["mtls-valid", "mtls-rogue", "plain-nocert"])
def test_tls03_mutual_tls_matrix(
    app_image: DutImage,
    version: Literal["1.2", "1.3"],
    case: str,
    capture: Capture,
    tls_front: Callable[..., TlsFront],
    pki: Pki,
    dut_reset: DutControl,
    dut_link: DutLink,
    bench: Bench,
) -> None:
    image_kind = "mtls" if app_image.mtls else "plain"
    if not case.startswith(image_kind):
        pytest.skip(f"{case} needs the {case.split('-')[0]} image; the DUT runs the {image_kind} image")
    dut = bench.dut.ip
    if case == "mtls-valid":
        tls_front(version, client_ca=pki.ca)
        report = dut_reset.reset(timeout=CONNACK_LIMIT_S)
        assert report.back is not None and report.back.app_s <= CONNACK_LIMIT_S
        capture.stop()
        connack = capture.rows(f"mqtt.msgtype == 2 && ip.dst == {dut}", "mqtt.conack.val")
        assert connack and connack[-1]["mqtt.conack.val"] == "0"
        return
    front = tls_front(version, client_ca=pki.rogue_ca if case == "mtls-rogue" else pki.ca)
    dut_reset.reset(wait=False)
    rebooted = dut_reset.reboot_mark()  # after the rig's own reset
    time.sleep(REFUSED_S)
    refused = list(front.sessions)
    assert refused and all(s.error for s in refused), f"a session was accepted: {refused}"
    mark = dut_link.mark()
    front.set_client_ca(pki.ca if app_image.mtls else None)  # a valid broker configuration again
    recovered = dut_link.wait_app(mark, RECOVER_S)
    capture.stop()
    assert recovered <= RECOVER_S
    refused_until = mark.t
    connacks = [
        r
        for r in capture.rows(f"mqtt.msgtype == 2 && ip.dst == {dut}", "mqtt.conack.val")
        if r.t < refused_until
    ]
    assert not connacks, "CONNACK while the broker refused the client"
    syns = [t for t in attempts(capture, dut) if t < refused_until]
    assert len(syns) >= 2, f"{len(syns)} attempt(s) while refused"
    gaps = [b - a for a, b in itertools.pairwise(syns)]
    assert all(g >= BACKOFF_MIN_S for g in gaps), f"attempts closer than 1 s: {gaps}"
    assert not dut_reset.rebooted_since(rebooted), "the DUT rebooted"
