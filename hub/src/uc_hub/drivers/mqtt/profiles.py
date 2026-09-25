"""Device profiles of the MQTT driver: how one device's topics and payloads
map to points.

- ``mqtt_tls``: this repository's ``apps/mqtt_tls`` firmware, topics
  ``<topic_root>/<client_id>/{status,info,telemetry,cmd,event}``.
- ``generic-json``: any device that publishes JSON (or a bare value) on one
  topic; every point picks its value with a JSONPath-lite ``path``.

A profile object holds the state of one device (status, last info, the last
sample of every point, outstanding commands) and decodes its messages. It
does no I/O: the driver owns the broker link and hands a ``send`` coroutine
to the calls that publish.
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, get_args

from ...core.errors import DeviceError, DeviceTimeout, InvalidRequest, Unsupported
from ...core.ids import PointRef
from ...core.types import (
    Datatype,
    DeviceRecord,
    Point,
    PointKind,
    Quality,
    Reading,
    Value,
)
from .connection import valid_filter, valid_topic
from .jsonpath import JsonPath, JsonPathError

logger = logging.getLogger(__name__)

Send = Callable[[str, bytes], Awaitable[None]]

DATATYPES: frozenset[str] = frozenset(get_args(Datatype))
DEFAULT_TOPIC_ROOT = "bacnet-uc"
#: What firmware without ``caps`` in its info (before request M1) understands.
LEGACY_COMMANDS = ("ping", "led")
LEGACY_TELEMETRY: dict[str, tuple[str | None, Datatype]] = {
    "seq": (None, "int"),
    "uptime_s": ("seconds", "int"),
    "sessions": (None, "int"),
}
_TELEMETRY_NAMES = {
    "seq": "Telemetry sequence number",
    "uptime_s": "Uptime",
    "sessions": "MQTT sessions since boot",
}
#: Key of a plain-text command's reply on the event topic.
_REPLY_KEYS = {"ping": "pong", "led": "led", "identify": "identify"}
#: Unit spellings seen in MQTT payload descriptions -> BACnet engineering units.
_UNITS = {
    "s": "seconds", "sec": "seconds", "ms": "milliseconds", "min": "minutes", "h": "hours",
    "%": "percent", "%RH": "percent-relative-humidity",
    "C": "degrees-celsius", "°C": "degrees-celsius", "degC": "degrees-celsius",
    "ppm": "parts-per-million", "V": "volts", "mV": "millivolts", "A": "amperes",
    "mA": "milliamperes", "W": "watts", "kW": "kilowatts", "kWh": "kilowatt-hours",
    "Pa": "pascals", "hPa": "hectopascals", "lx": "luxes", "count": None,
}
_TRUE = frozenset({"true", "on", "1", "yes", "active"})
_FALSE = frozenset({"false", "off", "0", "no", "inactive"})
_MAX_TELEMETRY_FIELDS = 64
_MAX_TEXT = 64


@dataclass(slots=True)
class Sample:
    value: Value
    ts: float
    #: Set when the payload value could not be converted to the datatype.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PointSpec:
    obj: str
    name: str
    kind: PointKind
    datatype: Datatype
    units: str | None = None
    writable: bool = False
    #: Refreshed by the device on its own, so an old sample is stale.
    periodic: bool = True
    #: The device's connection status, which stays valid while it is offline.
    status: bool = False
    #: A retained sample is the current value (the device publishes this
    #: point retained, on change only), not a stored copy of unknown age.
    trust_retained: bool = False
    description: str = ""
    source: str = ""
    tags: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


def coerce(value: Any, datatype: Datatype) -> Value:
    """Convert a payload or write value to ``datatype`` (numeric strings and
    0/1 booleans are accepted); ValueError when it does not fit."""
    if value is None:
        return None
    if datatype == "string":
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)
    if datatype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
            return value.strip().lower() in _TRUE
        raise ValueError(f"{_clip(repr(value))} is not a boolean")
    number: int | float
    if isinstance(value, bool):
        number = int(value)
    elif isinstance(value, (int, float)):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                raise ValueError(f"{_clip(repr(value))} is not a number") from None
    else:
        raise ValueError(f"a {type(value).__name__} is not a number")
    if isinstance(number, float) and not math.isfinite(number):
        raise ValueError(f"{number} is not a finite number")
    if datatype == "real":
        try:
            return float(number)
        except OverflowError:
            raise ValueError("the number is out of range") from None
    if isinstance(number, float):
        if not number.is_integer():
            raise ValueError(f"{number} is not an integer")
        number = int(number)
    if datatype == "enum" and number not in (0, 1):
        raise ValueError(f"{number} is not 0 or 1 (an enum point is two-state)")
    return number


def map_units(unit: str | None) -> str | None:
    if not unit:
        return None
    return _UNITS.get(unit, _clip(unit))


class Profile(abc.ABC):
    """State and decoding rules of one device."""

    name: ClassVar[str]

    def __init__(self, site: str, record: DeviceRecord) -> None:
        self.site = site
        self.record = record
        self.samples: dict[str, Sample] = {}
        #: Objects whose sample came from a retained message the broker
        #: replayed, with no live message since (the driver keeps this).
        self.retained: set[str] = set()
        #: None until the device is heard from; False while it is offline or
        #: the broker link is down.
        self.online: bool | None = None
        self.last_error: str | None = None
        self._specs: dict[str, PointSpec] = {}

    @property
    @abc.abstractmethod
    def filters(self) -> list[str]:
        """Topic filters to subscribe to."""

    @abc.abstractmethod
    def handle(self, topic: str, payload: bytes, now: float) -> list[str]:
        """Decode one message; return the objects whose samples it changed."""

    @abc.abstractmethod
    def extra(self) -> dict[str, Any]:
        """``DeviceDescription.extra``."""

    async def write(self, obj: str, value: Value, send: Send, timeout_s: float) -> Value:
        """Command a point; returns the value the device took."""
        raise InvalidRequest(f"{self.record.name}/{obj} is read-only")

    def link_lost(self) -> None:
        self.online = False

    def close(self, reason: str) -> None:
        """Fail outstanding commands (device removed, driver stopped)."""
        return None

    def spec(self, obj: str) -> PointSpec | None:
        return self._specs.get(obj)

    def points(self) -> list[Point]:
        return [self._point(s) for s in self._specs.values()]

    def objs(self) -> list[str]:
        return list(self._specs)

    def reading(self, obj: str, now: float, stale_after_s: float, link_up: bool) -> Reading:
        ref = PointRef(self.site, self.record.name, obj)
        spec = self._specs.get(obj)
        if spec is None:
            return Reading(ref, None, now, Quality.FAULT,
                           error=f"{self.record.name} has no point {obj!r}")
        sample = self.samples.get(obj)
        if not link_up:
            return Reading(ref, sample.value if sample else None, sample.ts if sample else now,
                           Quality.OFFLINE, error="MQTT broker not connected")
        if sample is None:
            why = "device offline" if self.online is False else "no value received yet"
            return Reading(ref, None, now, Quality.OFFLINE, error=why)
        if self.online is False and not spec.status:
            return Reading(ref, sample.value, sample.ts, Quality.OFFLINE, error="device offline")
        if sample.error:
            return Reading(ref, sample.value, sample.ts, Quality.FAULT, error=sample.error)
        age = now - sample.ts
        if spec.periodic and age > stale_after_s:
            return Reading(ref, sample.value, sample.ts, Quality.STALE,
                           error=f"no update for {age:.0f} s")
        return Reading(ref, sample.value, sample.ts)

    def _point(self, spec: PointSpec) -> Point:
        return Point(
            ref=PointRef(self.site, self.record.name, spec.obj),
            name=spec.name,
            kind=spec.kind,
            datatype=spec.datatype,
            units=spec.units,
            writable=spec.writable,
            commandable=False,
            tags=list(spec.tags),
            description=spec.description,
            source=spec.source,
            space=self.record.space,
            meta=dict(spec.meta),
        )

    def _note(self, problem: str) -> None:
        """Remember a payload problem for ``describe`` without flooding the log."""
        self.last_error = problem
        logger.debug("%s: %s", self.record.name, problem)


class MqttTlsProfile(Profile):
    """``apps/mqtt_tls``. The point list comes from ``info.caps`` when the
    firmware publishes it (request M1), else it is the fixed legacy set.

    Commands: with ``caps`` the firmware takes JSON commands and echoes their
    ID in the reply (M2), so replies are matched exactly. Older firmware only
    takes text commands and replies without an ID; the reply is then the next
    event that carries the expected key (``led``, ``pong``) or ``error``. That
    is best effort: a reply to another client's command sent at the same time
    can be taken for ours. Our own text commands to one device are serialized.
    """

    name = "mqtt_tls"

    def __init__(self, site: str, record: DeviceRecord, spec: Mapping[str, Any]) -> None:
        super().__init__(site, record)
        root = spec.get("topic_root", DEFAULT_TOPIC_ROOT)
        client_id = spec.get("client_id")
        if client_id is None:
            addr_root, sep, addr_id = record.address.rpartition("/")
            if not sep or not addr_root or not addr_id:
                raise InvalidRequest(
                    f"{record.name}: mqtt_tls needs client_id "
                    "(or an address <topic_root>/<client_id>)")
            client_id = addr_id
            if "topic_root" not in spec:
                root = addr_root
        if not isinstance(root, str) or not valid_topic(root) or root.startswith("/") \
                or root.endswith("/"):
            raise InvalidRequest(f"{record.name}: invalid topic_root {root!r}")
        if not isinstance(client_id, str) or not valid_topic(client_id) or "/" in client_id:
            raise InvalidRequest(f"{record.name}: invalid client_id {client_id!r}")
        self.topic_root = root
        self.client_id = client_id
        self.prefix = f"{root}/{client_id}"
        self.status: str | None = None
        self.info: dict[str, Any] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._text_waiter: tuple[str | None, asyncio.Future[dict[str, Any]]] | None = None
        self._text_lock = asyncio.Lock()
        self._specs = self._build_specs()

    def topic(self, leaf: str) -> str:
        return f"{self.prefix}/{leaf}"

    @property
    def filters(self) -> list[str]:
        return [self.topic(leaf) for leaf in ("status", "info", "telemetry", "event")]

    @property
    def caps(self) -> dict[str, Any] | None:
        caps = self.info.get("caps") if self.info else None
        return caps if isinstance(caps, dict) else None

    @property
    def json_commands(self) -> bool:
        return self.caps is not None

    @property
    def commands(self) -> tuple[str, ...]:
        caps = self.caps
        cmds = caps.get("cmds") if caps else None
        if isinstance(cmds, list):
            return tuple(c for c in cmds if isinstance(c, str))
        return LEGACY_COMMANDS

    # -- points -------------------------------------------------------------
    def _build_specs(self) -> dict[str, PointSpec]:
        base = f"mqtt:{self.prefix}"
        specs = [PointSpec(
            "status", "Connection status", PointKind.VALUE, "string",
            periodic=False, status=True, trust_retained=True, source=f"{base}/status",
            description="'online' while the device is connected to the broker; "
                        "'offline' is its retained last will.",
        )]
        for fld, (units, datatype) in self._telemetry_fields().items():
            specs.append(PointSpec(
                f"telemetry.{fld}", _TELEMETRY_NAMES.get(fld, fld), PointKind.INPUT, datatype,
                units, source=f"{base}/telemetry $.{fld}", meta={"field": fld},
                description=f"Field {fld!r} of the periodic telemetry message.",
            ))
        if "led" in self.commands:
            specs.append(PointSpec(
                "led", "User LED", PointKind.OUTPUT, "bool", writable=True, periodic=False,
                source=f"{base}/cmd led",
                description="Board LED, switched with the led command. Its value is learned "
                            "from command replies and is unknown until the first one.",
            ))
        return {s.obj: s for s in specs}

    def _telemetry_fields(self) -> dict[str, tuple[str | None, Datatype]]:
        caps = self.caps
        declared = caps.get("telemetry") if caps else None
        if not isinstance(declared, dict) or not declared:
            return dict(LEGACY_TELEMETRY)
        out: dict[str, tuple[str | None, Datatype]] = {}
        for fld, desc in declared.items():
            if len(out) >= _MAX_TELEMETRY_FIELDS:
                self._note(f"info: more than {_MAX_TELEMETRY_FIELDS} telemetry fields")
                break
            if not isinstance(fld, str) or not _valid_obj(f"telemetry.{fld}"):
                self._note(f"info: telemetry field {_clip(repr(fld))} is not a valid point id")
                continue
            unit: Any = desc
            datatype: Any = None
            if isinstance(desc, dict):
                unit = desc.get("units", desc.get("unit"))
                datatype = desc.get("datatype")
            unit = unit if isinstance(unit, str) else None
            if datatype not in DATATYPES:
                if fld in LEGACY_TELEMETRY:
                    datatype = LEGACY_TELEMETRY[fld][1]
                else:
                    datatype = "int" if unit == "count" else "real"
            out[fld] = (map_units(unit), datatype)
        return out

    # -- messages -------------------------------------------------------------
    def handle(self, topic: str, payload: bytes, now: float) -> list[str]:
        leaf = topic[len(self.prefix) + 1:] if topic.startswith(self.prefix + "/") else ""
        if leaf == "status":
            return self._on_status(payload, now)
        if leaf == "info":
            self._on_info(payload)
            return []
        if leaf not in ("telemetry", "event"):
            return []
        if self.online is None:
            # Live traffic without a retained status (e.g. it was cleared).
            self.online = True
        if leaf == "telemetry":
            return self._on_telemetry(payload, now)
        return self._on_event(payload, now)

    def _on_status(self, payload: bytes, now: float) -> list[str]:
        text = payload.decode("utf-8", "replace").strip().lower()
        if text not in ("online", "offline", ""):
            self._note(f"status: unexpected payload {_clip(repr(text))}")
            return []
        status = text or "offline"
        online = status == "online"
        if not online and self.online is not False:
            self._forget(f"{self.record.name} went offline")
        self.status = status
        self.online = online
        self.samples["status"] = Sample(status, now)
        return ["status"]

    def _on_info(self, payload: bytes) -> None:
        doc = json_object(payload)
        if doc is None:
            self._note("info: not a JSON object")
            return
        self.info = doc
        rec = self.record
        rec.model = short_text(doc.get("board")) or rec.model
        fw, zephyr = short_text(doc.get("fw")), short_text(doc.get("zephyr"))
        rec.firmware = fw or (f"zephyr {zephyr}" if zephyr else rec.firmware)
        rec.hwid = short_text(doc.get("hwid")) or rec.hwid
        self._specs = self._build_specs()
        for obj in [o for o in self.samples if o not in self._specs]:
            del self.samples[obj]

    def _on_telemetry(self, payload: bytes, now: float) -> list[str]:
        doc = json_object(payload)
        if doc is None:
            self._note("telemetry: not a JSON object")
            return []
        changed = []
        for spec in self._specs.values():
            fld = spec.meta.get("field")
            if fld is not None and fld in doc:
                self.samples[spec.obj] = _sample(doc[fld], spec.datatype, now)
                changed.append(spec.obj)
        return changed

    def _on_event(self, payload: bytes, now: float) -> list[str]:
        doc = json_object(payload)
        if doc is None:
            self._note("event: not a JSON object")
            return []
        changed = []
        led = doc.get("led")
        if isinstance(led, bool) and "led" in self._specs:
            self.samples["led"] = Sample(led, now)
            changed.append("led")
        cid = doc.get("id")
        if isinstance(cid, str):
            fut = self._pending.get(cid)
            if fut is not None and not fut.done():
                fut.set_result(doc)
        elif self._text_waiter is not None:
            key, fut = self._text_waiter
            if not fut.done() and (key is None or key in doc or "error" in doc):
                fut.set_result(doc)
        return changed

    def link_lost(self) -> None:
        if self.online is not False:
            self._forget("MQTT broker connection lost")
        self.online = False

    def close(self, reason: str) -> None:
        self._fail_commands(reason)

    def _forget(self, reason: str) -> None:
        """The device may reboot while out of sight, so state it only reports
        in command replies is no longer known."""
        self.samples.pop("led", None)
        self._fail_commands(reason)

    def _fail_commands(self, reason: str) -> None:
        futures = list(self._pending.values())
        if self._text_waiter is not None:
            futures.append(self._text_waiter[1])
        for fut in futures:
            if not fut.done():
                fut.set_exception(DeviceTimeout(reason))

    # -- commands -------------------------------------------------------------
    async def command(
        self, send: Send, cmd: str, arg: str | None, timeout_s: float,
    ) -> dict[str, Any]:
        """Send ``cmd`` and return the reply from the event topic. Raises
        DeviceTimeout (offline, no reply), DeviceError (error reply) or
        Unsupported (the firmware does not announce the command)."""
        name = self.record.name
        if self.online is False:
            raise DeviceTimeout(f"{name} is offline")
        if cmd not in self.commands:
            raise Unsupported(
                f"{name} does not support the {cmd!r} command "
                f"(it supports: {', '.join(self.commands) or 'none'})")
        if self.json_commands:
            reply = await self._json_command(send, cmd, arg, timeout_s)
        else:
            async with self._text_lock:
                reply = await self._text_command(send, cmd, arg, timeout_s)
        if reply.get("ok") is False or "error" in reply:
            detail = _clip(str(reply.get("error", "failed")), 120)
            code = reply.get("code")
            suffix = f" (code {code})" if isinstance(code, int) else ""
            raise DeviceError(f"{name}: {cmd} failed: {detail}{suffix}")
        return reply

    async def _json_command(
        self, send: Send, cmd: str, arg: str | None, timeout_s: float,
    ) -> dict[str, Any]:
        cid = secrets.token_hex(6)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        body: dict[str, Any] = {"id": cid, "cmd": cmd}
        if arg is not None:
            body["arg"] = arg
        try:
            async with asyncio.timeout(timeout_s):
                await send(self.topic("cmd"), _dumps(body))
                return await fut
        except TimeoutError:
            raise DeviceTimeout(
                f"{self.record.name}: no reply to {cmd!r} within {timeout_s:g} s") from None
        finally:
            self._pending.pop(cid, None)
            _abandon(fut)

    async def _text_command(
        self, send: Send, cmd: str, arg: str | None, timeout_s: float,
    ) -> dict[str, Any]:
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._text_waiter = (_REPLY_KEYS.get(cmd), fut)
        text = cmd if arg is None else f"{cmd} {arg}"
        try:
            async with asyncio.timeout(timeout_s):
                await send(self.topic("cmd"), text.encode())
                return await fut
        except TimeoutError:
            raise DeviceTimeout(
                f"{self.record.name}: no reply to {text!r} within {timeout_s:g} s") from None
        finally:
            self._text_waiter = None
            _abandon(fut)

    async def write(self, obj: str, value: Value, send: Send, timeout_s: float) -> Value:
        if obj != "led" or "led" not in self._specs:
            return await super().write(obj, value, send, timeout_s)
        try:
            on = coerce(value, "bool")
        except ValueError as e:
            raise InvalidRequest(f"{self.record.name}/led takes true or false: {e}") from None
        if on is None:
            raise InvalidRequest(f"{self.record.name}/led takes true or false")
        reply = await self.command(send, "led", "on" if on else "off", timeout_s)
        led = reply.get("led")
        return led if isinstance(led, bool) else on

    async def identify(self, send: Send, seconds: int, timeout_s: float) -> None:
        if "identify" not in self.commands:
            raise Unsupported(
                f"{self.record.name}: this firmware does not announce 'identify' in "
                f"info.caps.cmds; find the board by its client id {self.client_id}")
        await self.command(send, "identify", str(seconds), timeout_s)

    def extra(self) -> dict[str, Any]:
        return {
            "info": self.info,
            "status": self.status,
            "topic_prefix": self.prefix,
            "commands": list(self.commands),
            "json_commands": self.json_commands,
            "last_error": self.last_error,
        }


class GenericJsonProfile(Profile):
    """Points picked out of the payloads on ``topic`` (a filter; wildcards are
    allowed). A payload that is not JSON is taken as a string, so ``path: $``
    maps plain-text topics. Writable points publish ``{"<point id>": value}``
    on ``command_topic`` (per point or per device); nothing is read back, the
    new value arrives with the device's next message."""

    name = "generic-json"

    def __init__(self, site: str, record: DeviceRecord, spec: Mapping[str, Any]) -> None:
        super().__init__(site, record)
        topic = spec.get("topic")
        if not isinstance(topic, str) or not valid_filter(topic):
            raise InvalidRequest(f"{record.name}: generic-json needs a valid topic, not {topic!r}")
        command_topic = spec.get("command_topic")
        if command_topic is not None and (
                not isinstance(command_topic, str) or not valid_topic(command_topic)):
            raise InvalidRequest(f"{record.name}: invalid command_topic {command_topic!r}")
        raw_points = spec.get("points")
        if not isinstance(raw_points, list) or not raw_points:
            raise InvalidRequest(f"{record.name}: generic-json needs a non-empty points list")
        self.topic = topic
        self.command_topic: str | None = command_topic
        self._paths: dict[str, JsonPath] = {}
        self._command_topics: dict[str, str] = {}
        for i, raw in enumerate(raw_points):
            self._add_point(f"{record.name}: points[{i}]", raw)

    def _add_point(self, where: str, raw: Any) -> None:
        if not isinstance(raw, Mapping):
            raise InvalidRequest(f"{where} must be a mapping")
        obj = raw.get("id")
        if not isinstance(obj, str) or not _valid_obj(obj):
            raise InvalidRequest(f"{where}: invalid point id {obj!r}")
        if obj in self._specs:
            raise InvalidRequest(f"{where}: duplicate point id {obj!r}")
        try:
            path = JsonPath.compile(raw.get("path", f"$['{obj}']"))
        except JsonPathError as e:
            raise InvalidRequest(f"{where}: {e}") from None
        datatype = raw.get("datatype", "real")
        if datatype not in DATATYPES:
            raise InvalidRequest(f"{where}: datatype must be one of {sorted(DATATYPES)}")
        units = raw.get("units")
        if units is not None and not isinstance(units, str):
            raise InvalidRequest(f"{where}: units must be a string")
        writable = raw.get("writable", False)
        if not isinstance(writable, bool):
            raise InvalidRequest(f"{where}: writable must be true or false")
        command_topic = raw.get("command_topic", self.command_topic)
        if command_topic is not None and (
                not isinstance(command_topic, str) or not valid_topic(command_topic)):
            raise InvalidRequest(f"{where}: invalid command_topic {command_topic!r}")
        kind_text = raw.get("kind", "output" if writable else "input")
        try:
            kind = PointKind(kind_text)
        except ValueError:
            raise InvalidRequest(f"{where}: kind must be input, output or value") from None
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise InvalidRequest(f"{where}: tags must be a list of strings")
        trust_retained = raw.get("trust_retained", False)
        if not isinstance(trust_retained, bool):
            raise InvalidRequest(f"{where}: trust_retained must be true or false")
        meta: dict[str, Any] = {"topic": self.topic, "path": str(path)}
        if writable:
            if command_topic is None:
                raise InvalidRequest(f"{where}: a writable point needs a command_topic")
            meta["command_topic"] = command_topic
            self._command_topics[obj] = command_topic
        self._paths[obj] = path
        self._specs[obj] = PointSpec(
            obj, str(raw.get("name") or obj), kind, datatype, units, writable,
            trust_retained=trust_retained, description=str(raw.get("description", "")),
            source=f"mqtt:{self.topic} {path}", tags=tuple(tags), meta=meta,
        )

    @property
    def filters(self) -> list[str]:
        return [self.topic]

    def handle(self, topic: str, payload: bytes, now: float) -> list[str]:
        doc = _decode(payload)
        self.online = True
        changed = []
        for obj, path in self._paths.items():
            found, raw = path.find(doc)
            if found:
                self.samples[obj] = _sample(raw, self._specs[obj].datatype, now)
                changed.append(obj)
        if not changed:
            self._note(f"a message on {topic} has none of the configured paths")
        return changed

    async def write(self, obj: str, value: Value, send: Send, timeout_s: float) -> Value:
        spec = self._specs.get(obj)
        if spec is None or not spec.writable:
            return await super().write(obj, value, send, timeout_s)
        try:
            converted = coerce(value, spec.datatype)
        except ValueError as e:
            raise InvalidRequest(f"{self.record.name}/{obj}: {e}") from None
        if converted is None:
            raise InvalidRequest(f"{self.record.name}/{obj} needs a value")
        await send(self._command_topics[obj], _dumps({obj: converted}))
        return converted

    def extra(self) -> dict[str, Any]:
        return {
            "info": None,
            "topic": self.topic,
            "command_topic": self.command_topic,
            "last_error": self.last_error,
        }


PROFILES: dict[str, type[MqttTlsProfile] | type[GenericJsonProfile]] = {
    MqttTlsProfile.name: MqttTlsProfile,
    GenericJsonProfile.name: GenericJsonProfile,
}


def make_profile(site: str, record: DeviceRecord, spec: Mapping[str, Any]) -> Profile:
    """The profile named by ``spec["profile"]`` (default mqtt_tls)."""
    name = spec.get("profile", MqttTlsProfile.name)
    cls = PROFILES.get(name) if isinstance(name, str) else None
    if cls is None:
        raise InvalidRequest(
            f"{record.name}: unknown MQTT profile {name!r} (known: {', '.join(PROFILES)})")
    return cls(site, record, spec)


def _valid_obj(obj: str) -> bool:
    try:
        PointRef.parse(f"s/d/{obj}")
    except ValueError:
        return False
    return True


def _abandon(fut: asyncio.Future[Any]) -> None:
    """Settle a reply future its command no longer waits for. When the send
    failed after ``_fail_commands`` had already set an exception on it,
    retrieving that exception stops asyncio from logging it as unhandled."""
    if not fut.done():
        fut.cancel()
    elif not fut.cancelled():
        fut.exception()


def _sample(raw: Any, datatype: Datatype, now: float) -> Sample:
    try:
        return Sample(coerce(raw, datatype), now)
    except ValueError as e:
        return Sample(None, now, error=f"payload value is not {datatype}: {e}")


def _decode(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except (ValueError, RecursionError):
        return payload.decode("utf-8", "replace").strip()


def json_object(payload: bytes) -> dict[str, Any] | None:
    try:
        doc = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _dumps(doc: Mapping[str, Any]) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode()


def short_text(value: Any) -> str:
    """A device-supplied scalar as a bounded string ("" for anything else)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    return _clip(str(value))


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
