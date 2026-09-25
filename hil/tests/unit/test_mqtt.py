"""MQTT observer/commander (hilrig.mqtt) against mosquitto in netns svc and a fake DUT in sim1."""

from __future__ import annotations

import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from fake_mqtt_dut import FakeMqttDut
from hilrig.mqtt import Device, MqttClient, TlsOptions
from hilrig.netns import HOSTS, Topology
from hilrig.pki import Pki
from hilrig.services import BrokerTls, Mosquitto
from rig_skips import needs_tools

pytestmark = needs_tools("mosquitto", "openssl")

SVC_IP = HOSTS["svc"].ip


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    return Pki.generate(tmp_path_factory.mktemp("pki"))


@pytest.fixture(scope="module")
def broker(unit_net: Topology, pki: Pki, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Mosquitto]:
    good = pki.servers["good"]
    with Mosquitto(
        unit_net.ns("svc"),
        tmp_path_factory.mktemp("broker"),
        BrokerTls(pki.ca, good.cert, good.key),
        runtime="local",
        keylog=False,
    ) as server:
        yield server


def fake_dut(unit_net: Topology, broker: Mosquitto, pki: Pki, device_id: str) -> FakeMqttDut:
    tls = TlsOptions(pki.ca, pki.dut.cert, pki.dut.key)
    return FakeMqttDut(SVC_IP, broker.port, device_id=device_id, ns=unit_net.ns("sim1"), tls=tls)


@pytest.fixture(scope="module")
def dut(unit_net: Topology, broker: Mosquitto, pki: Pki) -> Iterator[FakeMqttDut]:
    device = fake_dut(unit_net, broker, pki, "hil-dut").start()
    yield device
    device.stop()


@pytest.fixture
def observer(unit_net: Topology, broker: Mosquitto) -> Iterator[MqttClient]:
    with MqttClient("127.0.0.1", broker.observer_port, netns=unit_net.ns("svc")) as client:
        yield client


def test_retained_status_and_info(dut: FakeMqttDut, observer: MqttClient) -> None:
    device = Device(observer, "hil-dut").observe()
    status = device.wait_status("online", timeout=5)
    assert status.retain and status.topic == "bacnet-uc/hil-dut/status"
    info = device.info(timeout=5)
    assert info["board"] == "fake" and "led" in info["caps"]["cmds"]


def test_json_command_echoes_the_request_id(dut: FakeMqttDut, observer: MqttClient) -> None:
    device = Device(observer, "hil-dut").observe()
    reply = device.command("led", "on")
    assert reply["ok"] is True and reply["led"] is True and reply["id"].startswith("hil")
    assert device.command("identify", "5", request_id="r:1.x") == {"id": "r:1.x", "ok": True, "identify": 5}
    assert device.command("nosuch")["error"] == "unknown command"
    sent = observer.messages("bacnet-uc/hil-dut/cmd")
    assert sent and not any(m.retain for m in sent)  # commands are never retained


def test_text_command_and_telemetry(dut: FakeMqttDut, observer: MqttClient) -> None:
    device = Device(observer, "hil-dut").observe()
    assert device.command_text("ping")["ok"] is True
    since = observer.mark()
    dut.publish_telemetry()
    record = device.telemetry(timeout=5, since=since)
    assert set(record) == {"seq", "uptime_s", "sessions"}


def test_wait_for_times_out_and_topics_are_checked(observer: MqttClient) -> None:
    device = Device(observer, "hil-nobody").observe()
    with pytest.raises(TimeoutError, match="no matching message"):
        device.wait_status("online", timeout=0.3)
    with pytest.raises(ValueError, match="unknown topic kind"):
        device.topic("config")


def test_last_will_when_the_device_vanishes(
    unit_net: Topology, broker: Mosquitto, pki: Pki, observer: MqttClient
) -> None:
    device = Device(observer, "hil-dut2").observe()
    fake = fake_dut(unit_net, broker, pki, "hil-dut2").start()
    try:
        device.wait_status("online", timeout=5)
        since = observer.mark()
    finally:
        fake.drop()
    device.wait_status("offline", timeout=10, since=since)
    with MqttClient(
        "127.0.0.1", broker.observer_port, netns=unit_net.ns("svc"), client_id="hil-late"
    ) as late:
        retained = Device(late, "hil-dut2").observe().wait_status("offline", timeout=5)
    assert retained.retain  # the will was published retained, as the firmware sets it


def test_tls_client_options(unit_net: Topology, broker: Mosquitto, pki: Pki, tmp_path: Path) -> None:
    keylog = tmp_path / "client-keys.log"
    tls = TlsOptions(pki.ca, version="1.3", keylog=keylog)
    ctx = tls.context()
    assert ctx.minimum_version == ctx.maximum_version == ssl.TLSVersion.TLSv1_3
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    with MqttClient(SVC_IP, broker.port, netns=unit_net.ns("sim1"), tls=tls, client_id="hil-tls") as client:
        client.subscribe("hil/#")
        client.publish("hil/echo", "hello", retain=False)
        assert client.wait_for("hil/echo", lambda m: m.text() == "hello", timeout=5).qos == 1
    assert "CLIENT_TRAFFIC_SECRET_0" in keylog.read_text()
    with pytest.raises(ssl.SSLError):
        MqttClient(SVC_IP, broker.port, netns=unit_net.ns("sim1"), tls=TlsOptions(pki.rogue_ca)).connect(
            timeout=3
        )


def test_missing_connack_stops_the_network_thread() -> None:
    """Regression: a broker that never sent CONNACK left paho's thread and socket running."""
    with socket.create_server(("127.0.0.1", 0)) as silent:  # accepts (backlog), never answers
        client = MqttClient("127.0.0.1", silent.getsockname()[1], client_id="hil-no-connack")
        with pytest.raises(TimeoutError, match="no CONNACK"):
            client.connect(timeout=0.5)
    assert not [t for t in threading.enumerate() if t.name == "paho-mqtt-client-hil-no-connack"]
