"""MqttDriver with the mqtt_tls profile against mosquitto and simulated nodes."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiomqtt
import pytest

from uc_hub.core.errors import DeviceError, DeviceTimeout, InvalidRequest, NotFound, Unsupported
from uc_hub.core.ids import PointRef
from uc_hub.core.types import Quality
from uc_hub.sim.mqtt_device import SimJsonSensor, SimMqttTlsDevice

from .conftest import SITE, Broker, DriverFactory, eventually, record

pytestmark = pytest.mark.mosquitto

LEGACY_ID = "zephyr-00aa11bb"
MODERN_ID = "zephyr-cc22dd33"
Sims = list[SimMqttTlsDevice | SimJsonSensor]


def ref(device: str, obj: str) -> PointRef:
    return PointRef(SITE, device, obj)


def node_spec(client_id: str, **extra: Any) -> dict[str, Any]:
    return {"name": "x", "protocol": "mqtt", "profile": "mqtt_tls", "client_id": client_id,
            **extra}


async def start_node(broker: Broker, sims: Sims, client_id: str = LEGACY_ID,
                     **options: Any) -> SimMqttTlsDevice:
    options.setdefault("telemetry_interval_s", 0.2)
    node = SimMqttTlsDevice(broker.settings(), client_id, **options)
    sims.append(node)
    await node.start()
    assert await node.wait_connected(5.0)
    return node


async def legacy_setup(
    broker: Broker, sims: Sims, make_driver: DriverFactory, **node_options: Any,
) -> tuple[Any, Any, SimMqttTlsDevice]:
    node = await start_node(broker, sims, **node_options)
    driver, rec = await make_driver(
        broker.settings(), [(record("r204-node"), node_spec(LEGACY_ID))])
    await eventually(lambda: rec.is_online("r204-node"), what="device online")
    return driver, rec, node


async def test_legacy_points_and_readings(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, _ = await legacy_setup(broker, sims, make_driver)
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="telemetry")
    desc = await driver.describe("r204-node")
    assert [p.ref.obj for p in desc.points] == [
        "status", "telemetry.seq", "telemetry.uptime_s", "telemetry.sessions", "led"]
    assert desc.extra["info"]["board"] == "nucleo_h563zi"
    assert desc.device.online and desc.device.model == "nucleo_h563zi"
    assert desc.device.firmware == "zephyr 3.7.2"
    assert desc.device.last_seen is not None
    assert desc.to_json()["device"]["points"] == 5

    readings = await driver.read([
        ref("r204-node", "status"), ref("r204-node", "telemetry.sessions"),
        ref("r204-node", "led"), ref("r204-node", "nope"), ref("ghost", "status"),
        PointRef("elsewhere", "r204-node", "status"),
    ])
    status, sessions, led, nope, ghost, other_site = readings
    assert (status.value, status.quality) == ("online", Quality.GOOD)
    assert (sessions.value, sessions.quality) == (1, Quality.GOOD)
    assert (led.value, led.quality) == (None, Quality.OFFLINE)
    assert nope.quality is Quality.FAULT and ghost.quality is Quality.FAULT
    assert other_site.quality is Quality.FAULT
    seq = rec.last("hq/r204-node/telemetry.seq")
    assert seq is not None and isinstance(seq.value, int) and seq.quality is Quality.GOOD
    # Telemetry keeps flowing and every message is published.
    count = sum(1 for r in rec.readings if r.ref.obj == "telemetry.seq")
    await eventually(lambda: sum(1 for r in rec.readings if r.ref.obj == "telemetry.seq")
                     > count, what="more telemetry")


async def test_legacy_led_write_and_commands(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, node = await legacy_setup(broker, sims, make_driver)
    led = ref("r204-node", "led")
    result = await driver.write(led, True, 12)
    assert (result.ok, result.value, result.priority) == (True, True, None)
    assert node.led is True and node.commands[-1] == b"led on"
    await eventually(lambda: (r := rec.last(str(led))) is not None and r.value is True,
                     what="led reading")
    (reading,) = await driver.read([led])
    assert (reading.value, reading.quality) == (True, Quality.GOOD)
    result = await driver.write(led, "off", None)
    assert result.ok and result.value is False and node.led is False

    reply = await driver.command("r204-node", "ping")
    assert "pong" in reply and node.commands[-1] == b"ping"
    with pytest.raises(Unsupported):
        await driver.identify("r204-node", 10)

    with pytest.raises(InvalidRequest):
        await driver.write(ref("r204-node", "telemetry.seq"), 1, None)
    with pytest.raises(InvalidRequest):
        await driver.write(led, None, 12)
    with pytest.raises(InvalidRequest):
        await driver.write(led, "dim", None)
    with pytest.raises(NotFound):
        await driver.write(ref("r204-node", "nope"), 1, None)
    with pytest.raises(NotFound):
        await driver.write(ref("ghost", "led"), 1, None)
    relinquished = await driver.relinquish(led, 12)
    assert not relinquished.ok and "priority array" in (relinquished.error or "")


async def test_legacy_led_error_and_timeout(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, _, node = await legacy_setup(broker, sims, make_driver, has_led=False)
    result = await driver.write(ref("r204-node", "led"), True, None)
    assert not result.ok and result.error is not None
    assert "led unavailable (code -19)" in result.error
    node.muted = True
    result = await driver.write(ref("r204-node", "led"), True, None)
    assert not result.ok and "no reply" in (result.error or "")
    with pytest.raises(DeviceTimeout):
        await driver.command("r204-node", "ping", timeout_s=0.2)


async def test_led_learned_from_other_clients(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    _, rec, node = await legacy_setup(broker, sims, make_driver)
    async with aiomqtt.Client(broker.host, broker.port, identifier="operator") as client:
        await client.publish(f"bacnet-uc/{LEGACY_ID}/cmd", b"led toggle", qos=1)
    await eventually(lambda: (r := rec.last("hq/r204-node/led")) is not None and r.value is True,
                     what="led learned from a foreign command")
    assert node.led is True


async def test_modern_json_commands(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    node = await start_node(broker, sims, MODERN_ID, modern=True,
                            extra_telemetry={"temp_c": ("C", 21.5)})
    driver, rec = await make_driver(
        broker.settings(), [(record("r205-node"), node_spec(MODERN_ID))])
    await eventually(lambda: rec.last("hq/r205-node/telemetry.temp_c"), what="temp_c")
    desc = await driver.describe("r205-node")
    points = {p.ref.obj: p for p in desc.points}
    assert list(points) == ["status", "telemetry.seq", "telemetry.uptime_s",
                            "telemetry.sessions", "telemetry.temp_c", "led"]
    assert points["telemetry.temp_c"].units == "degrees-celsius"
    assert desc.device.hwid == "cc22dd33" and desc.device.firmware == "0.3.0"
    assert desc.extra["json_commands"] is True
    (temp,) = await driver.read([ref("r205-node", "telemetry.temp_c")])
    assert (temp.value, temp.quality) == (21.5, Quality.GOOD)

    # Several commands in flight at once, each matched by its ID.
    write, pong, _ = await asyncio.gather(
        driver.write(ref("r205-node", "led"), True, None),
        driver.command("r205-node", "ping"),
        driver.identify("r205-node", 5),
    )
    assert write.ok and write.value is True and "pong" in pong and pong["ok"] is True
    assert node.led is True and node.identifying
    sent = [json.loads(c) for c in node.commands]
    assert sorted(c["cmd"] for c in sent) == ["identify", "led", "ping"]
    assert len({c["id"] for c in sent}) == 3

    with pytest.raises(DeviceError, match="bad argument"):
        await driver.command("r205-node", "led", "blink")
    with pytest.raises(DeviceError, match="bad argument"):
        await driver.identify("r205-node", 3601)
    with pytest.raises(Unsupported):
        await driver.command("r205-node", "reboot")


async def test_foreign_reply_does_not_answer_our_command(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    node = await start_node(broker, sims, MODERN_ID, modern=True)
    driver, rec = await make_driver(
        broker.settings(command_timeout_s=1.5), [(record("r205-node"), node_spec(MODERN_ID))])
    await eventually(lambda: rec.is_online("r205-node"), what="online")
    await eventually(lambda: driver._devices["r205-node"].extra()["json_commands"],
                     what="caps")
    node.muted = True
    write = asyncio.create_task(driver.write(ref("r205-node", "led"), False, None))
    await eventually(lambda: node.commands, what="command sent")
    async with aiomqtt.Client(broker.host, broker.port, identifier="other-hub") as client:
        await client.publish(f"bacnet-uc/{MODERN_ID}/event",
                             json.dumps({"id": "someone-else", "ok": True, "led": True}), qos=1)
    # The foreign reply still tells us the LED state...
    await eventually(lambda: (r := rec.last("hq/r205-node/led")) is not None and r.value is True,
                     what="led learned from the foreign reply")
    # ...but does not answer our command.
    result = await write
    assert not result.ok and "no reply" in (result.error or "")


async def test_last_will_marks_offline(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, node = await legacy_setup(broker, sims, make_driver)
    await driver.write(ref("r204-node", "led"), True, None)
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="telemetry")
    await node.crash()
    await eventually(lambda: rec.is_online("r204-node") is False, what="offline via will")
    status, seq, led = await driver.read([ref("r204-node", "status"),
                                          ref("r204-node", "telemetry.seq"),
                                          ref("r204-node", "led")])
    assert (status.value, status.quality) == ("offline", Quality.GOOD)
    assert (seq.quality, seq.error) == (Quality.OFFLINE, "device offline")
    assert seq.value is not None
    assert (led.value, led.quality) == (None, Quality.OFFLINE)
    published = rec.last("hq/r204-node/telemetry.seq")
    assert published is not None and published.quality is Quality.OFFLINE
    result = await driver.write(ref("r204-node", "led"), True, None)
    assert not result.ok and "offline" in (result.error or "")
    desc = await driver.describe("r204-node")
    assert desc.device.online is False

    reborn = await start_node(broker, sims)
    await eventually(lambda: rec.is_online("r204-node"), what="back online")
    assert (await driver.write(ref("r204-node", "led"), True, None)).ok
    assert reborn.led is True


async def test_clean_stop_marks_offline(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    _, rec, node = await legacy_setup(broker, sims, make_driver)
    await node.stop()
    await eventually(lambda: rec.is_online("r204-node") is False, what="offline")


async def test_stale_and_never_seen(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec = await make_driver(
        broker.settings(stale_after_s=1.0),
        [(record("r204-node"), node_spec(LEGACY_ID)),
         (record("silent"), node_spec("zephyr-silent"))])
    # Telemetry is not retained: only the first message, sent on connect, is seen.
    await start_node(broker, sims, telemetry_interval_s=60.0)
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="first telemetry")
    (fresh,) = await driver.read([ref("r204-node", "telemetry.seq")])
    assert fresh.quality is Quality.GOOD
    await asyncio.sleep(1.1)
    seq, status = await driver.read([ref("r204-node", "telemetry.seq"),
                                     ref("r204-node", "status")])
    assert seq.quality is Quality.STALE and seq.value == fresh.value
    assert seq.ts == fresh.ts
    assert status.quality is Quality.GOOD
    (silent,) = await driver.read([ref("silent", "status")])
    assert (silent.value, silent.quality, silent.error) == (
        None, Quality.OFFLINE, "no value received yet")
    assert rec.is_online("silent") is None


async def test_reconnect_after_broker_restart(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    driver, rec, node = await legacy_setup(broker, sims, make_driver)
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="telemetry")
    broker.stop()
    await eventually(lambda: not driver.connected, what="driver disconnected")
    await eventually(lambda: rec.is_online("r204-node") is False, what="device offline")
    down = rec.last("hq/r204-node/telemetry.seq")
    assert down is not None and down.quality is Quality.OFFLINE
    assert down.error == "MQTT broker not connected"
    (status,) = await driver.read([ref("r204-node", "status")])
    assert status.quality is Quality.OFFLINE
    with pytest.raises(DeviceTimeout):
        await driver.command("r204-node", "ping")

    restarted = time.time()
    broker.start()
    assert await driver.wait_connected(5.0)
    await eventually(lambda: rec.is_online("r204-node"), what="device back online")
    await eventually(
        lambda: (r := rec.last("hq/r204-node/telemetry.seq")) is not None
        and r.ts > restarted and r.quality is Quality.GOOD,
        what="telemetry after the restart")
    assert node.sessions >= 2
    # Subscriptions were restored: commands work again.
    assert (await driver.write(ref("r204-node", "led"), True, None)).ok


async def test_discovery(broker: Broker, sims: Sims, make_driver: DriverFactory) -> None:
    await start_node(broker, sims)
    await start_node(broker, sims, MODERN_ID, modern=True, board="frdm_mcxn947")
    async with aiomqtt.Client(broker.host, broker.port, identifier="junk") as client:
        await client.publish("foo/bar/info", b"not json", qos=1, retain=True)
        await client.publish("foo/baz/status", b"sleeping", qos=1, retain=True)
        await client.publish("a/b/c/info", b"{}", qos=1, retain=True)
        await client.publish("other/only-status/status", b"offline", qos=1, retain=True)
    driver, _ = await make_driver(broker.settings(),
                                  [(record("r204-node"), node_spec(LEGACY_ID))])
    found = await driver.discover(0.5)
    by_address = {d.address: d for d in found}
    assert list(by_address) == [f"bacnet-uc/{LEGACY_ID}", f"bacnet-uc/{MODERN_ID}",
                                "other/only-status"]
    legacy = by_address[f"bacnet-uc/{LEGACY_ID}"]
    assert (legacy.name, legacy.model, legacy.hwid) == (LEGACY_ID, "nucleo_h563zi", "")
    assert legacy.extra["device"] == "r204-node" and legacy.extra["status"] == "online"
    assert legacy.extra["client_id"] == LEGACY_ID and legacy.extra["topic_root"] == "bacnet-uc"
    modern = by_address[f"bacnet-uc/{MODERN_ID}"]
    assert (modern.model, modern.hwid) == ("frdm_mcxn947", "cc22dd33")
    assert "device" not in modern.extra and modern.extra["info"]["caps"]["cmds"]
    lonely = by_address["other/only-status"]
    assert lonely.extra["status"] == "offline" and lonely.extra["info"] is None
    assert all(d.protocol.value == "mqtt" and not d.bacnet_uc for d in found)
    # The driver's own session is untouched by the sweep.
    assert driver.connected


async def test_discovery_applies_the_payload_limit(
    broker: Broker, make_driver: DriverFactory,
) -> None:
    info = {"board": "b", "hwid": "01", "pad": "x" * 600}
    async with aiomqtt.Client(broker.host, broker.port, identifier="big") as client:
        await client.publish("big/node/info", json.dumps(info), qos=1, retain=True)
        await client.publish("big/node/status", b"online", qos=1, retain=True)
        await client.publish("small/node/info", b'{"board": "s"}', qos=1, retain=True)
    driver, _ = await make_driver(broker.settings(max_payload_bytes=512))
    found = {d.address: d for d in await driver.discover(0.3)}
    assert set(found) == {"big/node", "small/node"}
    big = found["big/node"]
    assert (big.extra["info"], big.extra["status"], big.model) == (None, "online", "")
    assert found["small/node"].model == "s"


async def test_discovery_without_broker(broker: Broker, make_driver: DriverFactory) -> None:
    driver, _ = await make_driver(broker.settings(timeout_s=1.0), start=False)
    broker.stop()
    with pytest.raises(DeviceError, match="discovery"):
        await driver.discover(0.2)


async def test_devices_added_and_removed_while_running(
    broker: Broker, sims: Sims, make_driver: DriverFactory,
) -> None:
    await start_node(broker, sims)
    driver, rec = await make_driver(broker.settings())
    await driver.add_device(record("r204-node"), node_spec(LEGACY_ID))
    await eventually(lambda: rec.is_online("r204-node"), what="retained status after add")
    await driver.remove_device("r204-node")
    await driver.remove_device("r204-node")
    count = len(rec.readings)
    await asyncio.sleep(0.5)
    assert len(rec.readings) == count
    with pytest.raises(NotFound):
        await driver.describe("r204-node")
    # Re-adding under the same name replaces the entry.
    await driver.add_device(record("r204-node"), node_spec(LEGACY_ID))
    await driver.add_device(record("r204-node"), node_spec(LEGACY_ID))
    await eventually(lambda: len(rec.readings) > count, what="readings after re-add")
    assert list(driver._exact[f"bacnet-uc/{LEGACY_ID}/status"]) == [
        driver._devices["r204-node"]]


async def test_oversized_message_is_ignored(
    broker: Broker, make_driver: DriverFactory,
) -> None:
    _, rec = await make_driver(broker.settings(max_payload_bytes=64),
                                    [(record("r204-node"), node_spec(LEGACY_ID))])
    topic = f"bacnet-uc/{LEGACY_ID}/telemetry"
    async with aiomqtt.Client(broker.host, broker.port, identifier="big") as client:
        await client.publish(topic, json.dumps({"seq": 1, "pad": "x" * 100}), qos=1)
        await client.publish(topic, json.dumps({"seq": 2}), qos=1)
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="small message")
    assert [r.value for r in rec.readings if r.ref.obj == "telemetry.seq"] == [2]
