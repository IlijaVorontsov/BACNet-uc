"""Simulated MQTT devices for demos and tests.

- ``SimMqttTlsDevice`` behaves like an ``apps/mqtt_tls`` node on the wire:
  retained ``online`` status with a retained ``offline`` last will, retained
  info, telemetry every ``telemetry_interval_s``, the text commands ``ping``
  and ``led on|off|toggle`` answered on the event topic, and reconnect with
  back-off. ``modern=True`` is firmware 0.3.0, which implements requests
  M1-M3 (``src/commands.c`` on the MQTT firmware branch): ``fw``, ``hwid``,
  ``mac`` and ``caps`` in the info, JSON commands whose replies echo the ID,
  ``identify``, an ``ok`` member in every reply, and retained or empty
  commands ignored.
- ``SimJsonSensor`` is a third-party sensor publishing a JSON document (by
  default a CO2 sensor: ``{"ppm": ..., "bat": {"pct": ...}}``) that merges
  JSON objects received on its optional command topic into its state.

``python -m uc_hub.sim.mqtt_device --help`` runs a demo fleet.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import random
import re
import secrets
import signal
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping
from types import TracebackType
from typing import Any, Self

import aiomqtt

from ..core.errors import HubError
from ..core.types import Value
from ..drivers.mqtt.connection import BrokerLink, BrokerSettings
from ..drivers.mqtt.profiles import json_object

logger = logging.getLogger(__name__)

BrokerLike = BrokerSettings | Mapping[str, Any]
StateUpdate = Callable[[dict[str, Any], random.Random], None]

#: Largest command the firmware processes (CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE).
MAX_COMMAND_PAYLOAD = 128
#: The legacy firmware's reply code when the LED GPIO is missing (-ENODEV).
_ENODEV = -19
#: Firmware 0.3.0 limits (commands.c).
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,16}")
IDENTIFY_DEFAULT_S = 30
IDENTIFY_MAX_S = 3600


class _SimClient:
    """Life cycle shared by the simulated devices: one ``BrokerLink`` plus
    background tasks that end with the device."""

    _link: BrokerLink

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        #: Set while a session is up and the device has announced itself.
        self._ready = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def wait_connected(self, timeout_s: float = 5.0) -> bool:
        """Wait until the device is connected and has published its first
        messages (status and info, or the first reading)."""
        try:
            async with asyncio.timeout(timeout_s):
                await self._ready.wait()
        except TimeoutError:
            return False
        return True

    async def start(self) -> None:
        await self._link.start()

    async def stop(self) -> None:
        await self._link.stop()
        await self._cancel_tasks()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _cancel_tasks(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class SimMqttTlsDevice(_SimClient):
    """An ``apps/mqtt_tls`` node. ``led``, ``seq``, ``sessions``,
    ``identify_until`` and ``commands`` (raw payloads received) are public for
    tests; ``muted`` makes it swallow commands without replying, and
    ``extra`` holds the values of ``extra_telemetry`` fields."""

    def __init__(
        self,
        broker: BrokerLike,
        client_id: str = "zephyr-0a1b2c3d",
        *,
        topic_root: str = "bacnet-uc",
        telemetry_interval_s: float = 10.0,
        modern: bool = False,
        board: str = "nucleo_h563zi",
        zephyr: str = "3.7.2",
        ip: str = "192.0.2.10",
        fw: str = "0.3.0",
        hwid: str | None = None,
        mac: str | None = None,
        has_led: bool = True,
        extra_telemetry: Mapping[str, tuple[str, Value]] | None = None,
        reply_delay_s: float = 0.0,
    ) -> None:
        super().__init__()
        settings = _settings(broker)
        self.client_id = client_id
        self.prefix = f"{topic_root}/{client_id}"
        self.telemetry_interval_s = telemetry_interval_s
        self.modern = modern
        self.board = board
        self.zephyr = zephyr
        self.ip = ip
        self.fw = fw
        self.hwid = hwid or _hwid(client_id)
        self.mac = mac or _mac(client_id)
        self.has_led = has_led
        self.reply_delay_s = reply_delay_s
        self.muted = False
        self.led = False
        self.identify_until: float | None = None
        self.seq = 0
        self.sessions = 0
        self.commands: list[bytes] = []
        self.extra: dict[str, Value] = {k: v for k, (_, v) in (extra_telemetry or {}).items()}
        self._extra_units = {k: unit for k, (unit, _) in (extra_telemetry or {}).items()}
        self._tls = settings.tls is not None
        self._boot = time.monotonic()
        self._telemetry: asyncio.Task[None] | None = None
        self._subscribed = False
        self._link = BrokerLink(
            settings,
            client_id=client_id,
            will=aiomqtt.Will(self.topic("status"), b"offline", qos=1, retain=True),
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

    def topic(self, leaf: str) -> str:
        return f"{self.prefix}/{leaf}"

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self._boot)

    @property
    def identifying(self) -> bool:
        return self.identify_until is not None and time.monotonic() < self.identify_until

    def info(self) -> dict[str, Any]:
        if not self.modern:
            return {"board": self.board, "zephyr": self.zephyr, "ip": self.ip, "tls": self._tls}
        # led and identify both need the LED, so firmware 0.3.0 lists them together.
        cmds = ["ping", "led", "identify"] if self.has_led else ["ping"]
        telemetry = {"seq": "count", "uptime_s": "s", "sessions": "count", **self._extra_units}
        return {"fw": self.fw, "board": self.board, "zephyr": self.zephyr, "hwid": self.hwid,
                "mac": self.mac, "ip": self.ip, "tls": self._tls,
                "caps": {"cmds": cmds, "telemetry": telemetry}}

    async def start(self) -> None:
        if not self._subscribed:
            await self._link.subscribe([self.topic("cmd")])
            self._subscribed = True
        await super().start()

    async def stop(self) -> None:
        """Power down cleanly: retained status ``offline``, then DISCONNECT."""
        if self._link.connected:
            with contextlib.suppress(HubError):
                await self._link.publish(self.topic("status"), b"offline", qos=1, retain=True)
        await super().stop()

    async def crash(self) -> None:
        """Drop off the network without DISCONNECT, so the broker publishes
        the retained ``offline`` last will."""
        await self._link.abort()
        await self._cancel_tasks()

    async def publish_telemetry(self) -> None:
        doc = {"seq": self.seq, "uptime_s": self.uptime_s, "sessions": self.sessions,
               **self.extra}
        await self._link.publish(self.topic("telemetry"), _dumps(doc), qos=1)
        self.seq += 1

    # -- session ----------------------------------------------------------------
    async def _on_connect(self) -> None:
        self.sessions += 1
        await self._link.publish(self.topic("status"), b"online", qos=1, retain=True)
        await self._link.publish(self.topic("info"), _dumps(self.info()), qos=1, retain=True)
        self._telemetry = self._spawn(self._telemetry_loop())
        self._ready.set()

    def _on_disconnect(self) -> None:
        self._ready.clear()
        if self._telemetry is not None:
            self._telemetry.cancel()
            self._telemetry = None

    async def _telemetry_loop(self) -> None:
        try:
            while True:
                await self.publish_telemetry()
                await asyncio.sleep(self.telemetry_interval_s)
        except HubError as e:
            logger.debug("%s: telemetry stopped: %s", self.client_id, e)

    # -- commands ---------------------------------------------------------------
    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if topic != self.topic("cmd"):
            return
        self.commands.append(payload)
        # Firmware 0.3.0 ignores stale retained commands and the empty
        # message that clears one.
        if self.muted or (self.modern and (retained or not payload)):
            return
        self._spawn(self._reply(self.execute(payload)))

    async def _reply(self, reply: dict[str, Any]) -> None:
        if self.reply_delay_s > 0:
            await asyncio.sleep(self.reply_delay_s)
        try:
            await self._link.publish(self.topic("event"), _dumps(reply), qos=1)
        except HubError as e:
            logger.debug("%s: reply lost: %s", self.client_id, e)

    def execute(self, payload: bytes) -> dict[str, Any]:
        """The reply the firmware publishes for one command payload."""
        if not self.modern:
            if len(payload) > MAX_COMMAND_PAYLOAD:
                return {"error": "payload too large"}
            return self._legacy_command(payload.decode("utf-8", "replace"))
        if len(payload) > MAX_COMMAND_PAYLOAD:
            return {"ok": False, "error": "payload too large"}
        text = payload.decode("utf-8", "replace")
        if text.startswith("{"):
            return self._json_command(text)
        cmd, space, arg = text.partition(" ")
        return self._result(None, *self._execute(cmd, arg if space else None))

    def _legacy_command(self, text: str) -> dict[str, Any]:
        # Exact matches, like the firmware's strcmp().
        if text == "ping":
            return {"pong": self.uptime_s}
        if text in ("led on", "led off", "led toggle"):
            if not self.has_led:
                return {"error": "led unavailable", "code": _ENODEV}
            self.led = {"led on": True, "led off": False}.get(text, not self.led)
            return {"led": self.led}
        return {"error": "unknown command"}

    def _json_command(self, text: str) -> dict[str, Any]:
        req = json_object(text.encode())
        # Zephyr's json_obj_parse fails on a type mismatch in a known field.
        if req is None or any(k in req and not isinstance(req[k], str)
                              for k in ("id", "cmd", "arg")):
            return self._result(None, None, "invalid json")
        cid = req.get("id")
        if cid is not None and not _REQUEST_ID.fullmatch(cid):
            return self._result(None, None, "invalid id")
        if "cmd" not in req:
            return self._result(cid, None, "missing cmd")
        return self._result(cid, *self._execute(req["cmd"], req.get("arg")))

    def _execute(self, cmd: str, arg: str | None) -> tuple[dict[str, Any] | None, str | None]:
        """Firmware 0.3.0 ``execute()``: ``(detail, None)`` or ``(None, error)``."""
        if cmd == "ping" and arg is None:
            return {"pong": self.uptime_s}, None
        if cmd == "led":
            states = {"on": True, "off": False, "toggle": not self.led}
            if arg is None or arg not in states:
                return None, "bad argument"
            if not self.has_led:
                return None, "led unavailable"
            self.led = states[arg]
            self.identify_until = None      # an LED command ends identify
            return {"led": self.led}, None
        if cmd == "identify":
            seconds = IDENTIFY_DEFAULT_S
            if arg is not None:
                if not re.fullmatch(r"[0-9]+", arg) or int(arg) > IDENTIFY_MAX_S:
                    return None, "bad argument"
                seconds = int(arg)
            if not self.has_led:
                return None, "led unavailable"
            self.identify_until = time.monotonic() + seconds if seconds > 0 else None
            return {"identify": seconds}, None
        return None, "unknown command"

    @staticmethod
    def _result(cid: str | None, detail: dict[str, Any] | None,
                error: str | None) -> dict[str, Any]:
        head: dict[str, Any] = {"id": cid} if cid is not None else {}
        if error is not None:
            return {**head, "ok": False, "error": error}
        return {**head, "ok": True, **(detail or {})}


def co2_walk(state: dict[str, Any], rng: random.Random) -> None:
    """A slow random walk of ``ppm`` in 400..2000 and a draining battery."""
    ppm = state.get("ppm")
    if isinstance(ppm, (int, float)):
        state["ppm"] = round(min(2000.0, max(400.0, ppm + rng.uniform(-15.0, 15.0))), 1)
    bat = state.get("bat")
    if isinstance(bat, dict) and isinstance(bat.get("pct"), int) and rng.random() < 0.01:
        bat["pct"] = max(0, bat["pct"] - 1)


class SimJsonSensor(_SimClient):
    """Publishes ``state`` on ``topic`` when it connects and every
    ``interval_s`` (after applying ``update``). JSON objects received on
    ``command_topic`` are merged into ``state`` (top-level keys), recorded in
    ``writes`` and published at once."""

    def __init__(
        self,
        broker: BrokerLike,
        topic: str = "sensors/r204/co2",
        *,
        state: Mapping[str, Any] | None = None,
        interval_s: float = 10.0,
        command_topic: str | None = None,
        retain: bool = False,
        client_id: str | None = None,
        update: StateUpdate | None = None,
        seed: int = 0,
    ) -> None:
        super().__init__()
        self.topic = topic
        self.command_topic = command_topic
        self.interval_s = interval_s
        self.retain = retain
        self.update: StateUpdate | None
        if state is None:
            self.state: dict[str, Any] = {"ppm": 612.0, "bat": {"pct": 97}}
            self.update = update or co2_walk
        else:
            self.state = copy.deepcopy(dict(state))
            self.update = update
        self.writes: list[dict[str, Any]] = []
        self.published = 0
        self._rng = random.Random(seed)
        self._subscribed = False
        self._publisher: asyncio.Task[None] | None = None
        self._link = BrokerLink(
            _settings(broker),
            client_id=client_id or f"sim-json-{secrets.token_hex(4)}",
            on_message=self._on_message,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

    async def start(self) -> None:
        if self.command_topic and not self._subscribed:
            await self._link.subscribe([self.command_topic])
            self._subscribed = True
        await super().start()

    async def publish_now(self) -> None:
        await self._link.publish(self.topic, _dumps(self.state), qos=1, retain=self.retain)
        self.published += 1

    async def _on_connect(self) -> None:
        await self.publish_now()
        self._publisher = self._spawn(self._publish_loop())
        self._ready.set()

    def _on_disconnect(self) -> None:
        self._ready.clear()
        if self._publisher is not None:
            self._publisher.cancel()
            self._publisher = None

    async def _publish_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_s)
                if self.update is not None:
                    self.update(self.state, self._rng)
                await self.publish_now()
        except HubError as e:
            logger.debug("%s: publishing stopped: %s", self.topic, e)

    def _on_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if topic != self.command_topic:
            return
        doc = json_object(payload)
        if doc is None:
            logger.info("%s: ignoring a command that is not a JSON object", self.topic)
            return
        self.writes.append(doc)
        self.state.update(doc)
        self._spawn(self._publish_quietly())

    async def _publish_quietly(self) -> None:
        try:
            await self.publish_now()
        except HubError as e:
            logger.debug("%s: publish failed: %s", self.topic, e)


SimMqttDevice = SimMqttTlsDevice | SimJsonSensor


async def start_sim_mqtt_devices(
    broker: BrokerLike,
    *,
    nodes: Iterable[str] = ("zephyr-0a1b2c3d",),
    co2_topics: Iterable[str] = ("sensors/r204/co2",),
    modern: bool = False,
    topic_root: str = "bacnet-uc",
    interval_s: float = 10.0,
) -> list[SimMqttDevice]:
    """Start ``apps/mqtt_tls`` nodes and CO2 sensors (writable through
    ``<topic>/set``); stop them with ``stop_sim_mqtt_devices``."""
    settings = _settings(broker)
    devices: list[SimMqttDevice] = [
        SimMqttTlsDevice(settings, client_id, topic_root=topic_root, modern=modern,
                         telemetry_interval_s=interval_s)
        for client_id in nodes
    ]
    for i, topic in enumerate(co2_topics):
        devices.append(SimJsonSensor(settings, topic, interval_s=interval_s,
                                     command_topic=f"{topic}/set", seed=i))
    try:
        for device in devices:
            await device.start()
    except BaseException:
        await stop_sim_mqtt_devices(devices)
        raise
    return devices


async def stop_sim_mqtt_devices(devices: Iterable[SimMqttDevice]) -> None:
    await asyncio.gather(*(d.stop() for d in devices), return_exceptions=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m uc_hub.sim.mqtt_device",
        description="Simulated apps/mqtt_tls nodes and JSON CO2 sensors on an MQTT broker.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="default 8883 with TLS, else 1883")
    parser.add_argument("--tls-ca", help="CA file; enables TLS")
    parser.add_argument("--tls-cert", help="client certificate (mutual TLS)")
    parser.add_argument("--tls-key", help="client key (mutual TLS)")
    parser.add_argument("--insecure", action="store_true", help="skip the host name check")
    parser.add_argument("--username")
    parser.add_argument("--password-env", help="environment variable holding the password")
    parser.add_argument("--node", action="append", default=[], metavar="CLIENT_ID",
                        help="an apps/mqtt_tls node (repeatable; default zephyr-0a1b2c3d)")
    parser.add_argument("--modern", action="store_true",
                        help="nodes publish caps and take JSON commands (requests M1-M3)")
    parser.add_argument("--topic-root", default="bacnet-uc")
    parser.add_argument("--co2", action="append", default=[], metavar="TOPIC",
                        help="a CO2 sensor on TOPIC, writable through TOPIC/set "
                             "(repeatable; default sensors/r204/co2)")
    parser.add_argument("--interval", type=float, default=10.0, help="publish interval (s)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings: dict[str, Any] = {"host": args.host, "username": args.username,
                                "password_env": args.password_env}
    if args.port is not None:
        settings["port"] = args.port
    if args.tls_ca or args.tls_cert:
        settings["tls"] = {"ca": args.tls_ca, "cert": args.tls_cert, "key": args.tls_key,
                           "insecure": args.insecure}
    try:
        asyncio.run(_demo(
            settings, nodes=args.node or ["zephyr-0a1b2c3d"],
            co2_topics=args.co2 or ["sensors/r204/co2"], modern=args.modern,
            topic_root=args.topic_root, interval_s=args.interval,
        ))
    except HubError as e:
        parser.exit(2, f"error: {e}\n")


async def _demo(settings: Mapping[str, Any], **options: Any) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    devices = await start_sim_mqtt_devices(settings, **options)
    logger.info("running %d simulated devices; Ctrl-C stops them", len(devices))
    try:
        await stop.wait()
    finally:
        await stop_sim_mqtt_devices(devices)


def _settings(broker: BrokerLike) -> BrokerSettings:
    return broker if isinstance(broker, BrokerSettings) else BrokerSettings.from_settings(broker)


def _hwid(client_id: str) -> str:
    """The MCU ID the firmware derived ``zephyr-<hex>`` from, or a stable fake."""
    head, _, tail = client_id.rpartition("-")
    if head and tail and all(c in "0123456789abcdef" for c in tail):
        return tail
    return hashlib.sha256(client_id.encode()).hexdigest()[:16]


def _mac(client_id: str) -> str:
    """A stable, locally administered MAC address."""
    digest = hashlib.sha256(b"mac:" + client_id.encode()).digest()
    return ":".join(f"{b:02x}" for b in (0x02, *digest[:5]))


def _dumps(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode()


if __name__ == "__main__":
    main()
