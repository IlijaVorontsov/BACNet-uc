"""Unit tests of hilrig.mstp: CRCs, COBS, the frame builder, the receive FSM, timing checks, pcap.

The byte-level reference is bacnet-stack itself: when its sources are available (the west
workspace's modules/lib/bacnet/stack, or $HIL_BACNET_STACK) a harness around
MSTP_Create_Frame() is compiled and compared with build_frame() on random frames.
"""

from __future__ import annotations

import math
import os
import random
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from hilrig import mstp
from hilrig.mstp import FrameType, Limits, Octet, RxError

BAUD = 38400
LIM = Limits(BAUD)
OCTET_S = mstp.OCTET_BITS / BAUD
IAM = bytes.fromhex("01001000c4020000012201e09103210f")
RP_REQ = bytes.fromhex("01040005010c0c02000001194d")


def frames_of(*parts: tuple[float, bytes], baud: int = BAUD, **kw: float) -> list[mstp.Frame]:
    """Parse byte strings sent back-to-back from the given start times."""
    octets = [o for t, raw in parts for o in mstp.to_octets(raw, t, baud)]
    return mstp.parse_frames(octets, baud, **kw)


# --- CRC and COBS -------------------------------------------------------------------------


def test_crc_golden_vectors() -> None:
    # bacnet-stack mstpcrc 54544d02: "mstpcrc 00 10 05 00 00" -> 0x8C, "-16 01 22 30" -> 10 BD
    assert mstp.header_crc(bytes.fromhex("0010050000")) == 0x8C
    assert mstp.data_crc(bytes.fromhex("012230")) == bytes.fromhex("10bd")
    # PFM 3->5, the example the v1 review corrected (0x7C, not 0x8C)
    assert mstp.build_frame(FrameType.POLL_FOR_MASTER, 5, 3) == bytes.fromhex("55ff01050300007c")


def test_crc_residues() -> None:
    rng = random.Random(1)
    for _ in range(200):
        header = bytes(rng.randrange(256) for _ in range(5))
        assert mstp._crc8(header + bytes([mstp.header_crc(header)])) == mstp.HEADER_CRC_RESIDUE
        data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64)))
        assert mstp._crc16(data + mstp.data_crc(data)) == mstp.DATA_CRC_RESIDUE


@pytest.mark.parametrize(
    "data",
    [
        b"\x00",
        b"\x55",
        b"\x01\x00\x02",
        bytes(10),
        bytes(range(1, 255)),
        bytes(range(1, 255)) + b"\x00",
        bytes(range(1, 256)) * 3,
        b"\x00" + bytes(range(1, 255)) + b"\x00\x00",
    ],
)
def test_cobs_round_trip_and_no_preamble_octet(data: bytes) -> None:
    encoded = mstp.cobs_encode(data)
    assert 0x55 not in encoded  # the point of the 0x55 mask: no preamble octet inside a frame
    assert mstp.cobs_decode(encoded) == data
    field = mstp.cobs_frame_encode(data)
    assert mstp.cobs_frame_decode(field) == data
    assert mstp.cobs_frame_decode(mstp.cobs_frame_encode(data, bad_crc=True)) is None


def test_cobs_rejects_malformed() -> None:
    assert mstp.cobs_decode(bytes([0x55])) is None  # code 0
    assert mstp.cobs_decode(bytes([0x05 ^ 0x55, 1, 2])) is None  # code runs past the end
    assert mstp.cobs_frame_decode(b"\x54" * 4) is None  # shorter than the encoded CRC
    with pytest.raises(ValueError, match="at least one octet"):
        mstp.cobs_encode(b"")


# --- bacnet-stack cross-check -------------------------------------------------------------

HARNESS_C = r"""
#include <stdio.h>
#include <stdint.h>
#include "bacnet/datalink/mstp.h"
int main(void) {
    unsigned ft, dst, src, len, v;
    static uint8_t data[2048], out[4096];
    while (scanf("%u %u %u %u", &ft, &dst, &src, &len) == 4) {
        for (unsigned i = 0; i < len; i++) { scanf("%2x", &v); data[i] = (uint8_t)v; }
        uint16_t n = MSTP_Create_Frame(out, sizeof out, ft, dst, src, data, len);
        for (unsigned i = 0; i < n; i++) printf("%02x", out[i]);
        printf("\n");
    }
    return 0;
}
"""


def _bacnet_stack() -> Path | None:
    candidates = [os.environ.get("HIL_BACNET_STACK", "")]
    if os.environ.get("ZEPHYR_BASE"):
        candidates.append(str(Path(os.environ["ZEPHYR_BASE"]).parent / "modules/lib/bacnet/stack"))
    for c in candidates:
        if c and (Path(c) / "src/bacnet/datalink/mstp.c").is_file():
            return Path(c)
    return None


@pytest.fixture(scope="module")
def create_frame(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Executable wrapping bacnet-stack MSTP_Create_Frame() (callbacks it never calls unresolved)."""
    root = _bacnet_stack()
    if root is None or shutil.which("gcc") is None:
        pytest.skip("bacnet-stack sources ($HIL_BACNET_STACK) or gcc not available")
    d = tmp_path_factory.mktemp("bacnet_stack")
    (d / "h.c").write_text(HARNESS_C)
    src = root / "src"
    dl = src / "bacnet/datalink"
    subprocess.run(
        ["gcc", "-O1", "-I", str(src), "-o", str(d / "h"), str(d / "h.c"),
         str(dl / "mstp.c"), str(dl / "crc.c"), str(dl / "cobs.c"), "-Wl,--unresolved-symbols=ignore-all"],
        check=True, capture_output=True,
    )  # fmt: skip
    return d / "h"


def _random_frames(n: int) -> list[tuple[int, int, int, bytes]]:
    rng = random.Random(54544)
    out = []
    for i in range(n):
        dst, src = rng.randrange(256), rng.randrange(256)
        # zero- and 0x55-heavy payloads exercise COBS and the preamble mask
        alphabet = rng.choice([range(256), [0, 0x55, 1, 0xFF], [0]])
        if i % 4 == 0:
            ftype, size = rng.choice([32, 33]), rng.choice([1, 2, 254, 255, 502, rng.randrange(1, 1498)])
        else:
            ftype, size = (
                rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 128, 200]),
                rng.choice([0, 1, rng.randrange(502)]),
            )
        out.append((ftype, dst, src, bytes(rng.choice(alphabet) for _ in range(size))))
    return out


def test_build_frame_matches_bacnet_stack(create_frame: Path) -> None:
    cases = _random_frames(1000)
    stdin = "".join(f"{t} {d} {s} {len(data)} {data.hex()}\n" for t, d, s, data in cases)
    lines = subprocess.run(
        [str(create_frame)], input=stdin, capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(lines) == len(cases)
    for (ftype, dst, src, data), expected in zip(cases, lines, strict=True):
        built = mstp.build_frame(ftype, dst, src, data)
        assert built.hex() == expected, (ftype, dst, src, len(data))
        [frame] = frames_of((0.0, built))
        assert frame.ok and (frame.ftype, frame.dst, frame.src, frame.data) == (ftype, dst, src, data)


# --- builder fault options ------------------------------------------------------------------


def test_build_frame_faults() -> None:
    good = mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, 5, 7, RP_REQ)
    assert mstp.build_frame(5, 5, 7, RP_REQ, bad_header_crc=True)[7] == good[7] ^ 1
    assert mstp.build_frame(5, 5, 7, RP_REQ, bad_data_crc=True)[-2] == good[-2] ^ 1
    assert mstp.build_frame(5, 5, 7, RP_REQ, truncate=10) == good[:10]
    long = mstp.build_frame(5, 5, 7, RP_REQ, length=600)
    assert long[5:7] == (600).to_bytes(2, "big") and long[7] == mstp.header_crc(long[2:7])
    refused: list[tuple[Callable[[], bytes], str]] = [
        (lambda: mstp.build_frame(256, 1, 2), "ftype=256 is not an octet"),
        (lambda: mstp.build_frame(0, -1, 2), "dst=-1 is not an octet"),
        (lambda: mstp.build_frame(0, 1, 2, length=0x10000), "does not fit the 16-bit length field"),
        (lambda: mstp.build_frame(0, 1, 2, bad_data_crc=True), "no data CRC"),
        (lambda: mstp.build_frame(33, 1, 2), "at least one octet"),  # extended frames need data
    ]
    for build, problem in refused:
        with pytest.raises(ValueError, match=problem):
            build()


# --- receive state machine ------------------------------------------------------------------


def test_parser_valid_frames_and_fields() -> None:
    token = mstp.build_frame(FrameType.TOKEN, 5, 7)
    iam = mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, 255, 5, IAM)
    ext = mstp.build_frame(FrameType.BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY, 7, 5, bytes(600))
    frames = frames_of((0.0, token), (0.01, iam), (0.02, ext))
    assert [f.error for f in frames] == [None, None, None]
    assert [f.ftype for f in frames] == [0, 6, 33]
    assert frames[1].data == IAM and frames[2].data == bytes(600) and frames[0].data == b""
    assert frames[1].raw == iam and frames[1].length == len(IAM)
    assert frames[0].t_start == 0.0 and frames[0].t_end == pytest.approx(8 * OCTET_S)
    assert str(frames[0]) == "TOKEN 7->5 len=0 @0.000000s"


def test_parser_skips_noise_and_repeated_preamble() -> None:
    token = mstp.build_frame(FrameType.TOKEN, 5, 7)
    frames = frames_of((0.0, b"\x00\x55\x12\x55\x55" + token))
    assert len(frames) == 1 and frames[0].ok and frames[0].raw == token


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (mstp.build_frame(0, 5, 7, bad_header_crc=True), RxError.BAD_HEADER_CRC),
        (mstp.build_frame(5, 5, 7, RP_REQ, bad_data_crc=True), RxError.BAD_DATA_CRC),
        (mstp.build_frame(33, 5, 7, bytes(40), bad_data_crc=True), RxError.BAD_DATA_CRC),
        (mstp.build_frame(5, 5, 7, RP_REQ, truncate=12), RxError.TIMEOUT),
        (mstp.build_frame(5, 5, 7, RP_REQ, length=100), RxError.TIMEOUT),
        (mstp.build_frame(0, 5, 7, truncate=5), RxError.TIMEOUT),
    ],
)
def test_parser_reports_invalid_frames_then_recovers(raw: bytes, error: RxError) -> None:
    token = mstp.build_frame(FrameType.TOKEN, 5, 7)
    first, second = frames_of((0.0, raw), (0.05, token))
    assert first.error is error and not first.ok
    assert second.ok and second.raw == token


def test_parser_framing_error() -> None:
    raw = mstp.build_frame(0, 5, 7)
    octets = mstp.to_octets(raw, 0.0, BAUD)
    octets[4] = octets[4]._replace(framing_error=True)
    [frame] = mstp.parse_frames(octets, BAUD)
    assert frame.error is RxError.RECEIVE_ERROR and len(frame.octets) == 5
    # outside a frame a framing error is eaten, as in the IDLE state of the FSM
    noisy = [Octet(0.0, 0x55, framing_error=True), *mstp.to_octets(raw, 0.001, BAUD)]
    assert [f.ok for f in mstp.parse_frames(noisy, BAUD)] == [True]


def test_parser_abort_boundary() -> None:
    """Tframe_abort counts between DataAvailable events: 60 bit times passes, more aborts."""
    raw = mstp.build_frame(0, 5, 7)
    bit = 1 / BAUD

    def with_idle(idle_bits: float) -> list[mstp.Frame]:
        octets = mstp.to_octets(raw, 0.0, BAUD)
        shifted = octets[:4] + [o._replace(t=o.t + idle_bits * bit) for o in octets[4:]]
        return mstp.parse_frames(shifted, BAUD)

    assert with_idle(49.9)[0].ok  # 49.9 idle + 10-bit octet < 60 bit between stop bits
    assert with_idle(50.1)[0].error is RxError.TIMEOUT
    assert mstp.parse_frames(mstp.to_octets(raw, 0.0, BAUD, gap_s=0.1), BAUD, frame_abort_s=0.2)[0].ok


def test_parser_flush_at_capture_end() -> None:
    partial = mstp.to_octets(mstp.build_frame(0, 5, 7)[:6], 0.0, BAUD)
    cut = 6 * OCTET_S + 0.0001
    assert mstp.parse_frames(partial, BAUD, until=cut) == []  # capture ended mid-frame: dropped
    [late] = mstp.parse_frames(partial, BAUD, until=1.0)
    assert late.error is RxError.TIMEOUT and late.ftype == 0 and late.length is None


# --- limits ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("baud", "turnaround", "postdrive", "gap", "abort"),
    [
        (9600, 4.167e-3, 1.562e-3, 2.083e-3, 6.25e-3),
        (38400, 1.042e-3, 0.391e-3, 0.521e-3, 1.56e-3),
        (115200, 0.347e-3, 0.130e-3, 0.174e-3, 0.521e-3),
    ],
)
def test_limits_match_design_table(
    baud: int, turnaround: float, postdrive: float, gap: float, abort: float
) -> None:
    lim = Limits(baud)
    assert lim.turnaround_min_s == pytest.approx(turnaround, abs=1e-6)
    assert lim.postdrive_max_s == pytest.approx(postdrive, abs=1e-6)
    assert lim.frame_gap_max_s == pytest.approx(gap, abs=1e-6)
    assert lim.frame_abort_range_s == pytest.approx((abort, 0.1), abs=1e-5)
    assert (lim.usage_delay_max_s, lim.reply_delay_max_s) == (0.015, 0.250)
    assert lim.no_token_window(5) == pytest.approx((0.550, 0.560))


# --- Check ----------------------------------------------------------------------------------


def test_check_semantics() -> None:
    empty = mstp.Check("x", ())
    assert not empty.ok  # measured nothing: never a pass
    c = mstp.Check(
        "x", tuple(mstp.Sample(i, v) for i, v in enumerate([1.0, 2.0, 3.0, 4.0])), min_s=1.5, max_s=3.5
    )
    assert [s.value_s for s in c.violations] == [1.0, 4.0] and not c.ok
    assert c.percentile(50) == 2.0 and c.percentile(100) == 4.0 and c.percentile(0) == 1.0
    assert "VIOLATION" in str(c)
    assert not mstp.Check("x", (mstp.Sample(0, 1.0),), missing=("lost",)).ok


# --- timing analysis on octet streams -----------------------------------------------------------


def dut_tx(t_de: float, raw: bytes, lead_s: float, tail_s: float) -> tuple[list[Octet], tuple[float, float]]:
    octets = mstp.to_octets(raw, t_de + lead_s, BAUD)
    return octets, (t_de, octets[-1].t + OCTET_S + tail_s)


def test_de_timing() -> None:
    a, de_a = dut_tx(0.010, mstp.build_frame(0, 7, 5), 30e-6, 20e-6)
    b, de_b = dut_tx(0.020, mstp.build_frame(2, 7, 5), -5e-6, 400e-6)  # DE late, tail too long
    stray = mstp.to_octets(b"\x55", 0.030, BAUD)
    res = mstp.de_timing([de_a, de_b, (0.040, 0.041)], a + b + stray, LIM)
    assert res.lead.values == pytest.approx((30e-6, -5e-6))
    assert res.postdrive.values == pytest.approx((20e-6, 400e-6))
    assert [s.value_s for s in res.lead.violations] == pytest.approx([-5e-6])
    assert [s.value_s for s in res.postdrive.violations] == pytest.approx([400e-6])
    assert res.tx_outside_de == tuple(stray) and res.idle_de == ((0.040, 0.041),)
    assert not res.ok


def test_de_timing_skips_edges_outside_capture() -> None:
    a, (_, fall) = dut_tx(0.0, mstp.build_frame(0, 7, 5), 0.0, 20e-6)
    res = mstp.de_timing([(-math.inf, fall)], a, LIM)
    assert res.lead.samples == () and res.postdrive.values == pytest.approx((20e-6,))


def test_turnaround_ignores_own_octets_and_catches_collisions() -> None:
    token = mstp.to_octets(mstp.build_frame(0, 5, 7), 0.0, BAUD)
    token_end = token[-1].t + OCTET_S
    reply, de = dut_tx(token_end + 2e-3, mstp.build_frame(6, 255, 5, IAM), 20e-6, 20e-6)
    bus = token + reply  # a sniffer sees the DUT's own frame too
    check = mstp.turnaround([de], bus, LIM)
    assert check.values == pytest.approx((2e-3,)) and check.ok
    early = mstp.turnaround([(token_end - 1e-3, token_end + 0.02)], token, LIM)  # DE during the token
    assert early.values[0] < 0 and not early.ok


def test_frame_gap() -> None:
    raw = mstp.build_frame(6, 255, 5, IAM)
    octets = mstp.to_octets(raw, 0.0, BAUD)
    octets[10:] = [o._replace(t=o.t + 25 / BAUD) for o in octets[10:]]
    check = mstp.frame_gap(mstp.parse_frames(octets, BAUD), LIM)
    assert check.values == pytest.approx((25 / BAUD,)) and not check.ok
    assert mstp.frame_gap(frames_of((0.0, raw)), LIM).ok


def test_split_and_first_responses() -> None:
    token_to_dut = mstp.build_frame(FrameType.TOKEN, 5, 7)
    reply = mstp.build_frame(FrameType.POLL_FOR_MASTER, 6, 5)
    frames = frames_of((0.0, token_to_dut), (0.005, reply), (0.100, token_to_dut))
    own, others = mstp.split_by_de(frames, [(0.004, 0.008)])
    assert [f.t_start for f in own] == [0.005] and [f.t_start for f in others] == [0.0, 0.100]
    pairs = mstp.first_responses(others, own)
    assert pairs[0][1] is own[0] and pairs[1][1] is None


def test_usage_delay() -> None:
    rx = frames_of(
        (0.0, mstp.build_frame(0, 5, 7)),
        (0.100, mstp.build_frame(1, 5, 7)),
        (0.200, mstp.build_frame(0, 5, 7)),
        (0.300, mstp.build_frame(0, 9, 7)),
    )
    tx = frames_of(
        (rx[0].t_end + 0.004, mstp.build_frame(0, 7, 5)), (rx[1].t_end + 0.020, mstp.build_frame(2, 7, 5))
    )
    check = mstp.usage_delay(rx, tx, 5, LIM)
    assert check.values == pytest.approx((0.004, 0.020))
    assert [s.value_s for s in check.violations] == pytest.approx([0.020])
    assert len(check.missing) == 1 and "not used" in check.missing[0]  # token at 0.200; 0.300 is not ours


def test_reply_delay() -> None:
    der = mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, 5, 7, RP_REQ)
    test_req = mstp.build_frame(FrameType.TEST_REQUEST, 5, 7, b"abc")
    rx = frames_of((0.0, der), (0.5, der), (1.0, test_req), (1.5, der), (2.0, der))
    tx = frames_of(
        (rx[0].t_end + 0.010, mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, 7, 5, IAM)),
        (rx[1].t_end + 0.240, mstp.build_frame(FrameType.REPLY_POSTPONED, 7, 5)),
        (rx[2].t_end + 0.001, mstp.build_frame(FrameType.TEST_RESPONSE, 7, 5, b"abc")),
        (rx[3].t_end + 0.300, mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, 7, 5, IAM)),
        (rx[4].t_end + 0.001, mstp.build_frame(FrameType.TOKEN, 7, 5)),  # not a reply
    )
    check = mstp.reply_delay(rx, tx, 5, LIM)
    assert check.values == pytest.approx((0.010, 0.240, 0.001, 0.300))
    assert [s.value_s for s in check.violations] == pytest.approx([0.300])
    assert len(check.missing) == 1 and "TOKEN" in check.missing[0]


def test_no_token_window() -> None:
    last = mstp.to_octets(mstp.build_frame(0, 8, 7), 0.0, BAUD)
    end = last[-1].t + OCTET_S
    pfm = mstp.build_frame(FrameType.POLL_FOR_MASTER, 6, 5)
    tx = frames_of((end + 0.555, pfm), (end + 0.555 + 0.030, pfm))
    bus = last + [o for f in tx for o in f.octets]
    check = mstp.no_token_window(bus, tx, 5, LIM)
    assert (
        check.values == pytest.approx((0.555,)) and check.ok
    )  # the second PFM followed 30 ms of silence only
    assert not mstp.no_token_window(bus, tx, 6, LIM).ok  # station 6 must wait 560..570 ms
    assert mstp.no_token_window(bus, tx, 6, LIM, tolerance_s=0.006).ok


# --- pcap -----------------------------------------------------------------------------------


def test_write_pcap_header_and_records(tmp_path: Path) -> None:
    frames = frames_of((1.25, mstp.build_frame(0, 5, 7)), (0.5, mstp.build_frame(1, 6, 5)))
    path = tmp_path / "x.pcap"
    assert mstp.write_pcap(path, frames, lambda t: 10**18 + round(t * 1e9)) == 2
    blob = path.read_bytes()
    assert blob[:4] == bytes.fromhex("4d3cb2a1") and blob[20:24] == (165).to_bytes(4, "little")
    first = blob[24:40]
    assert (
        int.from_bytes(first[:4], "little") == 10**9 and int.from_bytes(first[4:8], "little") == 500_000_000
    )
    assert blob[40:48] == mstp.build_frame(1, 6, 5)  # records sorted by time


@pytest.mark.skipif(shutil.which("tshark") is None, reason="tshark not installed")
def test_pcap_dissects_in_tshark(tmp_path: Path) -> None:
    n = 1000
    big = bytes.fromhex("010030010c0c02000001194d3e") + bytes([0x75, 0xFE, (n + 1) >> 8, (n + 1) & 0xFF, 0])
    big += b"A" * n + b"\x3f"  # ReadProperty-ACK with a long Object_Name: needs an extended frame
    raws = [
        mstp.build_frame(FrameType.TOKEN, 5, 7),
        mstp.build_frame(FrameType.POLL_FOR_MASTER, 6, 5),
        mstp.build_frame(FrameType.REPLY_TO_POLL_FOR_MASTER, 5, 6),
        mstp.build_frame(FrameType.TEST_REQUEST, 5, 7, b"\x00\x55\xff"),
        mstp.build_frame(FrameType.TEST_RESPONSE, 7, 5, b"\x00\x55\xff"),
        mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, 5, 7, RP_REQ),
        mstp.build_frame(FrameType.REPLY_POSTPONED, 7, 5),
        mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, 255, 5, IAM),
        mstp.build_frame(FrameType.BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY, 7, 5, big),
    ]
    frames = frames_of(*((0.1 * i, raw) for i, raw in enumerate(raws)))
    assert all(f.ok for f in frames)
    pcap = tmp_path / "mstp.pcap"
    mstp.write_pcap(pcap, frames, lambda t: 1_790_000_000 * 10**9 + round(t * 1e9))
    fields = ["frame.time_epoch", "mstp.frame_type", "mstp.src", "mstp.dst", "mstp.checksum.status",
              "bacapp.type", "bacapp.confirmed_service", "bacapp.unconfirmed_service",
              "_ws.malformed"]  # fmt: skip
    out = subprocess.run(
        [
            "tshark",
            "-r",
            str(pcap),
            "-T",
            "fields",
            "-E",
            "separator=|",
            *(a for f in fields for a in ("-e", f)),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    rows = [dict(zip(fields, line.split("|"), strict=True)) for line in out]
    assert [int(r["mstp.frame_type"]) for r in rows] == [f.ftype for f in frames]
    assert [(int(r["mstp.src"]), int(r["mstp.dst"])) for r in rows] == [(f.src, f.dst) for f in frames]
    assert all(r["_ws.malformed"] == "" for r in rows)
    # types 0-7: header (and data) CRC verified Good (1); for 32/33 Wireshark 4.2 always says
    # Good, so the evidence there is that the COBS payload was decoded and dissected (review).
    assert all(set(r["mstp.checksum.status"].split(",")) == {"1"} for r in rows[:-1])
    assert rows[5]["bacapp.confirmed_service"] == "12"  # ReadProperty request
    assert rows[7]["bacapp.unconfirmed_service"] == "0"  # I-Am
    assert rows[8]["bacapp.type"] == "3" and rows[8]["bacapp.confirmed_service"] == "12"  # Complex-ACK
    assert float(rows[3]["frame.time_epoch"]) == pytest.approx(1_790_000_000.3, abs=1e-6)
    warnings = "_ws.malformed || _ws.expert.severity >= 0x00600000"
    malformed = subprocess.run(
        ["tshark", "-r", str(pcap), "-Y", warnings], check=True, capture_output=True, text=True
    ).stdout
    assert malformed.strip() == ""


@pytest.mark.skipif(shutil.which("tshark") is None, reason="tshark not installed")
def test_pcap_bad_crc_flagged_by_tshark(tmp_path: Path) -> None:
    frames = frames_of(
        (0.0, mstp.build_frame(0, 5, 7, bad_header_crc=True)),
        (0.1, mstp.build_frame(5, 5, 7, RP_REQ, bad_data_crc=True)),
    )
    pcap = tmp_path / "bad.pcap"
    mstp.write_pcap(pcap, frames)
    out = subprocess.run(["tshark", "-r", str(pcap), "-T", "fields", "-e", "mstp.checksum.status"],
                         check=True, capture_output=True, text=True).stdout.split()  # fmt: skip
    assert out == ["0", "1,0"]  # header bad; header good + data bad
