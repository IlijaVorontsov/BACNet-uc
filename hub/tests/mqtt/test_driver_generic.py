"""MqttDriver with the generic-json profile against mosquitto."""

from __future__ import annotations

import asyncio
from typing import Any

import aiomqtt
import pytest

from uc_hub.core.errors import InvalidRequest, Unsupported
from uc_hub.core.ids import PointRef
from uc_hub.core.types import Quality
from uc_hub.sim.mqtt_device import SimJsonSensor, SimMqttTlsDevice

from .conftest import SITE, Broker, DriverFactory, eventually, record

pytestmark = pytest.mark.mosquitto

Sims = list[SimMqttTlsDevice | SimJsonSensor]
TOPIC = "sensors/r204/co2"
SPEC: dict[str, Any] = {
    "profile": "generic-json",
    "topic": TOPIC,
    "command_topic": f"{TOPIC}/set",
    "points": [
        {"id": "co2", "path": "$.ppm", "units": "parts-per-million", "datatype": "real"},
        {"id": "battery", "path": "$.bat.pct", "units": "percent", "datatype": "int"},
        {"id": "setpoint", "path": "$.sp", "datatype": "real", "writable": True},
        {"id": "boost", "path": "$.boost", "datatype": "bool", "writable": True,
         "command_topic": f"{TOPIC}/boost"},
    ],
}


def ref(obj: str, device: str = "r204-co2") -> PointRef:
    return PointRef(SITE, device, obj)


async def co2_setup(
    broker: Broker, sims: Sims, make_driver: DriverFactory, **settings: Any,
) -> tuple[Any, Any, SimJsonSensor]:
    driver, rec = await make_driver(broker.settings(**settings),
                                    [(record("r204-co2", TOPIC), SPEC)])
    sensor = SimJsonSensor(broker.settings(), TOPIC, command_topic=f"{TOPIC}/set",
                           state={"ppm": 612.0, "bat": {"pct": 97}, "sp": 800.0},
                           interval_s=0.1)
    sims.append(sensor)
    await sensor.start()
    await eventually(lambda: rec.last("hq/r204-co2/co2"), what="first CO2 reading")
    return driver, rec, sensor


async def test_readings_from_json_paths(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, _ = await co2_setup(broker, sims, make_driver)
    assert rec.is_online("r204-co2") is True
    co2, battery, setpoint, boost = await driver.read(
        [ref("co2"), ref("battery"), ref("setpoint"), ref("boost")])
    assert (co2.value, co2.quality) == (612.0, Quality.GOOD)
    assert (battery.value, battery.quality) == (97, Quality.GOOD)
    assert setpoint.value == 800.0
    assert (boost.value, boost.quality, boost.error) == (
        None, Quality.OFFLINE, "no value received yet")
    desc = await driver.describe("r204-co2")
    points = {p.ref.obj: p for p in desc.points}
    assert points["co2"].source == "mqtt:sensors/r204/co2 $.ppm"
    assert points["battery"].units == "percent"
    assert desc.extra["topic"] == TOPIC and desc.extra["info"] is None
    assert desc.device.online


async def test_writes_publish_point_objects(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, _, sensor = await co2_setup(broker, sims, make_driver)
    result = await driver.write(ref("setpoint"), "950", 12)
    assert (result.ok, result.value, result.priority) == (True, 950.0, None)
    await eventually(lambda: sensor.writes, what="write received")
    assert sensor.writes == [{"setpoint": 950.0}]
    async with aiomqtt.Client(broker.host, broker.port, identifier="spy") as spy:
        await spy.subscribe(f"{TOPIC}/boost", qos=1)
        assert (await driver.write(ref("boost"), "on", None)).ok
        async with asyncio.timeout(3):
            message = await anext(aiter(spy.messages))
    assert message.payload == b'{"boost":true}'

    with pytest.raises(InvalidRequest):
        await driver.write(ref("co2"), 400, None)
    with pytest.raises(InvalidRequest):
        await driver.write(ref("setpoint"), "high", None)
    with pytest.raises(InvalidRequest):
        await driver.write(ref("setpoint"), None, 12)
    assert not (await driver.relinquish(ref("setpoint"), 12)).ok
    with pytest.raises(Unsupported, match="takes no commands"):
        await driver.command("r204-co2", "ping")
    with pytest.raises(Unsupported):
        await driver.identify("r204-co2")


async def test_device_echo_updates_the_reading(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    spec = {**SPEC, "points": [*SPEC["points"],
                               {"id": "sp", "path": "$.sp", "writable": True}]}
    driver, rec = await make_driver(broker.settings(), [(record("r204-co2"), spec)])
    sensor = SimJsonSensor(broker.settings(), TOPIC, command_topic=f"{TOPIC}/set",
                           state={"ppm": 500.0, "sp": 800.0}, interval_s=30.0)
    sims.append(sensor)
    await sensor.start()
    await eventually(lambda: rec.last("hq/r204-co2/sp"), what="first reading")
    assert (await driver.write(ref("sp"), 700, None)).ok
    await eventually(lambda: (r := rec.last("hq/r204-co2/sp")) is not None and r.value == 700.0,
                     what="echoed setpoint")


async def test_malformed_and_plain_payloads(
    broker: Broker, make_driver: DriverFactory,
) -> None:
    spec = {"profile": "generic-json", "topic": "plant/+/state", "points": [
        {"id": "running", "path": "$", "datatype": "bool"},
        {"id": "speed", "path": "$.speed", "datatype": "real"},
    ]}
    driver, rec = await make_driver(broker.settings(max_payload_bytes=1024),
                                    [(record("pumps"), spec)])
    async with aiomqtt.Client(broker.host, broker.port, identifier="plant") as client:
        await client.publish("plant/p1/state", b"on", qos=1)
        await eventually(lambda: rec.last("hq/pumps/running"), what="plain text value")
        running = rec.last("hq/pumps/running")
        assert running is not None and running.value is True
        await client.publish("plant/p2/state", b'{"speed": "fast"}', qos=1)
        await eventually(lambda: rec.last("hq/pumps/speed"), what="bad speed")
        speed = rec.last("hq/pumps/speed")
        assert speed is not None and speed.quality is Quality.FAULT and speed.value is None
        await client.publish("plant/p2/state", b"x" * 2000, qos=1)
        await client.publish("elsewhere/state", b'{"speed": 1}', qos=1)
        await client.publish("plant/p2/state", b'{"speed": 12.5}', qos=1)
        await eventually(lambda: (r := rec.last("hq/pumps/speed")) is not None
                         and r.value == 12.5, what="good speed")
    (speed,) = await driver.read([ref("speed", "pumps")])
    assert speed.quality is Quality.GOOD
    assert not any(r.value == "x" * 2000 for r in rec.readings)


async def test_generic_stale_and_broker_loss(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, sensor = await co2_setup(broker, sims, make_driver, stale_after_s=1.0)
    await sensor.stop()
    await asyncio.sleep(1.1)
    (co2,) = await driver.read([ref("co2")])
    assert co2.quality is Quality.STALE and co2.value is not None
    broker.stop()
    await eventually(lambda: rec.is_online("r204-co2") is False, what="offline with the broker")
    (co2,) = await driver.read([ref("co2")])
    assert co2.quality is Quality.OFFLINE
    broker.start()
    assert await driver.wait_connected(5.0)
    (co2,) = await driver.read([ref("co2")])
    assert (co2.quality, co2.error) == (Quality.OFFLINE, "device offline")
    await sensor.start()
    await eventually(lambda: rec.is_online("r204-co2"), what="online after the next message")
    (co2,) = await driver.read([ref("co2")])
    assert co2.quality is Quality.GOOD
