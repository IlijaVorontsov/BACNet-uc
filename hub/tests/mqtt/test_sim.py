"""The simulated devices, checked on the wire with a plain MQTT client."""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiomqtt
import pytest

from uc_hub.sim.mqtt_device import (
    SimJsonSensor,
    SimMqttTlsDevice,
    co2_walk,
    start_sim_mqtt_devices,
    stop_sim_mqtt_devices,
)

from .conftest import Broker, eventually

Sims = list[SimMqttTlsDevice | SimJsonSensor]
PREFIX = "bacnet-uc/zephyr-0a1b2c3d"


@asynccontextmanager
async def watcher(broker: Broker, *filters: str) -> AsyncIterator[aiomqtt.Client]:
    async with aiomqtt.Client(broker.host, broker.port, identifier="watcher") as client:
        for f in filters:
            await client.subscribe(f, qos=1)
        yield client


async def next_on(client: aiomqtt.Client, topic: str, timeout_s: float = 3.0) -> bytes:
    async with asyncio.timeout(timeout_s):
        async for message in client.messages:
            if message.topic.value == topic:
                assert isinstance(message.payload, bytes)
                return message.payload
    raise AssertionError("unreachable")


async def command(client: aiomqtt.Client, payload: str | bytes) -> dict[str, Any]:
    await client.publish(f"{PREFIX}/cmd", payload, qos=1)
    reply: dict[str, Any] = json.loads(await next_on(client, f"{PREFIX}/event"))
    return reply


async def start(broker: Broker, sims: Sims, **options: Any) -> SimMqttTlsDevice:
    node = SimMqttTlsDevice(broker.settings(), telemetry_interval_s=options.pop("interval", 0.1),
                            **options)
    sims.append(node)
    await node.start()
    assert await node.wait_connected(5.0)
    return node


@pytest.mark.mosquitto
async def test_legacy_node_on_the_wire(broker: Broker, sims: Sims) -> None:
    node = await start(broker, sims)
    async with watcher(broker, f"{PREFIX}/#") as client:
        retained = {}
        async with asyncio.timeout(3):
            async for message in client.messages:
                if message.retain:
                    retained[message.topic.value.rsplit("/", 1)[1]] = message.payload
                if len(retained) == 2:
                    break
        assert retained["status"] == b"online"
        assert json.loads(retained["info"]) == {
            "board": "nucleo_h563zi", "zephyr": "3.7.2", "ip": "192.0.2.10", "tls": False}
        telemetry = json.loads(await next_on(client, f"{PREFIX}/telemetry"))
        assert set(telemetry) == {"seq", "uptime_s", "sessions"}
        assert telemetry["sessions"] == 1

        assert await command(client, "led toggle") == {"led": True}
        assert await command(client, "led off") == {"led": False}
        assert "pong" in await command(client, "ping")
        assert await command(client, "LED ON") == {"error": "unknown command"}
        assert await command(client, "identify 5") == {"error": "unknown command"}
        assert await command(client, '{"id": "a", "cmd": "ping"}') == {
            "error": "unknown command"}
        assert await command(client, b"x" * 200) == {"error": "payload too large"}
    assert node.commands[0] == b"led toggle"


@pytest.mark.mosquitto
async def test_modern_node_on_the_wire(broker: Broker, sims: Sims) -> None:
    """Firmware 0.3.0 (apps/mqtt_tls/src/commands.c on the MQTT firmware branch)."""
    node = await start(broker, sims, modern=True, extra_telemetry={"co2": ("ppm", 450)})
    info = node.info()
    assert list(info) == ["fw", "board", "zephyr", "hwid", "mac", "ip", "tls", "caps"]
    assert info["hwid"] == "0a1b2c3d" and info["fw"] == "0.3.0"
    assert info["mac"].startswith("02:") and len(info["mac"]) == 17
    assert info["caps"] == {"cmds": ["ping", "led", "identify"],
                            "telemetry": {"seq": "count", "uptime_s": "s",
                                          "sessions": "count", "co2": "ppm"}}
    async with watcher(broker, f"{PREFIX}/event", f"{PREFIX}/telemetry") as client:
        telemetry = json.loads(await next_on(client, f"{PREFIX}/telemetry"))
        assert telemetry["co2"] == 450
        reply = await command(client, '{"id": "c1", "cmd": "identify", "arg": "7"}')
        assert reply == {"id": "c1", "ok": True, "identify": 7}
        assert node.identifying
        # Any LED command ends identify.
        reply = await command(client, '{"id": "r.2:x-_", "cmd": "led", "arg": "on"}')
        assert reply == {"id": "r.2:x-_", "ok": True, "led": True} and not node.identifying
        # The ID is optional; every reply carries "ok", plain text too.
        assert await command(client, '{"cmd": "led", "arg": "toggle"}') == {
            "ok": True, "led": False}
        assert (await command(client, "ping"))["ok"] is True
        assert await command(client, "identify") == {"ok": True, "identify": 30}
        assert await command(client, "identify 0") == {"ok": True, "identify": 0}
        assert not node.identifying
        # Errors, with the ID echoed whenever it is valid.
        for payload, reply in [
            ('{"id": "c2", "cmd": "identify", "arg": 7}', {"ok": False, "error": "invalid json"}),
            ('{"id": 5, "cmd": "ping"}', {"ok": False, "error": "invalid json"}),
            ("{not json", {"ok": False, "error": "invalid json"}),
            ('{"id": "' + "x" * 17 + '", "cmd": "ping"}', {"ok": False, "error": "invalid id"}),
            ('{"id": "a b", "cmd": "ping"}', {"ok": False, "error": "invalid id"}),
            ('{"id": "c3"}', {"id": "c3", "ok": False, "error": "missing cmd"}),
            ('{"id": "c4", "cmd": "ping", "arg": "x"}',
             {"id": "c4", "ok": False, "error": "unknown command"}),
            ('{"id": "c5", "cmd": "led"}', {"id": "c5", "ok": False, "error": "bad argument"}),
            ('{"id": "c6", "cmd": "identify", "arg": "3601"}',
             {"id": "c6", "ok": False, "error": "bad argument"}),
            ("identify soon", {"ok": False, "error": "bad argument"}),
            ("identify ", {"ok": False, "error": "bad argument"}),
            ("led dim", {"ok": False, "error": "bad argument"}),
            ("reboot", {"ok": False, "error": "unknown command"}),
            (' {"cmd": "ping"}', {"ok": False, "error": "unknown command"}),
            (b"x" * 200, {"ok": False, "error": "payload too large"}),
        ]:
            assert await command(client, payload) == reply, payload


@pytest.mark.mosquitto
async def test_modern_node_without_led(broker: Broker, sims: Sims) -> None:
    node = await start(broker, sims, modern=True, has_led=False)
    assert node.info()["caps"]["cmds"] == ["ping"]
    async with watcher(broker, f"{PREFIX}/event") as client:
        assert await command(client, '{"id": "i", "cmd": "identify", "arg": "5"}') == {
            "id": "i", "ok": False, "error": "led unavailable"}
        assert await command(client, "led on") == {"ok": False, "error": "led unavailable"}
    assert not node.identifying


@pytest.mark.mosquitto
async def test_modern_node_ignores_retained_and_empty_commands(
    broker: Broker, sims: Sims,
) -> None:
    async with watcher(broker, f"{PREFIX}/event") as client:
        # A stale command left on the broker reaches a new subscriber retained.
        await client.publish(f"{PREFIX}/cmd", b"led on", qos=1, retain=True)
        node = await start(broker, sims, modern=True)
        await client.publish(f"{PREFIX}/cmd", b"", qos=1)
        await eventually(lambda: len(node.commands) == 2, what="both commands delivered")
        # Neither was answered: the first reply on the event topic is the ping's.
        reply = await command(client, "ping")
        assert reply["ok"] is True and "pong" in reply
    assert sorted(node.commands[:2]) == [b"", b"led on"] and node.led is False


@pytest.mark.mosquitto
async def test_crash_publishes_the_will_and_restart(broker: Broker, sims: Sims) -> None:
    node = await start(broker, sims, interval=30.0)
    async with watcher(broker, f"{PREFIX}/status") as client:
        assert await next_on(client, f"{PREFIX}/status") == b"online"
        await node.crash()
        assert not node.connected
        assert await next_on(client, f"{PREFIX}/status") == b"offline"
        await node.start()
        assert await next_on(client, f"{PREFIX}/status") == b"online"
        assert node.sessions == 2
        await node.stop()
        assert await next_on(client, f"{PREFIX}/status") == b"offline"


@pytest.mark.mosquitto
async def test_node_reconnects_after_broker_restart(broker: Broker, sims: Sims) -> None:
    node = await start(broker, sims, interval=30.0)
    broker.stop()
    await eventually(lambda: not node.connected, what="node disconnected")
    broker.start()
    assert await node.wait_connected(5.0)
    await eventually(lambda: node.sessions == 2, what="second session")
    async with watcher(broker, f"{PREFIX}/status") as client:
        assert await next_on(client, f"{PREFIX}/status") == b"online"


@pytest.mark.mosquitto
async def test_json_sensor(broker: Broker, sims: Sims) -> None:
    async with watcher(broker, "sensors/r204/co2") as client:
        sensor = SimJsonSensor(broker.settings(), command_topic="sensors/r204/co2/set",
                               interval_s=0.05, retain=True)
        sims.append(sensor)
        await sensor.start()
        first = json.loads(await next_on(client, "sensors/r204/co2"))
        assert first == {"ppm": 612.0, "bat": {"pct": 97}}
        second = json.loads(await next_on(client, "sensors/r204/co2"))
        assert 400.0 <= second["ppm"] <= 2000.0
        await client.publish("sensors/r204/co2/set", b'{"ppm": 999, "mode": "boost"}', qos=1)
        await client.publish("sensors/r204/co2/set", b"not json", qos=1)
        await eventually(lambda: sensor.writes, what="write applied")
        assert sensor.writes == [{"ppm": 999, "mode": "boost"}]
        assert sensor.state["mode"] == "boost"
    await sensor.stop()
    async with watcher(broker, "sensors/r204/co2") as client:
        retained = json.loads(await next_on(client, "sensors/r204/co2"))
    assert retained["mode"] == "boost"


def test_co2_walk_is_deterministic_and_bounded() -> None:
    def run(seed: int) -> list[float]:
        state: dict[str, Any] = {"ppm": 1995.0, "bat": {"pct": 1}}
        rng = random.Random(seed)
        values = []
        for _ in range(500):
            co2_walk(state, rng)
            values.append(state["ppm"])
        assert state["bat"]["pct"] in (0, 1)
        return values

    assert run(1) == run(1)
    assert max(run(1)) <= 2000.0 and min(run(2)) >= 400.0


@pytest.mark.mosquitto
async def test_start_and_stop_helpers(broker: Broker) -> None:
    devices = await start_sim_mqtt_devices(
        broker.settings(), nodes=["zephyr-01", "zephyr-02"], co2_topics=["a/co2"],
        modern=True, interval_s=0.1)
    try:
        assert len(devices) == 3
        for device in devices:
            assert await device.wait_connected(5.0)
        node = devices[0]
        assert isinstance(node, SimMqttTlsDevice) and node.modern
        sensor = devices[2]
        assert isinstance(sensor, SimJsonSensor) and sensor.command_topic == "a/co2/set"
    finally:
        await stop_sim_mqtt_devices(devices)
    assert not any(d.connected for d in devices)
