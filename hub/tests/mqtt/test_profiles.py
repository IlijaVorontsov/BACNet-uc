"""Profile decoding and command correlation, without a broker."""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import pytest

from uc_hub.core.errors import DeviceError, DeviceTimeout, InvalidRequest, Unsupported
from uc_hub.core.types import PointKind, Quality
from uc_hub.drivers.mqtt.profiles import (
    GenericJsonProfile,
    MqttTlsProfile,
    coerce,
    make_profile,
    map_units,
)

from .conftest import SITE, record

PREFIX = "bacnet-uc/zephyr-0a1b"
MODERN_INFO = {
    "board": "nucleo_h563zi", "zephyr": "3.7.2", "ip": "10.0.2.60", "tls": True,
    "fw": "0.3.0", "hwid": "0a1b",
    "caps": {"cmds": ["ping", "led", "identify"],
             "telemetry": {"seq": "count", "uptime_s": "s", "temp_c": "C",
                           "rh": {"units": "%RH", "datatype": "real"}, "bad field": "x"}},
}


class FakeSend:
    """Captures publishes and lets a test answer them on the event topic."""

    def __init__(self, profile: MqttTlsProfile) -> None:
        self.profile = profile
        self.sent: list[tuple[str, bytes]] = []
        self.published = asyncio.Event()

    async def __call__(self, topic: str, payload: bytes) -> None:
        self.sent.append((topic, payload))
        self.published.set()

    def reply(self, doc: dict[str, Any]) -> list[str]:
        return self.profile.handle(f"{PREFIX}/event", json.dumps(doc).encode(), 1.0)

    async def next_command(self) -> tuple[str, bytes]:
        await asyncio.wait_for(self.published.wait(), 1.0)
        self.published.clear()
        return self.sent[-1]


def tls_profile(**spec: Any) -> MqttTlsProfile:
    profile = make_profile(SITE, record("r204-node"),
                           {"profile": "mqtt_tls", "client_id": "zephyr-0a1b", **spec})
    assert isinstance(profile, MqttTlsProfile)
    return profile


def feed(profile: MqttTlsProfile, leaf: str, payload: Any, now: float = 1.0) -> list[str]:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return profile.handle(f"{PREFIX}/{leaf}", data, now)


# -- coercion ------------------------------------------------------------------
@pytest.mark.parametrize(("value", "datatype", "expected"), [
    (21, "real", 21.0),
    ("21.5", "real", 21.5),
    (True, "real", 1.0),
    (3.0, "int", 3),
    ("7", "int", 7),
    ("7.0", "int", 7),
    (2, "enum", 2),
    (1, "bool", True),
    (0.0, "bool", False),
    ("ON", "bool", True),
    (" off ", "bool", False),
    ("inactive", "bool", False),
    (12, "string", "12"),
    (False, "string", "false"),
    ({"a": [1]}, "string", '{"a":[1]}'),
    (None, "real", None),
])
def test_coerce(value: Any, datatype: Any, expected: Any) -> None:
    result = coerce(value, datatype)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(("value", "datatype"), [
    ("abc", "real"), (math.nan, "real"), (math.inf, "int"), (2.5, "int"), (-1, "enum"),
    (2, "bool"), ("maybe", "bool"), ([1], "real"), ({"a": 1}, "int"),
])
def test_coerce_rejects(value: Any, datatype: Any) -> None:
    with pytest.raises(ValueError):
        coerce(value, datatype)


def test_units() -> None:
    assert map_units("s") == "seconds"
    assert map_units("°C") == "degrees-celsius"
    assert map_units("count") is None
    assert map_units(None) is None
    assert map_units("furlongs") == "furlongs"


# -- mqtt_tls -------------------------------------------------------------------
def test_legacy_points() -> None:
    profile = tls_profile()
    assert profile.prefix == PREFIX
    assert profile.filters == [f"{PREFIX}/{leaf}" for leaf in
                               ("status", "info", "telemetry", "event")]
    points = {p.ref.obj: p for p in profile.points()}
    assert list(points) == ["status", "telemetry.seq", "telemetry.uptime_s",
                            "telemetry.sessions", "led"]
    assert points["led"].writable and points["led"].kind is PointKind.OUTPUT
    assert points["led"].datatype == "bool"
    assert points["telemetry.uptime_s"].units == "seconds"
    assert points["telemetry.seq"].datatype == "int"
    assert points["status"].source == f"mqtt:{PREFIX}/status"
    assert str(points["status"].ref) == "hq/r204-node/status"
    assert not any(p.commandable for p in points.values())


def test_client_id_from_address() -> None:
    profile = make_profile(SITE, record("n1", "site-a/floor2/node-1"), {"profile": "mqtt_tls"})
    assert isinstance(profile, MqttTlsProfile)
    assert profile.prefix == "site-a/floor2/node-1"
    assert profile.topic_root == "site-a/floor2"
    overridden = make_profile(SITE, record("n1", "a/node-1"),
                              {"profile": "mqtt_tls", "topic_root": "b"})
    assert isinstance(overridden, MqttTlsProfile)
    assert overridden.prefix == "b/node-1"


@pytest.mark.parametrize("spec", [
    {"profile": "mqtt_tls"},                                   # no client_id, no address
    {"profile": "mqtt_tls", "client_id": "a/b"},
    {"profile": "mqtt_tls", "client_id": "a+"},
    {"profile": "mqtt_tls", "client_id": "x", "topic_root": "#"},
    {"profile": "mqtt_tls", "client_id": "x", "topic_root": "/root"},
    {"profile": "mqtt_tls", "client_id": 5},
    {"profile": "sparkplug"},
    {"profile": None},
])
def test_invalid_mqtt_tls_specs(spec: dict[str, Any]) -> None:
    with pytest.raises(InvalidRequest):
        make_profile(SITE, record("n1"), spec)


def test_status_and_telemetry() -> None:
    profile = tls_profile()
    assert profile.reading("status", 5.0, 300, True).quality is Quality.OFFLINE
    assert feed(profile, "status", b"online") == ["status"]
    assert profile.online is True
    changed = feed(profile, "telemetry", {"seq": 4, "uptime_s": 60, "sessions": 1, "x": 1})
    assert changed == ["telemetry.seq", "telemetry.uptime_s", "telemetry.sessions"]
    reading = profile.reading("telemetry.seq", 2.0, 300, True)
    assert (reading.value, reading.quality, reading.ts) == (4, Quality.GOOD, 1.0)
    assert profile.reading("telemetry.seq", 400.0, 300, True).quality is Quality.STALE
    # Status is event driven: it never goes stale.
    assert profile.reading("status", 400.0, 300, True).quality is Quality.GOOD
    assert profile.reading("led", 2.0, 300, True).quality is Quality.OFFLINE
    assert profile.reading("nope", 2.0, 300, True).quality is Quality.FAULT
    down = profile.reading("telemetry.seq", 2.0, 300, False)
    assert (down.value, down.quality, down.error) == (4, Quality.OFFLINE,
                                                      "MQTT broker not connected")


def test_offline_status_and_forgotten_led() -> None:
    profile = tls_profile()
    feed(profile, "status", b"online")
    feed(profile, "event", {"led": True})
    assert profile.reading("led", 1.0, 300, True).value is True
    feed(profile, "telemetry", {"seq": 1, "uptime_s": 2, "sessions": 1})
    assert feed(profile, "status", b"offline") == ["status"]
    assert profile.online is False
    status = profile.reading("status", 1.0, 300, True)
    assert (status.value, status.quality) == ("offline", Quality.GOOD)
    seq = profile.reading("telemetry.seq", 1.0, 300, True)
    assert (seq.value, seq.quality, seq.error) == (1, Quality.OFFLINE, "device offline")
    assert profile.reading("led", 1.0, 300, True).value is None
    # A cleared retained status (empty payload) also means offline.
    feed(profile, "status", b"online")
    feed(profile, "status", b"")
    assert profile.online is False


def test_live_traffic_without_status_means_online() -> None:
    profile = tls_profile()
    feed(profile, "telemetry", {"seq": 1})
    assert profile.online is True


def test_malformed_payloads_are_noted_not_raised() -> None:
    profile = tls_profile()
    assert feed(profile, "status", b"rebooting") == []
    assert profile.online is None
    assert feed(profile, "telemetry", b"\xff\xfe") == []
    assert feed(profile, "telemetry", b"[1, 2]") == []
    assert feed(profile, "info", b"not json") == []
    assert feed(profile, "event", b"[" * 100_000) == []
    assert profile.extra()["last_error"] == "event: not a JSON object"
    changed = feed(profile, "telemetry", {"seq": "many", "uptime_s": 1.5, "sessions": 2})
    assert changed == ["telemetry.seq", "telemetry.uptime_s", "telemetry.sessions"]
    seq = profile.reading("telemetry.seq", 1.0, 300, True)
    assert seq.quality is Quality.FAULT and "not int" in (seq.error or "")
    assert profile.reading("telemetry.uptime_s", 1.0, 300, True).quality is Quality.FAULT
    assert feed(profile, "cmd", b"led on") == []
    assert profile.handle("other/topic", b"{}", 1.0) == []


def test_caps_define_points_and_record() -> None:
    profile = tls_profile()
    feed(profile, "telemetry", {"seq": 1, "sessions": 1})
    feed(profile, "info", MODERN_INFO)
    points = {p.ref.obj: p for p in profile.points()}
    assert list(points) == ["status", "telemetry.seq", "telemetry.uptime_s",
                            "telemetry.temp_c", "telemetry.rh", "led"]
    assert points["telemetry.temp_c"].units == "degrees-celsius"
    assert points["telemetry.temp_c"].datatype == "real"
    assert points["telemetry.rh"].units == "percent-relative-humidity"
    assert points["telemetry.uptime_s"].datatype == "int"
    # The sessions sample belongs to a point that no longer exists.
    assert "telemetry.sessions" not in profile.samples
    assert profile.json_commands
    assert profile.record.model == "nucleo_h563zi"
    assert profile.record.firmware == "0.3.0"
    assert profile.record.hwid == "0a1b"
    assert profile.extra()["info"] == MODERN_INFO


def test_legacy_info_sets_record() -> None:
    profile = tls_profile()
    feed(profile, "info", {"board": "nucleo_h563zi", "zephyr": "3.7.2", "ip": "x", "tls": True})
    assert profile.record.firmware == "zephyr 3.7.2"
    assert profile.record.hwid == ""
    assert not profile.json_commands
    assert profile.commands == ("ping", "led")


def test_caps_without_led() -> None:
    profile = tls_profile()
    feed(profile, "info", {"caps": {"cmds": ["ping"]}})
    assert [p.ref.obj for p in profile.points()] == [
        "status", "telemetry.seq", "telemetry.uptime_s", "telemetry.sessions"]
    assert feed(profile, "event", {"led": True}) == []


async def test_json_command_correlation() -> None:
    profile = tls_profile()
    feed(profile, "info", MODERN_INFO)
    feed(profile, "status", b"online")
    send = FakeSend(profile)
    first = asyncio.create_task(profile.command(send, "ping", None, 1.0))
    topic, payload = await send.next_command()
    assert topic == f"{PREFIX}/cmd"
    ping = json.loads(payload)
    assert ping["cmd"] == "ping" and "arg" not in ping and 0 < len(ping["id"]) <= 16
    second = asyncio.create_task(profile.write("led", True, send, 1.0))
    _, payload = await send.next_command()
    led = json.loads(payload)
    assert (led["cmd"], led["arg"]) == ("led", "on")
    # Replies out of order, plus one for somebody else and one without an ID.
    assert send.reply({"id": "stranger", "ok": True, "pong": 1}) == []
    send.reply({"pong": 2})
    assert send.reply({"id": led["id"], "ok": True, "led": True}) == ["led"]
    assert await second is True
    assert not first.done()
    send.reply({"id": ping["id"], "ok": True, "pong": 3})
    assert await first == {"id": ping["id"], "ok": True, "pong": 3}


async def test_json_command_error_and_timeout() -> None:
    profile = tls_profile()
    feed(profile, "info", MODERN_INFO)
    send = FakeSend(profile)
    task = asyncio.create_task(profile.command(send, "led", "blink", 1.0))
    _, payload = await send.next_command()
    send.reply({"id": json.loads(payload)["id"], "ok": False, "error": "unknown command"})
    with pytest.raises(DeviceError, match="led failed: unknown command"):
        await task
    with pytest.raises(DeviceTimeout, match="no reply"):
        await profile.command(send, "ping", None, 0.05)
    with pytest.raises(Unsupported):
        await profile.command(send, "reboot", None, 0.05)


async def test_text_command_takes_next_matching_reply() -> None:
    profile = tls_profile()
    send = FakeSend(profile)
    write = asyncio.create_task(profile.write("led", "on", send, 1.0))
    topic, payload = await send.next_command()
    assert (topic, payload) == (f"{PREFIX}/cmd", b"led on")
    send.reply({"pong": 12})          # not the reply we wait for
    assert not write.done()
    send.reply({"led": True})
    assert await write is True
    ping = asyncio.create_task(profile.command(send, "ping", None, 1.0))
    await send.next_command()
    send.reply({"error": "led unavailable", "code": -19})
    with pytest.raises(DeviceError, match=r"led unavailable \(code -19\)"):
        await ping


async def test_text_commands_are_serialized() -> None:
    profile = tls_profile()
    send = FakeSend(profile)
    first = asyncio.create_task(profile.command(send, "ping", None, 1.0))
    second = asyncio.create_task(profile.command(send, "ping", None, 1.0))
    await send.next_command()
    await asyncio.sleep(0.01)
    assert len(send.sent) == 1        # the second waits for the first reply
    send.reply({"pong": 1})
    assert await first == {"pong": 1}
    await send.next_command()
    send.reply({"pong": 2})
    assert await second == {"pong": 2}


async def test_offline_fails_commands() -> None:
    profile = tls_profile()
    send = FakeSend(profile)
    task = asyncio.create_task(profile.command(send, "ping", None, 5.0))
    await send.next_command()
    feed(profile, "status", b"offline")
    with pytest.raises(DeviceTimeout, match="went offline"):
        await task
    with pytest.raises(DeviceTimeout, match="is offline"):
        await profile.command(send, "ping", None, 5.0)
    feed(profile, "status", b"online")
    task = asyncio.create_task(profile.command(send, "ping", None, 5.0))
    await send.next_command()
    profile.link_lost()
    with pytest.raises(DeviceTimeout, match="broker connection lost"):
        await task


async def test_identify_needs_caps() -> None:
    profile = tls_profile()
    send = FakeSend(profile)
    with pytest.raises(Unsupported, match="identify"):
        await profile.identify(send, 30, 1.0)
    feed(profile, "info", MODERN_INFO)
    task = asyncio.create_task(profile.identify(send, 12, 1.0))
    _, payload = await send.next_command()
    body = json.loads(payload)
    assert (body["cmd"], body["arg"]) == ("identify", "12")
    send.reply({"id": body["id"], "ok": True, "identify": 12})
    await task


async def test_write_validation() -> None:
    profile = tls_profile()
    send = FakeSend(profile)
    with pytest.raises(InvalidRequest):
        await profile.write("led", "dim", send, 1.0)
    with pytest.raises(InvalidRequest):
        await profile.write("telemetry.seq", 1, send, 1.0)
    assert send.sent == []


# -- generic-json ----------------------------------------------------------------
CO2_SPEC: dict[str, Any] = {
    "profile": "generic-json",
    "topic": "sensors/r204/co2",
    "command_topic": "sensors/r204/co2/set",
    "points": [
        {"id": "co2", "path": "$.ppm", "units": "parts-per-million", "datatype": "real",
         "tags": ["Zone_Air_CO2_Sensor"]},
        {"id": "battery", "path": "$.bat.pct", "units": "percent", "datatype": "int"},
        {"id": "setpoint", "path": "$.sp", "writable": True, "name": "CO2 setpoint"},
        {"id": "fan", "path": "$.fan", "datatype": "bool", "writable": True,
         "command_topic": "sensors/r204/fan/set", "kind": "value"},
    ],
}


def generic(spec: dict[str, Any] | None = None) -> GenericJsonProfile:
    profile = make_profile(SITE, record("r204-co2", "sensors/r204/co2"), spec or CO2_SPEC)
    assert isinstance(profile, GenericJsonProfile)
    return profile


def test_generic_points() -> None:
    points = {p.ref.obj: p for p in generic().points()}
    assert list(points) == ["co2", "battery", "setpoint", "fan"]
    co2 = points["co2"]
    assert co2.source == "mqtt:sensors/r204/co2 $.ppm"
    assert co2.tags == ["Zone_Air_CO2_Sensor"]
    assert (co2.kind, co2.writable, co2.units) == (PointKind.INPUT, False, "parts-per-million")
    assert points["setpoint"].kind is PointKind.OUTPUT
    assert points["setpoint"].name == "CO2 setpoint"
    assert points["setpoint"].meta["command_topic"] == "sensors/r204/co2/set"
    assert points["fan"].kind is PointKind.VALUE
    assert points["fan"].meta["command_topic"] == "sensors/r204/fan/set"
    assert points["battery"].source == "mqtt:sensors/r204/co2 $.bat.pct"


def test_generic_decoding() -> None:
    profile = generic()
    assert profile.reading("co2", 1.0, 300, True).error == "no value received yet"
    changed = profile.handle("sensors/r204/co2", b'{"ppm": 640, "bat": {"pct": "88"}}', 1.0)
    assert changed == ["co2", "battery"]
    assert profile.online is True
    assert profile.reading("co2", 2.0, 300, True).value == 640.0
    assert profile.reading("battery", 2.0, 300, True).value == 88
    assert profile.reading("co2", 302.0, 300, True).quality is Quality.STALE
    # A partial update leaves the other points alone.
    assert profile.handle("sensors/r204/co2", b'{"ppm": "n/a"}', 3.0) == ["co2"]
    bad = profile.reading("co2", 3.0, 300, True)
    assert bad.quality is Quality.FAULT and bad.value is None
    assert profile.reading("battery", 3.0, 300, True).value == 88
    assert profile.handle("sensors/r204/co2", b'{"other": 1}', 4.0) == []
    assert "none of the configured paths" in (profile.extra()["last_error"] or "")


def test_generic_plain_text_payload() -> None:
    profile = generic({"profile": "generic-json", "topic": "plant/+/state",
                       "points": [{"id": "state", "path": "$", "datatype": "bool"},
                                  {"id": "raw", "path": "$", "datatype": "string"}]})
    assert profile.handle("plant/pump1/state", b"ON", 1.0) == ["state", "raw"]
    assert profile.reading("state", 1.0, 300, True).value is True
    assert profile.reading("raw", 1.0, 300, True).value == "ON"
    profile.handle("plant/pump1/state", b"21.5", 2.0)
    assert profile.reading("raw", 2.0, 300, True).value == "21.5"


def test_generic_default_path_is_the_id() -> None:
    profile = generic({"profile": "generic-json", "topic": "t",
                       "points": [{"id": "temp"}]})
    profile.handle("t", b'{"temp": 20}', 1.0)
    assert profile.reading("temp", 1.0, 300, True).value == 20.0


async def test_generic_write() -> None:
    profile = generic()
    sent: list[tuple[str, bytes]] = []

    async def send(topic: str, payload: bytes) -> None:
        sent.append((topic, payload))

    assert await profile.write("setpoint", "900", send, 1.0) == 900.0
    assert await profile.write("fan", 1, send, 1.0) is True
    assert sent == [("sensors/r204/co2/set", b'{"setpoint":900.0}'),
                    ("sensors/r204/fan/set", b'{"fan":true}')]
    with pytest.raises(InvalidRequest):
        await profile.write("co2", 1, send, 1.0)
    with pytest.raises(InvalidRequest):
        await profile.write("setpoint", "high", send, 1.0)
    with pytest.raises(InvalidRequest):
        await profile.write("setpoint", None, send, 1.0)
    assert len(sent) == 2


@pytest.mark.parametrize("change", [
    {"topic": None},
    {"topic": "a/#/b"},
    {"topic": "a/b+"},
    {"command_topic": "a/+/set"},
    {"points": []},
    {"points": "co2"},
    {"points": [{"id": "co2", "path": "$.ppm["}]},
    {"points": [{"id": "bad id", "path": "$"}]},
    {"points": [{"id": "a", "path": "$"}, {"id": "a", "path": "$.x"}]},
    {"points": [{"id": "a", "datatype": "float"}]},
    {"points": [{"id": "a", "writable": "yes"}]},
    {"points": [{"id": "a", "kind": "sensor"}]},
    {"points": [{"id": "a", "tags": "x"}]},
    {"points": [{"id": "a", "units": 5}]},
    {"points": ["co2"]},
    {"command_topic": None, "points": [{"id": "a", "writable": True}]},
])
def test_invalid_generic_specs(change: dict[str, Any]) -> None:
    spec = {**CO2_SPEC, **change}
    with pytest.raises(InvalidRequest):
        make_profile(SITE, record("r204-co2"), spec)
