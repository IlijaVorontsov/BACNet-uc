"""NET-03 link flap (release; hardware, and SIL without the carrier criterion), per image.

Catalogue NET-03: 10 flaps of 5 s down, per image. BACnet image: per link-up DHCP within 5 s
and Device RP OK within 10 s; no reboot: SMP uc_node info uptime_s never goes back. MQTT image:
per link-up DHCP within 5 s and CONNACK within 30 s (functional); no reboot: telemetry uptime
never goes back. Test timeout 420 s.

The flap takes the host side of the DUT's cable down (hardware: the DUT NIC in lan-a; SIL: TAP
zeth); the capture runs on br-a, which stays up. In SIL the TAP has no carrier to lose, so the
native_sim DUT never sees the flap and does not ask for DHCP again: the per-link-up DHCP
criterion is hardware-only (R-06 shows the DUT sees carrier loss). An MQTT session that
survives a 5 s outage in SIL has nothing to reconnect; there the criterion is fresh telemetry
within 30 s instead of a new CONNACK.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from hilrig import netns as nsmod
from hilrig.bacnet import Bacnet
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.dutctl import DutControl, DutLink
from hilrig.mqtt import Device
from hilrig.netns import Topology
from hilrig.release import DutImage

pytestmark = pytest.mark.release

FLAPS, DOWN_S = 10, 5.0
DHCP_LIMIT_S, RP_LIMIT_S, CONNACK_LIMIT_S = 5.0, 10.0, 30.0


def poll(check: Callable[[], object], limit: float) -> float:
    """Seconds until ``check()`` returns without raising, within ``limit`` (else it re-raises)."""
    start = time.monotonic()
    while True:
        try:
            check()
            return time.monotonic() - start
        except Exception:
            if time.monotonic() - start > limit:
                raise
            time.sleep(0.2)


@pytest.mark.timeout(420)
@pytest.mark.capture("udp port 67 or udp port 68 or udp port 47808 or udp port 1337", iface="br-a")
@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_net03_link_flap(
    app_image: DutImage,
    dut_link: DutLink,
    dut_reset: DutControl,
    capture: Capture,
    netns: Topology,
    bench: Bench,
    request: pytest.FixtureRequest,
) -> None:
    dut_reset.ensure_online()
    hardware = bench.mode == "hil"
    lan_a, nic, inst = netns.ns("lan-a"), netns.capture_iface, bench.dut.bacnet_instance
    uptimes: list[float] = []
    if app_image.app == "bacnet":
        bacnet: Bacnet = request.getfixturevalue("bacnet")
        smp = request.getfixturevalue("smp")

        def uptime() -> float:
            return float(smp.node_info()["uptime_s"])
    else:
        mqtt: Device = request.getfixturevalue("mqtt")

        def uptime() -> float:
            return float(mqtt.telemetry(timeout=CONNACK_LIMIT_S, since=mqtt.client.mark())["uptime_s"])

    uptimes.append(uptime())
    for flap in range(FLAPS):
        nsmod.set_link(lan_a, nic, up=False)
        time.sleep(DOWN_S)
        mark = dut_link.mark()
        nsmod.set_link(lan_a, nic, up=True)
        if hardware:
            assert dut_link.wait_dhcp(mark, DHCP_LIMIT_S) <= DHCP_LIMIT_S, f"flap {flap}: no DHCP within 5 s"
        if app_image.app == "bacnet":
            took = poll(lambda: bacnet.read(inst, "device", inst, "object-name"), RP_LIMIT_S)
            assert took <= RP_LIMIT_S, f"flap {flap}: Device RP after {took:.1f} s"
        elif hardware:
            assert dut_link.wait_app(mark, CONNACK_LIMIT_S) <= CONNACK_LIMIT_S, (
                f"flap {flap}: no CONNACK in 30 s"
            )
        uptimes.append(uptime())  # MQTT: fresh telemetry within 30 s of link-up
    capture.stop()
    assert uptimes == sorted(uptimes), f"uptime went back (the DUT rebooted): {uptimes}"
