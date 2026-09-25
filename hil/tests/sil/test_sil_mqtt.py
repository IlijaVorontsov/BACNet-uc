"""MQTT over TLS through the rig with the test PKI (SIL self-validation).

An MQTT stand-in (fake_mqtt_dut.py) in netns dut plays the DUT: it trusts the test CA and
presents the committed DUT client certificate, as an HIL image of apps/mqtt_tls does.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from fake_mqtt_dut import FakeMqttDut
from hilrig.bacnet import BacServ
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.mqtt import Device, TlsOptions
from hilrig.netns import HOSTS, Topology
from hilrig.pki import Pki
from hilrig.services import RigServices

TLS13 = "0x0304"
SERVER_HELLO = "2"


@contextlib.contextmanager
def mqtt_standin(bench: Bench, netns: Topology, services: RigServices, pki: Pki) -> Iterator[FakeMqttDut]:
    assert bench.dut.mqtt_client_id
    tls = TlsOptions(pki.ca, pki.dut.cert, pki.dut.key)
    dut = FakeMqttDut(
        HOSTS["svc"].ip, services.broker.port, device_id=bench.dut.mqtt_client_id, ns=netns.ns("dut"), tls=tls
    ).start()
    try:
        yield dut
    finally:
        dut.stop()


@pytest.mark.capture("tcp port 8883")
def test_mqtt_tls_round_trip(
    standin: BacServ,
    capture: Capture,
    services: RigServices,
    mqtt: Device,
    bench: Bench,
    netns: Topology,
    pki: Pki,
) -> None:
    with mqtt_standin(bench, netns, services, pki):
        mqtt.wait_status("online", timeout=10)
        assert mqtt.info()["caps"]["cmds"] == ["ping", "led", "identify"]
        reply = mqtt.command("led", "on", request_id="sil-1")
        assert reply == {"id": "sil-1", "ok": True, "led": True}
        assert mqtt.command_text("ping")["ok"] is True
    capture.stop()
    hello = capture.rows(
        f"tls.handshake.type == {SERVER_HELLO} && ip.src == {HOSTS['svc'].ip} && ip.dst == {bench.dut.ip}",
        "tls.handshake.extensions.supported_version",
    )
    assert [h["tls.handshake.extensions.supported_version"] for h in hello] == [TLS13]


@pytest.mark.capture("tcp port 8883")
def test_broker_keylog_decrypts_the_dut_session(
    standin: BacServ,
    capture: Capture,
    services: RigServices,
    mqtt: Device,
    bench: Bench,
    netns: Topology,
    pki: Pki,
) -> None:
    if services.broker.keylog is None:
        pytest.skip(services.broker.keylog_status)
    with mqtt_standin(bench, netns, services, pki):
        mqtt.command("identify", "3", request_id="sil-2")
    capture.stop()  # injects the broker's key log into the pcapng
    event = f"bacnet-uc/{bench.dut.mqtt_client_id}/event"
    connects = capture.rows(f"mqtt.msgtype == 1 && ip.src == {bench.dut.ip}", "mqtt.clientid")
    assert [c["mqtt.clientid"] for c in connects] == [bench.dut.mqtt_client_id]
    assert capture.rows(f'mqtt.msgtype == 3 && mqtt.topic == "{event}" && ip.src == {bench.dut.ip}')
