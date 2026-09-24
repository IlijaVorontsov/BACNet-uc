"""SMP (MCUmgr Simple Management Protocol) frames and error mapping.

A frame is an 8-byte header followed by a CBOR map::

    byte 0   Res(3) Ver(2) Op(3)   Ver 1 = SMP v2, Op 0 read .. 3 write-rsp
    byte 1   flags
    2-3      CBOR payload length, big endian
    4-5      group id, big endian
    6        sequence number, echoed in the response
    7        command id

Errors come in two shapes. SMP v2 group errors are ``{"err": {"group": g,
"rc": rc}}`` where ``rc`` is the group's own code; transport and generic
failures use the legacy ``{"rc": <mcumgr_err_t>}``. Both map onto
``core.errors``: "not found" becomes ``NotFound``, "not supported" becomes
``Unsupported`` and everything else ``DeviceError``. A legacy error carries
``group=None`` because its rc is an MCUmgr code, not a group code.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

import cbor2

from ...core.errors import DeviceError, HubError, NotFound, Unsupported
from .api import (
    GROUP_FS,
    GROUP_IMAGE,
    GROUP_OS,
    GROUP_SETTINGS,
    GROUP_SHELL,
    GROUP_STAT,
    GROUP_UC_APP,
    GROUP_UC_IO,
    GROUP_UC_NODE,
    RC_NAMES,
)

OP_READ, OP_READ_RSP, OP_WRITE, OP_WRITE_RSP = 0, 1, 2, 3
#: Header version field values: 0 is the original protocol, 1 is SMP v2.
SMP_V1, SMP_V2 = 0, 1
HEADER_SIZE = 8
MAX_PAYLOAD = 0xFFFF

# Command ids of the standard groups the hub uses.
OS_ECHO, OS_RESET, OS_MCUMGR_PARAMS = 0, 5, 6
FS_FILE, FS_STATUS, FS_HASH, FS_CLOSE = 0, 1, 2, 4

_HEADER = struct.Struct(">BBHHBB")

# enum mcumgr_err_t (zephyr/mgmt/mcumgr/mgmt/mgmt_defines.h)
MGMT_ERR_EOK = 0
MGMT_ERR_EUNKNOWN = 1
MGMT_ERR_ENOMEM = 2
MGMT_ERR_EINVAL = 3
MGMT_ERR_ETIMEOUT = 4
MGMT_ERR_ENOENT = 5
MGMT_ERR_EBADSTATE = 6
MGMT_ERR_EMSGSIZE = 7
MGMT_ERR_ENOTSUP = 8
MGMT_ERR_ECORRUPT = 9
MGMT_ERR_EBUSY = 10
MGMT_ERR_EACCESSDENIED = 11
MGMT_ERR_UNSUPPORTED_TOO_OLD = 12
MGMT_ERR_UNSUPPORTED_TOO_NEW = 13

MGMT_ERR_NAMES = {
    0: "EOK",
    1: "EUNKNOWN",
    2: "ENOMEM",
    3: "EINVAL",
    4: "ETIMEOUT",
    5: "ENOENT",
    6: "EBADSTATE",
    7: "EMSGSIZE",
    8: "ENOTSUP",
    9: "ECORRUPT",
    10: "EBUSY",
    11: "EACCESSDENIED",
    12: "UNSUPPORTED_TOO_OLD",
    13: "UNSUPPORTED_TOO_NEW",
    14: "BRIDGED_CONNECTION_UNAVAILABLE",
}

# enum fs_mgmt_err_code_t (zephyr/mgmt/mcumgr/grp/fs_mgmt/fs_mgmt.h)
FS_ERR_OK = 0
FS_ERR_UNKNOWN = 1
FS_ERR_FILE_INVALID_NAME = 2
FS_ERR_FILE_NOT_FOUND = 3
FS_ERR_FILE_IS_DIRECTORY = 4
FS_ERR_FILE_OPEN_FAILED = 5
FS_ERR_FILE_SEEK_FAILED = 6
FS_ERR_FILE_READ_FAILED = 7
FS_ERR_FILE_TRUNCATE_FAILED = 8
FS_ERR_FILE_DELETE_FAILED = 9
FS_ERR_FILE_WRITE_FAILED = 10
FS_ERR_FILE_OFFSET_NOT_VALID = 11
FS_ERR_FILE_OFFSET_LARGER_THAN_FILE = 12
FS_ERR_CHECKSUM_HASH_NOT_FOUND = 13
FS_ERR_MOUNT_POINT_NOT_FOUND = 14
FS_ERR_READ_ONLY_FILESYSTEM = 15
FS_ERR_FILE_EMPTY = 16
FS_ERR_FILE_CLOSE_FAILED = 17

FS_ERR_NAMES = {
    0: "OK",
    1: "UNKNOWN",
    2: "FILE_INVALID_NAME",
    3: "FILE_NOT_FOUND",
    4: "FILE_IS_DIRECTORY",
    5: "FILE_OPEN_FAILED",
    6: "FILE_SEEK_FAILED",
    7: "FILE_READ_FAILED",
    8: "FILE_TRUNCATE_FAILED",
    9: "FILE_DELETE_FAILED",
    10: "FILE_WRITE_FAILED",
    11: "FILE_OFFSET_NOT_VALID",
    12: "FILE_OFFSET_LARGER_THAN_FILE",
    13: "CHECKSUM_HASH_NOT_FOUND",
    14: "MOUNT_POINT_NOT_FOUND",
    15: "READ_ONLY_FILESYSTEM",
    16: "FILE_EMPTY",
    17: "FILE_CLOSE_FAILED",
}

# Custom group rc values (management-protocol.md, "Group rc values").
UC_RC_OK = 0
UC_RC_UNKNOWN = 1
UC_RC_INVALID = 2
UC_RC_NOT_FOUND = 3
UC_RC_EXISTS = 4
UC_RC_BUSY = 5
UC_RC_NO_MEM = 6
UC_RC_IO = 7
UC_RC_STATE = 8
UC_RC_VERIFY = 9
UC_RC_PERM = 10
UC_RC_UNSUPPORTED = 11
UC_RC_LIMIT = 12

CUSTOM_GROUPS = frozenset({GROUP_UC_APP, GROUP_UC_IO, GROUP_UC_NODE})

GROUP_NAMES = {
    GROUP_OS: "os",
    GROUP_IMAGE: "image",
    GROUP_STAT: "stat",
    GROUP_SETTINGS: "settings",
    GROUP_FS: "fs",
    GROUP_SHELL: "shell",
    GROUP_UC_APP: "uc_app",
    GROUP_UC_IO: "uc_io",
    GROUP_UC_NODE: "uc_node",
}


class SmpFrameError(ValueError):
    """Bytes that are not a well-formed SMP frame."""


@dataclass(slots=True)
class SmpFrame:
    op: int
    group: int
    command: int
    seq: int
    body: dict[str, Any] = field(default_factory=dict)
    version: int = SMP_V2
    flags: int = 0

    def encode(self) -> bytes:
        return encode_frame(
            self.op, self.group, self.command, self.seq, self.body,
            version=self.version, flags=self.flags,
        )


def encode_frame(
    op: int, group: int, command: int, seq: int, body: dict[str, Any] | None = None,
    *, version: int = SMP_V2, flags: int = 0,
) -> bytes:
    if not 0 <= op <= 7 or not 0 <= version <= 3:
        raise ValueError(f"bad op {op} or version {version}")
    payload = cbor2.dumps(body if body is not None else {})
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"SMP payload of {len(payload)} bytes exceeds {MAX_PAYLOAD}")
    header = _HEADER.pack(
        (version << 3) | op, flags & 0xFF, len(payload), group & 0xFFFF, seq & 0xFF,
        command & 0xFF,
    )
    return header + payload


def decode_header(data: bytes) -> tuple[int, int, int, int, int, int, int]:
    """``(op, version, flags, length, group, seq, command)``."""
    if len(data) < HEADER_SIZE:
        raise SmpFrameError(f"frame of {len(data)} bytes is shorter than the SMP header")
    b0, flags, length, group, seq, command = _HEADER.unpack_from(data)
    return b0 & 0x07, (b0 >> 3) & 0x03, flags, length, group, seq, command


def decode_frame(data: bytes) -> SmpFrame:
    op, version, flags, length, group, seq, command = decode_header(data)
    end = HEADER_SIZE + length
    if len(data) < end:
        raise SmpFrameError(f"frame announces {length} payload bytes, has {len(data) - HEADER_SIZE}")
    body = loads_body(data[HEADER_SIZE:end]) if length else {}
    return SmpFrame(op, group, command, seq, body, version, flags)


def loads_body(payload: bytes) -> dict[str, Any]:
    try:
        body = cbor2.loads(payload)
    except Exception as e:  # untrusted bytes: any decoder failure means a malformed frame
        raise SmpFrameError(f"invalid CBOR payload: {e}") from e
    if not isinstance(body, dict):
        raise SmpFrameError(f"SMP payload is a {type(body).__name__}, not a map")
    if not all(isinstance(k, str) for k in body):
        raise SmpFrameError("SMP payload map has non-text keys")
    return body


def response_op(op: int) -> int:
    return op | 1


def group_name(group: int | None) -> str:
    if group is None:
        return "smp"
    return GROUP_NAMES.get(group, f"group {group}")


def rc_name(group: int | None, rc: int) -> str:
    if group is None:
        return f"MGMT_ERR_{MGMT_ERR_NAMES.get(rc, str(rc))}"
    if group in CUSTOM_GROUPS:
        return RC_NAMES.get(rc, str(rc))
    if group == GROUP_FS:
        return FS_ERR_NAMES.get(rc, str(rc))
    return str(rc)


def error_code(body: dict[str, Any]) -> tuple[int | None, int] | None:
    """``(group, rc)`` of an error response, ``None`` for success. ``group``
    is None for the legacy form. Malformed error fields count as errors."""
    err = body.get("err")
    if err is not None:
        if not isinstance(err, dict):
            return None, MGMT_ERR_EUNKNOWN
        group, rc = err.get("group"), err.get("rc")
        if not isinstance(rc, int) or isinstance(rc, bool):
            return None, MGMT_ERR_EUNKNOWN
        if rc == 0:
            return None
        return (group if isinstance(group, int) and not isinstance(group, bool) else None), rc
    rc = body.get("rc")
    if rc is None:
        return None
    if not isinstance(rc, int) or isinstance(rc, bool):
        return None, MGMT_ERR_EUNKNOWN
    return None if rc == 0 else (None, rc)


def error_for(body: dict[str, Any], *, context: str = "") -> HubError | None:
    """The exception an error response stands for, or None on success."""
    code = error_code(body)
    if code is None:
        return None
    group, rc = code
    name = rc_name(group, rc)
    where = f"{context}: " if context else ""
    reason = body.get("rsn")
    detail = f" ({reason})" if isinstance(reason, str) and reason else ""
    if group is None:
        message = f"{where}{name} (legacy rc {rc}){detail}"
        if rc == MGMT_ERR_ENOENT:
            return NotFound(message)
        if rc == MGMT_ERR_ENOTSUP:
            return Unsupported(message)
        return DeviceError(message, rc=rc, group=None)
    message = f"{where}{name} ({group_name(group)} rc {rc}){detail}"
    if group in CUSTOM_GROUPS:
        if rc == UC_RC_NOT_FOUND:
            return NotFound(message)
        if rc == UC_RC_UNSUPPORTED:
            return Unsupported(message)
    elif group == GROUP_FS:
        if rc in (FS_ERR_FILE_NOT_FOUND, FS_ERR_MOUNT_POINT_NOT_FOUND):
            return NotFound(message)
        if rc == FS_ERR_CHECKSUM_HASH_NOT_FOUND:
            return Unsupported(message)
    return DeviceError(message, rc=rc, group=group)


def raise_for_error(body: dict[str, Any], *, context: str = "") -> dict[str, Any]:
    exc = error_for(body, context=context)
    if exc is not None:
        raise exc
    return body


def v2_error(group: int, rc: int) -> dict[str, Any]:
    return {"err": {"group": group, "rc": rc}}


def legacy_error(rc: int, reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"rc": rc}
    if reason:
        body["rsn"] = reason
    return body
