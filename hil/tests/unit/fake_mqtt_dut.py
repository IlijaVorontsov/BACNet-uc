"""A stand-in for the DUT's MQTT client (apps/mqtt_tls behaviour) for rig self-tests.

It connects over TLS with a last will ``offline`` on its status topic, subscribes to its
command topic, publishes retained ``online`` and an info record, and answers commands the
way apps/mqtt_tls/README.md describes: plain text or JSON, ``id`` echoed, retained and
empty commands ignored. It proves the rig's broker, PKI and observer work, not the DUT.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.reasoncodes import ReasonCode

from hilrig import netns
from hilrig.mqtt import TOPIC_ROOT, TlsOptions

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,16}$")


class FakeMqttDut:
    """Fake DUT MQTT client in a namespace."""

    def __init__(self, host: str, port: int, *, device_id: str, ns: str, tls: TlsOptions) -> None:
        self.device_id, self.ns = device_id, ns
        self.base = f"{TOPIC_ROOT}/{device_id}"
        self.led = False
        self.seq = 0
        self.handled: list[str] = []
        self._host, self._port = host, port
        self._t0 = time.monotonic()
        self._ready = threading.Event()
        self._client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=device_id,
            protocol=MQTTProtocolVersion.MQTTv311,
            clean_session=True,
        )
        self._client.tls_set_context(tls.context())
        self._client.will_set(f"{self.base}/status", b"offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_subscribe = self._on_subscribe
        self._client.on_message = self._on_message

    def start(self, timeout: float = 10.0) -> FakeMqttDut:
        """Connect and wait until the status is announced."""
        with netns.enter(self.ns):
            self._client.connect(self._host, self._port, keepalive=30)
            self._client.loop_start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TimeoutError("fake DUT did not come online")
        return self

    def stop(self) -> None:
        """Disconnect cleanly (the broker discards the last will)."""
        self._client.disconnect()
        self._client.loop_stop()

    def drop(self) -> None:
        """Vanish without DISCONNECT, so the broker publishes the last will."""
        self._client.loop_stop()
        sock = self._client.socket()
        if isinstance(sock, socket.socket):
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    def publish_telemetry(self) -> None:
        """Publish one telemetry record."""
        self.seq += 1
        body = {"seq": self.seq, "uptime_s": int(time.monotonic() - self._t0), "sessions": 1}
        self._client.publish(f"{self.base}/telemetry", json.dumps(body), qos=1).wait_for_publish(5)

    def _on_connect(self, client: paho.Client, _u: object, _f: object, rc: ReasonCode, _p: object) -> None:
        if not rc.is_failure:
            client.subscribe(f"{self.base}/cmd", qos=1)

    def _on_subscribe(self, client: paho.Client, _u: object, _mid: int, _rcs: object, _p: object) -> None:
        info = {
            "fw": "0.0.0-fake",
            "board": "fake",
            "zephyr": "4.4.2",
            "hwid": "00",
            "mac": "02:00:00:00:00:10",
            "ip": "192.0.2.10",
            "tls": True,
            "caps": {
                "cmds": ["ping", "led", "identify"],
                "telemetry": {"seq": "count", "uptime_s": "s", "sessions": "count"},
            },
        }
        client.publish(f"{self.base}/info", json.dumps(info), qos=1, retain=True)
        client.publish(f"{self.base}/status", b"online", qos=1, retain=True)
        self._ready.set()

    def _on_message(self, client: paho.Client, _u: object, msg: paho.MQTTMessage) -> None:
        if msg.retain or not msg.payload:
            return  # replayed or cleared retained commands are ignored
        text = msg.payload.decode(errors="replace")
        self.handled.append(text)
        client.publish(f"{self.base}/event", json.dumps(self._answer(text)), qos=1)

    def _answer(self, text: str) -> dict[str, object]:
        request_id = None
        if text.startswith("{"):
            try:
                body = json.loads(text)
            except ValueError:
                return {"ok": False, "error": "invalid json"}
            request_id = body.get("id")
            if request_id is not None and not (isinstance(request_id, str) and _ID.match(request_id)):
                return {"ok": False, "error": "invalid id"}
            cmd, arg = body.get("cmd"), body.get("arg")
            if cmd is None:
                return self._with_id({"ok": False, "error": "missing cmd"}, request_id)
        else:
            cmd, _, arg = text.partition(" ")
        return self._with_id(self._run(str(cmd), str(arg) if arg else None), request_id)

    @staticmethod
    def _with_id(reply: dict[str, object], request_id: str | None) -> dict[str, object]:
        return {"id": request_id, **reply} if request_id else reply

    def _run(self, cmd: str, arg: str | None) -> dict[str, object]:
        if cmd == "ping":
            return {"ok": True, "pong": int(time.monotonic() - self._t0)}
        if cmd == "led" and arg in ("on", "off", "toggle"):
            self.led = (not self.led) if arg == "toggle" else arg == "on"
            return {"ok": True, "led": self.led}
        if cmd == "identify" and (arg is None or arg.isdigit()):
            return {"ok": True, "identify": 30 if arg is None else int(arg)}
        if cmd in ("led", "identify"):
            return {"ok": False, "error": "bad argument"}
        return {"ok": False, "error": "unknown command"}
