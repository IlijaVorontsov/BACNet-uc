# SPDX-License-Identifier: Apache-2.0
"""SMP frame header and CBOR payload encoding."""

from __future__ import annotations

import cbor2
import pytest

from bacnet_uc_harness.errors import HarnessError, SmpError
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.codec import SmpHeader, decode_frame, encode_frame, frame_seq


def test_header_layout() -> None:
    frame = encode_frame(g.OP_WRITE, g.GROUP_UC_NODE, g.UC_NODE_RELOAD, 0x42, {"doc": "io"})
    body = cbor2.dumps({"doc": "io"})
    # byte 0: Res(3)=0, Ver(2)=1, Op(3)=2 -> 0b000_01_010
    assert frame[0] == 0x0A
    assert frame[1] == 0
    assert frame[2:4] == len(body).to_bytes(2, "big")
    assert frame[4:6] == (66).to_bytes(2, "big")
    assert frame[6] == 0x42
    assert frame[7] == g.UC_NODE_RELOAD
    assert frame[8:] == body
    assert frame_seq(frame) == 0x42


def test_header_pack_unpack() -> None:
    hdr = SmpHeader(op=3, flags=0, length=5, group=0x1234, seq=255, cmd=7, version=0)
    raw = hdr.pack()
    assert raw.hex() == "030000051234ff07"
    assert SmpHeader.unpack(raw) == hdr


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"d": "hello"},
        {"off": 0, "data": b"\x00" * 300, "len": 300, "name": "/lfs/cfg/io.json"},
        {"value": 21.5, "priority": 8, "type": "analog-value", "instance": 1, "prop": 85},
        {"value": None},
        {"argv": ["kernel", "version"]},
        {"params": {"k": "v"}, "perms": ["io"], "sha256": bytes(range(32))},
        {"err": {"group": 64, "rc": 3}},
    ],
)
def test_round_trip(payload: dict) -> None:
    for op in (g.OP_READ, g.OP_WRITE, g.OP_READ_RSP, g.OP_WRITE_RSP):
        hdr, out = decode_frame(encode_frame(op, g.GROUP_UC_APP, 5, 17, payload))
        assert out == payload
        assert (hdr.op, hdr.group, hdr.cmd, hdr.seq, hdr.version, hdr.flags) == (
            op,
            64,
            5,
            17,
            1,
            0,
        )


def test_decode_indefinite_length_map() -> None:
    # zcbor (non-canonical) encodes maps with indefinite length
    body = bytes.fromhex("bf6172f5ff")  # {_ "r": true}
    frame = SmpHeader(op=1, flags=0, length=len(body), group=0, seq=1, cmd=0).pack() + body
    assert decode_frame(frame)[1] == {"r": True}


def test_decode_errors() -> None:
    with pytest.raises(HarnessError):
        decode_frame(b"\x01\x00\x00")
    good = encode_frame(g.OP_READ_RSP, 0, 0, 1, {"r": "x"})
    with pytest.raises(HarnessError):
        decode_frame(good[:-1])  # truncated payload
    arr = cbor2.dumps([1, 2])
    with pytest.raises(HarnessError):
        decode_frame(SmpHeader(1, 0, len(arr), 0, 1, 0).pack() + arr)
    with pytest.raises(HarnessError):
        decode_frame(SmpHeader(1, 0, 2, 0, 1, 0).pack() + b"\xff\xff")
    # empty payload is an empty map
    assert decode_frame(SmpHeader(1, 0, 0, 0, 1, 0).pack())[1] == {}


def test_rc_names() -> None:
    assert g.UC_RC_NAMES[0] == "OK" and g.UC_RC_NAMES[12] == "LIMIT"
    assert len(g.UC_RC_NAMES) == 13
    assert g.rc_name(64, 3) == "NOT_FOUND"
    assert g.rc_name(8, 3) == "FILE_NOT_FOUND"
    assert g.rc_name(None, 8) == "ENOTSUP"
    assert g.rc_name(99, 1) == "rc1"
    err = SmpError(65, 10)
    assert (err.group, err.rc, err.rc_name) == (65, 10, "PERM")
    assert "uc_io" in str(err)
    legacy = SmpError(None, 3)
    assert legacy.rc_name == "EINVAL"


def test_custom_group_ids_match_firmware(repo_root) -> None:  # type: ignore[no-untyped-def]
    """groups.py mirrors firmware/include/uc/uc_mgmt.h."""
    import re

    header = (repo_root / "firmware/include/uc/uc_mgmt.h").read_text()
    defines = dict(re.findall(r"#define (UC_MGMT_\w+)\s+(\d+)", header))
    mapping = {
        "UC_MGMT_APP_LIST": g.UC_APP_LIST,
        "UC_MGMT_APP_INSTALL": g.UC_APP_INSTALL,
        "UC_MGMT_APP_START": g.UC_APP_START,
        "UC_MGMT_APP_STOP": g.UC_APP_STOP,
        "UC_MGMT_APP_REMOVE": g.UC_APP_REMOVE,
        "UC_MGMT_APP_STATUS": g.UC_APP_STATUS,
        "UC_MGMT_IO_CATALOG": g.UC_IO_CATALOG,
        "UC_MGMT_IO_READ": g.UC_IO_READ,
        "UC_MGMT_IO_WRITE": g.UC_IO_WRITE,
        "UC_MGMT_IO_FORCE": g.UC_IO_FORCE,
        "UC_MGMT_NODE_INFO": g.UC_NODE_INFO,
        "UC_MGMT_NODE_RELOAD": g.UC_NODE_RELOAD,
        "UC_MGMT_NODE_OBJECTS": g.UC_NODE_OBJECTS,
        "UC_MGMT_NODE_PROP_READ": g.UC_NODE_PROP_READ,
        "UC_MGMT_NODE_PROP_WRITE": g.UC_NODE_PROP_WRITE,
    }
    for name, value in mapping.items():
        assert int(defines[name]) == value, name
    rcs = dict(re.findall(r"UC_MGMT_RC_(\w+) = (\d+)", header))
    assert {int(v): k for k, v in rcs.items()} == g.UC_RC_NAMES
