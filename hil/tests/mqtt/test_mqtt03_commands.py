"""MQTT-03 command/event round trip and LED (release; ping and oversize also in SIL).

Catalogue MQTT-03: 100/100 JSON ping commands answered with the same id and ok:true. 'led on'
/ 'led off' give stim din do0 = 1/0 within 1 s (functional), 20/20. A payload over 128 B is
answered with 'payload too large', and a later ping still works.

do0 is LD1 (PB0), the app's led0 on nucleo_f767zi, active high.
"""

from __future__ import annotations

import time

import pytest

from hilrig.dutctl import DutControl
from hilrig.mqtt import Device
from hilrig.release import DutImage
from hilrig.stim import Stim

pytestmark = pytest.mark.release

PINGS, LED_RUNS = 100, 20
LED_LIMIT_S = 1.0
OVERSIZE = 200  # > CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE (128)


@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
def test_mqtt03_json_ping_round_trips(app_image: DutImage, mqtt: Device, dut_reset: DutControl) -> None:
    dut_reset.ensure_online()
    for i in range(PINGS):
        rid = f"hil-{i}"
        reply = mqtt.command("ping", request_id=rid)
        assert reply.get("id") == rid and reply.get("ok") is True, f"ping {i}: {reply}"


@pytest.mark.hil_only
@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
def test_mqtt03_led_reaches_the_pin(
    app_image: DutImage, mqtt: Device, stim: Stim, dut_reset: DutControl
) -> None:
    dut_reset.ensure_online()
    for run in range(LED_RUNS):
        for arg, level in (("on", 1), ("off", 0)):
            reply = mqtt.command("led", arg)
            assert reply.get("ok") is True and reply.get("led") is (level == 1), (
                f"run {run} led {arg}: {reply}"
            )
            deadline = time.monotonic() + LED_LIMIT_S
            while stim.din("do0") != level:
                assert time.monotonic() < deadline, f"run {run}: led {arg}, do0 not {level} within 1 s"


@pytest.mark.parametrize("app_image", ["mqtt"], indirect=True)
def test_mqtt03_oversized_payload_keeps_the_session(
    app_image: DutImage, mqtt: Device, dut_reset: DutControl
) -> None:
    dut_reset.ensure_online()
    reply = mqtt.command_text("x" * OVERSIZE)
    assert reply == {"ok": False, "error": "payload too large"}
    later = mqtt.command("ping", request_id="after-oversize")
    assert later.get("id") == "after-oversize" and later.get("ok") is True
