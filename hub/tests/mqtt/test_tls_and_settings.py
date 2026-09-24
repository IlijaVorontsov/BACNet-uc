"""Broker settings, and the driver over mutual TLS (mosquitto with a throwaway CA)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from uc_hub.core.driver import DriverContext
from uc_hub.core.errors import InvalidRequest
from uc_hub.core.ids import PointRef
from uc_hub.drivers.mqtt import BrokerSettings, MqttDriver, TlsSettings
from uc_hub.drivers.mqtt.connection import topic_matches, valid_filter
from uc_hub.sim.mqtt_device import SimJsonSensor, SimMqttTlsDevice

from .conftest import SITE, Broker, DriverFactory, eventually, record

Sims = list[SimMqttTlsDevice | SimJsonSensor]


# -- settings --------------------------------------------------------------------
def test_settings_defaults() -> None:
    plain = BrokerSettings.from_settings({})
    assert (plain.host, plain.port, plain.tls, plain.client_id) == (
        "127.0.0.1", 1883, None, "uc-hub")
    tls = BrokerSettings.from_settings({"tls": {"ca": "~/ca.crt"}})
    assert tls.port == 8883 and tls.tls is not None
    assert tls.tls.ca is not None and not tls.tls.ca.startswith("~")
    assert BrokerSettings.from_settings({"tls": True}).tls == TlsSettings()
    assert BrokerSettings.from_settings({"tls": None, "port": 1884}).port == 1884
    assert tls.url.startswith("mqtts://")


def test_password_from_environment() -> None:
    s = BrokerSettings.from_settings(
        {"username": "hub", "password_env": "MQTT_PW"}, environ={"MQTT_PW": "s3cret"})
    assert (s.username, s.password) == ("hub", "s3cret")
    assert "s3cret" not in repr(s)
    with pytest.raises(InvalidRequest, match="MQTT_PW"):
        BrokerSettings.from_settings({"username": "hub", "password_env": "MQTT_PW"}, environ={})
    literal = BrokerSettings.from_settings(
        {"username": "hub", "password": "lit", "password_env": "MQTT_PW"}, environ={})
    assert literal.password == "lit"


@pytest.mark.parametrize("settings", [
    {"port": 0},
    {"port": 70000},
    {"port": "1883"},
    {"timeout_s": -1},
    {"keepalive_s": True},
    {"host": ""},
    {"client_id": 5},
    {"tls": "yes"},
    {"tls": {"ca": 5}},
    {"tls": {"key": "k.pem"}},
    {"tls": {"cafile": "x"}},
    {"password": "p"},
])
def test_invalid_settings(settings: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        BrokerSettings.from_settings(settings)


@pytest.mark.parametrize("settings", [
    {"stale_after_s": 0}, {"command_timeout_s": "5"}, {"max_payload_bytes": -1},
])
def test_invalid_driver_settings(settings: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        MqttDriver(DriverContext(site=SITE, publish=lambda r: None, settings=settings))


async def test_missing_ca_file_fails_start(tmp_path: Any) -> None:
    driver = MqttDriver(DriverContext(site=SITE, publish=lambda r: None,
                                      settings={"tls": {"ca": str(tmp_path / "none.crt")}}))
    with pytest.raises(InvalidRequest, match="certificates"):
        await driver.start()
    await driver.stop()


@pytest.mark.parametrize(("filter_", "topic", "match"), [
    ("a/b", "a/b", True),
    ("a/+", "a/b", True),
    ("a/+", "a/b/c", False),
    ("a/+", "a", False),
    ("a/#", "a", True),
    ("a/#", "a/b/c", True),
    ("#", "a/b", True),
    ("+/+/info", "bacnet-uc/x/info", True),
    ("+/+/info", "bacnet-uc/x/status", False),
    ("#", "$SYS/broker/uptime", False),
    ("+/broker", "$SYS/broker", False),
    ("$SYS/#", "$SYS/broker", True),
    ("a/+/c", "a//c", True),
])
def test_topic_matches(filter_: str, topic: str, match: bool) -> None:
    assert topic_matches(filter_, topic) is match


@pytest.mark.parametrize(("filter_", "valid"), [
    ("a/b", True), ("a/+/c", True), ("#", True), ("a/#", True), ("", False),
    ("a/#/b", False), ("a#", False), ("a/b+", False), ("a\0b", False),
])
def test_valid_filter(filter_: str, valid: bool) -> None:
    assert valid_filter(filter_) is valid


def test_driver_is_not_connected_before_start() -> None:
    driver = MqttDriver(DriverContext(site=SITE, publish=lambda r: None))
    assert not driver.connected


# -- TLS ---------------------------------------------------------------------------
@pytest.mark.mosquitto
async def test_mutual_tls(tls_broker: Broker, sims: Sims, make_driver: DriverFactory) -> None:
    node = SimMqttTlsDevice(tls_broker.settings(), "zephyr-7715", modern=True,
                            telemetry_interval_s=0.2)
    sims.append(node)
    await node.start()
    assert await node.wait_connected(5.0)
    driver, rec = await make_driver(
        tls_broker.settings(),
        [(record("tls-node"), {"profile": "mqtt_tls", "client_id": "zephyr-7715"})])
    await eventually(lambda: rec.is_online("tls-node"), what="online over TLS")
    desc = await driver.describe("tls-node")
    assert desc.extra["info"]["tls"] is True
    result = await driver.write(PointRef(SITE, "tls-node", "led"), True, None)
    assert result.ok and node.led is True
    found = await driver.discover(0.3)
    assert [d.address for d in found] == ["bacnet-uc/zephyr-7715"]


@pytest.mark.mosquitto
async def test_host_name_is_verified(tls_broker: Broker, make_driver: DriverFactory) -> None:
    # The broker certificate names "localhost" only.
    strict, _ = await make_driver(tls_broker.settings(host="127.0.0.1"), start=False)
    await strict.start()
    assert not await strict.wait_connected(0.5)
    tls = tls_broker.settings()["tls"]
    relaxed, _ = await make_driver(
        tls_broker.settings(host="127.0.0.1", tls={**tls, "insecure": True}))
    assert relaxed.connected


@pytest.mark.mosquitto
async def test_unknown_ca_is_refused(
    tls_broker: Broker, make_driver: DriverFactory, pki: Any,
) -> None:
    tls = {**tls_broker.settings()["tls"], "ca": str(pki.other_ca)}
    driver, _ = await make_driver(tls_broker.settings(tls=tls), start=False)
    await driver.start()
    assert not await driver.wait_connected(0.5)


@pytest.mark.mosquitto
async def test_broker_requires_a_client_certificate(
    tls_broker: Broker, make_driver: DriverFactory, pki: Any,
) -> None:
    driver, _ = await make_driver(tls_broker.settings(tls={"ca": str(pki.ca)}), start=False)
    await driver.start()
    assert not await driver.wait_connected(0.5)
    await driver.stop()
    await asyncio.sleep(0)
    assert not driver.connected
