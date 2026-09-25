# SPDX-License-Identifier: Apache-2.0
"""In-process fake BACnet-uc node for tests without hardware.

:class:`FakeNode` serves, on two UDP sockets:

- SMP v2 (docs/management-protocol.md): OS (echo, reset, mcumgr_params,
  info), image (state, upload, erase), FS (file upload/download, status,
  hash, close) on the in-memory :attr:`FakeNode.files`, shell exec (a few
  canned commands) and the custom groups ``uc_app`` (64), ``uc_io`` (65) and
  ``uc_node`` (66) with the documented keys and rc values;
- BACnet/IP: Who-Is -> I-Am (unicast to the requester by default),
  ReadProperty, WriteProperty, DeviceCommunicationControl and
  ReinitializeDevice on the in-memory :attr:`FakeNode.objects`,
  Error/Reject/Abort like a bacnet-stack device.

The IO model follows ``firmware/src/io/uc_io.c``: a default catalog like
native_sim (di0, di1, do0, do1, ai0, ai1, ao0, ao1, all simulated),
``uc_node reload io`` binds channels to BACnet objects from
``/lfs/cfg/io.json``, inputs update the Present_Value of their object (unless
Out_Of_Service), outputs follow the Present_Value of their object, and
forcing overrides a channel until released (io.md section 5). WebAssembly
apps are not executed: install/start/stop/remove only track the documented
state.

Behaviour copied from the firmware as observed on native_sim (Zephyr
4.4.2, the integration build):

- configuration documents are validated in two stages like
  ``uc_config.c`` + ``uc_io.c``: a *parse* stage (JSON syntax, schema
  version, field types and ranges, table limits, duplicate objects,
  ``bacnet.password`` 1..20 printable ASCII) whose failure rejects the whole
  document (rc ``INVALID``), and an *apply* stage in which an ``io.json``
  point on an unknown channel, of the wrong kind, on a channel or object that
  is already bound, or an ``ao`` point with ``scale`` 0 is skipped (the
  reload still succeeds);
- staged documents: ``reload`` activates ``<doc>.json.new`` when it passes
  the parse stage and deletes it otherwise (see :meth:`FakeNode._reload`);
- ``uc_app list``, ``uc_io catalog`` and ``uc_node objects`` answer with
  ``total``, accept ``offset``/``count`` and stop early when the response
  (at most 1016 bytes of CBOR, the UDP MTU minus the SMP header) is full;
  ``objects`` returns 8 entries by default;
- objects are listed by type and instance and include ``network-port:1``;
  analog/binary/multi-state *output* objects are commandable (priority
  array, Relinquish_Default, priority 6 rejected), *value* objects are not:
  a write sets Present_Value whatever the priority, a NULL write succeeds
  without effect, analog-value rejects priority 6, and reading their
  Priority_Array/Relinquish_Default is ``unknown-property`` (rc
  ``NOT_FOUND``);
- ``prop_read`` without ``index`` returns an array or list property whole
  (a CBOR array, rc ``LIMIT`` when it does not fit one response); ``index``
  on any other property is ``INVALID``, index 0 is the array size; values
  without a natural CBOR type are rendered as text (``"(analog-input, 1)"``,
  ``"{false,false,false,false}"``);
- ``prop_write`` rejects NaN/infinity, fractions and negative numbers for
  integer datatypes, anything but 0/1 for binary present values, text for
  numeric properties and text longer than 63 bytes (rc ``INVALID``);
- ``bacnet.password`` of ``device.json`` guards DeviceCommunicationControl
  and ReinitializeDevice (``CONFIG_UC_BACNET_REQUIRE_PASSWORD=y``: without a
  password both are refused with security/password-failure);
- shell output uses ``\r\n`` line ends.

Deliberate simplifications: no WASM runtime (a module only needs a valid
header; ``ticks`` are derived from the uptime and ``period_ms``), outputs
follow Present_Value immediately (the firmware scans every 100 ms), no COV,
no ReadPropertyMultiple, no CreateObject/DeleteObject (like the default
firmware: Reject unrecognized-service), DeviceCommunicationControl is only
recorded (:attr:`FakeNode.dcc_state`), no network change on reboot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import zlib
from dataclasses import dataclass
from typing import Any

import cbor2

from bacnet_uc_harness.bacnet import codec, enums
from bacnet_uc_harness.bacnet.codec import BitString, Double, Enumerated, ObjectId
from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.codec import decode_frame, encode_frame

log = logging.getLogger(__name__)

UC_API_VERSION = (1 << 16) | 0  # wasm/sdk/include/bacnet_uc.h
APPS_MAX = 4  # CONFIG_UC_APPS_MAX default
APP_MAX_FILE_SIZE = 262144  # CONFIG_UC_APP_MAX_FILE_SIZE default
FS_DL_CHUNK = 512
SMP_BUF_SIZE = 1152  # CONFIG_MCUMGR_TRANSPORT_NETBUF_SIZE in prj.conf
SMP_BUF_COUNT = 2  # native_sim build
OBJECTS_COUNT_DEFAULT = 8
#: UC_MGMT_RSP_MAX: CBOR payload of one response (UDP MTU 1024 - 8 byte header)
RSP_MAX = 1024 - 8
#: UC_MGMT_RSP_RESERVE: bytes a list response keeps free after its last element
RSP_RESERVE = 8
IO_POINTS_MAX = 32  # CONFIG_UC_IO_POINTS_MAX
CONFIG_DOC_MAX = 8192  # CONFIG_UC_CONFIG_DOC_MAX
INSTANCE_MAX = 4194302
PASSWORD_MAX = 20
TEXT_WRITE_MAX = 63  # CharacterString bytes accepted by prop_write
LOG_LEVELS = ("err", "wrn", "inf", "dbg")

CFG_DEVICE = "/lfs/cfg/device.json"
CFG_IO = "/lfs/cfg/io.json"
CFG_APPS = "/lfs/cfg/apps.json"
#: staged configuration document: ``uc_node reload`` activates ``<doc>.json.new``
STAGED_SUFFIX = ".new"

PERMS = ("bacnet.local", "bacnet.remote", "io", "kv")
_APP_NAME_RE = re.compile(r"^[a-z0-9_-]{1,23}$")
_APP_FILE_RE = re.compile(r"^/lfs/apps/[A-Za-z0-9_.-]{1,40}\.(wasm|aot)$")
_PARAM_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,23}$")
_WASM_HDR = b"\x00asm\x01\x00\x00\x00"
_AOT_MAGIC = b"\x00aot"

# (name, kind, initial value, description): native_sim catalog
DEFAULT_CATALOG: tuple[tuple[str, str, float, str], ...] = (
    ("di0", "di", 0.0, "Simulated digital input 0"),
    ("di1", "di", 0.0, "Simulated digital input 1"),
    ("do0", "do", 0.0, "Simulated digital output 0"),
    ("do1", "do", 0.0, "Simulated digital output 1"),
    ("ai0", "ai", 2000.0, "Simulated analog input 0 (mV)"),
    ("ai1", "ai", 0.0, "Simulated analog input 1 (mV)"),
    ("ao0", "ao", 0.0, "Simulated analog output 0 (%)"),
    ("ao1", "ao", 0.0, "Simulated analog output 1 (%)"),
)

# io.json: channel kind -> allowed object types
_KIND_TYPES = {
    "di": ("binary-input", "multi-state-input"),
    "do": ("binary-output", "binary-value"),
    "ai": ("analog-input",),
    "ao": ("analog-output", "analog-value"),
}

# io.schema.json "type" enum (uc_config.c io_type_allowed)
_IO_TYPES = (
    "analog-input",
    "analog-output",
    "analog-value",
    "binary-input",
    "binary-output",
    "binary-value",
    "multi-state-input",
)

# commandable (priority array) in the pinned bacnet-stack; the value objects
# AV/BV/MSV have no priority array there
_COMMANDABLE = {
    enums.OBJECT_ANALOG_OUTPUT,
    enums.OBJECT_BINARY_OUTPUT,
    enums.OBJECT_MULTI_STATE_OUTPUT,
}
_VALUE_OBJECTS = {
    enums.OBJECT_ANALOG_VALUE,
    enums.OBJECT_BINARY_VALUE,
    enums.OBJECT_MULTI_STATE_VALUE,
}
_INPUTS = {enums.OBJECT_ANALOG_INPUT, enums.OBJECT_BINARY_INPUT, enums.OBJECT_MULTI_STATE_INPUT}
_BINARY = {enums.OBJECT_BINARY_INPUT, enums.OBJECT_BINARY_OUTPUT, enums.OBJECT_BINARY_VALUE}
_MULTISTATE = {
    enums.OBJECT_MULTI_STATE_INPUT,
    enums.OBJECT_MULTI_STATE_OUTPUT,
    enums.OBJECT_MULTI_STATE_VALUE,
}
_ANALOG = {enums.OBJECT_ANALOG_INPUT, enums.OBJECT_ANALOG_OUTPUT, enums.OBJECT_ANALOG_VALUE}

P = enums.PROPERTIES

# BACnetLIST properties (whole reads only: an index is INVALID); the other
# members of ARRAY_OR_LIST_PROPERTIES are BACnetARRAYs
_LISTS = frozenset(
    P[n]
    for n in (
        "active-cov-subscriptions",
        "alarm-values",
        "date-list",
        "device-address-binding",
        "fault-values",
        "list-of-group-members",
        "list-of-object-property-references",
        "log-buffer",
        "manual-slave-address-binding",
        "recipient-list",
        "slave-address-binding",
        "time-synchronization-recipients",
        "utc-time-synchronization-recipients",
    )
    if n in P
)
_ARRAYS = codec.ARRAY_OR_LIST_PROPERTIES - _LISTS

_WRITABLE = {
    P["present-value"],
    P["out-of-service"],
    P["object-name"],
    P["description"],
    P["location"],
    P["cov-increment"],
    P["relinquish-default"],
    P["units"],
}


@dataclass
class FakeChannel:
    """One IO channel of the fake catalog."""

    id: int
    name: str
    kind: str  # "di" | "do" | "ai" | "ao"
    hw: str = "sim"
    desc: str = ""
    value: float = 0.0  # simulated input value / commanded output value
    initial: float = 0.0
    forced: bool = False
    forced_value: float = 0.0
    point: dict[str, Any] | None = None  # bound io.json point
    obj: tuple[int, int] | None = None  # bound BACnet object

    @property
    def is_output(self) -> bool:
        return self.kind in ("do", "ao")

    @property
    def effective(self) -> float:
        """What the IO scan sees / what the output drives."""
        return self.forced_value if self.forced else self.value


def _normalise(kind: str, v: float) -> float:
    if kind in ("di", "do"):
        return 1.0 if v != 0.0 else 0.0
    if kind == "ao":
        return min(max(v, 0.0), 100.0)
    return v


class _Rc(Exception):
    """Group rc (SMP v2 ``err``) of the current request."""

    def __init__(self, rc: int, **extra: Any) -> None:
        super().__init__(rc)
        self.rc = rc
        self.extra = extra


class _Legacy(Exception):
    """Legacy ``{"rc": n}`` (mcumgr_err_t)."""

    def __init__(self, rc: int) -> None:
        super().__init__(rc)
        self.rc = rc


class _BnError(Exception):
    def __init__(self, error_class: str, error_code: str) -> None:
        super().__init__(f"{error_class}/{error_code}")
        self.error_class = enums.ERROR_CLASSES[error_class]
        self.error_code = enums.ERROR_CODES[error_code]
        self.code_name = error_code


# BACnet error -> uc group rc for prop_read/prop_write over SMP
_BN_TO_RC = {
    "unknown-object": g.UC_RC_NOT_FOUND,
    "unknown-property": g.UC_RC_NOT_FOUND,
    "write-access-denied": g.UC_RC_PERM,
    "invalid-data-type": g.UC_RC_INVALID,
    "value-out-of-range": g.UC_RC_INVALID,
    "invalid-array-index": g.UC_RC_INVALID,
    "property-is-not-an-array": g.UC_RC_INVALID,
}


class _SmpProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: FakeNode) -> None:
        self.node = node
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        rsp = self.node.handle_smp_frame(data)
        if rsp is not None and self.transport is not None:
            self.transport.sendto(rsp, addr)


class _BacnetProtocol(asyncio.DatagramProtocol):
    def __init__(self, node: FakeNode) -> None:
        self.node = node
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        for payload, dest in self.node.handle_bacnet_datagram(data, (addr[0], addr[1])):
            if self.transport is not None:
                self.transport.sendto(payload, dest)


class FakeNode:
    """A simulated BACnet-uc node (SMP over UDP + BACnet/IP) in the current
    event loop.

    Attributes:
        files: file system content, absolute path -> bytes.
        apps: installed applications, name -> record (manifest fields plus
            ``state``, ``ticks``, ``events``, ``errors``, ``last_error``).
        io_channels: channel name -> :class:`FakeChannel`.
        objects: ``(type, instance)`` -> ``{property number: value}``.
        owners: ``(type, instance)`` -> ``"system"``, ``"io"`` or ``"app:<name>"``.
        smp_log: ``(op, group, cmd)`` of every SMP request handled.
    """

    def __init__(
        self,
        device_instance: int = 1001,
        device_name: str = "fake-node",
        *,
        board: str = "native_sim/native/64",
        catalog_board: str = "native_sim",
        fw: str = "0.1.0",
        vendor_id: int = 260,
        files: dict[str, bytes] | None = None,
        catalog: tuple[tuple[str, str, float, str], ...] = DEFAULT_CATALOG,
        iam_broadcast: tuple[str, int] | None = None,
        wasm_aot: bool = False,
        require_password: bool = True,
    ) -> None:
        self.device_instance = device_instance
        self.device_name = device_name
        self.description = ""
        self.location = ""
        self.board = board
        self.catalog_board = catalog_board
        self.fw = fw
        self.vendor_id = vendor_id
        self.wasm_aot = wasm_aot
        #: CONFIG_UC_BACNET_REQUIRE_PASSWORD: refuse DCC/ReinitializeDevice
        #: while device.json has no bacnet.password
        self.require_password = require_password
        #: bacnet.password of the active device.json ("" = none)
        self.bacnet_password = ""
        #: last accepted DeviceCommunicationControl: "enable", "disable", ...
        self.dcc_state = "enable"
        #: accepted ReinitializeDevice requests (state names)
        self.reinitialized: list[str] = []
        #: log.level of the active device.json
        self.log_level = "inf"
        #: I-Am broadcast destination, e.g. ("255.255.255.255", 47808); None =
        #: unicast I-Am to the Who-Is sender
        self.iam_broadcast = iam_broadcast
        self.files: dict[str, bytes] = dict(files or {})
        self.apps: dict[str, dict[str, Any]] = {}
        self.io_channels: dict[str, FakeChannel] = {
            name: FakeChannel(i, name, kind, "sim", desc, _normalise(kind, init), init)
            for i, (name, kind, init, desc) in enumerate(catalog)
        }
        self.objects: dict[tuple[int, int], dict[int, Any]] = {}
        self.owners: dict[tuple[int, int], str] = {}
        self.smp_log: list[tuple[int, int, int]] = []
        self.bacnet_packets = 0
        self.resets = 0
        self.images: list[dict[str, Any]] = [
            {
                "image": 0,
                "slot": 0,
                "version": fw,
                "hash": hashlib.sha256(b"fw").digest(),
                "bootable": True,
                "pending": False,
                "confirmed": True,
                "active": True,
                "permanent": False,
            }
        ]
        self.host = "127.0.0.1"
        self.smp_port = 0
        self.bacnet_port = 0
        self._drop_smp = 0
        self._drop_smp_rsp = 0
        self._drop_smp_rsp_match: Any = None
        # FS upload context of Zephyr's fs_mgmt (path, offset, length)
        self._fs_up: dict[str, Any] | None = None
        self._img_buf = bytearray()
        self._img_len = 0
        self._img_sha = b""
        self._boot_time = time.monotonic()
        self._device_cfg: dict[str, Any] = {}
        self._boot_device_cfg: dict[str, Any] = {}
        self._smp: tuple[asyncio.DatagramTransport, _SmpProtocol] | None = None
        self._bn: tuple[asyncio.DatagramTransport, _BacnetProtocol] | None = None
        self._create_device_object()
        self._smp_handlers = self._build_smp_handlers()

    # --- lifecycle ----------------------------------------------------------------------

    async def start(self, smp_port: int = 0, bacnet_port: int = 0, host: str = "127.0.0.1") -> None:
        """Bind both sockets (port 0 = ephemeral) and boot from :attr:`files`."""
        loop = asyncio.get_running_loop()
        self.host = host
        smp_tr, smp_proto = await loop.create_datagram_endpoint(
            lambda: _SmpProtocol(self), local_addr=(host, smp_port)
        )
        self._smp = (smp_tr, smp_proto)
        self.smp_port = smp_tr.get_extra_info("sockname")[1]
        bn_tr, bn_proto = await loop.create_datagram_endpoint(
            lambda: _BacnetProtocol(self),
            local_addr=(host, bacnet_port),
            allow_broadcast=True,
        )
        self._bn = (bn_tr, bn_proto)
        self.bacnet_port = bn_tr.get_extra_info("sockname")[1]
        self._boot()

    async def stop(self) -> None:
        for pair in (self._smp, self._bn):
            if pair is not None:
                pair[0].close()
        self._smp = None
        self._bn = None

    async def __aenter__(self) -> FakeNode:
        if self._smp is None:
            await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    @property
    def smp_address(self) -> tuple[str, int]:
        return self.host, self.smp_port

    @property
    def bacnet_address(self) -> tuple[str, int]:
        return self.host, self.bacnet_port

    def drop_next_smp(self, count: int = 1) -> None:
        """Silently drop the next ``count`` SMP requests (tests retries)."""
        self._drop_smp += count

    def drop_next_smp_response(self, count: int = 1, match: Any = None) -> None:
        """Handle the next ``count`` SMP requests (only those whose request
        map satisfies ``match(req)`` when given) but drop their responses: a
        lost or late answer, so the client retransmits a request the node has
        already executed."""
        self._drop_smp_rsp += count
        self._drop_smp_rsp_match = match

    def _boot(self) -> None:
        self._boot_time = time.monotonic()
        self._fs_up = None
        for ch in self.io_channels.values():
            ch.forced = False
            ch.value = _normalise(ch.kind, ch.initial)
        for app in self.apps.values():
            app["state"] = "stopped"
            app["started_at"] = None
        for doc in ("device", "io", "apps"):
            try:
                self._reload(doc, boot=True)
            except _Rc as exc:
                log.warning("fake node boot: %s rejected (rc %d)", doc, exc.rc)

    # --- object table -------------------------------------------------------------------

    def _create_device_object(self) -> None:
        key = (enums.OBJECT_DEVICE, self.device_instance)
        # BACnetServicesSupported bit positions (clause 21): readProperty 12,
        # writeProperty 15, i-Am 26, who-Is 34
        services = [False] * 44
        for bit in (12, 15, 26, 34):
            services[bit] = True
        types = [False] * 65
        for t in (0, 1, 2, 3, 4, 5, 8, 13, 14, 19):
            types[t] = True
        self.objects[key] = {
            P["object-identifier"]: ObjectId(*key),
            P["object-name"]: self.device_name,
            P["object-type"]: Enumerated(enums.OBJECT_DEVICE),
            P["system-status"]: Enumerated(0),
            P["vendor-name"]: "BACnet Stack at SourceForge",
            P["vendor-identifier"]: self.vendor_id,
            P["model-name"]: self.catalog_board,
            P["firmware-revision"]: self.fw,
            P["application-software-version"]: self.fw,
            P["protocol-version"]: 1,
            P["protocol-revision"]: 28,
            P["protocol-services-supported"]: BitString(services),
            P["protocol-object-types-supported"]: BitString(types),
            P["max-apdu-length-accepted"]: codec.MAX_APDU_BIP,
            P["segmentation-supported"]: Enumerated(enums.SEGMENTATION_NONE),
            P["apdu-timeout"]: 3000,
            P["number-of-APDU-retries"]: 3,
            P["device-address-binding"]: [],
            P["database-revision"]: 1,
            P["description"]: self.description,
            P["location"]: self.location,
        }
        self.owners[key] = "system"
        # the BACnet/IP network port object of bacnet-stack's basic server
        port_key = (enums.OBJECT_NETWORK_PORT, 1)
        self.objects[port_key] = {
            P["object-identifier"]: ObjectId(*port_key),
            P["object-name"]: "BACnet/IP Port",
            P["object-type"]: Enumerated(enums.OBJECT_NETWORK_PORT),
            P["description"]: "",
            P["status-flags"]: BitString([False] * 4),
            P["reliability"]: Enumerated(0),
            P["out-of-service"]: False,
            P["network-type"]: Enumerated(5),  # ipv4
        }
        self.owners[port_key] = "system"

    @property
    def device_key(self) -> tuple[int, int]:
        return enums.OBJECT_DEVICE, self.device_instance

    def add_object(
        self,
        obj_type: str | int,
        instance: int,
        name: str | None = None,
        *,
        pv: Any = None,
        owner: str = "system",
        units: str | int | None = None,
        description: str = "",
        number_of_states: int = 2,
    ) -> dict[int, Any]:
        """Create a standard object (analog, binary or multi-state) and return
        its property map. Commandable types (analog/binary/multi-state
        output) get a priority array and Relinquish_Default; ``pv`` is their
        Relinquish_Default, else the Present_Value."""
        t = enums.object_type_number(obj_type)
        key = (t, int(instance))
        if key in self.objects:
            raise HarnessError(f"object {enums.format_object_ref(*key)} exists")
        type_name = enums.object_type_name(t)
        props: dict[int, Any] = {
            P["object-identifier"]: ObjectId(*key),
            P["object-name"]: name or f"{type_name}-{instance}",
            P["object-type"]: Enumerated(t),
            P["description"]: description,
            P["status-flags"]: BitString([False] * 4),
            P["event-state"]: Enumerated(0),
            P["out-of-service"]: False,
            P["reliability"]: Enumerated(0),
        }
        if t in _ANALOG:
            props[P["units"]] = Enumerated(enums.unit_number(units if units is not None else 95))
            props[P["cov-increment"]] = 0.1
            default: Any = 0.0
        elif t in _BINARY:
            props[P["polarity"]] = Enumerated(0)
            default = Enumerated(0)
        elif t in _MULTISTATE:
            props[P["number-of-states"]] = number_of_states
            props[P["state-text"]] = [f"state-{i + 1}" for i in range(number_of_states)]
            default = 1
        else:
            default = None
        if t in _COMMANDABLE:
            props[P["priority-array"]] = [None] * 16
            props[P["relinquish-default"]] = default
        props[P["present-value"]] = default
        self.objects[key] = props
        self.owners[key] = owner
        if pv is not None:
            if t in _COMMANDABLE:
                props[P["relinquish-default"]] = self._typed_pv(t, pv)
                self._update_commandable(t, props)
            else:
                props[P["present-value"]] = self._typed_pv(t, pv)
        return props

    def remove_object(self, obj_type: str | int, instance: int) -> None:
        key = (enums.object_type_number(obj_type), int(instance))
        if key == self.device_key:
            raise HarnessError("cannot remove the device object")
        self.objects.pop(key, None)
        self.owners.pop(key, None)

    def get_pv(self, obj_type: str | int, instance: int) -> Any:
        """Present_Value of a local object (after an IO scan)."""
        self._sync_io()
        key = (enums.object_type_number(obj_type), int(instance))
        return self.objects[key][P["present-value"]]

    @staticmethod
    def _typed_pv(t: int, value: Any) -> Any:
        if value is None:
            return None
        if t in _BINARY:
            return Enumerated(1 if value else 0)
        if t in _MULTISTATE:
            return int(value)
        if t in _ANALOG:
            return float(value)
        return value

    @staticmethod
    def _update_commandable(t: int, props: dict[int, Any]) -> None:
        pa = props.get(P["priority-array"])
        if pa is None:
            return
        pv = next((v for v in pa if v is not None), props.get(P["relinquish-default"]))
        props[P["present-value"]] = pv

    def _object_list(self) -> list[ObjectId]:
        keys = sorted(self.objects)  # by type, then instance, like the firmware
        return [ObjectId(*k) for k in keys]

    # --- IO scan --------------------------------------------------------------------------

    def _sync_io(self) -> None:
        """One IO scan: inputs -> Present_Value, Present_Value -> outputs."""
        for ch in self.io_channels.values():
            if ch.obj is None or ch.point is None:
                continue
            props = self.objects.get(ch.obj)
            if props is None:
                continue
            t = ch.obj[0]
            pt = ch.point
            if not ch.is_output:
                if props.get(P["out-of-service"]):
                    continue
                raw = ch.effective
                if ch.kind == "ai":
                    props[P["present-value"]] = raw * float(pt.get("scale", 1.0)) + float(
                        pt.get("offset", 0.0)
                    )
                else:
                    bit = (raw != 0.0) != bool(pt.get("invert", False))
                    if t == enums.OBJECT_MULTI_STATE_INPUT:
                        props[P["present-value"]] = 2 if bit else 1
                    else:
                        props[P["present-value"]] = Enumerated(1 if bit else 0)
            else:
                pv = props.get(P["present-value"])
                if pv is None:
                    continue
                if ch.kind == "do":
                    bit = (int(pv) != 0) != bool(pt.get("invert", False))
                    ch.value = 1.0 if bit else 0.0
                else:
                    eng = float(pv)
                    if "min" in pt:
                        eng = max(eng, float(pt["min"]))
                    if "max" in pt:
                        eng = min(eng, float(pt["max"]))
                    scale = float(pt.get("scale", 1.0)) or 1.0
                    ch.value = _normalise("ao", (eng - float(pt.get("offset", 0.0))) / scale)

    # --- configuration documents ------------------------------------------------------------

    def _parse_doc(self, doc: str, data: bytes) -> dict[str, Any]:
        """Parse stage of ``uc_config.c``: the checked document, or
        ``_Rc(INVALID)`` (syntax, schema version, field types/ranges, table
        limits, duplicate objects, size > ``CONFIG_UC_CONFIG_DOC_MAX``)."""
        if len(data) > CONFIG_DOC_MAX:
            raise _Rc(g.UC_RC_INVALID)
        try:
            cfg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log.warning("%s.json: %s", doc, exc)
            raise _Rc(g.UC_RC_INVALID) from None
        if not isinstance(cfg, dict) or not _is_int(cfg.get("schema"), 1, 1):
            raise _Rc(g.UC_RC_INVALID)
        if doc == "device":
            _check_device_doc(cfg)
        elif doc == "io":
            _check_io_doc(cfg)
        else:
            entries = cfg.get("apps")
            if not isinstance(entries, list) or len(entries) > APPS_MAX:
                raise _Rc(g.UC_RC_INVALID)
            manifests = [self._check_manifest(_manifest_from_entry(e)) for e in entries]
            if len({m["name"] for m in manifests}) != len(manifests):
                raise _Rc(g.UC_RC_INVALID)
            cfg = {"schema": 1, "apps": manifests}
        return cfg

    def _reload(self, doc: str, boot: bool = False) -> bool:
        """Load and apply one document like ``uc_config_reload()`` + the apply
        step of ``uc_node reload`` (at ``boot``: ``uc_config_init()``).

        A staged ``<path>.new`` that passes the parse stage replaces the
        active document and is applied; one that does not is deleted, the
        active document and the running configuration stay and the reload
        fails with ``INVALID`` (at boot the active document is loaded
        instead). Without a staged document the active one is re-read; a
        missing one yields the defaults, an invalid one fails the reload (at
        boot: defaults). Errors of the apply stage are raised after the
        document was activated.
        """
        path = {"device": CFG_DEVICE, "io": CFG_IO, "apps": CFG_APPS}.get(doc)
        if path is None:
            raise _Rc(g.UC_RC_INVALID)
        cfg: dict[str, Any] | None = None
        staged = self.files.get(path + STAGED_SUFFIX)
        if staged is not None:
            try:
                cfg = self._parse_doc(doc, staged)
            except _Rc:
                del self.files[path + STAGED_SUFFIX]
                log.warning("%s%s rejected, deleting it", path, STAGED_SUFFIX)
                if not boot:
                    raise
            else:
                self.files[path] = self.files.pop(path + STAGED_SUFFIX)
        if cfg is None:
            data = self.files.get(path)
            if data is None:
                cfg = {"schema": 1}
            else:
                try:
                    cfg = self._parse_doc(doc, data)
                except _Rc:
                    if not boot:
                        raise
                    log.warning("%s rejected, using defaults", path)
                    cfg = {"schema": 1}
        if doc == "device":
            return self._apply_device(cfg, boot)
        if doc == "io":
            self._apply_io(cfg)
            return False
        self._apply_apps(cfg)
        return False

    def _apply_device(self, cfg: dict[str, Any], boot: bool) -> bool:
        """Apply device.json: identity texts, password and log level at once;
        ``True`` (reboot required) when the instance, the UDP port or the
        network settings differ from what the node booted with."""
        dev = cfg.get("device", {})
        if boot:
            self._boot_device_cfg = cfg
            new_instance = int(dev.get("instance", self.device_instance))
            if new_instance != self.device_instance:
                props = self.objects.pop(self.device_key)
                self.owners.pop(self.device_key, None)
                self.device_instance = new_instance
                props[P["object-identifier"]] = ObjectId(*self.device_key)
                self.objects[self.device_key] = props
                self.owners[self.device_key] = "system"
        boot_cfg = self._boot_device_cfg
        reboot = (
            int(dev.get("instance", self.device_instance)) != self.device_instance
            or _udp_port(cfg) != _udp_port(boot_cfg)
            or _net_key(cfg) != _net_key(boot_cfg)
        )
        props = self.objects[self.device_key]
        if "name" in dev:
            self.device_name = str(dev["name"])
        self.description = str(dev.get("description", ""))
        self.location = str(dev.get("location", ""))
        props[P["object-name"]] = self.device_name
        props[P["description"]] = self.description
        props[P["location"]] = self.location
        # applied immediately (uc_bn_node.c: at start and on every reload)
        self.bacnet_password = str(cfg.get("bacnet", {}).get("password", ""))
        self.log_level = str(cfg.get("log", {}).get("level", "inf"))
        self._device_cfg = cfg
        return reboot

    def _apply_io(self, cfg: dict[str, Any]) -> None:
        """``uc_io_apply_config()``: re-create all IO objects; points that
        cannot be bound are skipped (logged), the others are bound."""
        points = cfg.get("points", [])
        for key in [k for k, o in self.owners.items() if o == "io"]:
            self.objects.pop(key, None)
            self.owners.pop(key, None)
        for ch in self.io_channels.values():
            ch.point = None
            ch.obj = None
        for idx, pt in enumerate(points):
            ch = self.io_channels.get(str(pt["channel"]))
            key = (enums.object_type_number(pt["type"]), int(pt["instance"]))
            why = None
            if ch is None:
                why = f"unknown channel {pt['channel']!r}"
            elif pt["type"] not in _KIND_TYPES[ch.kind]:
                why = f"{ch.kind} channel {ch.name} cannot be bound to {pt['type']}"
            elif ch.obj is not None:
                why = f"channel {ch.name} is already bound"
            elif key in self.objects:
                why = f"{enums.format_object_ref(*key)} exists ({self.owners.get(key)})"
            elif ch.kind == "ao" and float(pt.get("scale", 1.0)) == 0.0:
                why = f"scale 0 is invalid for output {ch.name}"
            if why is not None or ch is None:
                log.warning("io.json point %d skipped: %s", idx, why)
                continue
            props = self.add_object(
                key[0],
                key[1],
                pt.get("name", ch.name),
                owner="io",
                units=pt.get("units") if key[0] in _ANALOG else None,
                description=str(pt.get("description", ch.desc)),
            )
            if key[0] in _ANALOG:
                props[P["cov-increment"]] = float(pt.get("cov_increment", 0.1))
            if key[0] in _BINARY and pt.get("invert"):
                props[P["polarity"]] = Enumerated(1)
            if key[0] == enums.OBJECT_MULTI_STATE_INPUT:
                props[P["state-text"]] = ["Inactive", "Active"]
            if ch.is_output and key[0] in _COMMANDABLE:
                # outputs start from the Relinquish_Default
                if ch.kind == "do":
                    props[P["relinquish-default"]] = Enumerated(0)
                else:
                    props[P["relinquish-default"]] = 0.0
                self._update_commandable(key[0], props)
            ch.point = dict(pt)
            ch.obj = key
        self._sync_io()

    def _apply_apps(self, cfg: dict[str, Any]) -> None:
        manifests: list[dict[str, Any]] = cfg.get("apps", [])
        first_err: _Rc | None = None
        wanted = {m["name"] for m in manifests}
        for name in list(self.apps):
            if name not in wanted:
                self._drop_app_objects(name)
                del self.apps[name]
        for m in manifests:
            old = self.apps.get(m["name"])
            if old is not None and old["state"] == "running" and _cfg_equal(old, m):
                continue
            rec = _new_app_record(m)
            self.apps[m["name"]] = rec
            if m["autostart"]:
                try:
                    self._app_start(rec)
                except _Rc as exc:
                    first_err = first_err or exc
        if first_err is not None:
            raise first_err

    def _write_apps_json(self) -> None:
        doc = {"schema": 1, "apps": [_app_json(a) for a in self.apps.values()]}
        self.files[CFG_APPS] = json.dumps(doc, indent=1).encode()

    # --- apps -----------------------------------------------------------------------------------

    def _check_manifest(self, m: dict[str, Any]) -> dict[str, Any]:
        name = m.get("name")
        file = m.get("file")
        if not isinstance(name, str) or not _APP_NAME_RE.match(name):
            raise _Rc(g.UC_RC_INVALID)
        if not isinstance(file, str) or not _APP_FILE_RE.match(file):
            raise _Rc(g.UC_RC_INVALID)
        out = {
            "name": name,
            "file": file,
            "autostart": m.get("autostart", True),
            "period_ms": m.get("period_ms", 1000),
            "heap_kb": m.get("heap_kb", 8),
            "stack_kb": m.get("stack_kb", 4),
            "perms": m.get("perms", []),
            "params": m.get("params", {}),
            "sha256": m.get("sha256"),
        }
        ints = ("period_ms", "heap_kb", "stack_kb")
        if not isinstance(out["autostart"], bool) or any(
            not isinstance(out[k], int) or isinstance(out[k], bool) or out[k] < 0 for k in ints
        ):
            raise _Rc(g.UC_RC_INVALID)
        if out["period_ms"] > 3600000 or out["heap_kb"] > 256 or not 1 <= out["stack_kb"] <= 64:
            raise _Rc(g.UC_RC_INVALID)
        perms = out["perms"]
        if not isinstance(perms, list) or any(p not in PERMS for p in perms):
            raise _Rc(g.UC_RC_INVALID)
        out["perms"] = [p for p in PERMS if p in perms]
        params = out["params"]
        if (
            not isinstance(params, dict)
            or len(params) > 16
            or any(
                not isinstance(k, str)
                or not _PARAM_KEY_RE.match(k)
                or not isinstance(v, str)
                or len(v) > 95
                for k, v in params.items()
            )
        ):
            raise _Rc(g.UC_RC_INVALID)
        sha = out["sha256"]
        if sha is not None and (not isinstance(sha, bytes) or len(sha) != 32):
            raise _Rc(g.UC_RC_INVALID)
        return out

    def _verify_module(self, m: dict[str, Any]) -> None:
        data = self.files.get(m["file"])
        if data is None:
            raise _Rc(g.UC_RC_NOT_FOUND)
        if len(data) > APP_MAX_FILE_SIZE:
            raise _Rc(g.UC_RC_INVALID)
        if len(data) < 8:
            raise _Rc(g.UC_RC_VERIFY)
        if data[:4] == _WASM_HDR[:4]:
            if data[:8] != _WASM_HDR:
                raise _Rc(g.UC_RC_VERIFY)
        elif data[:4] == _AOT_MAGIC:
            if not self.wasm_aot:
                raise _Rc(g.UC_RC_UNSUPPORTED)
        else:
            raise _Rc(g.UC_RC_VERIFY)
        if m.get("sha256") is not None and hashlib.sha256(data).digest() != m["sha256"]:
            raise _Rc(g.UC_RC_VERIFY)

    def _app_start(self, rec: dict[str, Any]) -> None:
        if rec["state"] in ("running", "starting"):
            raise _Rc(g.UC_RC_STATE)
        try:
            self._verify_module(rec)
        except _Rc as exc:
            rec["state"] = "failed"
            rec["last_error"] = f"{rec['file']}: load failed (rc {exc.rc})"
            rec["errors"] += 1
            raise
        rec.update(state="running", started_at=time.monotonic(), ticks_base=0, events=0)

    def _drop_app_objects(self, name: str) -> None:
        """Delete the objects an app created (the host does this on removal)."""
        for key in [k for k, o in self.owners.items() if o == f"app:{name}"]:
            self.objects.pop(key, None)
            self.owners.pop(key, None)

    @staticmethod
    def _app_stop(rec: dict[str, Any]) -> None:
        if rec["state"] == "running":
            rec["ticks_base"] = _ticks(rec)
        rec["state"] = "stopped"
        rec["started_at"] = None

    def app_status(self, name: str) -> dict[str, Any]:
        """``<app status>`` map of an installed app."""
        rec = self.apps[name]
        running = rec["state"] == "running"
        return {
            "name": rec["name"],
            "file": rec["file"],
            "state": rec["state"],
            "autostart": rec["autostart"],
            "period_ms": rec["period_ms"],
            "heap_kb": rec["heap_kb"],
            "stack_kb": rec["stack_kb"],
            "perms": list(rec["perms"]),
            "ticks": _ticks(rec),
            "events": rec["events"],
            "errors": rec["errors"],
            "last_error": rec["last_error"],
            "uptime_ms": int((time.monotonic() - rec["started_at"]) * 1000) if running else 0,
        }

    # --- SMP --------------------------------------------------------------------------------------

    def _build_smp_handlers(self) -> dict[tuple[int, int], tuple[Any, Any]]:
        # (group, cmd) -> (read handler, write handler)
        return {
            (g.GROUP_OS, g.OS_ECHO): (self._os_echo, self._os_echo),
            (g.GROUP_OS, g.OS_RESET): (None, self._os_reset),
            (g.GROUP_OS, g.OS_MCUMGR_PARAMS): (self._os_params, None),
            (g.GROUP_OS, g.OS_INFO): (self._os_info, None),
            (g.GROUP_IMG, g.IMG_STATE): (self._img_state_read, self._img_state_write),
            (g.GROUP_IMG, g.IMG_UPLOAD): (None, self._img_upload),
            (g.GROUP_IMG, g.IMG_ERASE): (None, self._img_erase),
            (g.GROUP_FS, g.FS_FILE): (self._fs_download, self._fs_upload),
            (g.GROUP_FS, g.FS_STATUS): (self._fs_status, None),
            (g.GROUP_FS, g.FS_HASH_CHECKSUM): (self._fs_hash, None),
            (g.GROUP_FS, g.FS_SUPPORTED_HASH_CHECKSUM): (self._fs_supported_hash, None),
            (g.GROUP_FS, g.FS_OPENED_FILE): (None, self._fs_close),
            (g.GROUP_SHELL, g.SHELL_EXEC): (None, self._shell_exec),
            (g.GROUP_UC_APP, g.UC_APP_LIST): (self._app_list, None),
            (g.GROUP_UC_APP, g.UC_APP_INSTALL): (None, self._app_install),
            (g.GROUP_UC_APP, g.UC_APP_START): (None, self._app_start_req),
            (g.GROUP_UC_APP, g.UC_APP_STOP): (None, self._app_stop_req),
            (g.GROUP_UC_APP, g.UC_APP_REMOVE): (None, self._app_remove),
            (g.GROUP_UC_APP, g.UC_APP_STATUS): (self._app_status_req, None),
            (g.GROUP_UC_IO, g.UC_IO_CATALOG): (self._io_catalog, None),
            (g.GROUP_UC_IO, g.UC_IO_READ): (self._io_read, None),
            (g.GROUP_UC_IO, g.UC_IO_WRITE): (None, self._io_write),
            (g.GROUP_UC_IO, g.UC_IO_FORCE): (None, self._io_force),
            (g.GROUP_UC_NODE, g.UC_NODE_INFO): (self._node_info, None),
            (g.GROUP_UC_NODE, g.UC_NODE_RELOAD): (None, self._node_reload),
            (g.GROUP_UC_NODE, g.UC_NODE_OBJECTS): (self._node_objects, None),
            (g.GROUP_UC_NODE, g.UC_NODE_PROP_READ): (self._node_prop_read, None),
            (g.GROUP_UC_NODE, g.UC_NODE_PROP_WRITE): (None, self._node_prop_write),
        }

    def handle_smp_frame(self, frame: bytes) -> bytes | None:
        """Process one SMP request frame; returns the response frame (or
        ``None`` for frames that are ignored). Usable without sockets, e.g.
        behind a serial framing test harness."""
        try:
            hdr, req = decode_frame(frame)
        except HarnessError:
            return None
        if hdr.op not in (g.OP_READ, g.OP_WRITE):
            return None
        if self._drop_smp > 0:
            self._drop_smp -= 1
            return None
        self.smp_log.append((hdr.op, hdr.group, hdr.cmd))
        self._sync_io()
        handlers = self._smp_handlers.get((hdr.group, hdr.cmd))
        fn = None if handlers is None else handlers[0 if hdr.op == g.OP_READ else 1]
        try:
            if fn is None:
                raise _Legacy(g.MGMT_ERR_ENOTSUP)
            rsp = fn(req)
        except _Rc as exc:
            rsp = {**exc.extra, "err": {"group": hdr.group, "rc": exc.rc}}
        except _Legacy as exc:
            rsp = {"rc": exc.rc}
        except Exception:
            log.exception("fake node: SMP handler failed")
            rsp = {"rc": g.MGMT_ERR_EUNKNOWN}
        self._sync_io()
        if self._drop_smp_rsp > 0 and (self._drop_smp_rsp_match is None
                                       or self._drop_smp_rsp_match(req)):
            self._drop_smp_rsp -= 1
            return None
        return encode_frame(hdr.op + 1, hdr.group, hdr.cmd, hdr.seq, rsp, version=hdr.version)

    # request field helpers (uc groups: rc INVALID)
    @staticmethod
    def _str(req: dict[str, Any], key: str, required: bool = True) -> str | None:
        v = req.get(key)
        if v is None:
            if required:
                raise _Rc(g.UC_RC_INVALID)
            return None
        if not isinstance(v, str):
            raise _Rc(g.UC_RC_INVALID)
        return v

    @staticmethod
    def _num(req: dict[str, Any], key: str) -> float:
        v = req.get(key)
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise _Rc(g.UC_RC_INVALID)
        return float(v)

    @staticmethod
    def _uint(req: dict[str, Any], key: str, default: int | None = None) -> int:
        v = req.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise _Rc(g.UC_RC_INVALID)
        return v

    # OS
    def _os_echo(self, req: dict[str, Any]) -> dict[str, Any]:
        d = req.get("d")
        if not isinstance(d, str):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        return {"r": d}

    def _os_reset(self, req: dict[str, Any]) -> dict[str, Any]:
        self._reset()
        return {}

    def _os_params(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"buf_size": SMP_BUF_SIZE, "buf_count": SMP_BUF_COUNT}

    def _os_info(self, req: dict[str, Any]) -> dict[str, Any]:
        fmt = req.get("format", "s")
        if not isinstance(fmt, str):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        # same fields as the firmware on native_sim ("b", build time, is empty)
        parts = {
            "s": "Zephyr",
            "n": "unknown",
            "r": "fake",
            "v": "4.4.2",
            "b": "",
            "m": "posix",
            "p": "x86_64",
            "i": self.board,
            "o": "Zephyr",
        }
        if "a" in fmt:
            fmt = "snrvmpio"
        out = []
        for c in fmt:
            if c not in parts:
                raise _Rc(2)  # OS_MGMT_ERR_INVALID_FORMAT
            out.append(parts[c])
        return {"output": " ".join(out)}

    # IMG
    def _img_state_read(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"images": [dict(i) for i in self.images], "splitStatus": 0}

    def _img_state_write(self, req: dict[str, Any]) -> dict[str, Any]:
        confirm = bool(req.get("confirm", False))
        hsh = req.get("hash")
        if hsh is None:
            if not confirm:
                raise _Legacy(g.MGMT_ERR_EINVAL)
            self.images[0]["confirmed"] = True
        else:
            match = [i for i in self.images if i["hash"] == hsh]
            if not match:
                raise _Rc(8)  # IMG_MGMT_ERR_HASH_NOT_FOUND
            img = match[0]
            if img["active"]:
                img["confirmed"] = img["confirmed"] or confirm
            else:
                img["pending"] = True
                img["permanent"] = confirm
        return self._img_state_read({})

    def _img_upload(self, req: dict[str, Any]) -> dict[str, Any]:
        off = req.get("off")
        data = req.get("data")
        if not isinstance(off, int) or not isinstance(data, bytes):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        if off == 0:
            total = req.get("len")
            if not isinstance(total, int):
                raise _Legacy(g.MGMT_ERR_EINVAL)
            self._img_len = total
            self._img_sha = bytes(req.get("sha", b""))
            self._img_buf = bytearray()
            self.images = [i for i in self.images if i["slot"] == 0]
        elif off != len(self._img_buf):
            return {"off": len(self._img_buf)}
        self._img_buf += data
        rsp: dict[str, Any] = {"off": len(self._img_buf)}
        if len(self._img_buf) >= self._img_len:
            digest = hashlib.sha256(self._img_buf).digest()
            self.images.append(
                {
                    "image": 0,
                    "slot": 1,
                    "version": "0.0.0",
                    "hash": digest,
                    "bootable": True,
                    "pending": False,
                    "confirmed": False,
                    "active": False,
                    "permanent": False,
                }
            )
            if self._img_sha:
                rsp["match"] = digest.startswith(self._img_sha)
        return rsp

    def _img_erase(self, req: dict[str, Any]) -> dict[str, Any]:
        self.images = [i for i in self.images if i["slot"] == 0]
        return {}

    # FS
    @staticmethod
    def _fs_name(req: dict[str, Any]) -> str:
        name = req.get("name")
        if not isinstance(name, str) or not name:
            raise _Legacy(g.MGMT_ERR_EINVAL)
        return name

    def _fs_upload(self, req: dict[str, Any]) -> dict[str, Any]:
        """``fs_mgmt_file_upload()`` of Zephyr 4.4: one upload context (path,
        offset, length) stays open until the announced length is written or
        ``fs_close``. A chunk at offset 0 truncates the file but does not
        reset the context's offset (a retransmitted first chunk is written
        again and the reply advances by its length again); a chunk at another
        offset must match the context's offset, else the context is closed
        and rc ``FILE_OFFSET_NOT_VALID`` carries the file length."""
        name = self._fs_name(req)
        off = req.get("off")
        data = req.get("data")
        if not isinstance(off, int) or not isinstance(data, bytes):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        if not name.startswith("/lfs/"):
            raise _Rc(g.FS_RC_MOUNT_POINT_NOT_FOUND)
        if off == 0 and not isinstance(req.get("len"), int):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        up = self._fs_up
        if up is None or up["path"] != name:
            up = self._fs_up = {"path": name, "off": 0, "len": 0}
            self.files.setdefault(name, b"")  # FS_O_CREATE
        if off == 0:
            up["len"] = req["len"]
        elif up["off"] == 0:
            up["off"] = len(self.files[name])
        if off > 0 and off != up["off"]:
            current = up["off"]
            self._fs_up = None
            raise _Rc(g.FS_RC_FILE_OFFSET_NOT_VALID, len=current)
        if data:
            self.files[name] = (b"" if off == 0 else self.files[name]) + bytes(data)
            up["off"] += len(data)
        reply = up["off"]
        if up["len"] > 0 and up["off"] >= up["len"]:
            self._fs_up = None  # upload finished: file closed
        return {"off": reply}

    def _fs_close(self, req: dict[str, Any]) -> dict[str, Any]:
        self._fs_up = None
        return {}

    def _fs_download(self, req: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(req)
        off = req.get("off", 0)
        if not isinstance(off, int):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        data = self.files.get(name)
        if data is None:
            raise _Rc(g.FS_RC_FILE_NOT_FOUND)
        if off > len(data):
            raise _Rc(g.FS_RC_FILE_OFFSET_LARGER_THAN_FILE)
        rsp: dict[str, Any] = {"off": off, "data": data[off : off + FS_DL_CHUNK]}
        if off == 0:
            rsp["len"] = len(data)
        return rsp

    def _fs_status(self, req: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(req)
        if name not in self.files:
            raise _Rc(g.FS_RC_FILE_NOT_FOUND)
        return {"len": len(self.files[name])}

    def _fs_hash(self, req: dict[str, Any]) -> dict[str, Any]:
        name = self._fs_name(req)
        htype = req.get("type", "crc32")
        data = self.files.get(name)
        if data is None:
            raise _Rc(g.FS_RC_FILE_NOT_FOUND)
        if not data:
            raise _Rc(g.FS_RC_FILE_EMPTY)
        if htype == "sha256":
            out: Any = hashlib.sha256(data).digest()
        elif htype == "crc32":
            out = zlib.crc32(data)
        else:
            raise _Rc(g.FS_RC_CHECKSUM_HASH_NOT_FOUND)
        return {"type": htype, "len": len(data), "output": out}

    def _fs_supported_hash(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"types": {"crc32": {"format": 1, "size": 4}, "sha256": {"format": 0, "size": 32}}}

    # SHELL
    def _shell_exec(self, req: dict[str, Any]) -> dict[str, Any]:
        argv = req.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise _Legacy(g.MGMT_ERR_EINVAL)
        if not argv:
            raise _Rc(3)  # SHELL_MGMT_ERR_EMPTY_COMMAND
        # output format of the Zephyr shell dummy backend: "\r\n" line ends,
        # errors in color
        cmd = argv[0]
        if cmd == "echo":
            return {"o": "\r\n" + " ".join(argv[1:]) + "\r\n", "ret": 0}
        if argv[:2] == ["kernel", "version"]:
            return {"o": "\r\nZephyr version 4.4.2\r\n", "ret": 0}
        if argv[:2] == ["fs", "ls"]:
            base = argv[2].rstrip("/") + "/" if len(argv) > 2 else "/lfs/"
            names = sorted({p[len(base) :].split("/")[0] for p in self.files if p.startswith(base)})
            return {"o": "\r\n" + "".join(n + "\r\n" for n in names), "ret": 0}
        if argv[:2] == ["fs", "rm"] and len(argv) == 3:
            if self.files.pop(argv[2], None) is None:
                return {"o": f"\r\nFailed to remove {argv[2]} (-2)\r\n", "ret": -2}
            return {"o": "\r\n", "ret": 0}
        if argv[:2] == ["uc", "info"]:
            return {
                "o": f"\r\ndevice {self.device_instance} '{self.device_name}' "
                f"board {self.board} fw {self.fw}\r\n",
                "ret": 0,
            }
        return {"o": f"\r\n\x1b[1;31m{cmd}: command not found\r\n\x1b[m", "ret": -8}

    # uc_app
    def _app_list(self, req: dict[str, Any]) -> dict[str, Any]:
        offset = self._uint(req, "offset", 0)
        count = self._uint(req, "count", 0xFFFFFFFF)
        names = list(self.apps)
        return _fill({"total": len(names)}, "apps",
                     (self.app_status(n) for n in names[offset:]), count)

    def _app_install(self, req: dict[str, Any]) -> dict[str, Any]:
        restart = req.get("restart", False)
        if not isinstance(restart, bool):
            raise _Rc(g.UC_RC_INVALID)
        m = self._check_manifest(req)
        self._verify_module(m)
        old = self.apps.get(m["name"])
        if old is not None and old["state"] in ("running", "starting"):
            if not restart:
                raise _Rc(g.UC_RC_STATE)
            self._app_stop(old)
        if old is None and len(self.apps) >= APPS_MAX:
            raise _Rc(g.UC_RC_LIMIT)
        rec = _new_app_record(m)
        self.apps[m["name"]] = rec
        self._write_apps_json()
        if m["autostart"]:
            self._app_start(rec)
        return {}

    def _app_rec(self, req: dict[str, Any]) -> dict[str, Any]:
        name = self._str(req, "name")
        assert name is not None
        if not _APP_NAME_RE.match(name):
            raise _Rc(g.UC_RC_INVALID)
        rec = self.apps.get(name)
        if rec is None:
            raise _Rc(g.UC_RC_NOT_FOUND)
        return rec

    def _app_start_req(self, req: dict[str, Any]) -> dict[str, Any]:
        self._app_start(self._app_rec(req))
        return {}

    def _app_stop_req(self, req: dict[str, Any]) -> dict[str, Any]:
        self._app_stop(self._app_rec(req))
        return {}

    def _app_remove(self, req: dict[str, Any]) -> dict[str, Any]:
        rec = self._app_rec(req)
        delete_file = req.get("delete_file", False)
        if not isinstance(delete_file, bool):
            raise _Rc(g.UC_RC_INVALID)
        self._app_stop(rec)
        del self.apps[rec["name"]]
        self._drop_app_objects(rec["name"])
        self._write_apps_json()
        if delete_file and all(a["file"] != rec["file"] for a in self.apps.values()):
            self.files.pop(rec["file"], None)
        return {}

    def _app_status_req(self, req: dict[str, Any]) -> dict[str, Any]:
        return self.app_status(self._app_rec(req)["name"])

    # uc_io
    def _channel_map(self, ch: FakeChannel) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": ch.id,
            "name": ch.name,
            "desc": ch.desc,
            "kind": ch.kind,
            "hw": ch.hw,
            "forced": ch.forced,
        }
        if ch.obj is not None:
            out["object"] = {"type": enums.object_type_name(ch.obj[0]), "instance": ch.obj[1]}
        return out

    def _io_catalog(self, req: dict[str, Any]) -> dict[str, Any]:
        offset = self._uint(req, "offset", 0)
        count = self._uint(req, "count", 0xFFFFFFFF)
        chans = list(self.io_channels.values())
        return _fill({"board": self.catalog_board, "total": len(chans)}, "channels",
                     (self._channel_map(c) for c in chans[offset:]), count)

    def _channel(self, req: dict[str, Any]) -> FakeChannel:
        name = self._str(req, "name")
        ch = self.io_channels.get(name or "")
        if ch is None:
            raise _Rc(g.UC_RC_NOT_FOUND)
        return ch

    def _io_read(self, req: dict[str, Any]) -> dict[str, Any]:
        if req.get("name") is not None:
            ch = self._channel(req)
            return {"values": {ch.name: float(ch.effective)}}
        return {"values": {c.name: float(c.effective) for c in self.io_channels.values()}}

    def _io_write(self, req: dict[str, Any]) -> dict[str, Any]:
        ch = self._channel(req)
        value = self._num(req, "value")
        if not ch.is_output:
            raise _Rc(g.UC_RC_PERM)
        ch.value = _normalise(ch.kind, value)
        return {}

    def _io_force(self, req: dict[str, Any]) -> dict[str, Any]:
        ch = self._channel(req)
        release = req.get("release", False)
        if not isinstance(release, bool) or release == ("value" in req):
            raise _Rc(g.UC_RC_INVALID)  # exactly one of "value" and "release": true
        if release:
            ch.forced = False
            return {}
        value = _normalise(ch.kind, self._num(req, "value"))
        ch.forced = True
        ch.forced_value = value
        if ch.hw == "sim" and not ch.is_output:
            ch.value = value
        return {}

    # uc_node
    def _node_info(self, req: dict[str, Any]) -> dict[str, Any]:
        running = sum(1 for a in self.apps.values() if a["state"] == "running")
        used = sum(len(v) for v in self.files.values())
        return {
            "fw": self.fw,
            "board": self.board,
            "api": UC_API_VERSION,
            "device": {"instance": self.device_instance, "name": self.device_name},
            "net": {"ipv4": self.host, "bacnet_port": self.bacnet_port},
            "uptime_s": int(time.monotonic() - self._boot_time),
            "fs": {"ready": True, "total": 1617920, "free": max(0, 1617920 - used)},
            "bacnet": {"packets": self.bacnet_packets, "objects": len(self.objects)},
            "apps": {"installed": len(self.apps), "running": running},
            "wasm": {
                "interp": True,
                "aot": self.wasm_aot,
                "aot_target": "",
                "pool_total": 262144,
                "pool_free": 262144 - 32768 * running,
            },
        }

    def _node_reload(self, req: dict[str, Any]) -> dict[str, Any]:
        doc = self._str(req, "doc")
        docs = {
            "device": ["device"],
            "io": ["io"],
            "apps": ["apps"],
            "all": ["device", "io", "apps"],
        }.get(doc or "")
        if docs is None:
            raise _Rc(g.UC_RC_INVALID)
        reboot = False
        first: _Rc | None = None
        for d in docs:
            try:
                reboot = self._reload(d) or reboot
            except _Rc as exc:
                first = first or exc
        if first is not None:
            raise _Rc(first.rc, reboot_required=reboot)
        return {"reboot_required": reboot}

    def _node_objects(self, req: dict[str, Any]) -> dict[str, Any]:
        offset = self._uint(req, "offset", 0)
        count = self._uint(req, "count", OBJECTS_COUNT_DEFAULT)
        oids = self._object_list()

        def entries() -> Any:
            for oid in oids[offset:]:
                key = (oid.type, oid.instance)
                props = self.objects[key]
                entry: dict[str, Any] = {
                    "type": enums.object_type_name(oid.type),
                    "instance": oid.instance,
                    "name": _smp_text(props.get(P["object-name"], "")),
                    "owner": self.owners.get(key, "system"),
                }
                if P["present-value"] in props:
                    entry["pv"] = _smp_value(props[P["present-value"]])
                yield entry

        return _fill({"total": len(oids)}, "objects", entries(), count)

    def _smp_ref(self, req: dict[str, Any]) -> tuple[tuple[int, int], int, int | None]:
        try:
            t = enums.object_type_number(req["type"])
            p = enums.property_number(req["prop"])
        except (KeyError, ValueError):
            raise _Rc(g.UC_RC_INVALID) from None
        inst = req.get("instance")
        if isinstance(inst, bool) or not isinstance(inst, int) or not 0 <= inst <= 4194303:
            raise _Rc(g.UC_RC_INVALID)
        index = req.get("index")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
            raise _Rc(g.UC_RC_INVALID)
        if index is not None and index < 0:
            index = None
        if index is not None and p not in _ARRAYS:
            raise _Rc(g.UC_RC_INVALID)  # index on a property that is not an array
        return (t, inst), p, index

    def _node_prop_read(self, req: dict[str, Any]) -> dict[str, Any]:
        key, prop, index = self._smp_ref(req)
        try:
            values = self.read_property_values(key, prop, index)
        except _BnError as exc:
            raise _Rc(_BN_TO_RC.get(exc.code_name, g.UC_RC_UNKNOWN)) from None
        if index is None and prop in codec.ARRAY_OR_LIST_PROPERTIES:
            # the whole array/list, rc LIMIT when it does not fit one response
            rsp = {"value": [_smp_value(v) for v in values]}
            if len(cbor2.dumps(rsp)) + RSP_RESERVE > RSP_MAX:
                raise _Rc(g.UC_RC_LIMIT)
            return rsp
        value: Any = values[0] if values else None
        return {"value": _smp_value(value)}

    def _node_prop_write(self, req: dict[str, Any]) -> dict[str, Any]:
        key, prop, index = self._smp_ref(req)
        if "value" not in req:
            raise _Rc(g.UC_RC_INVALID)
        priority = req.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 16:
            raise _Rc(g.UC_RC_INVALID)
        raw = req["value"]
        tag = codec.property_value_tag(key[0], prop)
        try:
            if raw is None:
                value: Any = None
            elif isinstance(raw, str):
                if len(raw.encode()) > TEXT_WRITE_MAX:
                    raise _Rc(g.UC_RC_INVALID)
                value = raw  # CharacterString
            elif isinstance(raw, int | float):  # bool: the number 0 / 1
                value = _typed(float(raw) if isinstance(raw, bool) else raw, tag, key[0], prop)
            else:
                raise _Rc(g.UC_RC_INVALID)
            self.write_property_value(key, prop, value, priority or None, index)
        except _BnError as exc:
            raise _Rc(_BN_TO_RC.get(exc.code_name, g.UC_RC_UNKNOWN)) from None
        except (ValueError, OverflowError):
            raise _Rc(g.UC_RC_INVALID) from None
        return {}

    # --- BACnet object access (shared by SMP and BACnet/IP) --------------------------------------

    def _props(self, key: tuple[int, int]) -> dict[int, Any]:
        props = self.objects.get(key)
        if props is None:
            raise _BnError("object", "unknown-object")
        return props

    def read_property_values(
        self, key: tuple[int, int], prop: int, index: int | None = None
    ) -> list[Any]:
        """Values of a property as sent in a ReadProperty-ACK (raises
        ``_BnError``)."""
        props = self._props(key)
        if key == self.device_key and prop == P["object-list"]:
            value: Any = self._object_list()
        elif prop not in props:
            raise _BnError("property", "unknown-property")
        else:
            value = props[prop]
        if index is None:
            return list(value) if isinstance(value, list) else [value]
        if not isinstance(value, list):
            raise _BnError("property", "property-is-not-an-array")
        if index == 0:
            return [len(value)]
        if not 1 <= index <= len(value):
            raise _BnError("property", "invalid-array-index")
        return [value[index - 1]]

    def write_property_value(
        self,
        key: tuple[int, int],
        prop: int,
        value: Any,
        priority: int | None = None,
        index: int | None = None,
    ) -> None:
        """Apply a WriteProperty (typed Python value; raises ``_BnError``)."""
        props = self._props(key)
        t = key[0]
        if prop not in props:
            raise _BnError("property", "unknown-property")
        if prop not in _WRITABLE:
            raise _BnError("property", "write-access-denied")
        if index is not None:
            raise _BnError("property", "property-is-not-an-array")
        pv = prop == P["present-value"]
        commandable_pv = pv and t in _COMMANDABLE
        if value is None:
            if pv and t in _VALUE_OBJECTS:
                return  # no priority array: a relinquish changes nothing (135 15.9.2)
            if not commandable_pv:
                raise _BnError("property", "invalid-data-type")
        else:
            self._check_type(t, prop, value)
        if pv:
            if value is not None:
                self._check_range(t, props, value)
            if commandable_pv:
                prio = 16 if priority is None else priority
                if prio == 6:
                    raise _BnError("property", "write-access-denied")
                props[P["priority-array"]][prio - 1] = value
                self._update_commandable(t, props)
            else:
                if t in _INPUTS and not props.get(P["out-of-service"]):
                    raise _BnError("property", "write-access-denied")
                if t == enums.OBJECT_ANALOG_VALUE and priority == 6:
                    # bacnet-stack av.c: priority 6 is reserved (minimum on/off)
                    raise _BnError("property", "write-access-denied")
                # value objects: the priority is ignored, the last write wins
                props[prop] = value
        else:
            props[prop] = value
            if prop == P["object-name"] and key == self.device_key:
                self.device_name = value
            if prop == P["relinquish-default"]:
                self._update_commandable(t, props)
        self._sync_io()

    @staticmethod
    def _check_type(t: int, prop: int, value: Any) -> None:
        tag = codec.property_value_tag(t, prop)
        if tag is None:
            return
        ok = {
            "real": lambda v: isinstance(v, float) and not isinstance(v, Double),
            "double": lambda v: isinstance(v, float),
            "unsigned": lambda v: type(v) is int and v >= 0,
            "signed": lambda v: type(v) is int,
            "enumerated": lambda v: isinstance(v, Enumerated),
            "boolean": lambda v: isinstance(v, bool),
            "character-string": lambda v: isinstance(v, str),
        }.get(tag)
        if ok is not None and not ok(value):
            raise _BnError("property", "invalid-data-type")

    @staticmethod
    def _check_range(t: int, props: dict[int, Any], value: Any) -> None:
        if t in _BINARY and int(value) not in (0, 1):
            raise _BnError("property", "value-out-of-range")
        if t in _MULTISTATE and not 1 <= int(value) <= int(props.get(P["number-of-states"], 2)):
            raise _BnError("property", "value-out-of-range")

    # --- BACnet/IP --------------------------------------------------------------------------------

    def handle_bacnet_datagram(
        self, data: bytes, addr: tuple[str, int]
    ) -> list[tuple[bytes, tuple[str, int]]]:
        """Process one BACnet/IP datagram; returns ``(payload, destination)``
        pairs to send."""
        try:
            bvlc = codec.decode_bvlc(data)
            if bvlc.function not in (
                codec.BVLC_ORIGINAL_UNICAST_NPDU,
                codec.BVLC_ORIGINAL_BROADCAST_NPDU,
                codec.BVLC_FORWARDED_NPDU,
            ):
                return []
            src = bvlc.origin or addr
            npdu = codec.decode_npdu(bvlc.npdu)
            if npdu.is_network_message or (npdu.dnet is not None and npdu.dnet != 0xFFFF):
                return []
            apdu = codec.decode_apdu(npdu.apdu)
        except codec.CodecError:
            return []
        self.bacnet_packets += 1
        self._sync_io()

        # reply routing: back to a remote network through the router
        def wrap(apdu_bytes: bytes, expecting_reply: bool = False) -> bytes:
            if npdu.snet is not None:
                hdr = codec.encode_npdu(expecting_reply, dnet=npdu.snet, dadr=npdu.sadr)
            else:
                hdr = codec.encode_npdu(expecting_reply)
            return codec.encode_bvlc(hdr + apdu_bytes)

        if apdu.pdu_type == codec.PDU_UNCONFIRMED_REQUEST:
            if apdu.service == enums.SERVICE_UNCONFIRMED_WHO_IS:
                try:
                    low, high = codec.decode_who_is(apdu.data)
                except codec.CodecError:
                    return []
                if (
                    low is not None
                    and high is not None
                    and not (low <= self.device_instance <= high)
                ):
                    return []
                iam = codec.i_am(
                    self.device_instance,
                    codec.MAX_APDU_BIP,
                    enums.SEGMENTATION_NONE,
                    self.vendor_id,
                )
                if self.iam_broadcast is not None:
                    pkt = codec.encode_bvlc(codec.encode_npdu() + iam, broadcast=True)
                    return [(pkt, self.iam_broadcast)]
                return [(wrap(iam), src)]
            return []
        if apdu.pdu_type != codec.PDU_CONFIRMED_REQUEST or apdu.invoke_id is None:
            return []
        invoke = apdu.invoke_id
        if apdu.segmented:
            return [
                (
                    wrap(
                        codec.encode_abort(
                            invoke, enums.ABORT_REASONS["segmentation-not-supported"]
                        )
                    ),
                    src,
                )
            ]
        try:
            rsp = self._confirmed(apdu)
        except _BnError as exc:
            rsp = codec.encode_error(invoke, apdu.service or 0, exc.error_class, exc.error_code)
        except codec.CodecError:
            rsp = codec.encode_reject(invoke, enums.REJECT_REASONS["invalid-tag"])
        if len(rsp) > (apdu.max_apdu or 50):
            rsp = codec.encode_abort(invoke, enums.ABORT_REASONS["segmentation-not-supported"])
        return [(wrap(rsp), src)]

    def _confirmed(self, apdu: codec.Apdu) -> bytes:
        invoke = apdu.invoke_id or 0
        if apdu.service == enums.SERVICE_CONFIRMED_READ_PROPERTY:
            ref = codec.decode_read_property_request(apdu.data)
            key = (ref.obj_type, ref.instance)
            if key == (enums.OBJECT_DEVICE, enums.MAX_INSTANCE):
                key = self.device_key  # wildcard device instance
            if ref.prop in (P["all"], P["required"], P["optional"]):
                raise _BnError("property", "unknown-property")
            values = self.read_property_values(key, ref.prop, ref.index)
            if ref.index == 0:
                value_bytes = codec.encode_application_unsigned(values[0])
            else:
                value_bytes = b"".join(self._encode_value(key[0], ref.prop, v) for v in values)
            return codec.read_property_ack(invoke, key[0], key[1], ref.prop, value_bytes, ref.index)
        if apdu.service == enums.SERVICE_CONFIRMED_WRITE_PROPERTY:
            req = codec.decode_write_property_request(apdu.data)
            key = (req.obj_type, req.instance)
            if len(req.values) != 1:
                raise _BnError("property", "invalid-data-type")
            if req.priority is not None and not 1 <= req.priority <= 16:
                raise _BnError("services", "parameter-out-of-range")
            self.write_property_value(key, req.prop, req.values[0], req.priority, req.index)
            return codec.encode_simple_ack(invoke, enums.SERVICE_CONFIRMED_WRITE_PROPERTY)
        if apdu.service == enums.SERVICE_CONFIRMED_DEVICE_COMMUNICATION_CONTROL:
            _, enable, password = codec.decode_device_communication_control_request(apdu.data)
            self._check_password(password)
            names = {v: k for k, v in enums.DCC_ENABLE_DISABLE.items()}
            if enable not in names:
                raise _BnError("services", "parameter-out-of-range")
            self.dcc_state = names[enable]
            return codec.encode_simple_ack(invoke, apdu.service)
        if apdu.service == enums.SERVICE_CONFIRMED_REINITIALIZE_DEVICE:
            state, password = codec.decode_reinitialize_device_request(apdu.data)
            self._check_password(password)
            names = {v: k for k, v in enums.REINITIALIZED_STATES.items()}
            if state not in names:
                raise _BnError("services", "parameter-out-of-range")
            self.reinitialized.append(names[state])
            if names[state] in ("coldstart", "warmstart"):
                # restart after the SimpleACK went out
                asyncio.get_running_loop().call_soon(self._reset)
            return codec.encode_simple_ack(invoke, apdu.service)
        return codec.encode_reject(invoke, enums.REJECT_REASONS["unrecognized-service"])

    def _check_password(self, password: str | None) -> None:
        """uc_bn_node.c: without a configured password the request is refused
        (CONFIG_UC_BACNET_REQUIRE_PASSWORD=y); with one it must match."""
        if not self.bacnet_password:
            if self.require_password:
                raise _BnError("security", "password-failure")
            return
        if password != self.bacnet_password:
            raise _BnError("security", "password-failure")

    def _reset(self) -> None:
        self.resets += 1
        self._boot()

    @staticmethod
    def _encode_value(t: int, prop: int, value: Any) -> bytes:
        if value is None:
            return codec.encode_application_null()
        tag = codec.property_value_tag(t, prop)
        if tag is None or isinstance(value, ObjectId | BitString):
            return codec.encode_value(value)
        return codec.encode_value(value, tag)


# --- helpers ------------------------------------------------------------------------------------


def _is_int(v: Any, lo: int, hi: int) -> bool:
    """A JSON integer (or integral number) within ``lo..hi``."""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return False
    return float(v).is_integer() and lo <= v <= hi


def _is_num(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, int | float) and math.isfinite(v)


def _is_ipv4(v: Any) -> bool:
    if not isinstance(v, str):
        return False
    parts = v.split(".")
    return len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts)


def _text_ok(v: Any, lo: int, hi: int) -> bool:
    return isinstance(v, str) and lo <= len(v.encode()) <= hi


def _check_device_doc(cfg: dict[str, Any]) -> None:
    """Parse-stage checks of ``device_from_json()`` (uc_config.c)."""
    bad = _Rc(g.UC_RC_INVALID)
    dev = cfg.get("device")
    if not isinstance(dev, dict) or not _is_int(dev.get("instance"), 0, INSTANCE_MAX):
        raise bad
    if not _text_ok(dev.get("name"), 1, 63):
        raise bad
    for key in ("description", "location"):
        if key in dev and not _text_ok(dev[key], 0, 63):
            raise bad
    net = cfg.get("network", {})
    if not isinstance(net, dict) or not isinstance(net.get("dhcp", True), bool):
        raise bad
    for key in ("ipv4", "netmask", "gateway"):
        if key in net and not _is_ipv4(net[key]):
            raise bad
    bn = cfg.get("bacnet", {})
    if not isinstance(bn, dict):
        raise bad
    for key, lo, hi in (("udp_port", 1, 65535), ("apdu_timeout_ms", 100, 60000),
                        ("apdu_retries", 0, 10)):
        if key in bn and not _is_int(bn[key], lo, hi):
            raise bad
    fd = bn.get("foreign_device")
    if fd is not None and (not isinstance(fd, dict) or not _is_ipv4(fd.get("bbmd"))
                           or not _is_int(fd.get("port", 47808), 1, 65535)
                           or not _is_int(fd.get("ttl_s", 60), 10, 65535)):
        raise bad
    bindings = bn.get("static_bindings", [])
    if not isinstance(bindings, list) or len(bindings) > 16:
        raise bad
    for b in bindings:
        if (not isinstance(b, dict) or not _is_int(b.get("device"), 0, INSTANCE_MAX)
                or not _is_ipv4(b.get("address"))
                or not _is_int(b.get("port", 47808), 1, 65535)):
            raise bad
    pw = bn.get("password")
    if pw is not None and (not isinstance(pw, str) or not 1 <= len(pw) <= PASSWORD_MAX
                           or any(not 0x20 <= ord(c) <= 0x7E for c in pw)):
        raise bad
    lvl = cfg.get("log", {})
    if not isinstance(lvl, dict) or lvl.get("level", "inf") not in LOG_LEVELS:
        raise bad


def _check_io_doc(cfg: dict[str, Any]) -> None:
    """Parse-stage checks of ``uc_config_parse_io()``: field types and
    ranges, at most 32 points, no object twice. Channels are checked when
    the points are bound."""
    bad = _Rc(g.UC_RC_INVALID)
    points = cfg.get("points")
    if not isinstance(points, list) or len(points) > IO_POINTS_MAX:
        raise bad
    seen: set[tuple[int, int]] = set()
    for pt in points:
        if not isinstance(pt, dict) or not _text_ok(pt.get("channel"), 1, 15):
            raise bad
        if pt.get("type") not in _IO_TYPES or not _is_int(pt.get("instance"), 0, INSTANCE_MAX):
            raise bad
        for key in ("name", "description"):
            if key in pt and not _text_ok(pt[key], 0, 63):
                raise bad
        if "units" in pt:
            try:
                enums.unit_number(pt["units"])
            except (ValueError, TypeError):
                raise bad from None
        for key in ("scale", "offset", "min", "max", "cov_increment"):
            if key in pt and not _is_num(pt[key]):
                raise bad
        if "min" in pt and "max" in pt and pt["min"] > pt["max"]:
            raise bad
        if pt.get("cov_increment", 0) < 0:
            raise bad
        if not _is_int(pt.get("sample_ms", 100), 10, 3600000) or not _is_int(
                pt.get("debounce_ms", 20), 0, 10000):
            raise bad
        if not isinstance(pt.get("invert", False), bool):
            raise bad
        key = (enums.object_type_number(pt["type"]), int(pt["instance"]))
        if key in seen:
            raise bad
        seen.add(key)


def _manifest_from_entry(e: Any) -> dict[str, Any]:
    """apps.json entry -> ``uc_app install`` style manifest (params map,
    sha256 bytes); ``_Rc(INVALID)`` for malformed entries."""
    if not isinstance(e, dict):
        raise _Rc(g.UC_RC_INVALID)
    m = dict(e)
    if isinstance(m.get("params"), list):
        try:
            m["params"] = {str(p["key"]): p["value"] for p in m["params"]}
        except (KeyError, TypeError):
            raise _Rc(g.UC_RC_INVALID) from None
    elif "params" in m:
        raise _Rc(g.UC_RC_INVALID)
    if "sha256" in m:
        sha = m["sha256"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            raise _Rc(g.UC_RC_INVALID)
        m["sha256"] = bytes.fromhex(sha)
    perms = m.get("perms", [])
    if isinstance(perms, list) and len(set(map(str, perms))) != len(perms):
        raise _Rc(g.UC_RC_INVALID)
    return m


def _udp_port(cfg: dict[str, Any]) -> int:
    return int(cfg.get("bacnet", {}).get("udp_port", 47808))


def _net_key(cfg: dict[str, Any]) -> tuple[Any, ...]:
    """Network settings as the firmware compares them (static only)."""
    net = cfg.get("network", {})
    if net.get("dhcp", True) or not net.get("ipv4"):
        return ("dhcp",)
    return (net.get("ipv4"), net.get("netmask", "255.255.255.0"), net.get("gateway", ""))


_FLT_MAX = 3.4028234663852886e38
_BINARY_PROPS = frozenset({P["present-value"], P["relinquish-default"], P["priority-array"]})


def _typed(raw: int | float, tag: str | None, obj_type: int, prop: int) -> Any:
    """CBOR number -> Python value of the property's natural datatype, with
    the checks of ``uc_value_from_double()`` (``ValueError`` -> rc INVALID):
    finite; REAL within the float range; UNSIGNED/ENUMERATED integral,
    0..2^32-1; SIGNED integral, 32-bit; BOOLEAN and the binary present value
    (relinquish-default, priority-array) 0 or 1."""
    f = float(raw)
    if not math.isfinite(f):
        raise ValueError("not finite")
    if obj_type in _BINARY and prop in _BINARY_PROPS and f not in (0.0, 1.0):
        raise ValueError("binary value must be 0 or 1")
    if tag == "real":
        if abs(f) > _FLT_MAX:
            raise ValueError("out of the REAL range")
        return f
    if tag == "double":
        return Double(f)
    if tag in ("enumerated", "unsigned", "signed", "boolean"):
        if not f.is_integer():
            raise ValueError("not integral")
        v = int(f)
        if tag == "signed":
            if not -(2**31) <= v < 2**31:
                raise ValueError("out of the SIGNED range")
            return v
        if not 0 <= v <= 0xFFFFFFFF:
            raise ValueError("out of the UNSIGNED range")
        if tag == "enumerated":
            return Enumerated(v)
        if tag == "boolean":
            if v not in (0, 1):
                raise ValueError("boolean must be 0 or 1")
            return bool(v)
        return v
    return raw


def _smp_text(text: str, limit: int = 96) -> str:
    """Text as the firmware sends it: at most ``limit`` bytes of UTF-8, cut on
    a character boundary."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore")


def _fill(head: dict[str, Any], key: str, items: Any, count: int) -> dict[str, Any]:
    """A list response: ``head`` plus ``key`` with at most ``count`` items and
    only as many as fit one response (``RSP_MAX``, ``RSP_RESERVE`` bytes
    left), like ``uc_mgmt_elem_commit()``."""
    out: dict[str, Any] = {**head, key: []}
    for item in items:
        if len(out[key]) >= count:
            break
        out[key].append(item)
        if len(cbor2.dumps(out)) + RSP_RESERVE > RSP_MAX:
            out[key].pop()
            break
    return out


def _smp_value(value: Any) -> Any:
    """Python value -> CBOR value as the firmware reports it."""
    if isinstance(value, str):
        return _smp_text(value)
    if isinstance(value, bytes):
        return value[:96]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, ObjectId):
        return f"({enums.object_type_name(value.type)}, {value.instance})"
    if isinstance(value, BitString):
        return "{" + ",".join("true" if b else "false" for b in value) + "}"
    if isinstance(value, list):
        return "{" + ",".join(str(_smp_value(v)) for v in value) + "}"
    return str(value)


def _new_app_record(m: dict[str, Any]) -> dict[str, Any]:
    rec = dict(m)
    rec.update(state="stopped", started_at=None, ticks_base=0, events=0, errors=0, last_error="")
    return rec


def _cfg_equal(rec: dict[str, Any], m: dict[str, Any]) -> bool:
    return all(rec.get(k) == v for k, v in m.items())


def _ticks(rec: dict[str, Any]) -> int:
    base = int(rec.get("ticks_base", 0))
    if rec.get("state") != "running" or rec.get("started_at") is None or not rec["period_ms"]:
        return base
    return base + int((time.monotonic() - rec["started_at"]) * 1000 // rec["period_ms"])


def _app_json(rec: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": rec["name"],
        "file": rec["file"],
        "autostart": rec["autostart"],
        "period_ms": rec["period_ms"],
        "heap_kb": rec["heap_kb"],
        "stack_kb": rec["stack_kb"],
        "perms": list(rec["perms"]),
        "params": [{"key": k, "value": v} for k, v in rec["params"].items()],
    }
    if rec.get("sha256"):
        out["sha256"] = rec["sha256"].hex()
    return out
