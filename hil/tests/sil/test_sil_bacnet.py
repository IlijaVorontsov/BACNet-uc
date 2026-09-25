"""BACnet/IP through the rig against the bacserv stand-in DUT (SIL self-validation)."""

from __future__ import annotations

import pytest

from hilrig.bacnet import Bacnet, BacnetError, BacServ
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.netns import HOSTS, Topology

WHO_IS, I_AM = "8", "0"
COMPLEX_ACK, ERROR_PDU = "3", "5"
READ_PROPERTY = "12"
UNKNOWN_OBJECT = "31"


@pytest.mark.capture("udp port 47808")
def test_who_is_answered_within_500_ms(
    standin: BacServ, bacnet: Bacnet, capture: Capture, bench: Bench
) -> None:
    instance, dut = bench.dut.bacnet_instance, bench.dut.ip
    assert [d.instance for d in bacnet.whois(instance, instance, wait_ms=1000)] == [instance]
    capture.stop()
    who = capture.rows(f"bacapp.unconfirmed_service == {WHO_IS} && ip.src == {HOSTS['svc'].ip}", "ip.dst")
    iam = capture.rows(
        f"bacapp.unconfirmed_service == {I_AM} && ip.src == {dut} && bacapp.instance_number == {instance}",
        "bvlc.function",
    )
    assert who and iam, "no Who-Is/I-Am pair on the DUT's wire"
    latency_ms = (iam[0].t - who[0].t) * 1000
    assert 0 < latency_ms < 500, f"I-Am after {latency_ms:.1f} ms"
    assert iam[0]["bvlc.function"] in ("0x0a", "0x0b")  # unicast or broadcast I-Am


@pytest.mark.capture("udp port 47808")
def test_read_property_of_the_device_object(
    standin: BacServ, bacnet: Bacnet, capture: Capture, bench: Bench
) -> None:
    instance = bench.dut.bacnet_instance
    assert bacnet.read(instance, "device", instance, "object-name") == f'"{standin.name}"'
    assert bacnet.read(instance, "device", instance, "object-identifier") == f"(device, {instance})"
    capture.stop()
    acks = capture.rows(
        f"bacapp.type == {COMPLEX_ACK} && ip.src == {bench.dut.ip}", "bacapp.confirmed_service"
    )
    assert acks and all(a["bacapp.confirmed_service"] == READ_PROPERTY for a in acks)


@pytest.mark.capture("udp port 47808")
def test_unknown_object_returns_error(
    standin: BacServ, bacnet: Bacnet, capture: Capture, bench: Bench
) -> None:
    instance = bench.dut.bacnet_instance
    with pytest.raises(BacnetError) as err:
        bacnet.read(instance, "analog-input", 4194302, "present-value")
    assert (err.value.error_class, err.value.error_code) == ("object", "unknown-object")
    capture.stop()
    errors = capture.rows(f"bacapp.type == {ERROR_PDU} && ip.src == {bench.dut.ip}", "bacapp.error_code")
    assert [e["bacapp.error_code"] for e in errors] == [UNKNOWN_OBJECT]


@pytest.mark.capture("udp port 47808")
def test_foreign_device_registration_from_subnet_b(
    standin: BacServ, bacnet: Bacnet, capture: Capture, bench: Bench, netns: Topology
) -> None:
    instance, dut, fd_ip = bench.dut.bacnet_instance, bench.dut.ip, HOSTS["fd"].ip
    fd = Bacnet(netns.ns("fd"), "fd0", tools=bacnet.tools)
    assert fd.whois(instance, instance, wait_ms=500) == [], "a broadcast crossed rtr"
    registered = fd.foreign(dut, ttl_s=60)
    assert [d.instance for d in registered.whois(instance, instance, wait_ms=1500)] == [instance]
    assert [(e.ip, e.ttl_s) for e in bacnet.read_fdt(dut)] == [(fd_ip, 60)]
    capture.stop()
    registration = capture.rows(f"bvlc.function == 0x05 && ip.src == {fd_ip}", "bvlc.reg_ttl")
    assert registration and registration[0]["bvlc.reg_ttl"] == "60"
    results = capture.rows(f"bvlc.function == 0x00 && ip.src == {dut} && ip.dst == {fd_ip}", "bvlc.result")
    assert results and results[0]["bvlc.result"] == "0x0000"
    forwarded = capture.rows(f"bvlc.function == 0x04 && bvlc.fwd_ip == {fd_ip}", "ip.src")
    assert forwarded and forwarded[0]["ip.src"] == dut, "the foreign device's Who-Is was not re-broadcast"
