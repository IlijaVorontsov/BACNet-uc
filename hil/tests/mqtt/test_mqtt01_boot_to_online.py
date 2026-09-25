"""MQTT-01 boot to online (release; hardware and SIL), TLS 1.3 on mosquitto, TLS 1.2 via the front.

Catalogue MQTT-01: CONNACK rc 0 within 20 s of reset release. Decrypted CONNECT: keepalive 60,
clean session, client id = bench mqtt_client_id (z + base32 UID), will on
bacnet-uc/<id>/status. Retained 'online' status. Retained info has hwid/mac equal to the bench
values. ClientHello: supported_versions contains 1.3 and 1.2, SNI broker.hil.lan, suites only
TLS_AES_128/256_GCM and ECDHE-ECDSA/RSA-AES-GCM. TLS 1.3 negotiated on mosquitto; TLS 1.2 via
the front with broker.hil.lan switched to .2.

In SIL "reset release" is the native_sim process start. The 1.3 half needs the broker's key
log (mosquitto >= 2.1, the docker image) to decrypt the CONNECT; the 1.2 half always has the
front's.
"""

from __future__ import annotations

from typing import Literal

import pytest

from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.dutctl import DutControl
from hilrig.mqtt import Device, MqttClient
from hilrig.netns import SVC2_IP, Topology
from hilrig.release import DutImage
from hilrig.services import BROKER_NAME, RigServices

pytestmark = pytest.mark.release

CONNACK_LIMIT_S = 20.0
TLS12, TLS13 = "0x0303", "0x0304"
# TLS_AES_128_GCM_SHA256, TLS_AES_256_GCM_SHA384, ECDHE-ECDSA-AES128/256-GCM, ECDHE-RSA-AES128/256-GCM
ALLOWED_SUITES = {"0x1301", "0x1302", "0xc02b", "0xc02c", "0xc02f", "0xc030"}
SCSV = "0x00ff"  # TLS_EMPTY_RENEGOTIATION_INFO_SCSV signals renegotiation support; not a suite


def retained(services: RigServices, netns: Topology, client_id: str) -> dict[str, object]:
    """What a new subscriber gets for status and info: the retained messages."""
    with MqttClient(
        "127.0.0.1", services.broker.observer_port, netns=netns.ns("svc"), client_id="hil-retained"
    ) as c:
        device = Device(c, client_id).observe()
        status = c.wait_for(device.topic("status"), timeout=10)
        info = device.info(timeout=10)
    return {"status": status.payload.decode(), "status_retained": status.retain, "info": info}


@pytest.mark.capture("tcp port 8883")
@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
@pytest.mark.parametrize("version", ["1.3", "1.2"])
def test_mqtt01_boot_to_online(
    app_image: DutImage,
    version: Literal["1.2", "1.3"],
    capture: Capture,
    services: RigServices,
    mqtt: Device,
    dut_reset: DutControl,
    netns: Topology,
    bench: Bench,
    request: pytest.FixtureRequest,
) -> None:
    dut = bench.dut.ip
    broker = SVC2_IP if version == "1.2" else services.broker.bind_ip
    if version == "1.2":
        request.getfixturevalue("tls_front")("1.2")  # broker.hil.lan -> 192.0.2.2
    report = dut_reset.reset(timeout=CONNACK_LIMIT_S)
    assert report.back is not None and report.back.app_s <= CONNACK_LIMIT_S, "no 'online' within 20 s"
    got = retained(services, netns, mqtt.device_id)
    capture.stop()

    hello = capture.rows(
        f"tls.handshake.type == 1 && ip.src == {dut} && ip.dst == {broker}",
        "tls.handshake.extensions.supported_version",
        "tls.handshake.extensions_server_name",
        "tls.handshake.ciphersuite",
        all_occurrences=True,
    )
    assert hello, "no ClientHello from the DUT"
    last = hello[-1]
    assert {TLS13, TLS12} <= set(last["tls.handshake.extensions.supported_version"].split(","))
    assert last["tls.handshake.extensions_server_name"] == BROKER_NAME
    suites = set(last["tls.handshake.ciphersuite"].split(",")) - {SCSV}
    assert suites <= ALLOWED_SUITES, f"unexpected suites {sorted(suites - ALLOWED_SUITES)}"
    server = capture.rows(
        f"tls.handshake.type == 2 && ip.src == {broker} && ip.dst == {dut}",
        "tls.handshake.version",
        "tls.handshake.extensions.supported_version",
    )
    assert server, "no ServerHello"
    negotiated = (
        server[-1]["tls.handshake.extensions.supported_version"] or server[-1]["tls.handshake.version"]
    )
    assert negotiated == (TLS13 if version == "1.3" else TLS12)

    assert got["status"] == "online" and got["status_retained"], f"status {got}"
    info = got["info"]
    assert isinstance(info, dict)
    assert info.get("mac", "").lower() == bench.dut.mac, (
        f"info mac {info.get('mac')} != bench {bench.dut.mac}"
    )
    if bench.dut.uid:
        assert info.get("hwid", "").lower() == bench.dut.uid, f"info hwid {info.get('hwid')} != bench uid"

    connects = capture.rows(
        f"mqtt.msgtype == 1 && ip.src == {dut}",
        "mqtt.kalive",
        "mqtt.conflag.cleansess",
        "mqtt.clientid",
        "mqtt.conflag.willflag",
        "mqtt.willtopic",
    )
    if not connects:
        pytest.skip(f"CONNECT not decrypted: {services.broker.keylog_status}")
    c = connects[-1]
    assert c["mqtt.kalive"] == "60" and c["mqtt.conflag.cleansess"] in ("1", "True")
    assert c["mqtt.clientid"] == bench.dut.mqtt_client_id
    assert (
        c["mqtt.conflag.willflag"] in ("1", "True")
        and c["mqtt.willtopic"] == f"bacnet-uc/{mqtt.device_id}/status"
    )
    connack = capture.rows(f"mqtt.msgtype == 2 && ip.dst == {dut}", "mqtt.conack.val")
    assert connack and connack[-1]["mqtt.conack.val"] == "0"
    assert connack[-1].t - report.released <= CONNACK_LIMIT_S
