"""BACnet MS/TP (ANSI/ASHRAE 135 Clause 9) frames, receive parsing, timing checks and pcap export.

Byte-level rules follow bacnet-stack 54544d02: ``crc.c`` (header CRC-8, data CRC-16),
``cobs.c`` (extended frames: COBS encoding plus CRC-32K), ``mstp.c`` (``MSTP_Create_Frame``
and ``MSTP_Receive_Frame_FSM``) and ``mstpdef.h`` (timing constants). The unit tests compare
this module with a harness compiled from those sources.

All times are float seconds on a single timebase, normally the logic analyzer's; nothing here
reads a clock. The timing checks return structured results (:class:`Check`) so tests can assert
on them and print every violation with its timestamp.
"""

from __future__ import annotations

import enum
import math
import struct
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import ClassVar, NamedTuple

BROADCAST = 255
PREAMBLE = b"\x55\xff"
HEADER_LEN = 8  # 55 FF type dst src len_hi len_lo hcrc
NPDU_MAX = 501  # MSTP_FRAME_NPDU_MAX
EXTENDED_NPDU_MAX = 1497  # MSTP_EXTENDED_FRAME_NPDU_MAX
COBS_TYPES = range(32, 128)  # Nmin_COBS_type..Nmax_COBS_type
HEADER_CRC_RESIDUE = 0x55
DATA_CRC_RESIDUE = 0xF0B8
CRC32K_RESIDUE = 0x0843323B
COBS_MASK = 0x55  # MSTP_PREAMBLE_X55
COBS_CRC_LEN = 5  # COBS_ENCODED_CRC_SIZE
OCTET_BITS = 10  # 8N1: start bit, 8 data bits, stop bit

# Clause 9 timing parameters (bacnet-stack mstpdef.h; v1 design section 4.4).
TURNAROUND_BITS = 40
POSTDRIVE_BITS = 15
FRAME_GAP_BITS = 20
FRAME_ABORT_BITS = 60
FRAME_ABORT_MAX_S = 0.100
USAGE_DELAY_MAX_S = 0.015
USAGE_TIMEOUT_S = (0.020, 0.035)  # upper bound per bacnet-stack; 135 edition not yet checked
REPLY_DELAY_MAX_S = 0.250
REPLY_TIMEOUT_S = (0.255, 0.300)
NO_TOKEN_S = 0.500
SLOT_S = 0.010
LINKTYPE_BACNET_MS_TP = 165


class FrameType(enum.IntEnum):
    """Frame types of Clause 9 (FRAME_TYPE_* in mstpdef.h)."""

    TOKEN = 0
    POLL_FOR_MASTER = 1
    REPLY_TO_POLL_FOR_MASTER = 2
    TEST_REQUEST = 3
    TEST_RESPONSE = 4
    BACNET_DATA_EXPECTING_REPLY = 5
    BACNET_DATA_NOT_EXPECTING_REPLY = 6
    REPLY_POSTPONED = 7
    BACNET_EXTENDED_DATA_EXPECTING_REPLY = 32
    BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY = 33
    IPV6_ENCAPSULATION = 34


def frame_type_name(ftype: int) -> str:
    """Name of a frame type, or ``TYPE_<n>`` for proprietary and unassigned values."""
    try:
        return FrameType(ftype).name
    except ValueError:
        return f"TYPE_{ftype}"


# --- CRCs (table form of the formulas that crc.c and cobs.c copy from the standard) ---------


def _crc8_formula(index: int) -> int:
    """CRC_Calc_Header() for crcValue ^ dataValue == index."""
    c = index ^ (index << 1) ^ (index << 2) ^ (index << 3) ^ (index << 4) ^ (index << 5)
    c ^= (index << 6) ^ (index << 7)
    return ((c & 0xFE) ^ ((c >> 8) & 1)) & 0xFF


def _crc16_formula(low: int) -> int:
    """The part of CRC_Calc_Data() that depends on (crcValue & 0xFF) ^ dataValue."""
    return ((low << 8) ^ (low << 3) ^ (low << 12) ^ (low >> 4) ^ (low & 0x0F) ^ ((low & 0x0F) << 7)) & 0xFFFF


def _crc32k_formula(index: int) -> int:
    """cobs_crc32k() run on one octet with a register of ``index`` (reflected polynomial)."""
    crc = index
    for _ in range(8):
        crc = (crc >> 1) ^ 0xEB31D82E if crc & 1 else crc >> 1
    return crc


_CRC8 = tuple(_crc8_formula(i) for i in range(256))
_CRC16 = tuple(_crc16_formula(i) for i in range(256))
_CRC32K = tuple(_crc32k_formula(i) for i in range(256))


def _crc8(data: Iterable[int], crc: int = 0xFF) -> int:
    for octet in data:
        crc = _CRC8[crc ^ octet]
    return crc


def _crc16(data: Iterable[int], crc: int = 0xFFFF) -> int:
    for octet in data:
        crc = (crc >> 8) ^ _CRC16[(crc ^ octet) & 0xFF]
    return crc


def _crc32k(data: Iterable[int], crc: int = 0xFFFFFFFF) -> int:
    for octet in data:
        crc = (crc >> 8) ^ _CRC32K[(crc ^ octet) & 0xFF]
    return crc


def header_crc(header: bytes) -> int:
    """Transmitted header CRC octet for the five octets frame type .. length."""
    return ~_crc8(header) & 0xFF


def data_crc(data: bytes) -> bytes:
    """Transmitted data CRC-16 for ``data``: two octets, least significant first."""
    crc = ~_crc16(data) & 0xFFFF
    return bytes((crc & 0xFF, crc >> 8))


# --- COBS (extended frames, cobs.c) ------------------------------------------------------


def cobs_encode(data: bytes, mask: int = COBS_MASK) -> bytes:
    """COBS-encode ``data`` and XOR every output octet with ``mask`` (cobs_encode())."""
    if not data:
        raise ValueError("COBS encoding needs at least one octet")
    out = bytearray(1)
    code_index, code, last_code = 0, 1, 0
    for octet in data:
        if octet:
            out.append(octet ^ mask)
            code += 1
            if code != 255:
                continue
        last_code = code
        out[code_index] = code ^ mask
        code_index = len(out)
        out.append(0)
        code = 1
    if last_code == 255 and code == 1:
        out.pop()  # a final block of exactly 254 non-zero octets needs no phantom zero
    else:
        out[code_index] = code ^ mask
    return bytes(out)


def cobs_decode(data: bytes, mask: int = COBS_MASK) -> bytes | None:
    """Inverse of :func:`cobs_encode`; None for a malformed encoding (cobs_decode())."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        code = data[i] ^ mask
        if code == 0 or i + code > n:
            return None
        block = code
        i += 1
        for _ in range(code - 1):
            out.append(data[i] ^ mask)
            i += 1
        if block != 255 and i < n:
            out.append(0)
    return bytes(out)


def cobs_frame_encode(data: bytes, *, bad_crc: bool = False) -> bytes:
    """Encoded Data plus Encoded CRC-32K fields of an extended frame (cobs_frame_encode()).

    ``bad_crc`` flips the low bit of the CRC before it is encoded, so the frame still has a
    well-formed COBS encoding and fails only the CRC check.
    """
    encoded = cobs_encode(data)
    crc = ~_crc32k(encoded) & 0xFFFFFFFF
    if bad_crc:
        crc ^= 1
    return encoded + cobs_encode(crc.to_bytes(4, "little"))


def cobs_frame_decode(encoded: bytes) -> bytes | None:
    """Payload of an extended frame's data field, or None on a bad encoding or CRC-32K."""
    if len(encoded) < COBS_CRC_LEN:
        return None
    body, crc_field = encoded[:-COBS_CRC_LEN], encoded[-COBS_CRC_LEN:]
    data = cobs_decode(body)
    crc_octets = cobs_decode(crc_field)
    if not data or crc_octets is None or len(crc_octets) != 4:
        return None
    if _crc32k(crc_octets, _crc32k(body)) != CRC32K_RESIDUE:
        return None
    return data


# --- frame builder -----------------------------------------------------------------------


def build_frame(
    ftype: int,
    dst: int,
    src: int,
    data: bytes = b"",
    *,
    bad_header_crc: bool = False,
    bad_data_crc: bool = False,
    length: int | None = None,
    truncate: int | None = None,
) -> bytes:
    """Encode one frame, preamble included, as MSTP_Create_Frame() does.

    Frame types 32..127 carry COBS-encoded data plus CRC-32K and a length field two less than
    the encoded size; the other types carry the data and a CRC-16. Unlike MSTP_Create_Frame(),
    types 5 and 6 are never promoted to 32 and 33, so over-long frames can be built on purpose.

    Fault options for injection tests: ``bad_header_crc`` / ``bad_data_crc`` flip the low bit of
    the transmitted CRC; ``length`` replaces the header length field (the header CRC covers the
    replaced value); ``truncate`` keeps only the first N octets.
    """
    for name, value in (("ftype", ftype), ("dst", dst), ("src", src)):
        if not 0 <= value <= 255:
            raise ValueError(f"{name}={value} is not an octet")
    if ftype in COBS_TYPES:
        body = cobs_frame_encode(data, bad_crc=bad_data_crc)
        length_field = len(body) - 2
    elif data:
        crc = bytearray(data_crc(data))
        crc[0] ^= int(bad_data_crc)
        body = bytes(data) + bytes(crc)
        length_field = len(data)
    elif bad_data_crc:
        raise ValueError("a frame without data has no data CRC")
    else:
        body, length_field = b"", 0
    if length is not None:
        length_field = length
    if not 0 <= length_field <= 0xFFFF:
        raise ValueError(f"length {length_field} does not fit the 16-bit length field")
    header = bytes((ftype, dst, src, length_field >> 8, length_field & 0xFF))
    frame = PREAMBLE + header + bytes((header_crc(header) ^ int(bad_header_crc),)) + body
    return frame if truncate is None else frame[:truncate]


# --- receive side ------------------------------------------------------------------------


class Octet(NamedTuple):
    """One character from a UART decoder."""

    t: float  # start of the start bit, seconds
    value: int
    framing_error: bool = False


def to_octets(data: bytes, t0: float, baud: int, gap_s: float = 0.0) -> list[Octet]:
    """Timed octets for ``data`` sent from ``t0`` with ``gap_s`` of idle line between octets."""
    step = OCTET_BITS / baud + gap_s
    return [Octet(t0 + i * step, value) for i, value in enumerate(data)]


class RxError(enum.StrEnum):
    """Why the Receive Frame FSM discards a frame (it sets ReceivedInvalidFrame)."""

    TIMEOUT = "timeout"  # silence > Tframe_abort inside header or data (truncated frame)
    RECEIVE_ERROR = "receive_error"  # UART framing error inside header or data
    BAD_HEADER_CRC = "bad_header_crc"
    BAD_DATA_CRC = "bad_data_crc"  # CRC-16, or COBS encoding / CRC-32K of an extended frame


@dataclass(frozen=True, repr=False)
class Frame:
    """A received frame: valid (``error`` is None) or discarded by the receive FSM."""

    octets: tuple[Octet, ...]  # from the 0x55 before 0xFF through the last octet received
    t_end: float  # end of the stop bit of the last octet
    data: bytes = b""  # NPDU of a valid frame (COBS-decoded for extended types)
    error: RxError | None = None

    @property
    def t_start(self) -> float:
        return self.octets[0].t

    @property
    def raw(self) -> bytes:
        return bytes(o.value for o in self.octets)

    def _header(self, index: int) -> int | None:
        return self.octets[index].value if len(self.octets) > index else None

    @property
    def ftype(self) -> int | None:
        return self._header(2)

    @property
    def dst(self) -> int | None:
        return self._header(3)

    @property
    def src(self) -> int | None:
        return self._header(4)

    @property
    def length(self) -> int | None:
        """Header length field; None while the header is incomplete."""
        if len(self.octets) < 7:
            return None
        return self.octets[5].value << 8 | self.octets[6].value

    @property
    def ok(self) -> bool:
        return self.error is None

    def __str__(self) -> str:
        name = "?" if self.ftype is None else frame_type_name(self.ftype)
        status = "" if self.ok else f" [{self.error}]"
        return f"{name} {self.src}->{self.dst} len={self.length} @{self.t_start:.6f}s{status}"

    def __repr__(self) -> str:
        return f"<Frame {self}>"


class _State(enum.Enum):
    IDLE = enum.auto()
    PREAMBLE = enum.auto()
    HEADER = enum.auto()
    DATA = enum.auto()


class FrameParser:
    """Clause 9 Receive Frame state machine, run as a bus sniffer (every frame, not only ours).

    It follows MSTP_Receive_Frame_FSM(): a silence of more than ``frame_abort_s`` between two
    DataAvailable events (the ends of consecutive stop bits) aborts a frame, a framing error
    inside the header or data invalidates it, and the header and data CRCs are checked against
    their residues. Octets outside frames are eaten silently, as the FSM does. The default
    abort time is the strictest one a receiver may use (60 bit times).
    """

    def __init__(self, baud: int, frame_abort_s: float | None = None) -> None:
        self.octet_s = OCTET_BITS / baud
        self.frame_abort_s = FRAME_ABORT_BITS / baud if frame_abort_s is None else frame_abort_s
        self._state = _State.IDLE
        self._octets: list[Octet] = []
        self._need = 0
        self._last_rx = -math.inf
        self._handlers = {
            _State.IDLE: self._idle,
            _State.PREAMBLE: self._preamble,
            _State.HEADER: self._header,
            _State.DATA: self._data,
        }

    def feed(self, octet: Octet) -> Frame | None:
        """Process one octet (in time order); return the frame it completed or aborted."""
        rx = octet.t + self.octet_s
        silence, self._last_rx = rx - self._last_rx, rx
        if silence > self.frame_abort_s and self._state is not _State.IDLE:
            aborted = self._finish(RxError.TIMEOUT) if self._state in (_State.HEADER, _State.DATA) else None
            self._reset()
            self._idle(octet)
            return aborted
        return self._handlers[self._state](octet)

    def flush(self, t: float | None = None) -> Frame | None:
        """End of input at time ``t`` (None: the line stays silent for ever).

        A frame still in its header or data is reported as a timeout if the silence before
        ``t`` exceeds the abort time; otherwise the capture cut it off and it is dropped.
        """
        partial = self._state in (_State.HEADER, _State.DATA)
        timed_out = t is None or t - self._last_rx > self.frame_abort_s
        frame = self._finish(RxError.TIMEOUT) if partial and timed_out else None
        self._reset()
        return frame

    def _reset(self) -> None:
        self._state = _State.IDLE
        self._octets = []

    def _finish(self, error: RxError | None, data: bytes = b"") -> Frame:
        frame = Frame(tuple(self._octets), self._octets[-1].t + self.octet_s, data, error)
        self._reset()
        return frame

    def _idle(self, octet: Octet) -> None:
        if not octet.framing_error and octet.value == 0x55:
            self._octets = [octet]
            self._state = _State.PREAMBLE

    def _preamble(self, octet: Octet) -> None:
        if octet.framing_error:
            self._reset()
        elif octet.value == 0xFF:
            self._octets.append(octet)
            self._state = _State.HEADER
        elif octet.value == 0x55:
            self._octets = [octet]  # repeated first preamble octet
        else:
            self._reset()

    def _header(self, octet: Octet) -> Frame | None:
        self._octets.append(octet)
        if octet.framing_error:
            return self._finish(RxError.RECEIVE_ERROR)
        if len(self._octets) < HEADER_LEN:
            return None
        if _crc8(o.value for o in self._octets[2:HEADER_LEN]) != HEADER_CRC_RESIDUE:
            return self._finish(RxError.BAD_HEADER_CRC)
        length = self._octets[5].value << 8 | self._octets[6].value
        if length == 0:
            return self._finish(None)
        self._need = HEADER_LEN + length + 2
        self._state = _State.DATA
        return None

    def _data(self, octet: Octet) -> Frame | None:
        self._octets.append(octet)
        if octet.framing_error:
            return self._finish(RxError.RECEIVE_ERROR)
        if len(self._octets) < self._need:
            return None
        field = bytes(o.value for o in self._octets[HEADER_LEN:])
        if self._octets[2].value in COBS_TYPES:
            data = cobs_frame_decode(field)
        else:
            data = field[:-2] if _crc16(field) == DATA_CRC_RESIDUE else None
        return self._finish(RxError.BAD_DATA_CRC) if data is None else self._finish(None, data)


def parse_frames(
    octets: Iterable[Octet], baud: int, *, frame_abort_s: float | None = None, until: float | None = None
) -> list[Frame]:
    """All frames in a time-ordered octet stream; ``until`` is when the stream ended (see flush)."""
    parser = FrameParser(baud, frame_abort_s)
    frames = [f for f in map(parser.feed, octets) if f is not None]
    last = parser.flush(until)
    return [*frames, last] if last is not None else frames


# --- timing analysis ---------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """Clause 9 timing limits at one baud rate (bacnet-stack mstpdef.h; v1 design section 4.4)."""

    baud: int

    usage_delay_max_s: ClassVar[float] = USAGE_DELAY_MAX_S
    usage_timeout_s: ClassVar[tuple[float, float]] = USAGE_TIMEOUT_S
    reply_delay_max_s: ClassVar[float] = REPLY_DELAY_MAX_S
    reply_timeout_s: ClassVar[tuple[float, float]] = REPLY_TIMEOUT_S
    no_token_s: ClassVar[float] = NO_TOKEN_S
    slot_s: ClassVar[float] = SLOT_S

    @property
    def bit_s(self) -> float:
        return 1.0 / self.baud

    @property
    def octet_s(self) -> float:
        return OCTET_BITS / self.baud

    @property
    def turnaround_min_s(self) -> float:
        return TURNAROUND_BITS / self.baud

    @property
    def postdrive_max_s(self) -> float:
        return POSTDRIVE_BITS / self.baud

    @property
    def frame_gap_max_s(self) -> float:
        return FRAME_GAP_BITS / self.baud

    @property
    def frame_abort_range_s(self) -> tuple[float, float]:
        """Allowed receiver abort times: 60 bit times .. 100 ms."""
        return FRAME_ABORT_BITS / self.baud, FRAME_ABORT_MAX_S

    def no_token_window(self, ts: int) -> tuple[float, float]:
        """Silence after which station ``ts`` generates a token: Tno_token + Tslot * [TS, TS+1]."""
        return self.no_token_s + self.slot_s * ts, self.no_token_s + self.slot_s * (ts + 1)


class Sample(NamedTuple):
    """One measurement: when it happened, the measured time, and what was measured."""

    t: float
    value_s: float
    what: str = ""


@dataclass(frozen=True)
class Check:
    """Measurements of one Clause 9 parameter against its limits.

    ``missing`` lists events that should have produced a measurement but did not (for example
    a Token to the DUT that was never used). A check with no samples is not ok: a timing test
    that measured nothing must not pass.
    """

    name: str
    samples: tuple[Sample, ...]
    min_s: float | None = None
    max_s: float | None = None
    missing: tuple[str, ...] = ()

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(s.value_s for s in self.samples)

    @property
    def violations(self) -> tuple[Sample, ...]:
        lo = -math.inf if self.min_s is None else self.min_s
        hi = math.inf if self.max_s is None else self.max_s
        return tuple(s for s in self.samples if not lo <= s.value_s <= hi)

    @property
    def ok(self) -> bool:
        return bool(self.samples) and not self.violations and not self.missing

    def percentile(self, p: float) -> float:
        """Nearest-rank percentile of the measured values."""
        values = sorted(self.values)
        if not values:
            raise ValueError(f"{self.name}: no samples")
        return values[min(len(values) - 1, max(0, math.ceil(p / 100 * len(values)) - 1))]

    def __str__(self) -> str:
        def us(x: float) -> str:
            return f"{x * 1e6:.1f} us"

        bounds = [f"{op} {us(v)}" for op, v in ((">=", self.min_s), ("<=", self.max_s)) if v is not None]
        spread = f" min={us(min(self.values))} max={us(max(self.values))}" if self.samples else ""
        lines = [f"{self.name}: n={len(self.samples)}{spread} limit {' '.join(bounds) or '-'}"]
        lines += [f"  VIOLATION {us(s.value_s)} at {s.t:.6f}s {s.what}" for s in self.violations[:10]]
        lines += [f"  MISSING {m}" for m in self.missing[:10]]
        return "\n".join(lines)


def _overlapping(
    octet: Octet, octet_s: float, de: Sequence[tuple[float, float]], rises: list[float]
) -> int | None:
    """Index of the DE interval that overlaps ``octet`` at all (intervals sorted, disjoint)."""
    i = bisect_right(rises, octet.t + octet_s) - 1
    return i if i >= 0 and octet.t < de[i][1] and octet.t + octet_s > de[i][0] else None


def _containing(t: float, de: Sequence[tuple[float, float]], rises: list[float]) -> int | None:
    """Index of the DE interval with rise <= t < fall."""
    i = bisect_right(rises, t) - 1
    return i if i >= 0 and t < de[i][1] else None


@dataclass(frozen=True)
class DeTiming:
    """Driver-enable timing of the DUT's transmissions (MSTP-04, MSTP-05)."""

    lead: Check  # DE rise -> first start bit; negative means DE rose after the start bit
    postdrive: Check  # end of the last stop bit -> DE fall
    tx_outside_de: tuple[Octet, ...]  # transmitted with the driver disabled
    idle_de: tuple[tuple[float, float], ...]  # driver enabled without transmitting

    @property
    def ok(self) -> bool:
        return self.lead.ok and self.postdrive.ok and not self.tx_outside_de and not self.idle_de


def de_timing(de: Sequence[tuple[float, float]], tx: Sequence[Octet], limits: Limits) -> DeTiming:
    """Match each DE-high interval with the TX octets it covers.

    ``de`` are (rise, fall) intervals of the DUT's DE line, sorted; ``-inf``/``inf`` mark an
    edge outside the capture, and the measurement that needs it is skipped. ``tx`` are the
    octets on the DUT's DI (UART TX) line.
    """
    rises = [r for r, _ in de]
    covered: dict[int, list[Octet]] = {}
    outside = []
    for octet in tx:
        i = _overlapping(octet, limits.octet_s, de, rises)
        if i is None:
            outside.append(octet)
        else:
            covered.setdefault(i, []).append(octet)
    lead, post = [], []
    for i, (rise, fall) in enumerate(de):
        octets = covered.get(i)
        if not octets:
            continue
        first, last = octets[0], octets[-1]
        if math.isfinite(rise):
            lead.append(Sample(rise, first.t - rise, f"DE rise at {rise:.6f}s"))
        if math.isfinite(fall):
            post.append(Sample(fall, fall - (last.t + limits.octet_s), f"DE fall at {fall:.6f}s"))
    return DeTiming(
        lead=Check("DE lead", tuple(lead), min_s=0.0),
        postdrive=Check("Tpostdrive", tuple(post), min_s=0.0, max_s=limits.postdrive_max_s),
        tx_outside_de=tuple(outside),
        idle_de=tuple(iv for i, iv in enumerate(de) if i not in covered),
    )


def turnaround(de: Sequence[tuple[float, float]], bus: Sequence[Octet], limits: Limits) -> Check:
    """Tturnaround: each DUT driver enable after the last octet another node sent (MSTP-06).

    ``bus`` is every octet on the wire (sniffer RO); octets that start while DE is high are the
    DUT's own and are ignored. A DE rise during a foreign octet gives a negative value.
    """
    rises = [r for r, _ in de]
    foreign = [o for o in bus if _containing(o.t, de, rises) is None]
    starts = [o.t for o in foreign]
    samples = []
    for rise in rises:
        i = bisect_left(starts, rise) - 1
        if math.isfinite(rise) and i >= 0:
            end = foreign[i].t + limits.octet_s
            samples.append(Sample(rise, rise - end, f"after octet ending {end:.6f}s"))
    return Check("Tturnaround", tuple(samples), min_s=limits.turnaround_min_s)


def frame_gap(frames: Sequence[Frame], limits: Limits) -> Check:
    """Tframe_gap: the longest idle time between two octets of each frame (MSTP-07)."""
    samples = []
    for frame in frames:
        o = frame.octets
        if len(o) > 1:
            gap = max(b.t - (a.t + limits.octet_s) for a, b in pairwise(o))
            samples.append(Sample(frame.t_start, gap, str(frame)))
    return Check("Tframe_gap", tuple(samples), max_s=limits.frame_gap_max_s)


def split_by_de(
    frames: Sequence[Frame], de: Sequence[tuple[float, float]]
) -> tuple[list[Frame], list[Frame]]:
    """Split bus frames into (sent by the DUT, sent by others): the DUT's start while DE is high."""
    rises = [r for r, _ in de]
    own: list[Frame] = []
    others: list[Frame] = []
    for frame in frames:
        (own if _containing(frame.t_start, de, rises) is not None else others).append(frame)
    return own, others


def first_responses(rx: Sequence[Frame], tx: Sequence[Frame]) -> list[tuple[Frame, Frame | None]]:
    """Pair each frame from other nodes (``rx``) with the DUT's answer (``tx``), if any.

    The answer is the first DUT frame that starts after the ``rx`` frame ends and before the
    next ``rx`` frame starts. Both inputs must be sorted by time.
    """
    starts = [f.t_start for f in tx]
    pairs: list[tuple[Frame, Frame | None]] = []
    for i, request in enumerate(rx):
        window_end = rx[i + 1].t_start if i + 1 < len(rx) else math.inf
        k = bisect_left(starts, request.t_end)
        answer = tx[k] if k < len(tx) and tx[k].t_start < window_end else None
        pairs.append((request, answer))
    return pairs


_USE_TOKEN = (FrameType.TOKEN, FrameType.POLL_FOR_MASTER)
_REPLIES: dict[int, tuple[int, ...]] = {
    FrameType.TEST_REQUEST: (FrameType.TEST_RESPONSE,),
    FrameType.BACNET_DATA_EXPECTING_REPLY: (
        FrameType.BACNET_DATA_NOT_EXPECTING_REPLY,
        FrameType.BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY,
        FrameType.REPLY_POSTPONED,
    ),
    FrameType.BACNET_EXTENDED_DATA_EXPECTING_REPLY: (
        FrameType.BACNET_DATA_NOT_EXPECTING_REPLY,
        FrameType.BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY,
        FrameType.REPLY_POSTPONED,
    ),
}


def usage_delay(rx: Sequence[Frame], tx: Sequence[Frame], mac: int, limits: Limits) -> Check:
    """Tusage_delay: Token or Poll-For-Master to ``mac`` -> the DUT's first frame (MSTP-08)."""
    samples, missing = [], []
    for request, answer in first_responses(rx, tx):
        if request.ok and request.ftype in _USE_TOKEN and request.dst == mac:
            if answer is None:
                missing.append(f"{request}: not used")
            else:
                samples.append(
                    Sample(request.t_end, answer.t_start - request.t_end, f"{request} -> {answer}")
                )
    return Check("Tusage_delay", tuple(samples), max_s=limits.usage_delay_max_s, missing=tuple(missing))


def reply_delay(rx: Sequence[Frame], tx: Sequence[Frame], mac: int, limits: Limits) -> Check:
    """Treply_delay: request expecting a reply to ``mac`` -> reply or Reply Postponed (MSTP-09).

    Requests are Test_Request and (extended) Data Expecting Reply frames addressed to ``mac``;
    the answer must be of a matching type and addressed back to the requester.
    """
    samples, missing = [], []
    for request, answer in first_responses(rx, tx):
        if not (request.ok and request.dst == mac and request.ftype in _REPLIES):
            continue
        if answer is None or answer.ftype not in _REPLIES[request.ftype] or answer.dst != request.src:
            missing.append(f"{request}: answered by {answer}")
        else:
            samples.append(Sample(request.t_end, answer.t_start - request.t_end, f"{request} -> {answer}"))
    return Check("Treply_delay", tuple(samples), max_s=limits.reply_delay_max_s, missing=tuple(missing))


def no_token_window(
    bus: Sequence[Octet], tx: Sequence[Frame], ts: int, limits: Limits, tolerance_s: float = 0.0
) -> Check:
    """Tno_token + Tslot: silence before each DUT frame that follows a lost token (MSTP-11).

    ``bus`` is every octet on the wire, the DUT's included. Each DUT frame preceded by at least
    Tno_token of silence must start within the window of station ``ts``, widened by
    ``tolerance_s`` on both sides.
    """
    ends = [o.t + limits.octet_s for o in bus]
    samples = []
    for frame in tx:
        i = bisect_right(ends, frame.t_start) - 1
        if i >= 0 and frame.t_start - ends[i] >= limits.no_token_s:
            samples.append(Sample(frame.t_start, frame.t_start - ends[i], str(frame)))
    lo, hi = limits.no_token_window(ts)
    return Check("Tno_token+Tslot", tuple(samples), min_s=lo - tolerance_s, max_s=hi + tolerance_s)


# --- pcap export -------------------------------------------------------------------------


def write_pcap(
    path: Path | str, frames: Iterable[Frame], to_epoch_ns: Callable[[float], int] | None = None
) -> int:
    """Write frames as a nanosecond pcap with LINKTYPE_BACNET_MS_TP (165); return the count.

    Each record is the frame as received, preamble included, which is what Wireshark's mstp
    dissector expects. ``to_epoch_ns`` maps capture time to wall-clock ns (for example a
    :class:`hilrig.sync.ClockFit`); by default the capture time itself is used.
    """
    stamp = to_epoch_ns or (lambda t: round(t * 1e9))
    count = 0
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, LINKTYPE_BACNET_MS_TP))
        for frame in sorted(frames, key=lambda f: f.t_start):
            sec, nsec = divmod(stamp(frame.t_start), 1_000_000_000)
            raw = frame.raw
            fh.write(struct.pack("<IIII", sec, nsec, len(raw), len(raw)) + raw)
            count += 1
    return count
