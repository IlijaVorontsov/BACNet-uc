"""TLS-01 server certificate negatives, TLS 1.3 and 1.2 (release; hardware and SIL).

Catalogue TLS-01: per version: rogue CA gives a DUT fatal alert 48 and a wrong name gives
alert 42 (decrypted with the server key log under 1.3). No application data from the DUT.
Next attempt >= 1 s later. No reboot. Expired: xfail(strict) until FW-12.

openssl s_server on 192.0.2.2:8883 (pinned version, key log) takes over broker.hil.lan (D33);
a reset makes the DUT's next attempt go there. Under TLS 1.3 the DUT's alert is encrypted
under the client handshake secret, which the s_server key log holds.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Literal

import pytest

from hilrig.bench import Bench
from hilrig.capture import Capture, connection_attempts
from hilrig.dutctl import DutControl
from hilrig.netns import SVC2_IP
from hilrig.pki import Pki
from hilrig.release import DutImage
from hilrig.services import TlsServer

pytestmark = pytest.mark.release

# Observe at least OBSERVE_S, then until the DUT refused twice (the gap between its attempts is
# a criterion) or finished a handshake (the expired case), at most OBSERVE_MAX_S: the first
# attempt follows boot and DHCP, and the back-off may have grown meanwhile (MQTT 75e0620 tried
# again only 19 s after its first refusal); it tops out at 60 s plus jitter.
OBSERVE_S, OBSERVE_MAX_S = 20.0, 90.0
BACKOFF_MIN_S = 1.0
FATAL = "2"
CASES = [
    pytest.param("rogue", 48, id="rogue-ca-48"),  # unknown_ca
    pytest.param("wrongname", 42, id="wrong-name-42"),  # bad_certificate (CN_MISMATCH wins)
    pytest.param(
        "expired",
        45,  # certificate_expired
        id="expired-45",
        marks=pytest.mark.xfail(
            strict=True, reason="FW-12: MBEDTLS_HAVE_TIME_DATE is off, dates are not checked"
        ),
    ),
]


@pytest.mark.capture("tcp port 8883")
@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
@pytest.mark.parametrize("version", ["1.3", "1.2"])
@pytest.mark.parametrize(("case", "alert"), CASES)
def test_tls01_server_certificate_negatives(
    app_image: DutImage,
    version: Literal["1.2", "1.3"],
    case: str,
    alert: int,
    capture: Capture,
    tls_server: Callable[..., TlsServer],
    pki: Pki,
    dut_reset: DutControl,
    bench: Bench,
) -> None:
    server = tls_server(pki.servers[case], version)
    dut_reset.reset(wait=False)
    rebooted = dut_reset.reboot_mark()  # after the rig's own reset
    start = time.monotonic()
    while (elapsed := time.monotonic() - start) < OBSERVE_MAX_S:
        refused, completed = server.handshakes()
        if elapsed >= OBSERVE_S and (refused >= 2 or completed):
            break
        time.sleep(0.5)
    capture.stop()
    dut = bench.dut.ip
    alerts = capture.rows(
        f"tls.alert_message && ip.src == {dut} && ip.dst == {SVC2_IP}",
        "tls.alert_message.level",
        "tls.alert_message.desc",
    )
    assert alerts, f"no alert from the DUT to {SVC2_IP} (TLS {version}, {case})"
    seen = [(a["tls.alert_message.level"], a["tls.alert_message.desc"]) for a in alerts]
    assert all(x == (FATAL, str(alert)) for x in seen), f"alerts (level, desc) {seen}, expected fatal {alert}"
    assert not capture.rows(f"tls.app_data && ip.src == {dut}"), "the DUT sent application data"
    syn = f"tcp.flags.syn == 1 && tcp.flags.ack == 0 && ip.src == {dut} && ip.dst == {SVC2_IP}"
    syns = connection_attempts(capture.rows(syn, "tcp.srcport"))  # a resent SYN is the same attempt
    assert len(syns) >= 2, f"{len(syns)} attempt(s) in {elapsed:.0f} s"
    for a in alerts:
        later = [t for t in syns if t > a.t]
        if later:
            assert later[0] - a.t >= BACKOFF_MIN_S, f"next attempt {later[0] - a.t:.2f} s after the alert"
    assert not dut_reset.rebooted_since(rebooted), "the DUT rebooted"
