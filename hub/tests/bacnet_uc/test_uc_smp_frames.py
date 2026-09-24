from __future__ import annotations

import cbor2
import pytest

from uc_hub.core.errors import DeviceError, InvalidRequest, NotFound, Unsupported
from uc_hub.drivers.bacnet_uc import names
from uc_hub.drivers.bacnet_uc.api import GROUP_FS, GROUP_OS, GROUP_UC_APP, GROUP_UC_NODE
from uc_hub.drivers.bacnet_uc.smp import (
    HEADER_SIZE,
    OP_READ,
    OP_READ_RSP,
    OP_WRITE,
    OP_WRITE_RSP,
    SMP_V1,
    SMP_V2,
    SmpFrame,
    SmpFrameError,
    decode_frame,
    encode_frame,
    error_code,
    error_for,
    legacy_error,
    raise_for_error,
    response_op,
    v2_error,
)


def test_header_layout_is_smp_v2() -> None:
    data = encode_frame(OP_WRITE, GROUP_UC_NODE, 4, 0x2A, {"a": 1})
    payload = cbor2.dumps({"a": 1})
    assert data[0] == (1 << 3) | OP_WRITE
    assert data[1] == 0
    assert int.from_bytes(data[2:4], "big") == len(payload)
    assert int.from_bytes(data[4:6], "big") == GROUP_UC_NODE
    assert data[6] == 0x2A
    assert data[7] == 4
    assert data[HEADER_SIZE:] == payload


@pytest.mark.parametrize("op", [OP_READ, OP_READ_RSP, OP_WRITE, OP_WRITE_RSP])
def test_frame_round_trip(op: int) -> None:
    body = {"name": "/lfs/cfg/io.json", "off": 70000, "data": bytes(range(256)), "x": [1.5, None, True]}
    frame = SmpFrame(op, 0x1234, 7, 255, body)
    decoded = decode_frame(frame.encode())
    assert decoded == frame
    assert decoded.version == SMP_V2


def test_version_one_header_and_sequence_wrap() -> None:
    decoded = decode_frame(encode_frame(OP_READ, GROUP_OS, 0, 256 + 3, {}, version=SMP_V1))
    assert decoded.version == SMP_V1
    assert decoded.seq == 3
    assert decoded.body == {}


def test_response_op() -> None:
    assert response_op(OP_READ) == OP_READ_RSP
    assert response_op(OP_WRITE) == OP_WRITE_RSP


@pytest.mark.parametrize(
    "data",
    [
        b"\x08\x00\x00",  # shorter than the header
        encode_frame(OP_READ, 0, 0, 1, {"a": 1})[:-1],  # payload truncated
        bytes([8, 0, 0, 1, 0, 0, 1, 0]) + b"\xff",  # invalid CBOR
        bytes([8, 0, 0, 2, 0, 0, 1, 0]) + cbor2.dumps([1]),  # not a map
        bytes([8, 0, 0, 3, 0, 0, 1, 0]) + cbor2.dumps({1: 2}),  # non-text key
    ],
)
def test_malformed_frames_are_rejected(data: bytes) -> None:
    with pytest.raises(SmpFrameError):
        decode_frame(data)


def test_encode_rejects_bad_fields() -> None:
    with pytest.raises(ValueError):
        encode_frame(8, 0, 0, 0, {})


def test_success_bodies_are_not_errors() -> None:
    assert error_code({}) is None
    assert error_code({"rc": 0, "off": 5}) is None
    assert error_code({"err": {"group": 66, "rc": 0}}) is None
    assert raise_for_error({"value": 1}) == {"value": 1}


def test_v2_custom_group_errors() -> None:
    not_found = error_for(v2_error(GROUP_UC_NODE, 3), context="r204-ctl: prop_read")
    assert isinstance(not_found, NotFound)
    assert "NOT_FOUND" in str(not_found) and "r204-ctl" in str(not_found)
    assert isinstance(error_for(v2_error(GROUP_UC_APP, 11)), Unsupported)
    state = error_for(v2_error(GROUP_UC_APP, 8))
    assert isinstance(state, DeviceError)
    assert (state.rc, state.group) == (8, GROUP_UC_APP)
    assert "STATE" in str(state)


def test_v2_fs_group_errors() -> None:
    assert isinstance(error_for(v2_error(GROUP_FS, 3)), NotFound)  # FILE_NOT_FOUND
    assert isinstance(error_for(v2_error(GROUP_FS, 14)), NotFound)  # MOUNT_POINT_NOT_FOUND
    assert isinstance(error_for(v2_error(GROUP_FS, 13)), Unsupported)  # CHECKSUM_HASH_NOT_FOUND
    offset = error_for(v2_error(GROUP_FS, 11))
    assert isinstance(offset, DeviceError)
    assert (offset.rc, offset.group) == (11, GROUP_FS)
    assert "FILE_OFFSET_NOT_VALID" in str(offset)


def test_v2_other_group_error_is_device_error() -> None:
    err = error_for(v2_error(GROUP_OS, 3))
    assert isinstance(err, DeviceError) and (err.rc, err.group) == (3, GROUP_OS)


def test_legacy_errors() -> None:
    assert isinstance(error_for(legacy_error(5)), NotFound)  # ENOENT
    assert isinstance(error_for(legacy_error(8)), Unsupported)  # ENOTSUP
    busy = error_for(legacy_error(10, "fs busy"))
    assert isinstance(busy, DeviceError)
    assert (busy.rc, busy.group) == (10, None)
    assert "MGMT_ERR_EBUSY" in str(busy) and "fs busy" in str(busy)
    for rc in (12, 13):  # UNSUPPORTED_TOO_OLD/NEW stay device errors
        err = error_for(legacy_error(rc))
        assert type(err) is DeviceError and err.rc == rc


def test_malformed_error_fields_count_as_errors() -> None:
    assert isinstance(error_for({"err": "boom"}), DeviceError)
    assert isinstance(error_for({"err": {"group": 66, "rc": "x"}}), DeviceError)
    assert isinstance(error_for({"rc": True}), DeviceError)


def test_raise_for_error_raises_mapped_exception() -> None:
    with pytest.raises(NotFound):
        raise_for_error(v2_error(GROUP_UC_NODE, 3))


def test_names() -> None:
    assert names.object_type_id("analog-value") == 2
    assert names.object_type_id(19) == 19
    assert names.object_type_id("19") == 19
    assert names.object_type_name(4) == "binary-output"
    assert names.property_id("present-value") == 85
    assert names.property_name(87) == "priority-array"
    assert names.wire_property(104) == "relinquish-default"
    assert names.wire_object_type("multi-state-value") == "multi-state-value"
    assert names.wire_property(4000) == 4000  # proprietary: sent as a number
    assert names.units_name(62) == "degrees-celsius"
    assert names.units_name("percent") == "percent"
    assert names.units_name(95) is None  # no-units
    assert names.units_name(None) is None
    for bad in ("not-a-type", -1, True, 1.5):
        with pytest.raises(InvalidRequest):
            names.object_type_id(bad)  # type: ignore[arg-type]
