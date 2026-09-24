"""Simulated BACnet-uc node: the firmware's SMP management plane over UDP.

``SimNode`` answers the groups of ``docs/management-protocol.md`` from
in-memory state: OS (echo, reset, mcumgr_params), FS (upload, download,
status, hash) on a RAM file system, ``uc_app`` (install validates the module
and records it in ``/lfs/cfg/apps.json``), ``uc_io`` (simulated channels with
forces and leases) and ``uc_node`` (info, reload of device/io/apps.json,
paged objects, property read/write with 16-level priority arrays, identify).

IO channels are all of kind ``sim``. A scan maps them to their io.json
objects like the firmware: ``ai`` millivolts -> PV = raw * scale + offset,
``ao`` PV -> raw % = (PV - offset) / scale, binary with ``invert``. An
optional ``RoomModel`` closes the loop from ``ao0``/``do0`` to ``ai0``.
Applications are Python emulations of the WASM modules (see
``sim.network``), picked by file name.

``SimFeatures`` switches off what older firmware lacks (identify, lease_ms,
hwid/mac, mcumgr_params, FS hash) so the hub's fallbacks can be tested.
Time comes from an injectable clock; with a ``ManualClock`` nothing moves
unless the test advances it.

CLI: ``python -m uc_hub.sim.smp_node --port 1337 --instance 2041 --name r204-ctl``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import math
import re
import signal
import time
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, TypeVar

import cbor2
import jsonschema

from ..core.errors import InvalidRequest
from ..core.ids import OBJECT_TYPES
from ..drivers.bacnet_uc import names
from ..drivers.bacnet_uc.api import (
    APP_INSTALL,
    APP_LIST,
    APP_REMOVE,
    APP_START,
    APP_STATUS,
    APP_STOP,
    CFG_DIR,
    GROUP_FS,
    GROUP_OS,
    GROUP_UC_APP,
    GROUP_UC_IO,
    GROUP_UC_NODE,
    IO_CATALOG,
    IO_FORCE,
    IO_READ,
    IO_WRITE,
    NODE_IDENTIFY,
    NODE_INFO,
    NODE_OBJECTS,
    NODE_PROP_READ,
    NODE_PROP_WRITE,
    NODE_RELOAD,
)
from ..drivers.bacnet_uc.smp import (
    FS_CLOSE,
    FS_ERR_CHECKSUM_HASH_NOT_FOUND,
    FS_ERR_FILE_EMPTY,
    FS_ERR_FILE_INVALID_NAME,
    FS_ERR_FILE_NOT_FOUND,
    FS_ERR_FILE_OFFSET_LARGER_THAN_FILE,
    FS_ERR_FILE_OFFSET_NOT_VALID,
    FS_ERR_FILE_WRITE_FAILED,
    FS_ERR_MOUNT_POINT_NOT_FOUND,
    FS_FILE,
    FS_HASH,
    FS_STATUS,
    HEADER_SIZE,
    MGMT_ERR_EACCESSDENIED,
    MGMT_ERR_EBADSTATE,
    MGMT_ERR_EBUSY,
    MGMT_ERR_ECORRUPT,
    MGMT_ERR_EINVAL,
    MGMT_ERR_EMSGSIZE,
    MGMT_ERR_ENOENT,
    MGMT_ERR_ENOMEM,
    MGMT_ERR_ENOTSUP,
    MGMT_ERR_EUNKNOWN,
    MGMT_ERR_UNSUPPORTED_TOO_NEW,
    OP_READ,
    OP_WRITE,
    OS_ECHO,
    OS_MCUMGR_PARAMS,
    OS_RESET,
    SMP_V1,
    SMP_V2,
    UC_RC_BUSY,
    UC_RC_EXISTS,
    UC_RC_INVALID,
    UC_RC_IO,
    UC_RC_LIMIT,
    UC_RC_NO_MEM,
    UC_RC_NOT_FOUND,
    UC_RC_PERM,
    UC_RC_STATE,
    UC_RC_UNSUPPORTED,
    UC_RC_VERIFY,
    SmpFrameError,
    decode_frame,
    decode_header,
    encode_frame,
    legacy_error,
    response_op,
    v2_error,
)
from .network import (
    PROP_PRESENT_VALUE,
    UC_DEVICE_LOCAL,
    UC_ERR_BACNET,
    UC_ERR_BUSY,
    UC_ERR_EXISTS,
    UC_ERR_INVALID,
    UC_ERR_IO,
    UC_ERR_NO_MEM,
    UC_ERR_NO_ROUTE,
    UC_ERR_NOT_FOUND,
    UC_ERR_PERM,
    UC_ERR_TYPE,
    UC_ERR_UNSUPPORTED,
    EmulatedApp,
    ManualClock,
    SimNetwork,
    UcError,
    emulation_for,
    log_level,
)
from .room import RoomModel

logger = logging.getLogger(__name__)

T = TypeVar("T")
_Handler = Callable[[dict[str, Any]], dict[str, Any]]

AI, AO, AV, BI, BO, BV, DEVICE, MSI, MSO, MSV = 0, 1, 2, 3, 4, 5, 8, 13, 14, 19
NETWORK_PORT = 56
ANALOG = frozenset({AI, AO, AV})
BINARY = frozenset({BI, BO, BV})
MULTI_STATE = frozenset({MSI, MSO, MSV})
COMMANDABLE = frozenset({AO, AV, BO, BV, MSO, MSV})
POINT_TYPES = ANALOG | BINARY | MULTI_STATE

P_APP_SW, P_COV_INC, P_DESCRIPTION, P_EVENT_STATE, P_FIRMWARE = 12, 22, 28, 36, 44
P_LOCATION, P_MODEL, P_NUM_STATES, P_NAME, P_TYPE, P_OOS = 58, 70, 74, 77, 79, 81
P_PV, P_PRIORITY_ARRAY, P_RELIABILITY, P_RELINQUISH = 85, 87, 103, 104
P_SYSTEM_STATUS, P_UNITS, P_VENDOR_ID, P_VENDOR_NAME = 112, 117, 120, 121

OWNER_SYSTEM, OWNER_IO = "system", "io"
UC_API_VERSION = 0x00010000
FS_TOTAL = 1 << 20
APP_MAX_FILE_SIZE = 256 * 1024
APPS_MAX = 8
BUF_COUNT = 4
WASM_POOL = 256 * 1024
DEFAULT_BOARD = "native_sim/native/64"
APPS_JSON = f"{CFG_DIR}/apps.json"
PERMS = ("bacnet.local", "bacnet.remote", "io", "kv")

_IO_TYPES = {"di": {BI, MSI}, "do": {BO, BV}, "ai": {AI}, "ao": {AO, AV}}
_APP_NAME = re.compile(r"^[a-z0-9_-]{1,23}$")
_APP_FILE = re.compile(r"^/lfs/apps/[A-Za-z0-9_.-]{1,40}\.(wasm|aot)$")
_PARAM_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,23}$")
_MODULE_MAGIC = {".wasm": b"\0asm", ".aot": b"\0aot"}
_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "manifest" / "schemas"

#: (name, kind, description, initial raw value) of the sim board's catalog.
DEFAULT_CHANNELS: tuple[tuple[str, str, str, float], ...] = (
    ("di0", "di", "Simulated digital input 0", 0.0),
    ("di1", "di", "Simulated digital input 1", 0.0),
    ("do0", "do", "Simulated digital output 0", 0.0),
    ("do1", "do", "Simulated digital output 1", 0.0),
    ("ai0", "ai", "Simulated analog input 0 (mV)", 700.0),
    ("ai1", "ai", "Simulated analog input 1 (mV)", 700.0),
    ("ao0", "ao", "Simulated analog output 0 (%)", 0.0),
    ("ao1", "ao", "Simulated analog output 1 (%)", 0.0),
)

#: schemas/examples/io.json of the firmware branch.
EXAMPLE_IO: tuple[dict[str, Any], ...] = (
    {"channel": "di0", "type": "binary-input", "instance": 1, "name": "User Button",
     "debounce_ms": 30},
    {"channel": "do0", "type": "binary-output", "instance": 1, "name": "Heater Relay"},
    {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "Room Temperature",
     "units": "degrees-celsius", "scale": 0.1, "offset": -50.0, "cov_increment": 0.2,
     "sample_ms": 500},
    {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "Valve Position",
     "units": "percent", "min": 0, "max": 100},
)

#: Commands that exist only when the named ``SimFeatures`` flag is on; without
#: them Zephyr answers "not supported" (legacy rc ENOTSUP).
_FEATURE_COMMANDS = {
    (GROUP_OS, OS_MCUMGR_PARAMS): "mcumgr_params",
    (GROUP_FS, FS_HASH): "fs_hash",
    (GROUP_UC_NODE, NODE_IDENTIFY): "identify",
}

_RC_TO_UC = {
    UC_RC_INVALID: UC_ERR_INVALID,
    UC_RC_NOT_FOUND: UC_ERR_NOT_FOUND,
    UC_RC_EXISTS: UC_ERR_EXISTS,
    UC_RC_BUSY: UC_ERR_BUSY,
    UC_RC_NO_MEM: UC_ERR_NO_MEM,
    UC_RC_IO: UC_ERR_IO,
    UC_RC_PERM: UC_ERR_PERM,
    UC_RC_UNSUPPORTED: UC_ERR_UNSUPPORTED,
}
_UC_TO_LEGACY = {
    UC_RC_INVALID: MGMT_ERR_EINVAL,
    UC_RC_NOT_FOUND: MGMT_ERR_ENOENT,
    UC_RC_BUSY: MGMT_ERR_EBUSY,
    UC_RC_NO_MEM: MGMT_ERR_ENOMEM,
    UC_RC_STATE: MGMT_ERR_EBADSTATE,
    UC_RC_VERIFY: MGMT_ERR_ECORRUPT,
    UC_RC_PERM: MGMT_ERR_EACCESSDENIED,
    UC_RC_UNSUPPORTED: MGMT_ERR_ENOTSUP,
}
# fs_mgmt_translate_error_code() in Zephyr's fs_mgmt.c.
_FS_TO_LEGACY = {2: MGMT_ERR_EINVAL, 13: MGMT_ERR_EINVAL, 3: MGMT_ERR_ENOENT, 14: MGMT_ERR_ENOENT,
                 17: MGMT_ERR_ENOENT}


class RcError(Exception):
    """A request fails with ``rc``: a code of the request's group, or an
    MCUmgr code when ``legacy``. ``extra`` fields join the error response."""

    def __init__(
        self, rc: int, message: str = "", *, legacy: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or f"rc {rc}")
        self.rc = rc
        self.legacy = legacy
        self.extra = extra or {}


def _invalid(message: str, legacy: bool = False) -> RcError:
    return RcError(MGMT_ERR_EINVAL if legacy else UC_RC_INVALID, message, legacy=legacy)


def _is_uint(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _get_str(body: Mapping[str, Any], key: str, legacy: bool = False) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(f"'{key}' must be non-empty text", legacy)
    return value


def _get_uint(
    body: Mapping[str, Any], key: str, default: int | None = None, legacy: bool = False,
) -> int:
    value = body.get(key, default)
    if not _is_uint(value):
        raise _invalid(f"'{key}' must be an unsigned integer", legacy)
    return int(value)


def _get_number(body: Mapping[str, Any], key: str) -> float:
    value: Any = body.get(key)
    if not _is_number(value):
        raise _invalid(f"'{key}' must be a finite number")
    return float(value)


def _parse_id(value: Any, parse: Callable[[str | int], int], what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _invalid(f"'{what}' must be a name or a number")
    try:
        return parse(value)
    except InvalidRequest as e:
        raise _invalid(str(e)) from e


def _to_double(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise UcError(UC_ERR_TYPE, f"{type(value).__name__} value is not numeric")


@functools.cache
def _validator(doc: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((_SCHEMA_DIR / f"{doc}.schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@dataclass(slots=True)
class SimFeatures:
    """Firmware features the hub asked for or that are optional in Zephyr."""

    identify: bool = True
    lease_ms: bool = True
    hwid: bool = True
    mcumgr_params: bool = True
    fs_hash: bool = True
    #: A whole-array prop_read returns the list. The firmware decodes only
    #: the first application value, so it answers with slot 1.
    array_read: bool = False


@dataclass(slots=True)
class SimChannel:
    id: int
    name: str
    kind: str
    desc: str
    initial: float = 0.0
    value: float = 0.0
    forced: float | None = None
    force_until: float | None = None

    @property
    def is_input(self) -> bool:
        return self.kind in ("di", "ai")

    @property
    def effective(self) -> float:
        return self.forced if self.forced is not None else self.value

    def check_raw(self, value: float) -> float:
        if not math.isfinite(value):
            raise _invalid("channel values must be finite")
        if self.kind in ("di", "do") and value not in (0.0, 1.0):
            raise _invalid(f"{self.name} takes 0 or 1")
        if self.kind == "ao" and not 0.0 <= value <= 100.0:
            raise _invalid(f"{self.name} takes 0..100 %")
        return value

    def release(self) -> None:
        self.forced = None
        self.force_until = None


@dataclass(slots=True)
class SimObject:
    """A BACnet object with the properties BACnet-uc boards expose."""

    type: int
    instance: int
    name: str
    owner: str
    description: str = ""
    units: int | None = None
    cov_increment: float | None = None
    number_of_states: int | None = None
    out_of_service: bool = False
    pv: Any = None
    priority: list[Any] | None = None
    relinquish_default: Any = None
    #: Read-only (or device-level) properties by number.
    extra: dict[int, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls, obj_type: int, instance: int, name: str, owner: str, *, units: int | None = None,
        description: str = "", cov_increment: float = 0.1,
    ) -> SimObject:
        obj = cls(obj_type, instance, name, owner, description=description)
        if obj_type in ANALOG:
            obj.pv = 0.0
            obj.units = units if units is not None else names.UNITS_NO_UNITS
            obj.cov_increment = cov_increment
        elif obj_type in BINARY:
            obj.pv = 0
        elif obj_type in MULTI_STATE:
            obj.pv = 1
            obj.number_of_states = 4
        if obj_type in COMMANDABLE:
            obj.priority = [None] * 16
            obj.relinquish_default = obj.pv
        return obj

    @property
    def key(self) -> tuple[int, int]:
        return self.type, self.instance

    @property
    def present_value(self) -> Any:
        if self.priority is None:
            return self.pv
        for value in self.priority:
            if value is not None:
                return value
        return self.relinquish_default

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": names.object_type_name(self.type), "instance": self.instance,
            "name": self.name, "owner": self.owner,
        }
        if self.type in POINT_TYPES:
            out["pv"] = self.present_value
        return out

    def coerce(self, value: Any) -> Any:
        """A CBOR number as the present value datatype of this object."""
        if self.type in ANALOG:
            if isinstance(value, bool):
                return float(value)
            if _is_number(value):
                return float(value)
        elif self.type in BINARY:
            if isinstance(value, bool):
                return int(value)
            if _is_number(value) and value in (0, 1):
                return int(value)
        elif self.type in MULTI_STATE:
            if (_is_number(value) and float(value).is_integer()
                    and 1 <= value <= (self.number_of_states or 1)):
                return int(value)
        raise _invalid(f"{value!r} is not a valid value for {names.object_type_name(self.type)}")

    def read(self, prop: int, index: int | None, whole_array: bool) -> Any:
        if index is not None and prop != P_PRIORITY_ARRAY:
            raise _invalid("property is not an array")
        if prop == P_NAME:
            return self.name
        if prop == P_TYPE:
            return self.type
        if prop == P_DESCRIPTION:
            return self.description
        if prop in self.extra:
            return self.extra[prop]
        if self.type in POINT_TYPES:
            if prop == P_PV:
                return self.present_value
            if prop == P_OOS:
                return self.out_of_service
            if prop in (P_EVENT_STATE, P_RELIABILITY):
                return 0
            if prop == P_UNITS and self.units is not None:
                return self.units
            if prop == P_COV_INC and self.cov_increment is not None:
                return self.cov_increment
            if prop == P_NUM_STATES and self.number_of_states is not None:
                return self.number_of_states
            if self.priority is not None:
                if prop == P_RELINQUISH:
                    return self.relinquish_default
                if prop == P_PRIORITY_ARRAY:
                    if index is None:
                        return list(self.priority) if whole_array else self.priority[0]
                    if index == 0:
                        return len(self.priority)
                    if 1 <= index <= 16:
                        return self.priority[index - 1]
                    raise _invalid(f"array index {index} out of range")
        raise RcError(UC_RC_NOT_FOUND, f"unknown property {prop}")

    def write(self, prop: int, value: Any, priority: int | None, *, by_owner: bool = False) -> int | None:
        """Returns the priority slot written for commandable present values."""
        if prop in (P_NAME, P_DESCRIPTION, P_LOCATION):
            if prop == P_LOCATION and self.type != DEVICE:
                raise RcError(UC_RC_NOT_FOUND, f"unknown property {prop}")
            if not isinstance(value, str) or len(value) > 63 or (prop == P_NAME and not value):
                raise _invalid("expected text of up to 63 characters")
            if prop == P_NAME:
                self.name = value
            elif prop == P_DESCRIPTION:
                self.description = value
            else:
                self.extra[P_LOCATION] = value
            return None
        if self.type in POINT_TYPES:
            if prop == P_PV:
                return self._write_pv(value, priority, by_owner)
            if prop == P_OOS:
                if not isinstance(value, bool) and value not in (0, 1):
                    raise _invalid("out-of-service takes a boolean")
                self.out_of_service = bool(value)
                return None
            if prop == P_COV_INC and self.cov_increment is not None:
                if not _is_number(value) or value < 0:
                    raise _invalid("cov-increment takes a number >= 0")
                self.cov_increment = float(value)
                return None
            if prop == P_RELINQUISH and self.priority is not None:
                self.relinquish_default = self.coerce(value)
                return None
        self.read(prop, None, False)  # NOT_FOUND for unknown properties
        raise RcError(UC_RC_PERM, f"property {prop} is read-only")

    def _write_pv(self, value: Any, priority: int | None, by_owner: bool) -> int | None:
        if self.priority is not None:
            slot = priority or 16
            if not 1 <= slot <= 16:
                raise _invalid(f"priority {slot} not in 1..16")
            if slot == 6:
                raise RcError(UC_RC_PERM, "priority 6 is reserved for minimum on/off time")
            self.priority[slot - 1] = None if value is None else self.coerce(value)
            return slot
        if value is None:
            raise _invalid(f"{names.object_type_name(self.type)} is not commandable")
        if not (self.out_of_service or by_owner):
            raise RcError(UC_RC_PERM, "present-value of an input is read-only unless out-of-service")
        self.pv = self.coerce(value)
        return None


@dataclass(slots=True)
class _Binding:
    channel: SimChannel
    obj: SimObject
    scale: float = 1.0
    offset: float = 0.0
    min: float | None = None
    max: float | None = None
    invert: bool = False


@dataclass(slots=True)
class _Upload:
    path: str
    total: int | None
    data: bytearray


@dataclass(slots=True)
class AppConfig:
    """One installed application (an apps.json entry)."""

    name: str
    file: str
    autostart: bool = True
    period_ms: int = 1000
    heap_kb: int = 8
    stack_kb: int = 4
    perms: list[str] = field(default_factory=list)
    params: dict[str, str] = field(default_factory=dict)
    sha256: bytes | None = None

    @classmethod
    def from_wire(cls, body: Mapping[str, Any]) -> AppConfig:
        """``<app manifest>`` (params a map, sha256 bytes), validated like
        the firmware does."""
        name, file = body.get("name"), body.get("file")
        if not isinstance(name, str) or not _APP_NAME.match(name):
            raise _invalid("name must match [a-z0-9_-]{1,23}")
        if not isinstance(file, str) or not _APP_FILE.match(file):
            raise _invalid("file must be /lfs/apps/<name>.wasm or .aot")
        cfg = cls(name, file)
        autostart = body.get("autostart", True)
        if not isinstance(autostart, bool):
            raise _invalid("autostart must be a boolean")
        cfg.autostart = autostart
        for key, maximum, minimum in (("period_ms", 3_600_000, 0), ("heap_kb", 256, 0),
                                      ("stack_kb", 64, 1)):
            value = body.get(key, getattr(cfg, key))
            if not _is_uint(value) or not minimum <= value <= maximum:
                raise _invalid(f"{key} must be {minimum}..{maximum}")
            setattr(cfg, key, value)
        perms = body.get("perms", [])
        if (not isinstance(perms, list) or len(set(perms)) != len(perms)
                or any(p not in PERMS for p in perms)):
            raise _invalid(f"perms must be distinct items of {PERMS}")
        cfg.perms = list(perms)
        params = body.get("params", {})
        if not isinstance(params, dict) or len(params) > 16:
            raise _invalid("params must be a map of at most 16 entries")
        for key, value in params.items():
            if not _PARAM_KEY.match(key) or not isinstance(value, str) or len(value) > 95:
                raise _invalid(f"bad parameter {key!r}")
        cfg.params = dict(params)
        sha = body.get("sha256")
        if sha is not None:
            if not isinstance(sha, bytes) or len(sha) != 32:
                raise _invalid("sha256 must be 32 bytes")
            cfg.sha256 = sha
        return cfg

    @classmethod
    def from_json(cls, entry: Mapping[str, Any]) -> AppConfig:
        """An apps.json entry (schema-validated: params a list, sha256 hex)."""
        wire = dict(entry)
        wire["params"] = {p["key"]: p["value"] for p in entry.get("params", [])}
        if "sha256" in entry:
            wire["sha256"] = bytes.fromhex(entry["sha256"])
        return cls.from_wire(wire)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "file": self.file, "autostart": self.autostart,
            "period_ms": self.period_ms, "heap_kb": self.heap_kb, "stack_kb": self.stack_kb,
            "perms": list(self.perms),
            "params": [{"key": k, "value": v} for k, v in self.params.items()],
        }
        if self.sha256 is not None:
            out["sha256"] = self.sha256.hex()
        return out


@dataclass(slots=True)
class _Subscription:
    id: int
    device: int
    type: int
    instance: int
    last: float | None = None


class SimApp:
    """Runtime of one installed app, and the ``AppHost`` its emulation calls."""

    def __init__(self, node: SimNode, cfg: AppConfig) -> None:
        self.node = node
        self.cfg = cfg
        self.state = "stopped"
        self.ticks = 0
        self.events = 0
        self.errors = 0
        self.last_error = ""
        self.period_ms = cfg.period_ms
        self.app: EmulatedApp | None = None
        self._started_at: float | None = None
        self._next_tick = 0.0
        self._subs: dict[int, _Subscription] = {}
        self._next_sub = 0

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def owner(self) -> str:
        return f"app:{self.cfg.name}"

    @property
    def running(self) -> bool:
        return self.state == "running"

    def status(self) -> dict[str, Any]:
        uptime = 0
        if self.running and self._started_at is not None:
            uptime = round((self.node.now() - self._started_at) * 1000)
        return {
            "name": self.cfg.name, "file": self.cfg.file, "state": self.state,
            "autostart": self.cfg.autostart, "period_ms": self.cfg.period_ms,
            "heap_kb": self.cfg.heap_kb, "stack_kb": self.cfg.stack_kb,
            "perms": list(self.cfg.perms), "ticks": self.ticks, "events": self.events,
            "errors": self.errors, "last_error": self.last_error, "uptime_ms": uptime,
            "emulation": emulation_for(self.cfg.file).kind,
        }

    # -- life cycle -------------------------------------------------------------
    def start(self) -> None:
        if self.running:
            return
        self._teardown()
        self.ticks = self.events = self.errors = 0
        self.last_error = ""
        if self.cfg.file not in self.node.fs:
            self.state = "failed"
            self.last_error = f"module {self.cfg.file} not found"
            return
        now = self.node.now()
        self.period_ms = self.cfg.period_ms
        self._started_at = now
        self._next_tick = now + self.period_ms / 1000.0
        self.app = emulation_for(self.cfg.file)(self)
        self.state = "starting"
        try:
            rc = self.app.init()
        except Exception as e:  # a trap in the emulated module
            self._trap(e)
            return
        if rc != 0:
            self._teardown()
            self.state = "failed"
            self.last_error = f"uc_app_init returned {rc}"
            return
        self.state = "running"
        logger.info("%s: app %s running (%s emulation)", self.node.name, self.name, self.app.kind)

    def stop(self) -> None:
        if self.running and self.app is not None:
            try:
                self.app.deinit()
            except Exception as e:  # a trap in deinit still ends the instance
                logger.warning("%s: app %s trapped in deinit: %s", self.node.name, self.name, e)
        self._teardown()
        self.state = "stopped"

    def _teardown(self) -> None:
        self.node.delete_owned(self.owner)
        self._subs.clear()
        self.app = None
        self._started_at = None

    def _trap(self, exc: Exception) -> None:
        logger.warning("%s: app %s trapped: %s", self.node.name, self.name, exc, exc_info=exc)
        self.errors += 1
        self.last_error = f"trap: {exc}"
        self._teardown()
        self.state = "failed"

    def process(self, now: float) -> None:
        """Deliver COV notifications and run a due tick."""
        if not self.running or self.app is None:
            return
        try:
            self._deliver_cov()
            if self.running and self.period_ms > 0 and now + 1e-9 >= self._next_tick:
                self.app.tick(self.node.uptime_ms())
                self.ticks += 1
                self._next_tick += self.period_ms / 1000.0
                if self._next_tick <= now:
                    self._next_tick = now + self.period_ms / 1000.0
        except Exception as e:  # a trap in the emulated module
            self._trap(e)

    def deliver_write(self, obj_type: int, instance: int, prop: int, priority: int, value: Any) -> None:
        if not self.running or self.app is None or not _is_number(value):
            return
        self.events += 1
        try:
            self.app.on_write(obj_type, instance, prop, priority, float(value))
        except Exception as e:  # a trap in the emulated module
            self._trap(e)

    def _deliver_cov(self) -> None:
        for sub in list(self._subs.values()):
            try:
                value, increment = self._cov_state(sub)
            except UcError:
                continue
            if sub.last is not None and (
                abs(value - sub.last) < increment if increment > 0 else value == sub.last
            ):
                continue
            sub.last = value
            if sub.id in self._subs and self.app is not None:
                self.events += 1
                self.app.on_cov(sub.id, sub.device, sub.type, sub.instance, PROP_PRESENT_VALUE, value)

    def _cov_state(self, sub: _Subscription) -> tuple[float, float]:
        if sub.device == UC_DEVICE_LOCAL:
            return self.node.cov_state(sub.type, sub.instance)
        if self.node.network is None:
            raise UcError(UC_ERR_NO_ROUTE, "no network")
        return self.node.network.cov_state(sub.device, sub.type, sub.instance)

    # -- AppHost ---------------------------------------------------------------
    def _fail(self, code: int, message: str) -> UcError:
        self.errors += 1
        self.last_error = message
        return UcError(code, message)

    def _host(self, perm: str | None, call: Callable[[], T]) -> T:
        if perm is not None and perm not in self.cfg.perms:
            raise self._fail(UC_ERR_PERM, f"permission {perm} not granted")
        try:
            return call()
        except RcError as e:
            raise self._fail(_RC_TO_UC.get(e.rc, UC_ERR_INVALID), str(e)) from None
        except UcError as e:
            raise self._fail(e.code, str(e)) from None

    def _is_local(self, device: int) -> bool:
        return device in (UC_DEVICE_LOCAL, self.node.instance)

    @property
    def local_instance(self) -> int:
        return self.node.instance

    def log(self, level: int, message: str) -> None:
        logger.log(log_level(level), "%s/%s: %s", self.node.name, self.name, message)

    def param(self, key: str) -> str | None:
        return self.cfg.params.get(key)

    def set_tick_period(self, period_ms: int) -> None:
        if period_ms != 0 and period_ms < 10:
            raise self._fail(UC_ERR_INVALID, "tick period must be 0 or >= 10 ms")
        self.period_ms = period_ms
        self._next_tick = self.node.now() + period_ms / 1000.0

    def obj_create(self, obj_type: int, instance: int, name: str) -> None:
        self._host("bacnet.local", lambda: self.node.create_object(obj_type, instance, name, self.owner))

    def obj_delete(self, obj_type: int, instance: int) -> None:
        self._host("bacnet.local", lambda: self.node.delete_object(obj_type, instance, self.owner))

    def prop_read(self, obj_type: int, instance: int, prop: int, index: int = -1) -> float:
        return self._host(
            "bacnet.local",
            lambda: _to_double(self.node.local_read(obj_type, instance, prop, index)),
        )

    def prop_write(
        self, obj_type: int, instance: int, prop: int, value: float, priority: int = 0,
        index: int = -1,
    ) -> None:
        self._host(
            "bacnet.local",
            lambda: self.node.local_write(obj_type, instance, prop, value, priority, index,
                                          writer=self.owner, from_abi=True),
        )

    def prop_write_null(self, obj_type: int, instance: int, prop: int, priority: int) -> None:
        self._host(
            "bacnet.local",
            lambda: self.node.local_write(obj_type, instance, prop, None, priority, -1,
                                          writer=self.owner),
        )

    def _network(self) -> SimNetwork:
        if self.node.network is None:
            raise UcError(UC_ERR_NO_ROUTE, "node is not on a simulated network")
        return self.node.network

    def remote_read(
        self, device: int, obj_type: int, instance: int, prop: int, index: int = -1,
        timeout_ms: int = 1000,
    ) -> float:
        if self._is_local(device):
            return self.prop_read(obj_type, instance, prop, index)
        return self._host(
            "bacnet.remote",
            lambda: _to_double(self._network().read(device, obj_type, instance, prop, index)),
        )

    def remote_write(
        self, device: int, obj_type: int, instance: int, prop: int, value: float,
        priority: int = 0, index: int = -1, timeout_ms: int = 1000,
    ) -> None:
        if self._is_local(device):
            self.prop_write(obj_type, instance, prop, value, priority, index)
            return
        self._host(
            "bacnet.remote",
            lambda: self._network().write(device, obj_type, instance, prop, value, priority, index),
        )

    def cov_subscribe(self, device: int, obj_type: int, instance: int, lifetime_s: int) -> int:
        local = self._is_local(device)

        def subscribe() -> int:
            if local:
                self.node.cov_state(obj_type, instance)
            else:
                self._network().cov_state(device, obj_type, instance)
            self._next_sub += 1
            sub_id = self._next_sub
            self._subs[sub_id] = _Subscription(
                sub_id, UC_DEVICE_LOCAL if local else device, obj_type, instance
            )
            return sub_id

        return self._host("bacnet.local" if local else "bacnet.remote", subscribe)

    def cov_unsubscribe(self, sub_id: int) -> None:
        if self._subs.pop(sub_id, None) is None:
            raise self._fail(UC_ERR_NOT_FOUND, f"no subscription {sub_id}")

    def io_find(self, name: str) -> int:
        return self._host("io", lambda: self.node.channel(name).id)

    def io_read(self, channel: int) -> float:
        return self._host("io", lambda: self.node.channel_by_id(channel).effective)

    def io_write(self, channel: int, value: float) -> None:
        self._host("io", lambda: self.node.write_channel(self.node.channel_by_id(channel), value))


class SimNode:
    """A simulated BACnet-uc board answering SMP on a UDP port."""

    def __init__(
        self,
        *,
        name: str = "r204-ctl",
        instance: int = 2041,
        device_name: str | None = None,
        board: str = DEFAULT_BOARD,
        host: str = "127.0.0.1",
        port: int = 0,
        mtu: int = 1024,
        features: SimFeatures | None = None,
        io: Iterable[Mapping[str, Any]] | None = None,
        room: RoomModel | None = None,
        network: SimNetwork | None = None,
        clock: Callable[[], float] | None = None,
        autorun: bool | None = None,
        scan_interval_s: float = 0.05,
        fw: str = "0.1.0",
        hwid: str | None = None,
        mac: str | None = None,
    ) -> None:
        if not 128 <= mtu <= 0xFFFF:
            raise ValueError("mtu must be 128..65535")
        self.name = name
        self.board = board
        self.host = host
        self.port = port
        self.mtu = mtu
        self.features = features or SimFeatures()
        self.room = room
        self.network = network
        self._clock: Callable[[], float] = clock or (network.clock if network else time.monotonic)
        self.autorun = (not isinstance(self._clock, ManualClock)) if autorun is None else autorun
        self.scan_interval_s = scan_interval_s
        self.fw = fw
        digest = hashlib.sha256(f"{name}/{instance}".encode()).hexdigest()
        self.hwid = hwid or digest[:24]
        self.mac = mac or "02:00:" + ":".join(digest[i : i + 2] for i in range(24, 32, 2))
        #: False makes the node silently drop every request (powered off).
        self.online = True
        #: Number of upcoming requests to drop without an answer.
        self.drop = 0
        self.requests = 0
        self.dropped = 0
        self.bacnet_packets = 0
        self.identify_until = 0.0

        self.fs: dict[str, bytes] = {}
        self.channels: dict[str, SimChannel] = {
            ch_name: SimChannel(i, ch_name, kind, desc, initial, initial)
            for i, (ch_name, kind, desc, initial) in enumerate(DEFAULT_CHANNELS)
        }
        self.objects: dict[tuple[int, int], SimObject] = {}
        self.apps: dict[str, SimApp] = {}
        self._bindings: list[_Binding] = []
        self._leases: dict[tuple[int, int, int], float] = {}
        self._upload: _Upload | None = None
        self._reset_pending = False
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._instance = instance
        self._device_cfg: dict[str, Any] = {}
        self.boot_time = self.now()
        self._last_step = self.boot_time

        device_doc = {"schema": 1, "device": {"instance": instance, "name": device_name or name}}
        self.fs[f"{CFG_DIR}/device.json"] = json.dumps(device_doc, indent=2).encode()
        if io is not None:
            io_doc = {"schema": 1, "points": [dict(p) for p in io]}
            self.fs[f"{CFG_DIR}/io.json"] = json.dumps(io_doc, indent=2).encode()
        self._handlers = self._handler_table()
        self._boot()
        if network is not None:
            network.register(self)

    # -- identity and time --------------------------------------------------------
    @property
    def instance(self) -> int:
        return self._instance

    @property
    def device_name(self) -> str:
        return self.objects[(DEVICE, self._instance)].name

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def reachable(self) -> bool:
        return self.online and not self._stopped

    @property
    def identifying(self) -> bool:
        return self.identify_until > self.now()

    def now(self) -> float:
        return self._clock()

    def uptime_ms(self) -> int:
        return round((self.now() - self.boot_time) * 1000)

    # -- server -------------------------------------------------------------------
    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _ServerProtocol(self), local_addr=(self.host, self.port)
        )
        self._transport = transport
        self.port = transport.get_extra_info("sockname")[1]
        self._stopped = False
        if self.network is not None:
            self.network.register(self)
        if self.autorun:
            self._task = asyncio.create_task(self._run(), name=f"sim-node-{self.name}")
        logger.info("%s: simulated BACnet-uc node %d on udp %s", self.name, self.instance, self.address)

    async def stop(self) -> None:
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()
        if self.network is not None:
            self.network.unregister(self)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.scan_interval_s)
            try:
                self.step()
            except Exception:  # keep the simulated board alive; the traceback is logged
                logger.exception("%s: simulation step failed", self.name)

    async def __aenter__(self) -> SimNode:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- simulation -------------------------------------------------------------
    def step(self) -> None:
        """Advance to the clock's current time: leases, room, IO scan, apps."""
        now = self.now()
        dt = max(0.0, now - self._last_step)
        self._last_step = now
        self._expire(now)
        if self.room is not None and dt > 0:
            valve = self.channels.get(self.room.valve_channel)
            heater = self.channels.get(self.room.heater_channel)
            self.room.advance(
                dt, valve.effective if valve else 0.0, bool(heater and heater.effective >= 0.5)
            )
            sensor = self.channels.get(self.room.sensor_channel)
            if sensor is not None:
                sensor.value = self.room.sensor_mv()
        self._scan()
        for app in list(self.apps.values()):
            app.process(now)
        self._scan()

    def _expire(self, now: float) -> None:
        for ch in self.channels.values():
            if ch.force_until is not None and now >= ch.force_until:
                logger.info("%s: force of %s expired", self.name, ch.name)
                ch.release()
        for key, until in list(self._leases.items()):
            if now >= until:
                del self._leases[key]
                obj = self.objects.get(key[:2])
                if obj is not None and obj.priority is not None:
                    obj.priority[key[2] - 1] = None
                    logger.info("%s: lease on %s:%d priority %d expired",
                                self.name, names.object_type_name(key[0]), key[1], key[2])
        if self.identify_until and now >= self.identify_until:
            self.identify_until = 0.0

    def _scan(self) -> None:
        for b in self._bindings:
            ch, obj = b.channel, b.obj
            if ch.is_input:
                if obj.out_of_service:
                    continue
                raw = ch.effective
                if ch.kind == "ai":
                    obj.pv = raw * b.scale + b.offset
                else:
                    active = (raw >= 0.5) != b.invert
                    obj.pv = (2 if active else 1) if obj.type == MSI else int(active)
            elif ch.forced is None:
                pv = obj.present_value
                if ch.kind == "ao":
                    value = float(pv)
                    if b.min is not None:
                        value = max(value, b.min)
                    if b.max is not None:
                        value = min(value, b.max)
                    raw = (value - b.offset) / b.scale if b.scale else 0.0
                    ch.value = min(max(raw, 0.0), 100.0)
                else:
                    ch.value = float(bool(pv) != b.invert)

    # -- boot and configuration ----------------------------------------------------
    def _boot(self) -> None:
        """Power-on: RAM state is rebuilt from the documents on the FS."""
        for app in self.apps.values():
            app.stop()
        self.apps = {}
        self.objects = {}
        self._bindings = []
        self._leases = {}
        self._upload = None
        self.identify_until = 0.0
        for ch in self.channels.values():
            ch.release()
            ch.value = ch.initial
        try:
            doc = self._load_doc("device")
        except RcError as e:
            logger.warning("%s: device.json: %s; using defaults", self.name, e)
            doc = {"device": {"instance": self._instance, "name": self.name}}
        self._device_cfg = doc
        dev = doc["device"]
        self._instance = dev["instance"]
        device = SimObject(DEVICE, self._instance, dev["name"], OWNER_SYSTEM,
                           description=dev.get("description", ""))
        device.extra.update({
            P_LOCATION: dev.get("location", ""), P_MODEL: self.board, P_FIRMWARE: self.fw,
            P_APP_SW: self.fw, P_VENDOR_NAME: "BACnet-uc", P_VENDOR_ID: 0, P_SYSTEM_STATUS: 0,
        })
        self.objects[device.key] = device
        port_obj = SimObject(NETWORK_PORT, 1, "BACnet/IP port", OWNER_SYSTEM)
        self.objects[port_obj.key] = port_obj
        self.boot_time = self.now()
        self._last_step = self.boot_time
        if f"{CFG_DIR}/io.json" in self.fs:
            try:
                self._apply_io(self._load_doc("io")["points"])
            except RcError as e:
                logger.warning("%s: io.json: %s; no IO points", self.name, e)
        if APPS_JSON in self.fs:
            try:
                self._apply_apps(self._load_doc("apps")["apps"])
            except RcError as e:
                logger.warning("%s: apps.json: %s; no apps", self.name, e)

    def _load_doc(self, doc: str) -> dict[str, Any]:
        raw = self.fs.get(f"{CFG_DIR}/{doc}.json")
        if raw is None:
            raise RcError(UC_RC_NOT_FOUND, f"{doc}.json not found")
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise _invalid(f"{doc}.json is not JSON: {e}") from e
        error = jsonschema.exceptions.best_match(_validator(doc).iter_errors(data))
        if error is not None:
            where = "/".join(str(p) for p in error.absolute_path) or "document"
            raise _invalid(f"{doc}.json {where}: {error.message}")
        if not isinstance(data, dict):
            raise _invalid(f"{doc}.json is not an object")
        return data

    def _apply_io(self, points: list[dict[str, Any]]) -> int:
        for obj in [o for o in self.objects.values() if o.owner == OWNER_IO]:
            self._remove_object(obj)
        self._bindings = []
        bound: set[str] = set()
        for p in points:
            ch = self.channels.get(p["channel"])
            obj_type = OBJECT_TYPES[p["type"]]
            key = (obj_type, p["instance"])
            if ch is None:
                logger.error("%s: io.json: unknown channel %s", self.name, p["channel"])
                continue
            if obj_type not in _IO_TYPES[ch.kind]:
                logger.error("%s: io.json: %s cannot be a %s", self.name, ch.name, p["type"])
                continue
            if ch.name in bound or key in self.objects:
                logger.error("%s: io.json: %s or %s:%d already in use",
                             self.name, ch.name, p["type"], p["instance"])
                continue
            units = None
            if obj_type in ANALOG:
                try:
                    units = names.units_id(p.get("units", "no-units"))
                except InvalidRequest:
                    logger.error("%s: io.json: unknown units %r", self.name, p.get("units"))
            obj = SimObject.create(
                obj_type, p["instance"], p.get("name") or ch.name, OWNER_IO, units=units,
                description=p.get("description", ""), cov_increment=p.get("cov_increment", 0.1),
            )
            self.objects[key] = obj
            self._bindings.append(_Binding(
                ch, obj, float(p.get("scale", 1.0)), float(p.get("offset", 0.0)),
                p.get("min"), p.get("max"), bool(p.get("invert", False)),
            ))
            bound.add(ch.name)
        self._scan()
        return len(self._bindings)

    def _apply_apps(self, entries: list[dict[str, Any]]) -> None:
        wanted: dict[str, AppConfig] = {}
        for entry in entries:
            try:
                cfg = AppConfig.from_json(entry)
            except (RcError, ValueError) as e:
                logger.error("%s: apps.json: app %s skipped: %s", self.name, entry.get("name"), e)
                continue
            wanted[cfg.name] = cfg
        for name, app in list(self.apps.items()):
            if name not in wanted or wanted[name] != app.cfg:
                app.stop()
                del self.apps[name]
        for name, cfg in wanted.items():
            runtime = self.apps.get(name)
            if runtime is None:
                runtime = self.apps[name] = SimApp(self, cfg)
            if cfg.autostart and not runtime.running:
                runtime.start()

    def _save_apps(self) -> None:
        doc = {"schema": 1, "apps": [app.cfg.to_json() for app in self.apps.values()]}
        self.fs[APPS_JSON] = json.dumps(doc, indent=2).encode()

    # -- objects (used by handlers, apps and the network) -------------------------
    def create_object(self, obj_type: int, instance: int, name: str, owner: str) -> None:
        if obj_type not in POINT_TYPES:
            raise _invalid(f"object type {obj_type} cannot be created")
        if not 0 <= instance <= 0x3FFFFE:
            raise _invalid(f"instance {instance} out of range")
        existing = self.objects.get((obj_type, instance))
        if existing is not None:
            if existing.owner == owner:
                return
            raise RcError(UC_RC_EXISTS, f"{names.object_type_name(obj_type)}:{instance} exists")
        obj = SimObject.create(obj_type, instance, name or f"{names.object_type_name(obj_type)}-{instance}", owner)
        self.objects[obj.key] = obj

    def delete_object(self, obj_type: int, instance: int, owner: str) -> None:
        obj = self.objects.get((obj_type, instance))
        if obj is None:
            raise RcError(UC_RC_NOT_FOUND, "no such object")
        if obj.owner != owner:
            raise RcError(UC_RC_PERM, f"object is owned by {obj.owner}")
        self._remove_object(obj)

    def delete_owned(self, owner: str) -> None:
        for obj in [o for o in self.objects.values() if o.owner == owner]:
            self._remove_object(obj)

    def _remove_object(self, obj: SimObject) -> None:
        self.objects.pop(obj.key, None)
        self._bindings = [b for b in self._bindings if b.obj is not obj]
        for key in [k for k in self._leases if k[:2] == obj.key]:
            del self._leases[key]

    def object(self, obj_type: int, instance: int) -> SimObject:
        obj = self.objects.get((obj_type, instance))
        if obj is None:
            raise RcError(UC_RC_NOT_FOUND, f"no object {names.object_type_name(obj_type)}:{instance}")
        return obj

    def local_read(self, obj_type: int, instance: int, prop: int, index: int | None = None) -> Any:
        if index is not None and index < 0:
            index = None
        return self.object(obj_type, instance).read(prop, index, self.features.array_read)

    def local_write(
        self, obj_type: int, instance: int, prop: int, value: Any, priority: int | None = None,
        index: int | None = None, *, writer: str | None = None, from_abi: bool = False,
        lease_ms: int | None = None,
    ) -> None:
        """WriteProperty semantics. ``from_abi`` values are doubles that are
        truncated for binary and multi-state objects, as the app host does."""
        if index is not None and index >= 0:
            raise _invalid("array element writes are not supported")
        obj = self.object(obj_type, instance)
        if from_abi and value is not None and obj.type in BINARY | MULTI_STATE and prop in (P_PV, P_RELINQUISH):
            value = math.trunc(value)
        slot = obj.write(prop, value, priority or None, by_owner=writer is not None and writer == obj.owner)
        if slot is not None:
            lease_key = (obj.type, obj.instance, slot)
            if value is not None and lease_ms is not None and lease_ms > 0:
                self._leases[lease_key] = self.now() + lease_ms / 1000.0
            else:
                self._leases.pop(lease_key, None)
        if obj.owner.startswith("app:") and writer != obj.owner:
            app = self.apps.get(obj.owner[4:])
            if app is not None:
                app.deliver_write(obj.type, obj.instance, prop, slot or 0, value)
        self._scan()

    def cov_state(self, obj_type: int, instance: int) -> tuple[float, float]:
        try:
            obj = self.object(obj_type, instance)
            value = _to_double(obj.present_value)
        except RcError as e:
            raise UcError(UC_ERR_NOT_FOUND, str(e)) from None
        return value, obj.cov_increment or 0.0

    # NetworkNode: remote BACnet access from other simulated nodes.
    def bacnet_read(self, obj_type: int, instance: int, prop: int, index: int = -1) -> Any:
        self.bacnet_packets += 1
        try:
            return self.local_read(obj_type, instance, prop, index)
        except RcError as e:
            raise UcError(UC_ERR_BACNET, f"device {self.instance}: {e}") from None

    def bacnet_write(
        self, obj_type: int, instance: int, prop: int, value: Any, priority: int = 0,
        index: int = -1,
    ) -> None:
        self.bacnet_packets += 1
        try:
            self.local_write(obj_type, instance, prop, value, priority, index, from_abi=True)
        except RcError as e:
            raise UcError(UC_ERR_BACNET, f"device {self.instance}: {e}") from None

    # -- IO ------------------------------------------------------------------------
    def channel(self, name: str) -> SimChannel:
        ch = self.channels.get(name)
        if ch is None:
            raise RcError(UC_RC_NOT_FOUND, f"no channel {name}")
        return ch

    def channel_by_id(self, channel_id: int) -> SimChannel:
        for ch in self.channels.values():
            if ch.id == channel_id:
                return ch
        raise RcError(UC_RC_NOT_FOUND, f"no channel {channel_id}")

    def write_channel(self, ch: SimChannel, value: float) -> None:
        if ch.is_input:
            raise RcError(UC_RC_PERM, f"{ch.name} is an input")
        ch.value = ch.check_raw(float(value))

    def force(self, name: str, value: float | None, lease_ms: int | None = None) -> None:
        ch = self.channel(name)
        if value is None:
            ch.release()
        else:
            ch.forced = ch.check_raw(float(value))
            ch.force_until = self.now() + lease_ms / 1000.0 if lease_ms else None
        self._scan()

    # -- request handling ------------------------------------------------------------
    def handle_datagram(self, data: bytes) -> bytes | None:
        """One SMP request -> the response frame, or None when nothing is sent."""
        if not self.reachable:
            return None
        if self.drop > 0:
            self.drop -= 1
            self.dropped += 1
            return None
        try:
            op, version, _flags, _length, group, seq, command = decode_header(data)
        except SmpFrameError as e:
            logger.debug("%s: ignored datagram: %s", self.name, e)
            return None
        if op not in (OP_READ, OP_WRITE):
            return None
        self.requests += 1
        rsp_version = min(version, SMP_V2)

        def reply(body: dict[str, Any]) -> bytes:
            frame = encode_frame(response_op(op), group, command, seq, body, version=rsp_version)
            if len(frame) > self.mtu:
                frame = encode_frame(response_op(op), group, command, seq,
                                     legacy_error(MGMT_ERR_EMSGSIZE), version=rsp_version)
            return frame

        if len(data) > self.mtu:
            return reply(legacy_error(MGMT_ERR_EMSGSIZE, f"request exceeds the {self.mtu} byte buffer"))
        if version > SMP_V2:
            return reply(legacy_error(MGMT_ERR_UNSUPPORTED_TOO_NEW))
        try:
            request = decode_frame(data)
        except SmpFrameError as e:
            return reply(legacy_error(MGMT_ERR_EINVAL, str(e)))
        handlers = self._handlers.get((group, command))
        handler = handlers[0 if op == OP_READ else 1] if handlers else None
        feature = _FEATURE_COMMANDS.get((group, command))
        if handler is None or (feature is not None and not getattr(self.features, feature)):
            return reply(legacy_error(MGMT_ERR_ENOTSUP))
        try:
            self.step()
            body = handler(request.body)
        except RcError as e:
            body = self._error_body(group, version, e)
        except Exception:  # a bug in the simulator must not kill the server
            logger.exception("%s: handler for group %d cmd %d failed", self.name, group, command)
            body = legacy_error(MGMT_ERR_EUNKNOWN)
        out = reply(body)
        if self._reset_pending:
            self._reset_pending = False
            self._boot()
        return out

    def _error_body(self, group: int, version: int, e: RcError) -> dict[str, Any]:
        logger.debug("%s: group %d error rc %d: %s", self.name, group, e.rc, e)
        if e.legacy:
            body = legacy_error(e.rc, str(e))
        elif version == SMP_V1:
            table = _FS_TO_LEGACY if group == GROUP_FS else _UC_TO_LEGACY
            body = legacy_error(table.get(e.rc, MGMT_ERR_EUNKNOWN), str(e))
        else:
            body = v2_error(group, e.rc)
        body.update(e.extra)
        return body

    def _handler_table(self) -> dict[tuple[int, int], tuple[_Handler | None, _Handler | None]]:
        table: dict[tuple[int, int], tuple[_Handler | None, _Handler | None]] = {
            (GROUP_OS, OS_ECHO): (self._os_echo, self._os_echo),
            (GROUP_OS, OS_RESET): (None, self._os_reset),
            (GROUP_FS, FS_FILE): (self._fs_download, self._fs_upload),
            (GROUP_FS, FS_STATUS): (self._fs_status, None),
            (GROUP_FS, FS_CLOSE): (None, self._fs_close),
            (GROUP_UC_APP, APP_LIST): (self._app_list, None),
            (GROUP_UC_APP, APP_INSTALL): (None, self._app_install),
            (GROUP_UC_APP, APP_START): (None, self._app_start),
            (GROUP_UC_APP, APP_STOP): (None, self._app_stop),
            (GROUP_UC_APP, APP_REMOVE): (None, self._app_remove),
            (GROUP_UC_APP, APP_STATUS): (self._app_status, None),
            (GROUP_UC_IO, IO_CATALOG): (self._io_catalog, None),
            (GROUP_UC_IO, IO_READ): (self._io_read, None),
            (GROUP_UC_IO, IO_WRITE): (None, self._io_write),
            (GROUP_UC_IO, IO_FORCE): (None, self._io_force),
            (GROUP_UC_NODE, NODE_INFO): (self._node_info, None),
            (GROUP_UC_NODE, NODE_RELOAD): (None, self._node_reload),
            (GROUP_UC_NODE, NODE_OBJECTS): (self._node_objects, None),
            (GROUP_UC_NODE, NODE_PROP_READ): (self._node_prop_read, None),
            (GROUP_UC_NODE, NODE_PROP_WRITE): (None, self._node_prop_write),
            (GROUP_OS, OS_MCUMGR_PARAMS): (self._os_params, None),
            (GROUP_FS, FS_HASH): (self._fs_hash, None),
            (GROUP_UC_NODE, NODE_IDENTIFY): (None, self._node_identify),
        }
        return table

    # OS group
    def _os_echo(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("d", "")
        if not isinstance(text, str):
            raise _invalid("'d' must be text", legacy=True)
        return {"r": text}

    def _os_reset(self, body: dict[str, Any]) -> dict[str, Any]:
        self._reset_pending = True
        return {}

    def _os_params(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"buf_size": self.mtu, "buf_count": BUF_COUNT}

    # FS group
    @staticmethod
    def _fs_name(body: dict[str, Any]) -> str:
        name = _get_str(body, "name", legacy=True)
        if "\0" in name or "//" in name or "/../" in f"{name}/" or not name.startswith("/"):
            raise RcError(FS_ERR_FILE_INVALID_NAME, f"bad file name {name!r}")
        return name

    def _fs_used(self) -> int:
        return sum(len(v) for v in self.fs.values())

    def _fs_upload(self, body: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(body)
        off = _get_uint(body, "off", legacy=True)
        data = body.get("data", b"")
        if not isinstance(data, bytes):
            raise _invalid("'data' must be a byte string", legacy=True)
        if not name.startswith("/lfs/"):
            raise RcError(FS_ERR_MOUNT_POINT_NOT_FOUND, f"{name} is not on a mounted file system")
        if off == 0:
            total = _get_uint(body, "len", legacy=True)
            self._upload = _Upload(name, total, bytearray())
        elif self._upload is None or self._upload.path != name:
            existing = self.fs.get(name)
            if existing is None:
                raise RcError(FS_ERR_FILE_NOT_FOUND, f"{name} not found")
            self._upload = _Upload(name, None, bytearray(existing))
        upload = self._upload
        if off != len(upload.data):
            self._upload = None
            raise RcError(FS_ERR_FILE_OFFSET_NOT_VALID, "offset mismatch",
                          extra={"len": len(upload.data)})
        old = len(self.fs.get(name, b""))
        if self._fs_used() - old + len(upload.data) + len(data) > FS_TOTAL:
            self._upload = None
            raise RcError(FS_ERR_FILE_WRITE_FAILED, "file system full")
        upload.data += data
        self.fs[name] = bytes(upload.data)
        if upload.total is not None and len(upload.data) >= upload.total:
            self._upload = None
        return {"off": len(upload.data)}

    def _fs_download(self, body: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(body)
        off = _get_uint(body, "off", legacy=True)
        content = self.fs.get(name)
        if content is None:
            raise RcError(FS_ERR_FILE_NOT_FOUND, f"{name} not found")
        if off > len(content):
            raise RcError(FS_ERR_FILE_OFFSET_LARGER_THAN_FILE, "offset beyond the end of the file")
        head: dict[str, Any] = {"off": off, "data": b""}
        if off == 0:
            head["len"] = len(content)
        room = self.mtu - HEADER_SIZE - len(cbor2.dumps(head)) - 2
        head["data"] = content[off : off + max(room, 1)]
        return head

    def _fs_status(self, body: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(body)
        if name not in self.fs:
            raise RcError(FS_ERR_FILE_NOT_FOUND, f"{name} not found")
        return {"len": len(self.fs[name])}

    def _fs_hash(self, body: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(body)
        kind = body.get("type", "crc32")
        off = _get_uint(body, "off", 0, legacy=True)
        if kind not in ("sha256", "crc32"):
            raise RcError(FS_ERR_CHECKSUM_HASH_NOT_FOUND, f"hash {kind!r} not supported")
        content = self.fs.get(name)
        if content is None:
            raise RcError(FS_ERR_FILE_NOT_FOUND, f"{name} not found")
        if not content:
            raise RcError(FS_ERR_FILE_EMPTY, f"{name} is empty")
        if off > len(content):
            raise RcError(FS_ERR_FILE_OFFSET_LARGER_THAN_FILE, "offset beyond the end of the file")
        length = _get_uint(body, "len", len(content) - off, legacy=True)
        data = content[off : off + length]
        output: Any = hashlib.sha256(data).digest() if kind == "sha256" else zlib.crc32(data)
        rsp: dict[str, Any] = {"type": kind, "len": len(data), "output": output}
        if off:
            rsp["off"] = off
        return rsp

    def _fs_close(self, body: dict[str, Any]) -> dict[str, Any]:
        self._upload = None
        return {}

    # uc_app group
    def _app(self, body: dict[str, Any]) -> SimApp:
        name = _get_str(body, "name")
        app = self.apps.get(name)
        if app is None:
            raise RcError(UC_RC_NOT_FOUND, f"no app {name}")
        return app

    def _app_list(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"apps": [app.status() for app in self.apps.values()]}

    def _app_install(self, body: dict[str, Any]) -> dict[str, Any]:
        cfg = AppConfig.from_wire(body)
        restart = body.get("restart", False)
        if not isinstance(restart, bool):
            raise _invalid("restart must be a boolean")
        content = self.fs.get(cfg.file)
        if content is None:
            raise RcError(UC_RC_NOT_FOUND, f"{cfg.file} not found")
        if len(content) > APP_MAX_FILE_SIZE:
            raise RcError(UC_RC_NO_MEM, f"{cfg.file} exceeds {APP_MAX_FILE_SIZE} bytes")
        magic = _MODULE_MAGIC[Path(cfg.file).suffix]
        if not content.startswith(magic):
            raise RcError(UC_RC_VERIFY, f"{cfg.file} is not a {Path(cfg.file).suffix} module")
        if cfg.sha256 is not None and hashlib.sha256(content).digest() != cfg.sha256:
            raise RcError(UC_RC_VERIFY, f"sha256 of {cfg.file} does not match")
        existing = self.apps.get(cfg.name)
        was_running = existing is not None and existing.running
        if was_running and not restart:
            raise RcError(UC_RC_STATE, f"app {cfg.name} is running")
        if existing is None and len(self.apps) >= APPS_MAX:
            raise RcError(UC_RC_LIMIT, f"at most {APPS_MAX} apps")
        if existing is not None:
            existing.stop()
        app = SimApp(self, cfg)
        self.apps[cfg.name] = app
        self._save_apps()
        if cfg.autostart or was_running:
            app.start()
        return {}

    def _app_start(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self._app(body)
        if app.running:
            raise RcError(UC_RC_STATE, f"app {app.name} is running")
        app.start()
        return {}

    def _app_stop(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self._app(body)
        if app.state == "stopped":
            raise RcError(UC_RC_STATE, f"app {app.name} is not running")
        app.stop()
        return {}

    def _app_remove(self, body: dict[str, Any]) -> dict[str, Any]:
        app = self._app(body)
        delete_file = body.get("delete_file", False)
        if not isinstance(delete_file, bool):
            raise _invalid("delete_file must be a boolean")
        app.stop()
        del self.apps[app.name]
        self._save_apps()
        if delete_file and all(a.cfg.file != app.cfg.file for a in self.apps.values()):
            self.fs.pop(app.cfg.file, None)
        return {}

    def _app_status(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._app(body).status()

    # uc_io group
    def _io_catalog(self, body: dict[str, Any]) -> dict[str, Any]:
        bound = {b.channel.name: b.obj for b in self._bindings}
        channels = []
        for ch in self.channels.values():
            entry: dict[str, Any] = {"id": ch.id, "name": ch.name, "desc": ch.desc, "kind": ch.kind,
                                     "hw": "sim", "forced": ch.forced is not None}
            obj = bound.get(ch.name)
            if obj is not None:
                entry["object"] = {"type": names.object_type_name(obj.type), "instance": obj.instance}
            channels.append(entry)
        return {"board": self.board, "channels": channels}

    def _io_read(self, body: dict[str, Any]) -> dict[str, Any]:
        if "name" in body:
            ch = self.channel(_get_str(body, "name"))
            return {"values": {ch.name: float(ch.effective)}}
        return {"values": {ch.name: float(ch.effective) for ch in self.channels.values()}}

    def _io_write(self, body: dict[str, Any]) -> dict[str, Any]:
        ch = self.channel(_get_str(body, "name"))
        self.write_channel(ch, _get_number(body, "value"))
        return {}

    def _io_force(self, body: dict[str, Any]) -> dict[str, Any]:
        name = _get_str(body, "name")
        if body.get("release") is True:
            self.force(name, None)
            return {}
        value = _get_number(body, "value")
        lease_ms = _get_uint(body, "lease_ms") if self.features.lease_ms and "lease_ms" in body else None
        self.force(name, value, lease_ms)
        return {}

    # uc_node group
    def _node_info(self, body: dict[str, Any]) -> dict[str, Any]:
        running = sum(1 for a in self.apps.values() if a.running)
        info: dict[str, Any] = {
            "fw": self.fw,
            "board": self.board,
            "api": UC_API_VERSION,
            "device": {"instance": self.instance, "name": self.device_name},
            "net": {"ipv4": self.host,
                    "bacnet_port": self._device_cfg.get("bacnet", {}).get("udp_port", 47808)},
            "uptime_s": self.uptime_ms() // 1000,
            "fs": {"ready": True, "total": FS_TOTAL, "free": max(FS_TOTAL - self._fs_used(), 0)},
            "bacnet": {"packets": self.bacnet_packets, "objects": len(self.objects)},
            "apps": {"installed": len(self.apps), "running": running},
            "wasm": {"interp": True, "aot": False, "aot_target": "", "pool_total": WASM_POOL,
                     "pool_free": max(WASM_POOL - running * 16384, 0)},
        }
        if self.features.hwid:
            info["hwid"] = self.hwid
            info["mac"] = self.mac
        return info

    def _node_reload(self, body: dict[str, Any]) -> dict[str, Any]:
        doc = body.get("doc")
        if doc not in ("device", "io", "apps", "all"):
            raise _invalid("doc must be device, io, apps or all")
        reboot = False
        if doc in ("device", "all") and (doc == "device" or f"{CFG_DIR}/device.json" in self.fs):
            reboot = self._reload_device()
        if doc in ("io", "all") and (doc == "io" or f"{CFG_DIR}/io.json" in self.fs):
            self._apply_io(self._load_doc("io")["points"])
        if doc in ("apps", "all") and (doc == "apps" or APPS_JSON in self.fs):
            self._apply_apps(self._load_doc("apps")["apps"])
        return {"reboot_required": reboot}

    def _reload_device(self) -> bool:
        doc = self._load_doc("device")
        dev = doc["device"]
        device = self.objects[(DEVICE, self._instance)]
        device.name = dev["name"]
        device.description = dev.get("description", "")
        device.extra[P_LOCATION] = dev.get("location", "")
        running = self._device_cfg
        return (
            dev["instance"] != self._instance
            or doc.get("network") != running.get("network")
            or doc.get("bacnet", {}).get("udp_port", 47808)
            != running.get("bacnet", {}).get("udp_port", 47808)
        )

    def _node_objects(self, body: dict[str, Any]) -> dict[str, Any]:
        offset = _get_uint(body, "offset", 0)
        count = _get_uint(body, "count", 16) or 16

        def order(obj: SimObject) -> tuple[int, int, int]:
            rank = 0 if obj.type == DEVICE else 1 if obj.type == NETWORK_PORT else 2
            return rank, obj.type, obj.instance

        ordered = sorted(self.objects.values(), key=order)
        page: list[dict[str, Any]] = []
        rsp = {"total": len(ordered), "objects": page}
        for obj in ordered[offset : offset + min(count, 64)]:
            page.append(obj.summary())
            if len(page) > 1 and HEADER_SIZE + len(cbor2.dumps(rsp)) > self.mtu:
                page.pop()
                break
        return rsp

    def _prop_target(self, body: dict[str, Any]) -> tuple[SimObject, int, int | None]:
        obj_type = _parse_id(body.get("type"), names.object_type_id, "type")
        instance = _get_uint(body, "instance")
        prop = _parse_id(body.get("prop"), names.property_id, "prop")
        index = body.get("index")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
            raise _invalid("'index' must be an integer")
        return self.object(obj_type, instance), prop, None if index is None or index < 0 else index

    def _node_prop_read(self, body: dict[str, Any]) -> dict[str, Any]:
        obj, prop, index = self._prop_target(body)
        return {"value": obj.read(prop, index, self.features.array_read)}

    def _node_prop_write(self, body: dict[str, Any]) -> dict[str, Any]:
        obj, prop, index = self._prop_target(body)
        if "value" not in body:
            raise _invalid("'value' is required")
        priority = body.get("priority")
        if priority is not None and (not _is_uint(priority) or not 1 <= priority <= 16):
            raise _invalid("priority must be 1..16")
        lease_ms = None
        if self.features.lease_ms and "lease_ms" in body:
            lease_ms = _get_uint(body, "lease_ms")
        self.local_write(obj.type, obj.instance, prop, body["value"], priority, index,
                         lease_ms=lease_ms)
        return {}

    def _node_identify(self, body: dict[str, Any]) -> dict[str, Any]:
        seconds = _get_uint(body, "seconds", 30)
        self.identify_until = self.now() + seconds if seconds else 0.0
        logger.info("%s: identify %s", self.name, f"for {seconds} s" if seconds else "off")
        return {}

    def __repr__(self) -> str:
        return f"SimNode({self.name!r}, instance={self.instance}, address={self.address!r})"


class _ServerProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: SimNode) -> None:
        self.node = node
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        reply = self.node.handle_datagram(data)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("%s: socket error: %s", self.node.name, exc)


async def start_sim_nodes(
    specs: Iterable[Mapping[str, Any]], *, network: SimNetwork | None = None,
    host: str = "127.0.0.1",
) -> tuple[SimNetwork, list[SimNode]]:
    """Start one ``SimNode`` per spec (``SimNode`` keyword arguments) on
    ephemeral ports of ``host``, all on one network. Close them with
    ``await network.aclose()`` (or ``async with network``)."""
    net = network or SimNetwork()
    nodes: list[SimNode] = []
    try:
        for spec in specs:
            node = SimNode(host=host, port=0, network=net, **spec)
            nodes.append(node)
            await node.start()
    except BaseException:
        for node in nodes:
            await node.stop()
        raise
    return net, nodes


def _read_io(arg: str) -> list[dict[str, Any]] | None:
    if arg == "none":
        return None
    if arg == "example":
        return [dict(p) for p in EXAMPLE_IO]
    doc = json.loads(Path(arg).read_text(encoding="utf-8"))
    points = doc.get("points") if isinstance(doc, dict) else None
    if not isinstance(points, list):
        raise SystemExit(f"{arg}: not an io.json document")
    return points


async def _serve(args: argparse.Namespace) -> None:
    features = SimFeatures(**{f.name: f.name not in args.without for f in fields(SimFeatures)
                              if f.name != "array_read"})
    node = SimNode(
        name=args.name, instance=args.instance, board=args.board, host=args.host, port=args.port,
        mtu=args.mtu, features=features, io=_read_io(args.io),
        room=RoomModel() if args.room else None, network=SimNetwork(),
    )
    await node.start()
    print(f"{node.name}: BACnet-uc sim node, device {node.instance}, SMP on udp {node.address}",
          flush=True)
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, done.set)
    try:
        await done.wait()
    finally:
        await node.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m uc_hub.sim.smp_node",
        description="Simulated BACnet-uc node speaking SMP (MCUmgr) over UDP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=1337, help="UDP port, 0 = ephemeral")
    parser.add_argument("--instance", type=int, default=2041, help="BACnet device instance")
    parser.add_argument("--name", default="r204-ctl", help="node and device name")
    parser.add_argument("--board", default=DEFAULT_BOARD)
    parser.add_argument("--mtu", type=int, default=1024, help="SMP buffer size in bytes")
    parser.add_argument("--io", default="none",
                        help="'example' (di0/do0/ai0/ao0 of schemas/examples/io.json), "
                             "'none', or the path of an io.json")
    parser.add_argument("--room", action="store_true",
                        help="drive ai0 from a thermal room heated through ao0 and do0")
    parser.add_argument("--without", action="append", default=[],
                        choices=["identify", "lease_ms", "hwid", "mcumgr_params", "fs_hash"],
                        help="behave like firmware without this feature (repeatable)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(args))


if __name__ == "__main__":
    main()
