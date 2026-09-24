"""Broker settings and a self-healing MQTT connection on top of aiomqtt.

``BrokerLink`` keeps one MQTT 3.1.1 session up. It reconnects with
exponential back-off and jitter (like the firmware, so a fleet does not
reconnect in lock-step after a broker restart) and re-subscribes every filter
it holds after each reconnect. Callers subscribe once and only see reconnects
through ``on_connect`` and ``on_disconnect``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import socket
import ssl
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import aiomqtt
from paho.mqtt.reasoncodes import ReasonCode

from ...core.errors import DeviceError, DeviceTimeout, HubError, InvalidRequest

logger = logging.getLogger(__name__)

#: A session that lasted this long resets the back-off. Shorter sessions (a
#: broker that accepts and then drops us, e.g. a duplicate client id) keep
#: backing off instead of reconnecting in a tight loop.
HEALTHY_SESSION_S = 10.0
MAX_QUEUED_MESSAGES = 10_000

MessageHandler = Callable[[str, bytes, bool], None]
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TlsSettings:
    """``ca`` None trusts the system store. ``insecure`` skips only the host
    name check (like ``mosquitto_sub --insecure``); the chain is still verified."""

    ca: str | None = None
    cert: str | None = None
    key: str | None = None
    insecure: bool = False

    def context(self) -> ssl.SSLContext:
        try:
            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca)
            if self.cert:
                ctx.load_cert_chain(self.cert, self.key)
        except (OSError, ssl.SSLError) as e:
            raise InvalidRequest(f"MQTT TLS: cannot load certificates: {e}") from e
        if self.insecure:
            ctx.check_hostname = False
        return ctx


@dataclass(frozen=True, slots=True)
class BrokerSettings:
    host: str = "127.0.0.1"
    port: int = 1883
    tls: TlsSettings | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    client_id: str = "uc-hub"
    keepalive_s: int = 30
    timeout_s: float = 10.0
    reconnect_min_s: float = 0.5
    reconnect_max_s: float = 30.0

    @classmethod
    def from_settings(
        cls, settings: Mapping[str, Any], environ: Mapping[str, str] | None = None,
    ) -> BrokerSettings:
        """Parse the ``drivers.mqtt`` block of hub.yaml. The port defaults to
        8883 with ``tls`` and 1883 without; ``tls: true`` means TLS with the
        system trust store."""
        env = os.environ if environ is None else environ
        tls = _tls(settings.get("tls"))
        password = settings.get("password")
        password_env = settings.get("password_env")
        if password is None and password_env:
            if password_env not in env:
                raise InvalidRequest(f"MQTT: environment variable {password_env!r} is not set")
            password = env[password_env]
        username = settings.get("username")
        if password is not None and not username:
            raise InvalidRequest("MQTT: a password needs a username (MQTT 3.1.1)")
        reconnect_min_s = positive_number(settings, "reconnect_min_s", 0.5)
        return cls(
            host=_text(settings, "host", "127.0.0.1"),
            port=int(positive_number(settings, "port", 8883 if tls else 1883, maximum=65535)),
            tls=tls,
            username=str(username) if username else None,
            password=str(password) if password is not None else None,
            client_id=_text(settings, "client_id", "uc-hub"),
            keepalive_s=int(positive_number(settings, "keepalive_s", 30, maximum=65535)),
            timeout_s=positive_number(settings, "timeout_s", 10.0),
            reconnect_min_s=reconnect_min_s,
            reconnect_max_s=max(reconnect_min_s,
                                positive_number(settings, "reconnect_max_s", 30.0)),
        )

    def ssl_context(self) -> ssl.SSLContext | None:
        return self.tls.context() if self.tls is not None else None

    def client(
        self,
        *,
        client_id: str | None = None,
        will: aiomqtt.Will | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> aiomqtt.Client:
        """A new, unconnected client (clean session). ``ssl_context`` defaults
        to a fresh ``ssl_context()``; pass one to avoid reloading the files."""
        if ssl_context is None and self.tls is not None:
            ssl_context = self.ssl_context()
        return aiomqtt.Client(
            self.host,
            self.port,
            username=self.username,
            password=self.password,
            identifier=client_id or self.client_id,
            will=will,
            clean_session=True,
            keepalive=self.keepalive_s,
            timeout=self.timeout_s,
            tls_context=ssl_context,
            max_queued_incoming_messages=MAX_QUEUED_MESSAGES,
            logger=logger.getChild("paho"),
        )

    @property
    def url(self) -> str:
        return f"{'mqtts' if self.tls else 'mqtt'}://{self.host}:{self.port}"


class BrokerLink:
    """One MQTT session that survives broker restarts.

    ``on_message(topic, payload, retained)`` runs synchronously for every
    message, in arrival order. ``on_connect`` runs once the subscriptions are
    restored, while messages are already being delivered, and
    ``on_disconnect`` runs when a session that was up has ended.
    """

    def __init__(
        self,
        settings: BrokerSettings,
        *,
        on_message: MessageHandler,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_disconnect: Callable[[], None] | None = None,
        client_id: str | None = None,
        will: aiomqtt.Will | None = None,
        qos: int = 1,
    ) -> None:
        self.settings = settings
        self.client_id = client_id or settings.client_id
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._will = will
        self._qos = qos
        self._filters: Counter[str] = Counter()
        self._client: aiomqtt.Client | None = None
        #: Resolved when the current session ends; in-flight calls race it.
        self._down: asyncio.Future[None] | None = None
        self._up = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._ssl: ssl.SSLContext | None = None

    @property
    def connected(self) -> bool:
        return self._up.is_set()

    async def wait_connected(self, timeout_s: float) -> bool:
        if self._up.is_set():
            return True
        try:
            async with asyncio.timeout(timeout_s):
                await self._up.wait()
        except TimeoutError:
            return False
        return True

    # -- life cycle -----------------------------------------------------------
    async def start(self) -> None:
        """Start connecting in the background; returns at once."""
        if self._task is not None and not self._task.done():
            return
        self._ssl = self.settings.ssl_context()
        self._closing = False
        self._task = asyncio.create_task(self._run(), name=f"mqtt-link-{self.client_id}")

    async def stop(self) -> None:
        """Disconnect cleanly (DISCONNECT, so the broker discards the will)."""
        self._closing = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def abort(self) -> None:
        """Drop the TCP connection without DISCONNECT, as a crashed device
        would, so the broker publishes the will. For simulations."""
        self._closing = True
        task, self._task = self._task, None
        if task is None:
            return
        client = self._client
        # aiomqtt always sends DISCONNECT on exit; only closing the socket under
        # paho avoids it.
        sock = client._client.socket() if client is not None else None
        if isinstance(sock, socket.socket):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.settings.timeout_s):
                    await asyncio.shield(task)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # -- subscriptions and publishing ------------------------------------------
    async def subscribe(self, filters: Iterable[str]) -> None:
        """Add filters (reference counted). They are subscribed now when
        connected, and again after every reconnect."""
        new = []
        for f in filters:
            if self._filters[f] == 0:
                new.append(f)
            self._filters[f] += 1
        client, down = self._client, self._down
        if new and client is not None and down is not None:
            await self._send_subscribe(client, down, new)

    async def unsubscribe(self, filters: Iterable[str]) -> None:
        gone = []
        for f in filters:
            if self._filters[f] <= 0:
                continue
            self._filters[f] -= 1
            if self._filters[f] == 0:
                del self._filters[f]
                gone.append(f)
        client, down = self._client, self._down
        if gone and client is not None and down is not None:
            try:
                await self._call(client.unsubscribe(gone), down, "unsubscribe")
            except HubError as e:
                logger.warning("%s: unsubscribe %s failed: %s", self.client_id, gone, e)

    async def publish(
        self, topic: str, payload: bytes | str, *, qos: int = 1, retain: bool = False,
    ) -> None:
        """Publish now; raises ``DeviceTimeout`` when the broker is not
        connected and ``DeviceError`` when the publish fails."""
        client, down = self._client, self._down
        if client is None or down is None or not self._up.is_set():
            raise DeviceTimeout(f"MQTT broker {self.settings.url} is not connected")
        await self._call(client.publish(topic, payload, qos=qos, retain=retain), down,
                         f"publish to {topic}")

    # -- connection loop --------------------------------------------------------
    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        backoff = self.settings.reconnect_min_s
        last_failure = ""
        while not self._closing:
            client = self.settings.client(
                client_id=self.client_id, will=self._will, ssl_context=self._ssl,
            )
            up_since: float | None = None
            try:
                async with client:
                    up_since = loop.time()
                    last_failure = ""
                    down = loop.create_future()
                    self._client, self._down = client, down
                    setup = asyncio.create_task(self._setup(client, down))
                    try:
                        async for message in client.messages:
                            self._deliver(message)
                    finally:
                        down.set_result(None)
                        setup.cancel()
                        await asyncio.gather(setup, return_exceptions=True)
            except aiomqtt.MqttError as e:
                if not self._closing:
                    what = "cannot connect to" if up_since is None else "connection lost to"
                    failure = f"{what} {self.settings.url}: {e.__cause__ or e}"
                    # While the broker stays away, say so once, not on every retry.
                    level = logging.DEBUG if failure == last_failure else logging.WARNING
                    logger.log(level, "%s: %s", self.client_id, failure)
                    last_failure = failure
            except Exception:
                logger.exception("%s: MQTT session failed", self.client_id)
            finally:
                self._client = self._down = None
                if self._up.is_set():
                    self._up.clear()
                    self._notify_down()
            if self._closing:
                break
            if up_since is not None and loop.time() - up_since >= HEALTHY_SESSION_S:
                backoff = self.settings.reconnect_min_s
            delay = backoff * random.uniform(0.75, 1.25)
            backoff = min(backoff * 2, self.settings.reconnect_max_s)
            logger.debug("%s: reconnecting in %.2f s", self.client_id, delay)
            await asyncio.sleep(delay)

    async def _setup(self, client: aiomqtt.Client, down: asyncio.Future[None]) -> None:
        """Restore the subscriptions, then report the session up. Runs beside
        the message pump, so a connection that dies meanwhile is noticed at once."""
        if self._filters:
            await self._send_subscribe(client, down, list(self._filters))
        self._up.set()
        logger.info("%s: connected to %s", self.client_id, self.settings.url)
        if self._on_connect is None:
            return
        try:
            await self._on_connect()
        except HubError as e:
            logger.warning("%s: connect handler failed: %s", self.client_id, e)
        except Exception:
            logger.exception("%s: connect handler failed", self.client_id)

    async def _call(self, op: Awaitable[T], down: asyncio.Future[None], what: str) -> T:
        """One broker round trip that fails as soon as the session ends;
        aiomqtt itself only gives up after ``timeout_s``."""
        task = asyncio.ensure_future(op)
        try:
            await asyncio.wait((task, down), return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if not task.done():
            task.cancel()
            raise DeviceTimeout(f"MQTT connection to {self.settings.url} lost during {what}")
        try:
            return task.result()
        except aiomqtt.MqttError as e:
            raise DeviceError(f"MQTT {what} failed: {e}") from e

    async def _send_subscribe(
        self, client: aiomqtt.Client, down: asyncio.Future[None], filters: list[str],
    ) -> None:
        try:
            granted: Sequence[int | ReasonCode] = await self._call(
                client.subscribe([(f, self._qos) for f in filters]), down, "subscribe")
        except HubError as e:
            # The session is broken; the reconnect subscribes again.
            logger.warning("%s: subscribe %s failed: %s", self.client_id, filters, e)
            return
        for f, rc in zip(filters, granted, strict=False):
            if (rc if isinstance(rc, int) else rc.value) >= 0x80:
                logger.warning("%s: broker refused subscription %s", self.client_id, f)

    def _deliver(self, message: aiomqtt.Message) -> None:
        payload = message.payload
        if isinstance(payload, str):
            data = payload.encode()
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        elif payload is None:
            data = b""
        else:
            data = str(payload).encode()
        try:
            self._on_message(message.topic.value, data, bool(message.retain))
        except Exception:
            # One bad message or device must not end the session for all.
            logger.exception("%s: handling a message on %s failed",
                             self.client_id, message.topic.value)

    def _notify_down(self) -> None:
        if self._on_disconnect is None:
            return
        try:
            self._on_disconnect()
        except Exception:
            logger.exception("%s: disconnect handler failed", self.client_id)


def topic_matches(filter_: str, topic: str) -> bool:
    """MQTT topic filter matching (``+`` one level, ``#`` the rest); wildcards
    at the first level do not match ``$``-topics such as ``$SYS/...``."""
    if topic.startswith("$") and filter_[:1] in ("+", "#"):
        return False
    f_parts = filter_.split("/")
    t_parts = topic.split("/")
    for i, part in enumerate(f_parts):
        if part == "#":
            return True
        if i >= len(t_parts):
            return False
        if part != "+" and part != t_parts[i]:
            return False
    return len(f_parts) == len(t_parts)


def valid_filter(filter_: str) -> bool:
    if not filter_ or "\0" in filter_:
        return False
    parts = filter_.split("/")
    for i, part in enumerate(parts):
        if "#" in part and (part != "#" or i != len(parts) - 1):
            return False
        if "+" in part and part != "+":
            return False
    return True


def valid_topic(topic: str) -> bool:
    return bool(topic) and "\0" not in topic and "+" not in topic and "#" not in topic


def _tls(raw: Any) -> TlsSettings | None:
    if raw is None or raw is False:
        return None
    if raw is True:
        return TlsSettings()
    if not isinstance(raw, Mapping):
        raise InvalidRequest("MQTT: tls must be a mapping {ca, cert, key, insecure}")
    unknown = set(raw) - {"ca", "cert", "key", "insecure"}
    if unknown:
        raise InvalidRequest(f"MQTT: unknown tls settings {sorted(unknown)}")
    paths = {}
    for k in ("ca", "cert", "key"):
        v = raw.get(k)
        if v is not None and not isinstance(v, (str, os.PathLike)):
            raise InvalidRequest(f"MQTT: tls.{k} must be a file path")
        paths[k] = os.fspath(os.path.expanduser(v)) if v is not None else None
    if paths["key"] and not paths["cert"]:
        raise InvalidRequest("MQTT: tls.key needs tls.cert")
    return TlsSettings(ca=paths["ca"], cert=paths["cert"], key=paths["key"],
                       insecure=bool(raw.get("insecure", False)))


def _text(settings: Mapping[str, Any], key: str, default: str) -> str:
    v = settings.get(key)
    if v is None:
        return default
    if not isinstance(v, str) or not v:
        raise InvalidRequest(f"MQTT: {key} must be a non-empty string")
    return v


def positive_number(
    settings: Mapping[str, Any], key: str, default: float, *, maximum: float | None = None,
) -> float:
    v = settings.get(key)
    if v is None:
        return float(default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise InvalidRequest(f"MQTT: {key} must be a positive number, not {v!r}")
    if maximum is not None and v > maximum:
        raise InvalidRequest(f"MQTT: {key} must be at most {maximum:g}")
    return float(v)
