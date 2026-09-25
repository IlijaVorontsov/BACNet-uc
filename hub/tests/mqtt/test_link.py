"""BrokerLink edge cases that a real broker does not produce on demand: a
minimal scripted MQTT 3.1.1 server, or a patched aiomqtt failure."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiomqtt
import pytest

from uc_hub.core.types import Quality
from uc_hub.drivers.mqtt import BrokerLink, BrokerSettings

from .conftest import Broker, DriverFactory, eventually, record

NODE = "zephyr-5eed"
Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


async def read_packet(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """One control packet: (packet type, variable header and payload)."""
    header = (await reader.readexactly(1))[0]
    length, shift = 0, 0
    while True:
        byte = (await reader.readexactly(1))[0]
        length |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    return header >> 4, await reader.readexactly(length)


def publish_packet(topic: str, payload: bytes, *, retain: bool) -> bytes:
    """A QoS 0 PUBLISH shorter than 128 bytes."""
    name = topic.encode()
    body = len(name).to_bytes(2, "big") + name + payload
    assert len(body) < 128
    return bytes([0x30 | int(retain), len(body)]) + body


@pytest.fixture
async def scripted_broker() -> AsyncIterator[Callable[[Script], Awaitable[int]]]:
    """``port = await serve(script)``: accepts one connection, runs ``script``
    on it and refuses every later one."""
    servers: list[asyncio.Server] = []
    handlers: list[asyncio.Task[None]] = []

    async def serve(script: Script) -> int:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            server.close()
            handlers.append(asyncio.current_task())  # type: ignore[arg-type]
            try:
                await script(reader, writer)
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        return int(server.sockets[0].getsockname()[1])

    yield serve
    for server in servers:
        server.close()
    for task in handlers:
        task.cancel()
    await asyncio.gather(*handlers, return_exceptions=True)


async def test_session_lost_before_suback_marks_devices_down(
    scripted_broker: Callable[[Script], Awaitable[int]], make_driver: DriverFactory,
) -> None:
    """A broker may send retained messages before the SUBACK. When the
    session ends in between, the devices those messages brought online must
    go offline again, although the session never counted as up."""
    delivered = asyncio.Event()

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        kind, _ = await read_packet(reader)
        assert kind == 1                                   # CONNECT
        writer.write(b"\x20\x02\x00\x00")                  # CONNACK, accepted
        kind, _ = await read_packet(reader)
        assert kind == 8                                   # SUBSCRIBE
        writer.write(publish_packet(f"bacnet-uc/{NODE}/status", b"online", retain=True))
        writer.write(publish_packet(f"bacnet-uc/{NODE}/telemetry",
                                    b'{"seq":7,"uptime_s":60,"sessions":1}', retain=False))
        await writer.drain()
        await delivered.wait()
        # ...and the connection drops without a SUBACK.

    port = await scripted_broker(script)
    driver, rec = await make_driver(
        {"host": "127.0.0.1", "port": port, "timeout_s": 2.0, "reconnect_min_s": 0.05,
         "reconnect_max_s": 0.2},
        [(record("r204-node"), {"profile": "mqtt_tls", "client_id": NODE})], start=False)
    await driver.start()
    await eventually(lambda: rec.is_online("r204-node"), what="retained status delivered")
    await eventually(lambda: rec.last("hq/r204-node/telemetry.seq"), what="telemetry delivered")
    delivered.set()
    await eventually(lambda: rec.is_online("r204-node") is False, what="device marked offline")
    seq = rec.last("hq/r204-node/telemetry.seq")
    assert seq is not None and seq.quality is Quality.OFFLINE and seq.value == 7
    assert not driver.connected
    desc = await driver.describe("r204-node")
    assert desc.device.online is False


@pytest.mark.mosquitto
async def test_cancellation_survives_a_failing_disconnect(
    broker: Broker, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiomqtt's ``__aexit__`` raises MqttError when the DISCONNECT is not
    acknowledged within the timeout. A task cancelled from outside (not via
    ``stop()``, e.g. by ``asyncio.run`` shutting down) must still end instead
    of treating that as a lost connection and reconnecting forever."""
    original = aiomqtt.Client.__aexit__

    async def slow_disconnect(self: aiomqtt.Client, *exc_info: Any) -> None:
        await original(self, *exc_info)
        raise aiomqtt.MqttError("Operation timed out")

    # Patched before the session starts: "async with" looks __aexit__ up on entry.
    monkeypatch.setattr(aiomqtt.Client, "__aexit__", slow_disconnect)
    link = BrokerLink(BrokerSettings.from_settings(broker.settings()),
                      on_message=lambda topic, payload, retained: None)
    await link.start()
    assert await link.wait_connected(5.0)
    task = link._task
    assert task is not None
    task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=3.0)
        assert task in done and task.cancelled()
        assert not link.connected
    finally:
        await link.stop()
