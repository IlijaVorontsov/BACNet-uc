"""MQTT observer and commander for the DUT's topics (paho-mqtt 2.x).

Topic scheme of apps/mqtt_tls (origin/claude/inter-session-communication-h989ye), with
``<root>`` = ``bacnet-uc`` by default and ``<id>`` the DUT's MQTT client ID:

=========================  ========  ========  =============================================
topic                      from      retained  payload
=========================  ========  ========  =============================================
``<root>/<id>/status``     device    yes       ``online``, or the last will ``offline``
``<root>/<id>/info``       device    yes       JSON: fw, board, zephyr, hwid, mac, ip, tls, caps
``<root>/<id>/telemetry``  device    no        JSON: seq, uptime_s, sessions
``<root>/<id>/cmd``        host      never     ``ping``, ``led on``, ``identify 10`` or JSON
                                               ``{"cmd": ..., "arg": ..., "id": ...}``
``<root>/<id>/event``      device    no        JSON reply, echoing a valid ``id``
=========================  ========  ========  =============================================

Clients created with ``netns`` open their socket and run their network thread inside that
namespace (see :func:`hilrig.netns.enter`), so reconnects stay there too.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.reasoncodes import ReasonCode

from hilrig import netns as nsmod

TOPIC_ROOT = "bacnet-uc"
KINDS = ("status", "info", "telemetry", "cmd", "event")


class MqttError(RuntimeError):
    """The broker refused the connection or did not acknowledge in time."""


@dataclass(frozen=True)
class TlsOptions:
    """Client TLS: trust anchor, optional client certificate (mTLS), version pin, key log."""

    ca: Path
    cert: Path | None = None
    key: Path | None = None
    version: Literal["1.2", "1.3"] | None = None
    keylog: Path | None = None

    def context(self) -> ssl.SSLContext:
        """Return an SSLContext that verifies the broker's certificate and host name."""
        ctx = ssl.create_default_context(cafile=str(self.ca))
        if self.cert:
            ctx.load_cert_chain(str(self.cert), str(self.key) if self.key else None)
        if self.version:
            pinned = ssl.TLSVersion.TLSv1_2 if self.version == "1.2" else ssl.TLSVersion.TLSv1_3
            ctx.minimum_version = ctx.maximum_version = pinned
        if self.keylog:
            ctx.keylog_filename = str(self.keylog)
        return ctx


@dataclass(frozen=True)
class Message:
    """A received message; ``t`` is host ``time.monotonic()`` at reception."""

    topic: str
    payload: bytes
    retain: bool
    qos: int
    t: float

    def text(self) -> str:
        """Return the payload as UTF-8 text."""
        return self.payload.decode()

    def json(self) -> Any:
        """Return the payload parsed as JSON."""
        return json.loads(self.payload)


class MqttClient:
    """A paho client that records every message it receives and can wait for one."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        netns: str | None = None,
        tls: TlsOptions | None = None,
        client_id: str = "hil-observer",
        keepalive: int = 30,
    ) -> None:
        self.host, self.port, self.netns, self.keepalive = host, port, netns, keepalive
        self._client = paho.Client(
            CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTProtocolVersion.MQTTv311,
            clean_session=True,
        )
        if tls:
            self._client.tls_set_context(tls.context())
        self._cond = threading.Condition()
        self._messages: list[Message] = []
        self._connack: ReasonCode | None = None
        self._closed_by: ReasonCode | None = None
        self._subacks: dict[int, list[ReasonCode]] = {}
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_subscribe = self._on_subscribe
        self._client.on_disconnect = self._on_disconnect

    # ---- paho callbacks (network thread) ------------------------------------------------
    def _on_connect(self, _c: paho.Client, _u: object, _f: object, rc: ReasonCode, _p: object) -> None:
        with self._cond:
            self._connack = rc
            self._cond.notify_all()

    def _on_disconnect(self, _c: paho.Client, _u: object, _f: object, rc: ReasonCode, _p: object) -> None:
        with self._cond:
            self._closed_by = rc
            self._cond.notify_all()

    def _on_message(self, _c: paho.Client, _u: object, msg: paho.MQTTMessage) -> None:
        with self._cond:
            self._messages.append(
                Message(msg.topic, bytes(msg.payload), bool(msg.retain), msg.qos, time.monotonic())
            )
            self._cond.notify_all()

    def _on_subscribe(self, _c: paho.Client, _u: object, mid: int, rcs: list[ReasonCode], _p: object) -> None:
        with self._cond:
            self._subacks[mid] = rcs
            self._cond.notify_all()

    def _wait(self, check: Callable[[], Any], timeout: float, what: str) -> Any:
        with self._cond:
            result = self._cond.wait_for(check, timeout)
        if not result:
            raise TimeoutError(f"{what} within {timeout} s")
        return result

    # ---- API ------------------------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> MqttClient:
        """Connect and wait for CONNACK 0.

        A TLS 1.2 handshake failure raises from here (ssl.SSLError). Under TLS 1.3 a broker
        that rejects the client certificate closes the connection after the handshake;
        that, like a CONNACK refusal, raises :class:`MqttError`.
        """
        with nsmod.enter(self.netns) if self.netns else contextlib.nullcontext():
            self._client.connect(self.host, self.port, keepalive=self.keepalive)
            self._client.loop_start()
        try:
            self._wait(
                lambda: self._connack is not None or self._closed_by is not None,
                timeout,
                f"no CONNACK from {self.host}:{self.port}",
            )
        except BaseException:
            self.close()  # the network thread and its socket would outlive the error
            raise
        rc = self._connack
        if rc is None or rc.is_failure:
            self.close()
            reason = rc if rc is not None else f"connection closed before CONNACK ({self._closed_by})"
            raise MqttError(f"{self.host}:{self.port} refused the connection: {reason}")
        return self

    def close(self) -> None:
        """Disconnect cleanly (no last will) and stop the network thread."""
        self._client.disconnect()
        self._client.loop_stop()

    def __enter__(self) -> MqttClient:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def subscribe(self, topic_filter: str, qos: int = 1, timeout: float = 10.0) -> None:
        """Subscribe and wait for a SUBACK that grants it."""
        rc, mid = self._client.subscribe(topic_filter, qos)
        if mid is None:
            raise MqttError(f"subscribe to {topic_filter} not sent: {rc}")
        self._wait(lambda: mid in self._subacks, timeout, f"no SUBACK for {topic_filter}")
        rcs = self._subacks[mid]
        if any(rc.is_failure for rc in rcs):
            raise MqttError(f"subscription to {topic_filter} refused: {rcs}")

    def publish(
        self, topic: str, payload: bytes | str, qos: int = 1, retain: bool = False, timeout: float = 10.0
    ) -> None:
        """Publish and, for QoS 1, wait for the PUBACK."""
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise MqttError(f"publish to {topic} not acknowledged within {timeout} s")

    def mark(self) -> int:
        """Return a position in the message log, for ``since`` in :meth:`wait_for`."""
        with self._cond:
            return len(self._messages)

    def messages(self, topic_filter: str = "#", since: int = 0) -> list[Message]:
        """Return the messages received on ``topic_filter`` (wildcards allowed)."""
        with self._cond:
            return [m for m in self._messages[since:] if paho.topic_matches_sub(topic_filter, m.topic)]

    def wait_for(
        self,
        topic_filter: str,
        predicate: Callable[[Message], bool] | None = None,
        timeout: float = 10.0,
        since: int = 0,
    ) -> Message:
        """Return the first message on ``topic_filter`` (from ``since``) that satisfies ``predicate``.

        Messages already received count, so retained messages delivered at subscription are
        found too; pass ``since=mark()`` to wait only for new ones.
        """

        def match() -> Message | None:
            return next(
                (
                    m
                    for m in self._messages[since:]
                    if paho.topic_matches_sub(topic_filter, m.topic) and (predicate is None or predicate(m))
                ),
                None,
            )

        result: Message = self._wait(match, timeout, f"no matching message on {topic_filter}")
        return result


class Device:
    """The topics of one DUT, observed and commanded through a :class:`MqttClient`."""

    _ids = itertools.count(1)

    def __init__(self, client: MqttClient, device_id: str, root: str = TOPIC_ROOT) -> None:
        self.client, self.device_id, self.root = client, device_id, root

    def topic(self, kind: str) -> str:
        """Return the full topic of ``status``, ``info``, ``telemetry``, ``cmd`` or ``event``."""
        if kind not in KINDS:
            raise ValueError(f"unknown topic kind {kind!r} (one of {', '.join(KINDS)})")
        return f"{self.root}/{self.device_id}/{kind}"

    def observe(self) -> Device:
        """Subscribe to all of the device's topics."""
        self.client.subscribe(f"{self.root}/{self.device_id}/#")
        return self

    def wait_status(self, value: str, timeout: float = 30.0, since: int = 0) -> Message:
        """Wait for status ``online`` or ``offline``."""
        return self.client.wait_for(
            self.topic("status"), lambda m: m.payload == value.encode(), timeout, since
        )

    def info(self, timeout: float = 30.0) -> dict[str, Any]:
        """Return the retained info record."""
        info: dict[str, Any] = self.client.wait_for(self.topic("info"), timeout=timeout).json()
        return info

    def telemetry(self, timeout: float = 30.0, since: int = 0) -> dict[str, Any]:
        """Return the next telemetry record after ``since``."""
        record: dict[str, Any] = self.client.wait_for(
            self.topic("telemetry"), timeout=timeout, since=since
        ).json()
        return record

    def command(
        self, cmd: str, arg: str | None = None, *, request_id: str | None = None, timeout: float = 10.0
    ) -> dict[str, Any]:
        """Send a JSON command and return the event that echoes its ``id``."""
        rid = request_id or f"hil{next(self._ids)}"
        body: dict[str, str] = {"cmd": cmd, "id": rid}
        if arg is not None:
            body["arg"] = arg
        since = self.client.mark()
        self.client.publish(self.topic("cmd"), json.dumps(body))
        reply: dict[str, Any] = self.client.wait_for(
            self.topic("event"), lambda m: _json_id(m) == rid, timeout, since
        ).json()
        return reply

    def command_text(self, text: str, timeout: float = 10.0) -> dict[str, Any]:
        """Send a plain-text command and return the next event."""
        since = self.client.mark()
        self.client.publish(self.topic("cmd"), text)
        reply: dict[str, Any] = self.client.wait_for(self.topic("event"), timeout=timeout, since=since).json()
        return reply


def _json_id(message: Message) -> object:
    try:
        body = message.json()
    except ValueError:
        return None
    return body.get("id") if isinstance(body, dict) else None
