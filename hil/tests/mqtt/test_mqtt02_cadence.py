"""MQTT-02 telemetry cadence at QoS 1 (release; hardware and SIL; slow).

Catalogue MQTT-02: in a 300 s window: 30 +- 1 telemetry messages (functional count), seq
strictly +1. Every QoS 1 PUBLISH has a PUBACK with the same message id. No DISCONNECT or
reconnect. Test timeout 420 s.

The expected count follows the image's publish interval (10 s in release images). The PUBACK
pairing needs the decrypted session (broker key log). It reads every MQTT message of a TCP
segment (``Capture.pdus``): the DUT bursts PUBLISHes and the broker coalesces PUBACKs, and the
first-value fields of ``Capture.rows`` would hide all but the first one.
"""

from __future__ import annotations

import itertools
import json
import time

import pytest

from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.dutctl import DutControl
from hilrig.mqtt import Device
from hilrig.release import DutImage
from hilrig.services import RigServices

pytestmark = [pytest.mark.release, pytest.mark.slow]

WINDOW_S = 300.0
PUBLISH, PUBACK = "3", "4"  # MQTT control packet types


@pytest.mark.timeout(420)
@pytest.mark.capture("tcp port 8883")
@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
def test_mqtt02_telemetry_cadence(
    app_image: DutImage,
    mqtt: Device,
    capture: Capture,
    services: RigServices,
    dut_reset: DutControl,
    bench: Bench,
) -> None:
    dut_reset.ensure_online()
    telemetry, status = mqtt.topic("telemetry"), mqtt.topic("status")
    since, start = mqtt.client.mark(), time.monotonic()
    wall_start = time.time()
    time.sleep(WINDOW_S)
    wall_end = time.time()
    capture.stop()
    records = [
        json.loads(m.payload) for m in mqtt.client.messages(telemetry, since) if m.t - start <= WINDOW_S
    ]
    expected = WINDOW_S / app_image.publish_interval_s
    assert abs(len(records) - expected) <= 1, (
        f"{len(records)} telemetry messages in 300 s, expected {expected:.0f} +- 1"
    )
    seqs = [r["seq"] for r in records]
    assert all(b - a == 1 for a, b in itertools.pairwise(seqs)), f"seq not strictly +1: {seqs}"
    assert not mqtt.client.messages(status, since), "the DUT announced its status again: it reconnected"

    dut = bench.dut.ip
    window = f"frame.time_epoch >= {wall_start} && frame.time_epoch <= {wall_end}"
    fields = ("mqtt.msgtype", "mqtt.qos", "mqtt.msgid", "mqtt.topic", "ip.src")
    messages = capture.pdus(f"mqtt && ip.addr == {dut}", "mqtt", *fields)
    in_window = [m for m in messages if wall_start <= m.t <= wall_end]
    publishes = [m for m in in_window if m["ip.src"] == dut and m["mqtt.msgtype"] == PUBLISH]
    if not publishes and len(records):
        pytest.skip(f"session not decrypted, PUBACKs not paired: {services.broker.keylog_status}")
    qos = {m["mqtt.qos"] for m in publishes if m["mqtt.topic"] == telemetry}
    assert qos == {"1"}, f"telemetry PUBLISH QoS {sorted(qos)}, expected 1"
    acks = {m["mqtt.msgid"] for m in messages if m["ip.src"] != dut and m["mqtt.msgtype"] == PUBACK}
    unacked = [m["mqtt.msgid"] for m in publishes if m["mqtt.qos"] == "1" and m["mqtt.msgid"] not in acks]
    assert not unacked, f"QoS 1 PUBLISH without PUBACK: message ids {unacked}"
    assert not capture.rows(f"mqtt.msgtype == 14 && ip.src == {dut} && {window}"), "the DUT sent DISCONNECT"
    assert not capture.rows(f"mqtt.msgtype == 1 && ip.src == {dut} && {window}"), (
        "the DUT reconnected (CONNECT)"
    )
