# SPDX-License-Identifier: Apache-2.0
"""BACnet encoding: known-good vectors and round trips.

The reference octets were produced with the bacnet-stack encoders used by the
firmware (rp_encode_apdu, wp_encode_apdu, iam_encode_apdu, whois_encode_apdu,
bacerror/abort/reject_encode_apdu, encode_application_* from bacdcode.c,
bacnet-stack 54544d02) and checked by hand against ASHRAE 135 clause 20.
"""

from __future__ import annotations

import math

import pytest

from bacnet_uc_harness.bacnet import codec, enums
from bacnet_uc_harness.bacnet.codec import (
    BitString,
    Constructed,
    ContextValue,
    Date,
    Double,
    Enumerated,
    ObjectId,
    Time,
)

# --- known-good vectors ------------------------------------------------------------------


def test_read_property_device_object_name() -> None:
    # 00 confirmed request, 05 = no max-segments / max APDU 1476, invoke 1,
    # 0c ReadProperty; 0c = [0] len 4 object id device(8)<<22 | 1234; 19 4d = [1] 77
    apdu = codec.read_property(1, "device", 1234, "object-name")
    assert apdu.hex() == "0005010c0c020004d2194d"
    # full datagram: BVLC original-unicast, length 17, NPDU expecting reply
    pkt = codec.encode_bip(apdu, expecting_reply=True)
    assert pkt.hex() == "810a0011" + "0104" + "0005010c0c020004d2194d"


def test_read_property_with_array_index() -> None:
    apdu = codec.read_property(7, enums.OBJECT_ANALOG_INPUT, 1, "priority-array", index=5)
    assert apdu.hex() == "0005070c0c0000000119572905"


def test_read_property_max_values() -> None:
    apdu = codec.read_property(255, "multi-state-value", 4194302, 4194303)
    assert apdu.hex() == "0005ff0c0c04fffffe1b3fffff"


def test_read_property_ack_real() -> None:
    apdu = codec.read_property_ack(9, "analog-value", 3, "present-value", codec.encode_value(72.5))
    assert apdu.hex() == "30090c0c0080000319553e44429100003f"


def test_write_property_real_with_priority() -> None:
    apdu = codec.write_property(
        2, "analog-value", 3, "present-value", codec.encode_value(72.5), priority=8
    )
    assert apdu.hex() == "0005020f0c0080000319553e44429100003f4908"


def test_write_property_null_priority_16() -> None:
    apdu = codec.write_property(
        3, "binary-output", 1, "present-value", codec.encode_value(None), priority=16
    )
    assert apdu.hex() == "0005030f0c0100000119553e003f4910"


def test_i_am_vector() -> None:
    apdu = codec.i_am(1234, 1476, enums.SEGMENTATION_NONE, 260)
    assert apdu.hex() == "1000c4020004d22205c49103220104"
    pkt = codec.encode_bip(apdu, broadcast=True)
    assert pkt.hex() == "810b0015" + "0100" + apdu.hex()


def test_who_is_vectors() -> None:
    assert codec.who_is().hex() == "1008"
    assert codec.who_is(100, 70000).hex() == "100809641b011170"


def test_error_reject_abort_vectors() -> None:
    assert codec.encode_error(1, 12, 1, 31).hex() == "50010c9101911f"
    assert codec.encode_abort(5, 4, server=True).hex() == "710504"
    assert codec.encode_reject(6, 9).hex() == "600609"


@pytest.mark.parametrize(
    ("value", "tag", "expected"),
    [
        (-1, None, "31ff"),
        (-129, None, "32ff7f"),
        (8388608, "signed", "3400800000"),
        (-(2**31), None, "3480000000"),
        (0, None, "2100"),
        (256, None, "220100"),
        (2**32, None, "25050100000000"),
        (2**64 - 1, None, "2508ffffffffffffffff"),
        (-0.1, None, "44bdcccccd"),
        (math.pi, "double", "5508400921fb54442d18"),
        (Double(math.pi), None, "5508400921fb54442d18"),
        (Enumerated(0), None, "9100"),
        (65535, "enumerated", "92ffff"),
        (True, None, "11"),
        (False, None, "10"),
        (None, None, "00"),
        ("abc", None, "7400616263"),
        (BitString([1, 0, 1, 0]), None, "8204a0"),
        (BitString([i % 3 == 0 for i in range(10)]), None, "83069240"),
        (Date(2024, 5, 17, 5), None, "a47c051105"),
        (Time(13, 45, 7, 50), None, "b40d2d0732"),
        (ObjectId(0, 1), None, "c400000001"),
        ("analog-input:1", "object-identifier", "c400000001"),
        (b"\x01\x02", None, "620102"),
    ],
)
def test_application_encoding_vectors(value: object, tag: str | None, expected: str) -> None:
    assert codec.encode_value(value, tag).hex() == expected


def test_long_character_string_lengths() -> None:
    # 252 chars + charset octet = 253 -> one extended length octet (0xFD)
    assert codec.encode_value("x" * 252)[:3].hex() == "75fd00"
    # 253 chars -> 254 octets -> 0xFE + 2-octet length
    assert codec.encode_value("x" * 253)[:5].hex() == "75fe00fe00"
    # 300 chars -> 301 octets
    enc = codec.encode_value("x" * 300)
    assert enc[:5].hex() == "75fe012d00" and len(enc) == 305
    # > 65535 octets -> 0xFF + 4-octet length
    enc = codec.encode_value(b"\xaa" * 70000)
    assert enc[:6].hex() == "65ff00011170"
    for text in ("", "x" * 252, "x" * 253, "x" * 300, "ä€𝄞" * 100):
        value, end = codec.decode_application_value(codec.encode_value(text))
        assert value == text


def test_context_and_opening_tags() -> None:
    assert codec.encode_context_unsigned(20, 5).hex() == "f91405"
    assert codec.encode_opening_tag(3).hex() == "3e"
    assert codec.encode_closing_tag(3).hex() == "3f"
    assert codec.encode_opening_tag(30).hex() == "fe1e"
    assert codec.encode_closing_tag(30).hex() == "ff1e"
    assert codec.encode_context_boolean(2, True).hex() == "2901"
    hdr = codec.decode_tag(bytes.fromhex("f91405"))
    assert (hdr.number, hdr.context, hdr.length, hdr.header_len) == (20, True, 1, 2)
    hdr = codec.decode_tag(bytes.fromhex("fe1e"))
    assert hdr.opening and hdr.number == 30
    hdr = codec.decode_tag(bytes.fromhex("ff1e"))
    assert hdr.closing and hdr.number == 30


# --- round trips -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        0,
        1,
        255,
        256,
        65535,
        2**24,
        2**32 - 1,
        2**32,
        2**63,
        2**64 - 1,
        -1,
        -128,
        -129,
        -32768,
        -32769,
        -(2**31),
        -(2**63),
        2**63 - 1,
        "",
        "hello",
        "x" * 1000,
        b"",
        b"\x00\xff" * 200,
        Enumerated(1),
        Enumerated(2**32 - 1),
        BitString([]),
        BitString([True]),
        BitString([False] * 8),
        BitString([True] * 9),
        Date(2000, 1, 1, 6),
        Date(None, None, None, None),
        Date(2024, 13, 32, None),
        Time(0, 0, 0, 0),
        Time(23, 59, 59, 99),
        Time(None, None, None, None),
        ObjectId(8, 4194303),
        ObjectId(1023, 0),
        ObjectId(0, 0),
    ],
)
def test_round_trip(value: object) -> None:
    enc = codec.encode_value(value)
    dec, end = codec.decode_application_value(enc)
    assert end == len(enc)
    assert dec == value
    assert type(dec) is type(value) or (isinstance(value, int) and not isinstance(dec, bool))


@pytest.mark.parametrize(
    "value", [0.0, -0.0, 1.5, 72.5, -(2.0**-100), 3.4028234663852886e38, float("-inf")]
)
def test_real_round_trip(value: float) -> None:
    dec, _ = codec.decode_application_value(codec.encode_value(value))
    assert dec == value
    dec, _ = codec.decode_application_value(codec.encode_value(value, "double"))
    assert isinstance(dec, Double) and dec == value


def test_real_nan_and_precision() -> None:
    dec, _ = codec.decode_application_value(codec.encode_value(float("nan")))
    assert math.isnan(dec)
    dec, _ = codec.decode_application_value(codec.encode_value(0.1))
    assert dec == pytest.approx(0.1, rel=1e-7) and dec != 0.1  # REAL is single precision
    dec, _ = codec.decode_application_value(codec.encode_value(0.1, "double"))
    assert dec == 0.1


def test_encode_value_tag_names_and_conversions() -> None:
    assert codec.encode_value(21, "real") == codec.encode_value(21.0)
    assert codec.encode_value("5", "unsigned") == codec.encode_value(5)
    assert codec.encode_value(5.0, "uint") == codec.encode_value(5)
    assert codec.encode_value(-5, "int") == codec.encode_value(-5)
    assert codec.encode_value(1, "enumerated") == codec.encode_value(Enumerated(1))
    assert codec.encode_value(1, "boolean") == b"\x11"
    assert codec.encode_value("false", "bool") == b"\x10"
    assert codec.encode_value(12, "character-string") == codec.encode_value("12")
    assert codec.encode_value(None, "null") == b"\x00"
    assert codec.encode_value("0102", "octet-string") == b"\x62\x01\x02"
    assert codec.encode_value("1010", "bit-string") == bytes.fromhex("8204a0")
    assert codec.encode_value((2024, 5, 17, 5), "date") == bytes.fromhex("a47c051105")
    assert codec.encode_value(("AI", 1), "object-identifier") == bytes.fromhex("c400000001")
    with pytest.raises(codec.CodecError):
        codec.encode_value(1.5, "unsigned")
    with pytest.raises(codec.CodecError):
        codec.encode_value(-1, "unsigned")
    with pytest.raises(codec.CodecError):
        codec.encode_value(1, "no-such-tag")
    with pytest.raises(codec.CodecError):
        codec.encode_value(object())
    with pytest.raises(codec.CodecError):
        codec.encode_value(1e39)  # does not fit a REAL
    with pytest.raises(codec.CodecError):
        codec.encode_value(ObjectId(0, 4194304))


def test_decode_values_constructed_and_context() -> None:
    data = (
        codec.encode_value(1.5)
        + codec.encode_opening_tag(0)
        + codec.encode_context_unsigned(1, 7)
        + codec.encode_value("in")
        + codec.encode_closing_tag(0)
        + codec.encode_value(None)
    )
    values, end = codec.decode_values(data)
    assert end == len(data)
    assert values[0] == 1.5
    assert values[1] == Constructed(0, [ContextValue(1, b"\x07"), "in"])
    assert values[2] is None
    with pytest.raises(codec.CodecError):
        codec.decode_values(codec.encode_opening_tag(0) + codec.encode_value(1))
    with pytest.raises(codec.CodecError):
        codec.decode_values(codec.encode_closing_tag(2))


def test_decode_truncated() -> None:
    for bad in (b"\x44\x42\x91", b"\x75", b"\x75\xfe\x01", b"\xc4\x00", b"\x82"):
        with pytest.raises(codec.CodecError):
            codec.decode_application_value(bad)


# --- services ------------------------------------------------------------------------------------


def test_decode_i_am() -> None:
    apdu = codec.decode_apdu(codec.i_am(1234, 1476, 3, 260))
    assert apdu.pdu_type == codec.PDU_UNCONFIRMED_REQUEST and apdu.service == 0
    iam = codec.decode_i_am(apdu.data)
    assert (iam.device, iam.max_apdu, iam.segmentation, iam.vendor_id) == (1234, 1476, 3, 260)
    with pytest.raises(codec.CodecError):
        codec.decode_i_am(codec.encode_value(ObjectId(0, 1)) + codec.encode_value(1))


def test_decode_who_is() -> None:
    assert codec.decode_who_is(codec.decode_apdu(codec.who_is()).data) == (None, None)
    assert codec.decode_who_is(codec.decode_apdu(codec.who_is(5, 4194303)).data) == (
        5,
        4194303,
    )
    with pytest.raises(codec.CodecError):
        codec.who_is(5)


def test_read_property_request_and_ack_decode() -> None:
    apdu = codec.decode_apdu(codec.read_property(7, "analog-input", 1, "priority-array", 5))
    assert apdu.pdu_type == codec.PDU_CONFIRMED_REQUEST
    assert (apdu.invoke_id, apdu.service, apdu.max_apdu, apdu.max_segments) == (7, 12, 1476, None)
    assert not apdu.segmented_response_accepted
    ref = codec.decode_read_property_request(apdu.data)
    assert (ref.obj_type, ref.instance, ref.prop, ref.index) == (0, 1, 87, 5)

    values = codec.encode_value(None) * 15 + codec.encode_value(3.0)
    ack = codec.decode_apdu(codec.read_property_ack(9, "analog-output", 2, 87, values))
    assert ack.pdu_type == codec.PDU_COMPLEX_ACK and ack.invoke_id == 9
    rp = codec.decode_read_property_ack(ack.data)
    assert (rp.obj_type, rp.instance, rp.prop, rp.index) == (1, 2, 87, None)
    assert rp.values == [None] * 15 + [3.0]


def test_write_property_request_decode() -> None:
    apdu = codec.decode_apdu(
        codec.write_property(2, "analog-value", 3, "present-value", codec.encode_value(72.5), 8)
    )
    req = codec.decode_write_property_request(apdu.data)
    assert (req.obj_type, req.instance, req.prop, req.index, req.priority) == (2, 3, 85, None, 8)
    assert req.values == [72.5]
    assert req.value_bytes == codec.encode_value(72.5)
    with pytest.raises(codec.CodecError):
        codec.write_property(1, "av", 1, 85, b"\x00", priority=17)


def test_decode_error_reject_abort() -> None:
    err = codec.decode_apdu(bytes.fromhex("50010c9101911f"))
    assert (err.pdu_type, err.invoke_id, err.service) == (codec.PDU_ERROR, 1, 12)
    assert (err.error_class, err.error_code) == (1, 31)
    # wrapped form ([0] opening/closing) used by some services
    wrapped = bytes.fromhex("5002100e9102912a0f")
    assert codec.decode_apdu(wrapped).error_code == 42
    rej = codec.decode_apdu(bytes.fromhex("600609"))
    assert (rej.pdu_type, rej.invoke_id, rej.reason) == (codec.PDU_REJECT, 6, 9)
    ab = codec.decode_apdu(bytes.fromhex("710504"))
    assert (ab.pdu_type, ab.invoke_id, ab.reason, ab.server) == (codec.PDU_ABORT, 5, 4, True)
    ack = codec.decode_apdu(codec.encode_simple_ack(4, 15))
    assert (ack.pdu_type, ack.invoke_id, ack.service) == (codec.PDU_SIMPLE_ACK, 4, 15)


def test_segmented_complex_ack_header() -> None:
    raw = bytes([0x3C, 0x11, 0x00, 0x04, 0x0C]) + b"\x0c\x02\x00\x00\x01"
    apdu = codec.decode_apdu(raw)
    assert apdu.segmented and apdu.more_follows
    assert (apdu.invoke_id, apdu.sequence_number, apdu.window_size, apdu.service) == (
        0x11,
        0,
        4,
        12,
    )


def test_max_segs_max_apdu_octet() -> None:
    assert codec.encode_max_segs_max_apdu(None, 1476) == 0x05
    assert codec.encode_max_segs_max_apdu(None, 1024) == 0x04
    assert codec.encode_max_segs_max_apdu(None, 480) == 0x03
    assert codec.encode_max_segs_max_apdu(None, 50) == 0x00
    assert codec.encode_max_segs_max_apdu(64, 1476) == 0x65
    assert codec.encode_max_segs_max_apdu(100, 1476) == 0x75
    req = codec.encode_confirmed_request(1, 12, b"", 480, 16, segmented_response_accepted=True)
    assert req.hex() == "0243010c"
    apdu = codec.decode_apdu(req)
    assert apdu.segmented_response_accepted and apdu.max_segments == 16 and apdu.max_apdu == 480


# --- BVLC / NPDU ----------------------------------------------------------------------------------


def test_bvlc_unicast_broadcast_forwarded() -> None:
    npdu = bytes.fromhex("0100") + codec.who_is()
    b = codec.decode_bvlc(codec.encode_bvlc(npdu))
    assert (b.function, b.npdu, b.origin) == (codec.BVLC_ORIGINAL_UNICAST_NPDU, npdu, None)
    b = codec.decode_bvlc(codec.encode_bvlc(npdu, broadcast=True))
    assert b.function == codec.BVLC_ORIGINAL_BROADCAST_NPDU
    fwd = codec.encode_bvlc_forwarded(npdu, ("192.168.10.51", 47808))
    assert fwd[:10].hex() == "8104000e" + "c0a80a33" + "bac0"
    b = codec.decode_bvlc(fwd)
    assert (b.function, b.origin, b.npdu) == (
        codec.BVLC_FORWARDED_NPDU,
        ("192.168.10.51", 47808),
        npdu,
    )
    res = codec.decode_bvlc(bytes.fromhex("810000060030"))
    assert res.function == codec.BVLC_RESULT and res.result_code == 0x30
    assert codec.encode_bvlc_register_foreign_device(60).hex() == "81050006003c"
    for bad in (b"", b"\x82\x0a\x00\x04", bytes.fromhex("810a0010")):
        with pytest.raises(codec.CodecError):
            codec.decode_bvlc(bad)


def test_npdu_encode_decode() -> None:
    assert codec.encode_npdu(expecting_reply=True).hex() == "0104"
    assert codec.encode_npdu().hex() == "0100"
    routed = codec.encode_npdu(True, dnet=5, dadr=b"\x07", hop_count=255)
    assert routed.hex() == "0124" + "000501" + "07" + "ff"
    n = codec.decode_npdu(routed + b"\x10\x08")
    assert (n.dnet, n.dadr, n.hop_count, n.expecting_reply, n.apdu) == (
        5,
        b"\x07",
        255,
        True,
        b"\x10\x08",
    )
    # from a remote network: SNET 2, SADR 0x0a
    src = codec.decode_npdu(bytes.fromhex("01080002010a") + b"\x20\x01\x0f")
    assert (src.snet, src.sadr, src.dnet, src.apdu) == (2, b"\x0a", None, b"\x20\x01\x0f")
    msg = codec.decode_npdu(bytes.fromhex("0180010005"))
    assert msg.is_network_message and msg.message_type == 1
    with pytest.raises(codec.CodecError):
        codec.decode_npdu(b"\x02\x00")


def test_property_value_tag() -> None:
    tag = codec.property_value_tag
    assert tag("analog-value", "present-value") == "real"
    assert tag("analog-input", "present-value") == "real"
    assert tag("binary-output", "present-value") == "enumerated"
    assert tag("binary-value", "relinquish-default") == "enumerated"
    assert tag("multi-state-value", "present-value") == "unsigned"
    assert tag("large-analog-value", "present-value") == "double"
    assert tag("integer-value", "present-value") == "signed"
    assert tag("analog-input", "out-of-service") == "boolean"
    assert tag("analog-input", "units") == "enumerated"
    assert tag("analog-input", "cov-increment") == "real"
    assert tag("device", "object-name") == "character-string"
    assert tag("device", "number-of-apdu-retries") == "unsigned"
    assert tag("device", "object-list") == "object-identifier"
    assert tag("analog-input", "status-flags") == "bit-string"
    assert tag("device", "no-such-property") is None
    assert tag("schedule", "present-value") is None


def test_to_jsonable() -> None:
    value = [
        ObjectId(0, 1),
        BitString([1, 0]),
        Enumerated(3),
        Date(2024, 1, 2, None),
        Time(1, 2, 3, 4),
        b"\x01",
        float("inf"),
        Double(1.5),
        None,
    ]
    assert codec.to_jsonable(value) == [
        "analog-input:1",
        [True, False],
        3,
        "2024-01-02",
        "01:02:03.04",
        "01",
        "inf",
        1.5,
        None,
    ]
