# SPDX-License-Identifier: Apache-2.0
"""SMP frame encoding: 8-byte header followed by a CBOR map.

| Byte | Field |
|------|-------|
| 0    | ``Res(3) Ver(2) Op(3)`` |
| 1    | flags |
| 2-3  | CBOR payload length, big endian |
| 4-5  | group id, big endian |
| 6    | sequence number |
| 7    | command id |
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

import cbor2

from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.smp.groups import SMP_VERSION_2

SMP_HEADER_SIZE = 8
_HDR = struct.Struct(">BBHHBB")


@dataclass
class SmpHeader:
    """Decoded SMP header."""

    op: int
    flags: int
    length: int
    group: int
    seq: int
    cmd: int
    version: int = SMP_VERSION_2

    def pack(self) -> bytes:
        """The 8 header bytes."""
        return _HDR.pack(
            ((self.version & 0x03) << 3) | (self.op & 0x07),
            self.flags & 0xFF,
            self.length & 0xFFFF,
            self.group & 0xFFFF,
            self.seq & 0xFF,
            self.cmd & 0xFF,
        )

    @classmethod
    def unpack(cls, data: bytes) -> SmpHeader:
        """Parse the first 8 bytes of ``data``."""
        if len(data) < SMP_HEADER_SIZE:
            raise HarnessError(f"SMP frame too short ({len(data)} bytes)")
        b0, flags, length, group, seq, cmd = _HDR.unpack_from(data, 0)
        return cls(
            op=b0 & 0x07,
            flags=flags,
            length=length,
            group=group,
            seq=seq,
            cmd=cmd,
            version=(b0 >> 3) & 0x03,
        )


def encode_payload(payload: dict[str, Any] | None) -> bytes:
    """CBOR-encode a request/response map (``None`` -> empty map)."""
    return cbor2.dumps({} if payload is None else payload)


def encode_frame(
    op: int,
    group: int,
    cmd: int,
    seq: int,
    payload: dict[str, Any] | None,
    version: int = SMP_VERSION_2,
    flags: int = 0,
) -> bytes:
    """Encode one SMP frame (header + CBOR map)."""
    body = encode_payload(payload)
    if len(body) > 0xFFFF:
        raise HarnessError(f"SMP payload too large ({len(body)} bytes)")
    hdr = SmpHeader(
        op=op, flags=flags, length=len(body), group=group, seq=seq, cmd=cmd, version=version
    )
    return hdr.pack() + body


def decode_frame(data: bytes) -> tuple[SmpHeader, dict[str, Any]]:
    """Decode one SMP frame.

    Raises:
        HarnessError: truncated frame, length mismatch or a payload that is
            not a CBOR map.
    """
    hdr = SmpHeader.unpack(data)
    body = bytes(data[SMP_HEADER_SIZE:])
    if len(body) < hdr.length:
        raise HarnessError(f"SMP frame truncated: header says {hdr.length}, got {len(body)} bytes")
    body = body[: hdr.length]
    if not body:
        return hdr, {}
    try:
        payload = cbor2.loads(body)
    except Exception as exc:  # cbor2 raises several exception types
        raise HarnessError(f"SMP payload is not valid CBOR: {exc}") from exc
    if not isinstance(payload, dict):
        raise HarnessError(f"SMP payload is not a CBOR map: {type(payload).__name__}")
    return hdr, payload


def frame_seq(data: bytes) -> int | None:
    """Sequence number of a raw frame, ``None`` if too short."""
    return data[6] if len(data) >= SMP_HEADER_SIZE else None
