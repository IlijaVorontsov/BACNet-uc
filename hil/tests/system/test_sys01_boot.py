"""SYS-01 flash and boot to network (release; hardware), on the session start's flash.

Catalogue SYS-01: flash exits 0 within 120 s. First DHCPDISCOVER from the bench MAC within
10 s of reset release. Lease 192.0.2.10. BACnet image: I-Am from 192.0.2.10:47808 with the
bench instance within 15 s. MQTT image: CONNACK 0 within 20 s (functional deadlines).
Instrumented variant: HIL-BOOT, then HIL-READY ip=192.0.2.10 with the bench MAC.

The session start (conftest ``hil_session``) flashes inside a capture, because in Twister
mode the ``dut`` fixture's flash is the only flash (D31). Reset release is when the flash
returned (OpenOCD ends with ``reset run``).
"""

from __future__ import annotations

import pytest

from hilrig.bench import Bench
from hilrig.capture import rows_of
from hilrig.console import Console
from hilrig.dutctl import SessionStart
from hilrig.release import DutImage

pytestmark = [pytest.mark.release, pytest.mark.hil_only]

FLASH_LIMIT_S, DHCP_LIMIT_S, IAM_LIMIT_S, CONNACK_LIMIT_S = 120.0, 10.0, 15.0, 20.0
DHCP_DISCOVER, DHCP_ACK = 1, 5


@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_sys01_flash_and_boot_to_network(
    app_image: DutImage, hil_session: SessionStart | None, bench: Bench, request: pytest.FixtureRequest
) -> None:
    start = hil_session
    if start is None or start.flash is None or start.flash_end is None:
        pytest.skip("no flash in this session: give --dut-build (or run under Twister)")
    flash, end, pcap = start.flash, start.flash_end, start.capture
    assert flash.returncode == 0 and flash.seconds <= FLASH_LIMIT_S, (
        f"flash rc {flash.returncode}, {flash.seconds:.0f} s"
    )
    assert pcap is not None, f"the flash was not captured: {start.notes}"
    mac, ip = bench.dut.mac, bench.dut.ip
    discover = rows_of(pcap, f"eth.src == {mac} && dhcp.option.dhcp == {DHCP_DISCOVER}")
    discover = [r for r in discover if r.t >= end - 1.0]
    assert discover and discover[0].t - end <= DHCP_LIMIT_S, "no DHCPDISCOVER within 10 s of reset release"
    acks = rows_of(pcap, f"dhcp.option.dhcp == {DHCP_ACK} && dhcp.hw.mac_addr == {mac}", "dhcp.ip.your")
    assert [r["dhcp.ip.your"] for r in acks if r.t >= end][:1] == [ip], "no lease of 192.0.2.10"
    if app_image.app == "bacnet":
        iam = rows_of(
            pcap,
            f"bacapp.unconfirmed_service == 0 && ip.src == {ip} && udp.srcport == 47808 "
            f"&& bacapp.instance_number == {bench.dut.bacnet_instance}",
        )
        iam = [r for r in iam if r.t >= end]
        assert iam and iam[0].t - end <= IAM_LIMIT_S, "no I-Am with the bench instance within 15 s"
    else:
        connack = rows_of(pcap, f"mqtt.msgtype == 2 && ip.dst == {ip}", "mqtt.conack.val")
        if connack:  # decrypted with the broker key log
            assert connack[0]["mqtt.conack.val"] == "0" and connack[0].t - end <= CONNACK_LIMIT_S
        else:  # no key log: 'online' is published only after CONNACK 0 and SUBACK
            assert start.online_s is not None and start.online_s <= CONNACK_LIMIT_S, (
                f"no 'online' within 20 s of reset release ({start.online_s})"
            )
    if app_image.instrumented:
        console: Console = request.getfixturevalue("console")
        since = start.console_mark
        console.wait_for(r"HIL-BOOT board=", 1.0, since)
        _, ready = console.wait_for(rf"HIL-READY ip={ip} mac=(\S+)", 1.0, since)
        assert ready[1].lower() == mac
