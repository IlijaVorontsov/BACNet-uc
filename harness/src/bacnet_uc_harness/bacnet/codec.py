# SPDX-License-Identifier: Apache-2.0
"""BACnet/IP encoding and decoding (ASHRAE 135 clauses 20, 6 and Annex J).

Layers, outermost first:

- BVLC (Annex J): ``0x81 <function> <length:2>``; original unicast (0x0A),
  original broadcast (0x0B) and forwarded NPDU (0x04, carries the original
  source B/IP address).
- NPDU (clause 6.2): version 1, control octet (expecting-reply bit 2,
  DNET bit 5, SNET bit 3, network-layer-message bit 7).
- APDU (clause 20.1): confirmed request, unconfirmed request, simple ack,
  complex ack, error, reject, abort.
- Tagged values (clause 20.2): application tags 0..12, context tags,
  opening/closing tags.

Python value mapping of the application datatypes:

| BACnet | Python |
|--------|--------|
| NULL | ``None`` |
| BOOLEAN | ``bool`` |
| Unsigned | ``int`` (>= 0) |
| Signed | ``int`` |
| REAL / Double | ``float`` (``Double`` wrapper to force Double on encode) |
| Octet String | ``bytes`` |
| Character String | ``str`` (UTF-8 on encode) |
| Bit String | :class:`BitString` (tuple of bool) |
| Enumerated | :class:`Enumerated` (int subclass) |
| Date | :class:`Date` (``None`` = unspecified field) |
| Time | :class:`Time` (``None`` = unspecified field) |
| BACnetObjectIdentifier | :class:`ObjectId` (named tuple ``(type, instance)``) |

Context-tagged primitives inside constructed data decode to
:class:`ContextValue` (raw bytes), opening/closing groups to
:class:`Constructed`.
"""

from __future__ import annotations

import ipaddress
import math
import struct
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from bacnet_uc_harness.bacnet import enums
from bacnet_uc_harness.errors import HarnessError

# --- application tag numbers (clause 20.2.1.4) --------------------------------------------
TAG_NULL = 0
TAG_BOOLEAN = 1
TAG_UNSIGNED = 2
TAG_SIGNED = 3
TAG_REAL = 4
TAG_DOUBLE = 5
TAG_OCTET_STRING = 6
TAG_CHARACTER_STRING = 7
TAG_BIT_STRING = 8
TAG_ENUMERATED = 9
TAG_DATE = 10
TAG_TIME = 11
TAG_OBJECT_ID = 12

TAG_NAMES: dict[str, int] = {
    "null": TAG_NULL,
    "boolean": TAG_BOOLEAN,
    "unsigned": TAG_UNSIGNED,
    "signed": TAG_SIGNED,
    "real": TAG_REAL,
    "double": TAG_DOUBLE,
    "octet-string": TAG_OCTET_STRING,
    "character-string": TAG_CHARACTER_STRING,
    "bit-string": TAG_BIT_STRING,
    "enumerated": TAG_ENUMERATED,
    "date": TAG_DATE,
    "time": TAG_TIME,
    "object-identifier": TAG_OBJECT_ID,
}
TAG_NUMBER_NAMES: dict[int, str] = {v: k for k, v in TAG_NAMES.items()}

# accepted spellings for encode_value(tag=...)
_TAG_ALIASES: dict[str, str] = {
    "bool": "boolean",
    "uint": "unsigned",
    "unsigned-int": "unsigned",
    "unsigned-integer": "unsigned",
    "int": "signed",
    "signed-int": "signed",
    "integer": "signed",
    "float": "real",
    "enum": "enumerated",
    "str": "character-string",
    "string": "character-string",
    "charstring": "character-string",
    "characterstring": "character-string",
    "octetstring": "octet-string",
    "bytes": "octet-string",
    "bitstring": "bit-string",
    "objectid": "object-identifier",
    "object-id": "object-identifier",
    "oid": "object-identifier",
}

# character sets of CharacterString (clause 20.2.9)
CHARSET_UTF8 = 0
CHARSET_DBCS = 1
CHARSET_JIS_X_0208 = 2
CHARSET_UCS4 = 3
CHARSET_UCS2 = 4
CHARSET_ISO_8859_1 = 5

# --- BVLC (Annex J) ----------------------------------------------------------------------
BVLC_TYPE_BIP = 0x81
BVLC_RESULT = 0x00
BVLC_WRITE_BDT = 0x01
BVLC_READ_BDT = 0x02
BVLC_READ_BDT_ACK = 0x03
BVLC_FORWARDED_NPDU = 0x04
BVLC_REGISTER_FOREIGN_DEVICE = 0x05
BVLC_READ_FDT = 0x06
BVLC_READ_FDT_ACK = 0x07
BVLC_DELETE_FDT_ENTRY = 0x08
BVLC_DISTRIBUTE_BROADCAST_TO_NETWORK = 0x09
BVLC_ORIGINAL_UNICAST_NPDU = 0x0A
BVLC_ORIGINAL_BROADCAST_NPDU = 0x0B
BVLC_SECURE_BVLL = 0x0C

# --- NPDU (clause 6.2) -------------------------------------------------------------------
NPDU_VERSION = 0x01
NPDU_NETWORK_MESSAGE = 0x80
NPDU_DNET_PRESENT = 0x20
NPDU_SNET_PRESENT = 0x08
NPDU_EXPECTING_REPLY = 0x04
NPDU_PRIORITY_MASK = 0x03

# --- APDU (clause 20.1) ------------------------------------------------------------------
PDU_CONFIRMED_REQUEST = 0
PDU_UNCONFIRMED_REQUEST = 1
PDU_SIMPLE_ACK = 2
PDU_COMPLEX_ACK = 3
PDU_SEGMENT_ACK = 4
PDU_ERROR = 5
PDU_REJECT = 6
PDU_ABORT = 7

PDU_FLAG_SEGMENTED = 0x08
PDU_FLAG_MORE_FOLLOWS = 0x04
PDU_FLAG_SEGMENTED_RESPONSE_ACCEPTED = 0x02  # confirmed request only
PDU_FLAG_SERVER = 0x01  # abort / segment ack

# Max-APDU-length-accepted code (bits 3..0 of octet 1 of a confirmed request)
MAX_APDU_BY_CODE: dict[int, int] = {0: 50, 1: 128, 2: 206, 3: 480, 4: 1024, 5: 1476}
# Max-segments-accepted code (bits 6..4); None = unspecified, 65 = "more than 64"
MAX_SEGMENTS_BY_CODE: dict[int, int | None] = {
    0: None,
    1: 2,
    2: 4,
    3: 8,
    4: 16,
    5: 32,
    6: 64,
    7: 65,
}

MAX_APDU_BIP = 1476
BACNET_MAX_PRIORITY = 16
BACNET_ARRAY_ALL = 0xFFFFFFFF


class CodecError(HarnessError, ValueError):
    """Malformed BACnet data or a value that cannot be encoded."""


# --- value types ------------------------------------------------------------------------


class Enumerated(int):
    """An ENUMERATED value (behaves as int, encodes with tag 9)."""

    def __repr__(self) -> str:
        return f"Enumerated({int(self)})"


class Double(float):
    """A float that encodes as Double (tag 5) instead of REAL."""

    def __repr__(self) -> str:
        return f"Double({float(self)!r})"


class BitString(tuple):
    """A BIT STRING: tuple of bools, bit 0 first."""

    def __new__(cls, bits: Any = ()) -> BitString:
        return super().__new__(cls, (bool(b) for b in bits))

    def __repr__(self) -> str:
        return f"BitString({''.join('1' if b else '0' for b in self)!r})"


class ObjectId(NamedTuple):
    """BACnetObjectIdentifier."""

    type: int
    instance: int

    def __str__(self) -> str:
        return enums.format_object_ref(self.type, self.instance)


class Date(NamedTuple):
    """Date; ``None`` fields are unspecified (encoded 255). ``year`` is the full
    year, ``weekday`` 1 = Monday .. 7 = Sunday. Month 13/14 and day 32..34 are
    the BACnet special values."""

    year: int | None
    month: int | None
    day: int | None
    weekday: int | None = None

    def __str__(self) -> str:
        def f(v: int | None, width: int) -> str:
            return "*" * width if v is None else f"{v:0{width}d}"

        return f"{f(self.year, 4)}-{f(self.month, 2)}-{f(self.day, 2)}"


class Time(NamedTuple):
    """Time of day; ``None`` fields are unspecified (encoded 255)."""

    hour: int | None
    minute: int | None
    second: int | None = 0
    hundredths: int | None = 0

    def __str__(self) -> str:
        def f(v: int | None) -> str:
            return "**" if v is None else f"{v:02d}"

        return f"{f(self.hour)}:{f(self.minute)}:{f(self.second)}.{f(self.hundredths)}"


@dataclass
class ContextValue:
    """A context-tagged primitive that was not interpreted (raw content)."""

    tag: int
    data: bytes


@dataclass
class Constructed:
    """Values between an opening and a closing context tag."""

    tag: int
    values: list[Any] = field(default_factory=list)


# --- tag encoding (clause 20.2.1) ----------------------------------------------------------


def encode_tag(tag_number: int, context: bool, length: int) -> bytes:
    """Tag octet(s) for a primitive with ``length`` content octets.

    For an application BOOLEAN pass the value (0/1) as ``length``.
    """
    if tag_number < 0 or tag_number > 254:
        raise CodecError(f"tag number out of range: {tag_number}")
    if length < 0 or length > 0xFFFFFFFF:
        raise CodecError(f"tag length out of range: {length}")
    out = bytearray()
    first = 0x08 if context else 0x00
    if tag_number <= 14:
        first |= tag_number << 4
        ext_tag = None
    else:
        first |= 0xF0
        ext_tag = tag_number
    if length <= 4:
        first |= length
        out.append(first)
        if ext_tag is not None:
            out.append(ext_tag)
        return bytes(out)
    first |= 5
    out.append(first)
    if ext_tag is not None:
        out.append(ext_tag)
    if length <= 253:
        out.append(length)
    elif length <= 0xFFFF:
        out.append(254)
        out += struct.pack(">H", length)
    else:
        out.append(255)
        out += struct.pack(">I", length)
    return bytes(out)


def _open_close(tag_number: int, lvt: int) -> bytes:
    if tag_number <= 14:
        return bytes([(tag_number << 4) | 0x08 | lvt])
    if tag_number > 254:
        raise CodecError(f"tag number out of range: {tag_number}")
    return bytes([0xF8 | lvt, tag_number])


def encode_opening_tag(tag_number: int) -> bytes:
    return _open_close(tag_number, 6)


def encode_closing_tag(tag_number: int) -> bytes:
    return _open_close(tag_number, 7)


# --- primitive contents ------------------------------------------------------------------


def unsigned_content(value: int) -> bytes:
    if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
        raise CodecError(f"unsigned value out of range: {value}")
    n = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(n, "big")


def signed_content(value: int) -> bytes:
    if value < -(1 << 63) or value >= (1 << 63):
        raise CodecError(f"signed value out of range: {value}")
    n = 1
    while not -(1 << (8 * n - 1)) <= value < (1 << (8 * n - 1)):
        n += 1
    return value.to_bytes(n, "big", signed=True)


def real_content(value: float) -> bytes:
    try:
        return struct.pack(">f", value)
    except OverflowError:
        raise CodecError(f"value does not fit a REAL: {value}") from None


def double_content(value: float) -> bytes:
    return struct.pack(">d", value)


def character_string_content(value: str, charset: int = CHARSET_UTF8) -> bytes:
    if charset == CHARSET_UTF8:
        data = value.encode("utf-8")
    elif charset == CHARSET_UCS2:
        data = value.encode("utf-16-be")
    elif charset == CHARSET_UCS4:
        data = value.encode("utf-32-be")
    elif charset == CHARSET_ISO_8859_1:
        data = value.encode("latin-1")
    else:
        raise CodecError(f"unsupported character set {charset}")
    return bytes([charset]) + data


def bit_string_content(bits: Any) -> bytes:
    bits = [bool(b) for b in bits]
    if not bits:
        return b"\x00"
    nbytes = (len(bits) + 7) // 8
    unused = nbytes * 8 - len(bits)
    out = bytearray(nbytes)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 0x80 >> (i % 8)
    return bytes([unused]) + bytes(out)


def _byte_or_unspecified(v: int | None) -> int:
    if v is None:
        return 255
    if not 0 <= v <= 255:
        raise CodecError(f"date/time field out of range: {v}")
    return v


def date_content(value: Date) -> bytes:
    year = value.year
    if year is None:
        yb = 255
    else:
        yb = year - 1900 if year >= 1900 else year
        if not 0 <= yb <= 254:
            raise CodecError(f"year out of range: {year}")
    return bytes(
        [
            yb,
            _byte_or_unspecified(value.month),
            _byte_or_unspecified(value.day),
            _byte_or_unspecified(value.weekday),
        ]
    )


def time_content(value: Time) -> bytes:
    return bytes(_byte_or_unspecified(v) for v in value)


def object_id_content(obj_type: int, instance: int) -> bytes:
    if not 0 <= obj_type <= enums.MAX_OBJECT_TYPE:
        raise CodecError(f"object type out of range: {obj_type}")
    if not 0 <= instance <= enums.MAX_INSTANCE:
        raise CodecError(f"object instance out of range: {instance}")
    return struct.pack(">I", (obj_type << 22) | instance)


# --- application-tagged encoders ------------------------------------------------------------


def _app(tag: int, content: bytes) -> bytes:
    return encode_tag(tag, False, len(content)) + content


def encode_application_null() -> bytes:
    return b"\x00"


def encode_application_boolean(value: bool) -> bytes:
    return bytes([0x11 if value else 0x10])


def encode_application_unsigned(value: int) -> bytes:
    return _app(TAG_UNSIGNED, unsigned_content(value))


def encode_application_signed(value: int) -> bytes:
    return _app(TAG_SIGNED, signed_content(value))


def encode_application_real(value: float) -> bytes:
    return _app(TAG_REAL, real_content(value))


def encode_application_double(value: float) -> bytes:
    return _app(TAG_DOUBLE, double_content(value))


def encode_application_octet_string(value: bytes) -> bytes:
    return _app(TAG_OCTET_STRING, bytes(value))


def encode_application_character_string(value: str, charset: int = CHARSET_UTF8) -> bytes:
    return _app(TAG_CHARACTER_STRING, character_string_content(value, charset))


def encode_application_bit_string(bits: Any) -> bytes:
    return _app(TAG_BIT_STRING, bit_string_content(bits))


def encode_application_enumerated(value: int) -> bytes:
    return _app(TAG_ENUMERATED, unsigned_content(int(value)))


def encode_application_date(value: Date) -> bytes:
    return _app(TAG_DATE, date_content(value))


def encode_application_time(value: Time) -> bytes:
    return _app(TAG_TIME, time_content(value))


def encode_application_object_id(obj_type: int | str, instance: int) -> bytes:
    return _app(TAG_OBJECT_ID, object_id_content(enums.object_type_number(obj_type), instance))


# --- context-tagged encoders ---------------------------------------------------------------


def _ctx(tag_number: int, content: bytes) -> bytes:
    return encode_tag(tag_number, True, len(content)) + content


def encode_context_unsigned(tag_number: int, value: int) -> bytes:
    return _ctx(tag_number, unsigned_content(value))


def encode_context_signed(tag_number: int, value: int) -> bytes:
    return _ctx(tag_number, signed_content(value))


def encode_context_enumerated(tag_number: int, value: int) -> bytes:
    return _ctx(tag_number, unsigned_content(int(value)))


def encode_context_boolean(tag_number: int, value: bool) -> bytes:
    # context BOOLEAN has one content octet (clause 20.2.3)
    return _ctx(tag_number, b"\x01" if value else b"\x00")


def encode_context_real(tag_number: int, value: float) -> bytes:
    return _ctx(tag_number, real_content(value))


def encode_context_double(tag_number: int, value: float) -> bytes:
    return _ctx(tag_number, double_content(value))


def encode_context_octet_string(tag_number: int, value: bytes) -> bytes:
    return _ctx(tag_number, bytes(value))


def encode_context_character_string(tag_number: int, value: str) -> bytes:
    return _ctx(tag_number, character_string_content(value))


def encode_context_bit_string(tag_number: int, bits: Any) -> bytes:
    return _ctx(tag_number, bit_string_content(bits))


def encode_context_object_id(tag_number: int, obj_type: int | str, instance: int) -> bytes:
    return _ctx(tag_number, object_id_content(enums.object_type_number(obj_type), instance))


# --- generic value encoder ------------------------------------------------------------------


def normalize_tag_name(tag: str | int) -> int:
    """Application tag number of a tag name (``"real"``, ``"uint"``, ...) or number."""
    if isinstance(tag, int) and not isinstance(tag, bool):
        if tag not in TAG_NUMBER_NAMES:
            raise CodecError(f"unknown application tag {tag}")
        return tag
    key = str(tag).strip().lower().replace("_", "-").replace(" ", "-")
    key = _TAG_ALIASES.get(key, key)
    try:
        return TAG_NAMES[key]
    except KeyError:
        raise CodecError(f"unknown application tag name {tag!r}") from None


def _as_object_id(value: Any) -> ObjectId:
    if isinstance(value, str):
        t, i = enums.parse_object_ref(value)
        return ObjectId(t, i)
    if isinstance(value, tuple | list) and len(value) == 2:
        return ObjectId(enums.object_type_number(value[0]), int(value[1]))
    raise CodecError(f"not an object identifier: {value!r}")


def encode_value(value: Any, tag: str | int | None = None) -> bytes:
    """Encode one application-tagged value.

    With ``tag`` (name or number) the value is converted to that datatype
    (``"real"``, ``"double"``, ``"unsigned"``, ``"signed"``, ``"enumerated"``,
    ``"boolean"``, ``"null"``, ``"character-string"``, ``"octet-string"``,
    ``"bit-string"``, ``"date"``, ``"time"``, ``"object-identifier"``). Without
    it the datatype follows the Python type: None -> NULL, bool -> BOOLEAN,
    Enumerated -> ENUMERATED, int -> Unsigned (>= 0) or Signed, Double ->
    Double, float -> REAL, str -> CharacterString, bytes -> OctetString,
    BitString -> BIT STRING, ObjectId -> object identifier, Date, Time.
    """
    if tag is None:
        if value is None:
            return encode_application_null()
        if isinstance(value, bool):
            return encode_application_boolean(value)
        if isinstance(value, Enumerated):
            return encode_application_enumerated(value)
        if isinstance(value, int):
            if value >= 0:
                return encode_application_unsigned(value)
            return encode_application_signed(value)
        if isinstance(value, Double):
            return encode_application_double(value)
        if isinstance(value, float):
            return encode_application_real(value)
        if isinstance(value, str):
            return encode_application_character_string(value)
        if isinstance(value, bytes | bytearray | memoryview):
            return encode_application_octet_string(bytes(value))
        if isinstance(value, BitString):
            return encode_application_bit_string(value)
        if isinstance(value, ObjectId):
            return encode_application_object_id(value.type, value.instance)
        if isinstance(value, Date):
            return encode_application_date(value)
        if isinstance(value, Time):
            return encode_application_time(value)
        raise CodecError(f"cannot encode {type(value).__name__} without an explicit tag")

    number = normalize_tag_name(tag)
    try:
        if number == TAG_NULL:
            return encode_application_null()
        if number == TAG_BOOLEAN:
            return encode_application_boolean(_to_bool(value))
        if number == TAG_UNSIGNED:
            return encode_application_unsigned(_to_int(value))
        if number == TAG_SIGNED:
            return encode_application_signed(_to_int(value))
        if number == TAG_REAL:
            return encode_application_real(float(value))
        if number == TAG_DOUBLE:
            return encode_application_double(float(value))
        if number == TAG_OCTET_STRING:
            if isinstance(value, str):
                return encode_application_octet_string(bytes.fromhex(value))
            return encode_application_octet_string(bytes(value))
        if number == TAG_CHARACTER_STRING:
            return encode_application_character_string(str(value))
        if number == TAG_BIT_STRING:
            if isinstance(value, str):
                value = [c == "1" for c in value if c in "01"]
            return encode_application_bit_string(value)
        if number == TAG_ENUMERATED:
            return encode_application_enumerated(_to_int(value))
        if number == TAG_DATE:
            return encode_application_date(value if isinstance(value, Date) else Date(*value))
        if number == TAG_TIME:
            return encode_application_time(value if isinstance(value, Time) else Time(*value))
        oid = _as_object_id(value)
        return encode_application_object_id(oid.type, oid.instance)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CodecError):
            raise
        raise CodecError(f"cannot encode {value!r} as {TAG_NUMBER_NAMES[number]}: {exc}") from exc


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise CodecError(f"not an integral value: {value!r}")
        return int(value)
    if isinstance(value, str):
        return int(value.strip(), 0)
    raise CodecError(f"not an integer: {value!r}")


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "on", "yes", "active"):
            return True
        if text in ("false", "0", "off", "no", "inactive"):
            return False
        raise CodecError(f"not a boolean: {value!r}")
    return bool(value)


# --- tag decoding ------------------------------------------------------------------------


@dataclass
class TagHeader:
    """A decoded tag.

    ``length`` is the number of content octets (for an application BOOLEAN the
    value itself, with no content octets); 0 for opening/closing tags.
    """

    number: int
    context: bool
    length: int
    header_len: int
    opening: bool = False
    closing: bool = False

    @property
    def content_len(self) -> int:
        if self.opening or self.closing:
            return 0
        if not self.context and self.number == TAG_BOOLEAN:
            return 0
        return self.length


def decode_tag(buf: bytes, offset: int = 0) -> TagHeader:
    """Decode the tag at ``offset``."""
    try:
        first = buf[offset]
        i = offset + 1
        number = first >> 4
        if number == 15:
            number = buf[i]
            i += 1
        context = bool(first & 0x08)
        lvt = first & 0x07
        if context and lvt == 6:
            return TagHeader(number, True, 0, i - offset, opening=True)
        if context and lvt == 7:
            return TagHeader(number, True, 0, i - offset, closing=True)
        if lvt == 5:
            ext = buf[i]
            i += 1
            if ext == 254:
                length = struct.unpack_from(">H", buf, i)[0]
                i += 2
            elif ext == 255:
                length = struct.unpack_from(">I", buf, i)[0]
                i += 4
            else:
                length = ext
        else:
            length = lvt
    except (IndexError, struct.error):
        raise CodecError(f"truncated tag at offset {offset}") from None
    return TagHeader(number, context, length, i - offset)


def _content(buf: bytes, offset: int, hdr: TagHeader) -> tuple[bytes, int]:
    start = offset + hdr.header_len
    end = start + hdr.content_len
    if end > len(buf):
        raise CodecError(f"truncated value at offset {offset}")
    return bytes(buf[start:end]), end


def decode_unsigned(content: bytes) -> int:
    if not content:
        raise CodecError("empty unsigned")
    return int.from_bytes(content, "big")


def decode_signed(content: bytes) -> int:
    if not content:
        raise CodecError("empty signed")
    return int.from_bytes(content, "big", signed=True)


def decode_real(content: bytes) -> float:
    if len(content) != 4:
        raise CodecError(f"REAL with {len(content)} octets")
    return struct.unpack(">f", content)[0]


def decode_double(content: bytes) -> float:
    if len(content) != 8:
        raise CodecError(f"Double with {len(content)} octets")
    return struct.unpack(">d", content)[0]


def decode_character_string(content: bytes) -> str:
    if not content:
        raise CodecError("empty CharacterString")
    charset, data = content[0], content[1:]
    if charset == CHARSET_UTF8:
        return data.decode("utf-8", errors="replace")
    if charset == CHARSET_UCS2:
        return data.decode("utf-16-be", errors="replace")
    if charset == CHARSET_UCS4:
        return data.decode("utf-32-be", errors="replace")
    return data.decode("latin-1")


def decode_bit_string(content: bytes) -> BitString:
    if not content:
        raise CodecError("empty BIT STRING")
    unused = content[0]
    data = content[1:]
    if unused > 7 or (not data and unused):
        raise CodecError(f"invalid unused-bits count {unused}")
    nbits = len(data) * 8 - unused
    return BitString(bool(data[i // 8] & (0x80 >> (i % 8))) for i in range(nbits))


def _unspec(v: int) -> int | None:
    return None if v == 255 else v


def decode_date(content: bytes) -> Date:
    if len(content) != 4:
        raise CodecError(f"Date with {len(content)} octets")
    y, m, d, w = content
    return Date(None if y == 255 else 1900 + y, _unspec(m), _unspec(d), _unspec(w))


def decode_time(content: bytes) -> Time:
    if len(content) != 4:
        raise CodecError(f"Time with {len(content)} octets")
    return Time(*(_unspec(v) for v in content))


def decode_object_id(content: bytes) -> ObjectId:
    if len(content) != 4:
        raise CodecError(f"object identifier with {len(content)} octets")
    raw = struct.unpack(">I", content)[0]
    return ObjectId(raw >> 22, raw & 0x3FFFFF)


def decode_application_content(tag: int, content: bytes, length: int = 0) -> Any:
    """Python value of an application tag's content (``length`` is the LVT,
    used for BOOLEAN)."""
    if tag == TAG_NULL:
        return None
    if tag == TAG_BOOLEAN:
        return bool(length)
    if tag == TAG_UNSIGNED:
        return decode_unsigned(content)
    if tag == TAG_SIGNED:
        return decode_signed(content)
    if tag == TAG_REAL:
        return decode_real(content)
    if tag == TAG_DOUBLE:
        return Double(decode_double(content))
    if tag == TAG_OCTET_STRING:
        return content
    if tag == TAG_CHARACTER_STRING:
        return decode_character_string(content)
    if tag == TAG_BIT_STRING:
        return decode_bit_string(content)
    if tag == TAG_ENUMERATED:
        return Enumerated(decode_unsigned(content))
    if tag == TAG_DATE:
        return decode_date(content)
    if tag == TAG_TIME:
        return decode_time(content)
    if tag == TAG_OBJECT_ID:
        return decode_object_id(content)
    raise CodecError(f"reserved application tag {tag}")


def decode_application_value(buf: bytes, offset: int = 0) -> tuple[Any, int]:
    """Decode one application-tagged value; returns ``(value, next_offset)``."""
    hdr = decode_tag(buf, offset)
    if hdr.context:
        raise CodecError(f"expected an application tag at offset {offset}")
    content, end = _content(buf, offset, hdr)
    return decode_application_content(hdr.number, content, hdr.length), end


def decode_values(
    buf: bytes, offset: int = 0, end: int | None = None, closing_tag: int | None = None
) -> tuple[list[Any], int]:
    """Decode a sequence of tagged values.

    Stops at ``end`` or, if ``closing_tag`` is given, at that closing tag (the
    returned offset points behind it). Nested opening/closing groups become
    :class:`Constructed`, context primitives :class:`ContextValue`.
    """
    stop = len(buf) if end is None else end
    values: list[Any] = []
    i = offset
    while i < stop:
        hdr = decode_tag(buf, i)
        if hdr.closing:
            if closing_tag is not None and hdr.number == closing_tag:
                return values, i + hdr.header_len
            raise CodecError(f"unexpected closing tag {hdr.number} at offset {i}")
        if hdr.opening:
            inner, i = decode_values(buf, i + hdr.header_len, stop, closing_tag=hdr.number)
            values.append(Constructed(hdr.number, inner))
            continue
        content, nxt = _content(buf, i, hdr)
        if hdr.context:
            values.append(ContextValue(hdr.number, content))
        else:
            values.append(decode_application_content(hdr.number, content, hdr.length))
        i = nxt
    if closing_tag is not None:
        raise CodecError(f"missing closing tag {closing_tag}")
    return values, i


def _expect_context(buf: bytes, offset: int, number: int) -> tuple[bytes, int]:
    hdr = decode_tag(buf, offset)
    if not hdr.context or hdr.opening or hdr.closing or hdr.number != number:
        raise CodecError(f"expected context tag [{number}] at offset {offset}")
    return _content(buf, offset, hdr)


def _peek_context(buf: bytes, offset: int, number: int) -> bool:
    if offset >= len(buf):
        return False
    hdr = decode_tag(buf, offset)
    return hdr.context and not hdr.opening and not hdr.closing and hdr.number == number


def _expect_opening(buf: bytes, offset: int, number: int) -> int:
    hdr = decode_tag(buf, offset)
    if not hdr.opening or hdr.number != number:
        raise CodecError(f"expected opening tag [{number}] at offset {offset}")
    return offset + hdr.header_len


# --- BVLC ----------------------------------------------------------------------------------


@dataclass
class Bvlc:
    """A decoded BVLL message."""

    function: int
    npdu: bytes = b""
    origin: tuple[str, int] | None = None  # forwarded NPDU: original source
    result_code: int | None = None  # BVLC-Result


def encode_bvlc(npdu: bytes, broadcast: bool = False) -> bytes:
    """Original-Unicast-NPDU (0x0A) or Original-Broadcast-NPDU (0x0B)."""
    function = BVLC_ORIGINAL_BROADCAST_NPDU if broadcast else BVLC_ORIGINAL_UNICAST_NPDU
    return struct.pack(">BBH", BVLC_TYPE_BIP, function, len(npdu) + 4) + npdu


def encode_bvlc_forwarded(npdu: bytes, origin: tuple[str, int]) -> bytes:
    """Forwarded-NPDU (0x04) with the original source B/IP address."""
    addr = ipaddress.IPv4Address(origin[0]).packed + struct.pack(">H", origin[1])
    return struct.pack(">BBH", BVLC_TYPE_BIP, BVLC_FORWARDED_NPDU, len(npdu) + 10) + addr + npdu


def encode_bvlc_register_foreign_device(ttl: int) -> bytes:
    """Register-Foreign-Device (0x05) with a time-to-live in seconds."""
    return struct.pack(">BBHH", BVLC_TYPE_BIP, BVLC_REGISTER_FOREIGN_DEVICE, 6, ttl)


def decode_bvlc(data: bytes) -> Bvlc:
    if len(data) < 4 or data[0] != BVLC_TYPE_BIP:
        raise CodecError("not a BACnet/IP BVLL message")
    function = data[1]
    length = struct.unpack_from(">H", data, 2)[0]
    if length < 4 or length > len(data):
        raise CodecError(f"BVLC length {length} does not match datagram ({len(data)})")
    body = bytes(data[4:length])
    if function in (
        BVLC_ORIGINAL_UNICAST_NPDU,
        BVLC_ORIGINAL_BROADCAST_NPDU,
        BVLC_DISTRIBUTE_BROADCAST_TO_NETWORK,
    ):
        return Bvlc(function, body)
    if function == BVLC_FORWARDED_NPDU:
        if len(body) < 6:
            raise CodecError("truncated Forwarded-NPDU")
        host = str(ipaddress.IPv4Address(body[:4]))
        port = struct.unpack_from(">H", body, 4)[0]
        return Bvlc(function, body[6:], origin=(host, port))
    if function == BVLC_RESULT:
        code = struct.unpack_from(">H", body, 0)[0] if len(body) >= 2 else None
        return Bvlc(function, b"", result_code=code)
    return Bvlc(function, body)


# --- NPDU ----------------------------------------------------------------------------------


@dataclass
class Npdu:
    control: int
    apdu: bytes = b""
    dnet: int | None = None
    dadr: bytes = b""
    snet: int | None = None
    sadr: bytes = b""
    hop_count: int | None = None
    message_type: int | None = None  # network layer message
    vendor_id: int | None = None

    @property
    def expecting_reply(self) -> bool:
        return bool(self.control & NPDU_EXPECTING_REPLY)

    @property
    def priority(self) -> int:
        return self.control & NPDU_PRIORITY_MASK

    @property
    def is_network_message(self) -> bool:
        return bool(self.control & NPDU_NETWORK_MESSAGE)


def encode_npdu(
    expecting_reply: bool = False,
    priority: int = 0,
    dnet: int | None = None,
    dadr: bytes = b"",
    hop_count: int = 255,
) -> bytes:
    """NPDU header for an APDU (append the APDU to the result)."""
    control = priority & NPDU_PRIORITY_MASK
    if expecting_reply:
        control |= NPDU_EXPECTING_REPLY
    out = bytearray([NPDU_VERSION, control])
    if dnet is not None:
        out[1] |= NPDU_DNET_PRESENT
        out += struct.pack(">HB", dnet, len(dadr)) + bytes(dadr)
        out.append(hop_count & 0xFF)
    return bytes(out)


def decode_npdu(data: bytes) -> Npdu:
    if len(data) < 2 or data[0] != NPDU_VERSION:
        raise CodecError("not a BACnet NPDU (version != 1)")
    control = data[1]
    npdu = Npdu(control)
    i = 2
    try:
        if control & NPDU_DNET_PRESENT:
            npdu.dnet, dlen = struct.unpack_from(">HB", data, i)
            i += 3
            npdu.dadr = bytes(data[i : i + dlen])
            i += dlen
        if control & NPDU_SNET_PRESENT:
            npdu.snet, slen = struct.unpack_from(">HB", data, i)
            i += 3
            npdu.sadr = bytes(data[i : i + slen])
            i += slen
        if control & NPDU_DNET_PRESENT:
            npdu.hop_count = data[i]
            i += 1
        if control & NPDU_NETWORK_MESSAGE:
            npdu.message_type = data[i]
            i += 1
            if npdu.message_type >= 0x80:
                npdu.vendor_id = struct.unpack_from(">H", data, i)[0]
                i += 2
    except (IndexError, struct.error):
        raise CodecError("truncated NPDU") from None
    npdu.apdu = bytes(data[i:])
    return npdu


# --- APDU ----------------------------------------------------------------------------------


def encode_max_segs_max_apdu(max_segments: int | None, max_apdu: int) -> int:
    """Octet 1 of a confirmed request (clause 20.1.2.4 / 20.1.2.5)."""
    seg_code = 0
    if max_segments is not None and max_segments > 0:
        seg_code = 7
        for code, n in MAX_SEGMENTS_BY_CODE.items():
            if n is not None and max_segments <= n:
                seg_code = code
                break
    apdu_code = 0
    for code, n in MAX_APDU_BY_CODE.items():
        if max_apdu >= n:
            apdu_code = code
    return (seg_code << 4) | apdu_code


@dataclass
class Apdu:
    """A decoded APDU. ``data`` is the service data (behind the service choice
    for requests and acks, behind the header for error/reject/abort)."""

    pdu_type: int
    invoke_id: int | None = None
    service: int | None = None
    data: bytes = b""
    segmented: bool = False
    more_follows: bool = False
    segmented_response_accepted: bool = False
    max_segments: int | None = None
    max_apdu: int | None = None
    sequence_number: int | None = None
    window_size: int | None = None
    server: bool = False
    reason: int | None = None  # reject / abort
    error_class: int | None = None
    error_code: int | None = None


def encode_confirmed_request(
    invoke_id: int,
    service: int,
    service_data: bytes,
    max_apdu: int = MAX_APDU_BIP,
    max_segments: int | None = None,
    segmented_response_accepted: bool = False,
) -> bytes:
    """Unsegmented BACnet-Confirmed-Request-PDU."""
    first = PDU_CONFIRMED_REQUEST << 4
    if segmented_response_accepted:
        first |= PDU_FLAG_SEGMENTED_RESPONSE_ACCEPTED
    return (
        bytes(
            [
                first,
                encode_max_segs_max_apdu(max_segments, max_apdu),
                invoke_id & 0xFF,
                service,
            ]
        )
        + service_data
    )


def encode_unconfirmed_request(service: int, service_data: bytes) -> bytes:
    return bytes([PDU_UNCONFIRMED_REQUEST << 4, service]) + service_data


def encode_simple_ack(invoke_id: int, service: int) -> bytes:
    return bytes([PDU_SIMPLE_ACK << 4, invoke_id & 0xFF, service])


def encode_complex_ack(invoke_id: int, service: int, service_data: bytes) -> bytes:
    """Unsegmented BACnet-ComplexACK-PDU."""
    return bytes([PDU_COMPLEX_ACK << 4, invoke_id & 0xFF, service]) + service_data


def encode_error(invoke_id: int, service: int, error_class: int, error_code: int) -> bytes:
    return (
        bytes([PDU_ERROR << 4, invoke_id & 0xFF, service])
        + encode_application_enumerated(error_class)
        + encode_application_enumerated(error_code)
    )


def encode_reject(invoke_id: int, reason: int) -> bytes:
    return bytes([PDU_REJECT << 4, invoke_id & 0xFF, reason & 0xFF])


def encode_abort(invoke_id: int, reason: int, server: bool = True) -> bytes:
    first = (PDU_ABORT << 4) | (PDU_FLAG_SERVER if server else 0)
    return bytes([first, invoke_id & 0xFF, reason & 0xFF])


def encode_segment_ack(
    invoke_id: int, sequence_number: int, window_size: int, nak: bool = False, server: bool = False
) -> bytes:
    first = (PDU_SEGMENT_ACK << 4) | (0x02 if nak else 0) | (PDU_FLAG_SERVER if server else 0)
    return bytes([first, invoke_id & 0xFF, sequence_number & 0xFF, window_size & 0xFF])


def decode_apdu(data: bytes) -> Apdu:
    if not data:
        raise CodecError("empty APDU")
    first = data[0]
    pdu_type = first >> 4
    try:
        if pdu_type == PDU_CONFIRMED_REQUEST:
            apdu = Apdu(
                pdu_type,
                segmented=bool(first & PDU_FLAG_SEGMENTED),
                more_follows=bool(first & PDU_FLAG_MORE_FOLLOWS),
                segmented_response_accepted=bool(first & PDU_FLAG_SEGMENTED_RESPONSE_ACCEPTED),
                max_segments=MAX_SEGMENTS_BY_CODE.get((data[1] >> 4) & 0x07),
                max_apdu=MAX_APDU_BY_CODE.get(data[1] & 0x0F, 50),
                invoke_id=data[2],
            )
            i = 3
            if apdu.segmented:
                apdu.sequence_number = data[i]
                apdu.window_size = data[i + 1]
                i += 2
            apdu.service = data[i]
            apdu.data = bytes(data[i + 1 :])
            return apdu
        if pdu_type == PDU_UNCONFIRMED_REQUEST:
            return Apdu(pdu_type, service=data[1], data=bytes(data[2:]))
        if pdu_type == PDU_SIMPLE_ACK:
            return Apdu(pdu_type, invoke_id=data[1], service=data[2])
        if pdu_type == PDU_COMPLEX_ACK:
            apdu = Apdu(
                pdu_type,
                segmented=bool(first & PDU_FLAG_SEGMENTED),
                more_follows=bool(first & PDU_FLAG_MORE_FOLLOWS),
                invoke_id=data[1],
            )
            i = 2
            if apdu.segmented:
                apdu.sequence_number = data[i]
                apdu.window_size = data[i + 1]
                i += 2
            apdu.service = data[i]
            apdu.data = bytes(data[i + 1 :])
            return apdu
        if pdu_type == PDU_SEGMENT_ACK:
            return Apdu(
                pdu_type,
                invoke_id=data[1],
                sequence_number=data[2],
                window_size=data[3],
                server=bool(first & PDU_FLAG_SERVER),
            )
        if pdu_type == PDU_ERROR:
            apdu = Apdu(pdu_type, invoke_id=data[1], service=data[2], data=bytes(data[3:]))
            apdu.error_class, apdu.error_code = decode_error(apdu.data)
            return apdu
        if pdu_type == PDU_REJECT:
            return Apdu(pdu_type, invoke_id=data[1], reason=data[2])
        if pdu_type == PDU_ABORT:
            return Apdu(
                pdu_type, invoke_id=data[1], reason=data[2], server=bool(first & PDU_FLAG_SERVER)
            )
    except IndexError:
        raise CodecError("truncated APDU") from None
    raise CodecError(f"unknown PDU type {pdu_type}")


def decode_error(data: bytes) -> tuple[int, int]:
    """Error class and code of an Error-PDU's service data.

    Handles the plain ``error-class, error-code`` form and the forms wrapped
    in a context-tagged ``[0]`` group used by some services.
    """
    found: list[int] = []
    i = 0
    while i < len(data) and len(found) < 2:
        hdr = decode_tag(data, i)
        if hdr.opening:
            i += hdr.header_len
            continue
        if hdr.closing:
            i += hdr.header_len
            continue
        content, nxt = _content(data, i, hdr)
        if not hdr.context and hdr.number == TAG_ENUMERATED:
            found.append(decode_unsigned(content))
        i = nxt
    if len(found) != 2:
        raise CodecError("Error-PDU without error class and code")
    return found[0], found[1]


# --- services: encoders -----------------------------------------------------------------------


def _obj(obj_type: int | str) -> int:
    return enums.object_type_number(obj_type)


def _prop(prop: int | str) -> int:
    return enums.property_number(prop)


def who_is(low: int | None = None, high: int | None = None) -> bytes:
    """Who-Is APDU (unconfirmed service 8); limits must be given together."""
    if (low is None) != (high is None):
        raise CodecError("Who-Is limits must both be given or both omitted")
    data = b""
    if low is not None and high is not None:
        if not (0 <= low <= enums.MAX_INSTANCE and 0 <= high <= enums.MAX_INSTANCE):
            raise CodecError("Who-Is limit out of range")
        data = encode_context_unsigned(0, low) + encode_context_unsigned(1, high)
    return encode_unconfirmed_request(enums.SERVICE_UNCONFIRMED_WHO_IS, data)


def i_am(
    device: int,
    max_apdu: int = MAX_APDU_BIP,
    segmentation: int = enums.SEGMENTATION_NONE,
    vendor_id: int = 0,
) -> bytes:
    """I-Am APDU (unconfirmed service 0)."""
    data = (
        encode_application_object_id(enums.OBJECT_DEVICE, device)
        + encode_application_unsigned(max_apdu)
        + encode_application_enumerated(segmentation)
        + encode_application_unsigned(vendor_id)
    )
    return encode_unconfirmed_request(enums.SERVICE_UNCONFIRMED_I_AM, data)


def read_property_request_data(
    obj_type: int | str, instance: int, prop: int | str, index: int | None = None
) -> bytes:
    data = encode_context_object_id(0, _obj(obj_type), instance) + encode_context_enumerated(
        1, _prop(prop)
    )
    if index is not None:
        data += encode_context_unsigned(2, index)
    return data


def read_property(
    invoke_id: int,
    obj_type: int | str,
    instance: int,
    prop: int | str,
    index: int | None = None,
    max_apdu: int = MAX_APDU_BIP,
) -> bytes:
    """ReadProperty confirmed request APDU (service 12), no segmentation."""
    return encode_confirmed_request(
        invoke_id,
        enums.SERVICE_CONFIRMED_READ_PROPERTY,
        read_property_request_data(obj_type, instance, prop, index),
        max_apdu=max_apdu,
    )


def write_property_request_data(
    obj_type: int | str,
    instance: int,
    prop: int | str,
    value_bytes: bytes,
    priority: int | None = None,
    index: int | None = None,
) -> bytes:
    """WriteProperty-Request service data."""
    data = encode_context_object_id(0, _obj(obj_type), instance) + encode_context_enumerated(
        1, _prop(prop)
    )
    if index is not None:
        data += encode_context_unsigned(2, index)
    data += encode_opening_tag(3) + value_bytes + encode_closing_tag(3)
    if priority is not None:
        if not 1 <= priority <= BACNET_MAX_PRIORITY:
            raise CodecError(f"priority out of range 1..16: {priority}")
        data += encode_context_unsigned(4, priority)
    return data


def write_property(
    invoke_id: int,
    obj_type: int | str,
    instance: int,
    prop: int | str,
    value_bytes: bytes,
    priority: int | None = None,
    index: int | None = None,
    max_apdu: int = MAX_APDU_BIP,
) -> bytes:
    """WriteProperty confirmed request APDU (service 15).

    ``value_bytes`` are the application-tagged value(s), see
    :func:`encode_value`.
    """
    data = write_property_request_data(obj_type, instance, prop, value_bytes, priority, index)
    return encode_confirmed_request(
        invoke_id, enums.SERVICE_CONFIRMED_WRITE_PROPERTY, data, max_apdu=max_apdu
    )


def read_property_ack(
    invoke_id: int,
    obj_type: int | str,
    instance: int,
    prop: int | str,
    value_bytes: bytes,
    index: int | None = None,
) -> bytes:
    """ReadProperty-ACK complex ack APDU."""
    data = encode_context_object_id(0, _obj(obj_type), instance) + encode_context_enumerated(
        1, _prop(prop)
    )
    if index is not None:
        data += encode_context_unsigned(2, index)
    data += encode_opening_tag(3) + value_bytes + encode_closing_tag(3)
    return encode_complex_ack(invoke_id, enums.SERVICE_CONFIRMED_READ_PROPERTY, data)


def encode_bip(apdu: bytes, expecting_reply: bool = False, broadcast: bool = False) -> bytes:
    """BVLC + NPDU + APDU datagram for a local B/IP network."""
    return encode_bvlc(encode_npdu(expecting_reply=expecting_reply) + apdu, broadcast=broadcast)


# --- services: decoders ---------------------------------------------------------------------


@dataclass
class IAmData:
    device: int
    max_apdu: int
    segmentation: int
    vendor_id: int


def decode_i_am(data: bytes) -> IAmData:
    """I-Am service data (behind the service choice)."""
    values, _ = decode_values(data)
    if (
        len(values) < 4
        or not isinstance(values[0], ObjectId)
        or values[0].type != enums.OBJECT_DEVICE
        or not isinstance(values[1], int)
        or not isinstance(values[2], Enumerated)
        or not isinstance(values[3], int)
    ):
        raise CodecError("malformed I-Am")
    return IAmData(values[0].instance, int(values[1]), int(values[2]), int(values[3]))


def decode_who_is(data: bytes) -> tuple[int | None, int | None]:
    """Who-Is service data -> ``(low, high)`` (both ``None`` for all devices)."""
    if not data:
        return None, None
    low_c, i = _expect_context(data, 0, 0)
    high_c, _ = _expect_context(data, i, 1)
    return decode_unsigned(low_c), decode_unsigned(high_c)


@dataclass
class PropertyRef:
    obj_type: int
    instance: int
    prop: int
    index: int | None = None


@dataclass
class ReadPropertyAck(PropertyRef):
    values: list[Any] = field(default_factory=list)


@dataclass
class WritePropertyRequest(PropertyRef):
    value_bytes: bytes = b""
    values: list[Any] = field(default_factory=list)
    priority: int | None = None


def _decode_ref(data: bytes) -> tuple[PropertyRef, int]:
    oid_c, i = _expect_context(data, 0, 0)
    oid = decode_object_id(oid_c)
    prop_c, i = _expect_context(data, i, 1)
    ref = PropertyRef(oid.type, oid.instance, decode_unsigned(prop_c))
    if _peek_context(data, i, 2):
        idx_c, i = _expect_context(data, i, 2)
        ref.index = decode_unsigned(idx_c)
    return ref, i


def decode_read_property_request(data: bytes) -> PropertyRef:
    ref, i = _decode_ref(data)
    if i != len(data):
        raise CodecError("trailing data in ReadProperty request")
    return ref


def decode_read_property_ack(data: bytes) -> ReadPropertyAck:
    """ReadProperty-ACK service data -> reference and the list of values."""
    ref, i = _decode_ref(data)
    i = _expect_opening(data, i, 3)
    values, _ = decode_values(data, i, closing_tag=3)
    return ReadPropertyAck(ref.obj_type, ref.instance, ref.prop, ref.index, values)


def decode_write_property_request(data: bytes) -> WritePropertyRequest:
    ref, i = _decode_ref(data)
    start = _expect_opening(data, i, 3)
    values, after = decode_values(data, start, closing_tag=3)
    value_bytes = bytes(data[start : after - len(encode_closing_tag(3))])
    req = WritePropertyRequest(ref.obj_type, ref.instance, ref.prop, ref.index, value_bytes, values)
    if after < len(data):
        prio_c, _ = _expect_context(data, after, 4)
        req.priority = decode_unsigned(prio_c)
    return req


# --- natural datatypes ------------------------------------------------------------------------

_ANALOG_PV = {"analog-input", "analog-output", "analog-value", "loop", "pulse-converter"}
_BINARY_PV = {
    "binary-input",
    "binary-output",
    "binary-value",
    "binary-lighting-output",
}
_MULTISTATE_PV = {"multi-state-input", "multi-state-output", "multi-state-value"}

_PROPERTY_TAGS: dict[str, str] = {
    "object-identifier": "object-identifier",
    "object-name": "character-string",
    "object-type": "enumerated",
    "description": "character-string",
    "location": "character-string",
    "status-flags": "bit-string",
    "event-state": "enumerated",
    "reliability": "enumerated",
    "out-of-service": "boolean",
    "units": "enumerated",
    "cov-increment": "real",
    "number-of-states": "unsigned",
    "state-text": "character-string",
    "active-text": "character-string",
    "inactive-text": "character-string",
    "polarity": "enumerated",
    "vendor-name": "character-string",
    "vendor-identifier": "unsigned",
    "model-name": "character-string",
    "firmware-revision": "character-string",
    "application-software-version": "character-string",
    "protocol-version": "unsigned",
    "protocol-revision": "unsigned",
    "max-apdu-length-accepted": "unsigned",
    "segmentation-supported": "enumerated",
    "system-status": "enumerated",
    "object-list": "object-identifier",
    "structured-object-list": "object-identifier",
    "database-revision": "unsigned",
    "apdu-timeout": "unsigned",
    "number-of-apdu-retries": "unsigned",
    "min-pres-value": "real",
    "max-pres-value": "real",
    "resolution": "real",
    "high-limit": "real",
    "low-limit": "real",
    "deadband": "real",
    "notification-class": "unsigned",
    "time-delay": "unsigned",
    "priority-for-writing": "unsigned",
    "profile-name": "character-string",
    "utc-offset": "signed",
    "daylight-savings-status": "boolean",
    "local-date": "date",
    "local-time": "time",
    "minimum-on-time": "unsigned",
    "minimum-off-time": "unsigned",
    "feedback-value": "enumerated",
    "change-of-state-count": "unsigned",
    "elapsed-active-time": "unsigned",
    "protocol-services-supported": "bit-string",
    "protocol-object-types-supported": "bit-string",
    "event-enable": "bit-string",
    "acked-transitions": "bit-string",
    "property-list": "enumerated",
}


def _pv_tag(type_name: str) -> str | None:
    if type_name in _ANALOG_PV:
        return "real"
    if type_name in _BINARY_PV:
        return "enumerated"
    if type_name in _MULTISTATE_PV:
        return "unsigned"
    return {
        "large-analog-value": "double",
        "integer-value": "signed",
        "positive-integer-value": "unsigned",
        "characterstring-value": "character-string",
        "octetstring-value": "octet-string",
        "bitstring-value": "bit-string",
        "date-value": "date",
        "time-value": "time",
        "accumulator": "unsigned",
        "averaging": None,
    }.get(type_name)


def property_value_tag(obj_type: int | str, prop: int | str) -> str | None:
    """The natural application tag name of a property's value, ``None`` if
    unknown or not a single primitive datatype.

    Present_Value, Relinquish_Default and Priority_Array elements depend on
    the object type: analog -> ``"real"``, binary -> ``"enumerated"``,
    multi-state -> ``"unsigned"``, large-analog-value -> ``"double"``, ...
    """
    try:
        type_name = enums.object_type_name(enums.object_type_number(obj_type))
        prop_name = enums.property_name(enums.property_number(prop)).lower()
    except ValueError:
        return None
    if prop_name in ("present-value", "relinquish-default", "priority-array"):
        return _pv_tag(type_name)
    if prop_name in ("alarm-value",) and type_name in _BINARY_PV:
        return "enumerated"
    if prop_name in ("min-pres-value", "max-pres-value", "cov-increment") and (
        type_name == "large-analog-value"
    ):
        return "double"
    return _PROPERTY_TAGS.get(prop_name)


# Properties whose value is a BACnetARRAY or BACnetLIST (read without index
# returns all elements).
ARRAY_OR_LIST_PROPERTIES: frozenset[int] = frozenset(
    enums.PROPERTIES[n]
    for n in (
        "action",
        "action-text",
        "active-cov-subscriptions",
        "alarm-values",
        "configuration-files",
        "date-list",
        "device-address-binding",
        "event-message-texts",
        "event-message-texts-config",
        "event-time-stamps",
        "exception-schedule",
        "fault-values",
        "list-of-group-members",
        "list-of-object-property-references",
        "log-buffer",
        "manual-slave-address-binding",
        "object-list",
        "priority",
        "priority-array",
        "property-list",
        "recipient-list",
        "slave-address-binding",
        "state-text",
        "structured-object-list",
        "subordinate-annotations",
        "subordinate-list",
        "tags",
        "time-synchronization-recipients",
        "utc-time-synchronization-recipients",
        "weekly-schedule",
    )
    if n in enums.PROPERTIES
)


def to_jsonable(value: Any) -> Any:
    """Convert a decoded value to JSON-compatible data.

    ObjectId -> ``"analog-input:1"``, Date/Time -> text, BitString -> list of
    bool, bytes -> hex text, Enumerated -> int, ContextValue/Constructed ->
    dicts, lists recursively; NaN/inf floats -> text.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Date | Time):
        return str(value)
    if isinstance(value, BitString):
        return [bool(b) for b in value]
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else str(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value).hex()
    if isinstance(value, ContextValue):
        return {"context_tag": value.tag, "data": value.data.hex()}
    if isinstance(value, Constructed):
        return {"context_tag": value.tag, "values": [to_jsonable(v) for v in value.values]}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    return str(value)
