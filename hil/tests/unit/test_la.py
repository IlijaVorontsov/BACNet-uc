"""Unit tests of hilrig.la (sigrok side) on synthetic MS/TP bench captures.

The generator below is the v2 port of the prototype gen_wave.py: it paints DUT DE / TX and the
bus sniffer RO for a short MS/TP exchange with known timing, writes it as a sigrok session file
(or raw binary), and the tests check that capture reading, UART decoding and the Clause 9
analysis recover every edge within one sample, at 9600, 38400 and 115200 baud. sigrok-cli is
used, where installed, as an independent reader, to run the bacnet_mstp decoder, and with its
'demo' driver for a real acquisition without hardware.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from hilrig import la, mstp
from hilrig.mstp import FrameType

FS = 24_000_000  # FX2 rate on the bench
CH = {"de": 0, "tx": 1, "bus": 2, "mark": 3}  # v1 design 6.1: CH0 DE, CH1 DUT TX, CH2 sniffer RO
DUT, MASTER = 5, 7
IAM = bytes.fromhex("01001000c4020000012201e09103210f")
RP_REQ = bytes.fromhex("01040005010c0c02000001194d")
RP_ACK = bytes.fromhex("010030010c0c02000001194d3e7504004455543f")
DECODERS = Path(__file__).resolve().parents[2] / "decoders"
needs_sigrok = pytest.mark.skipif(shutil.which("sigrok-cli") is None, reason="sigrok-cli not installed")


@dataclass
class SyntheticBus:
    """An 8-channel capture painted edge by edge; also records the ground truth."""

    baud: int
    duration_s: float
    fs: int = FS
    bus_delay_s: float = 150e-9  # transceiver propagation DUT DI -> sniffer RO
    samples: np.ndarray = field(init=False)
    de: list[tuple[float, float]] = field(default_factory=list)
    tx_starts: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.samples = np.full(round(self.duration_s * self.fs), (1 << CH["tx"]) | (1 << CH["bus"]), np.uint8)

    def q(self, t: float) -> float:
        """Where an edge painted at ``t`` shows up: the nearest sample."""
        return round(t * self.fs) / self.fs

    def paint(self, ch: int, t0: float, t1: float, level: int) -> None:
        a, b = round(t0 * self.fs), round(t1 * self.fs)
        if level:
            self.samples[a:b] |= np.uint8(1 << ch)
        else:
            self.samples[a:b] &= np.uint8(~(1 << ch) & 0xFF)

    def uart(self, ch: int, t0: float, data: bytes) -> tuple[list[float], float]:
        """Paint 8N1 characters back to back; return their start times and the end of the last."""
        bit = 1.0 / self.baud
        starts = [t0 + i * 10 * bit for i in range(len(data))]
        for start, octet in zip(starts, data, strict=True):
            for k, level in enumerate([0] + [(octet >> j) & 1 for j in range(8)] + [1]):
                self.paint(ch, start + k * bit, start + (k + 1) * bit, level)
        return starts, t0 + len(data) * 10 * bit

    def master(self, t: float, raw: bytes) -> float:
        """Another node transmits: bus only. Returns the end of the last stop bit."""
        return self.uart(CH["bus"], t, raw)[1]

    def dut(self, t_de: float, raw: bytes, lead_s: float, tail_s: float) -> float:
        """The DUT transmits with DE rising at ``t_de``; returns the DE fall time."""
        starts, end = self.uart(CH["tx"], t_de + lead_s, raw)
        self.uart(CH["bus"], t_de + lead_s + self.bus_delay_s, raw)
        self.paint(CH["de"], t_de, end + tail_s, 1)
        self.de.append((t_de, end + tail_s))
        self.tx_starts += starts
        return end + tail_s

    def write_sr(self, path: Path, chunk: int = 1 << 18) -> Path:
        """sigrok session file, format version 2, as sigrok-cli 0.7.2 writes it."""
        meta = (
            "[global]\nsigrok version=0.5.2\n\n[device 1]\ncapturefile=logic-1\ntotal probes=8\n"
            f"samplerate={self.fs // 1_000_000} MHz\ntotal analog=0\n"
            + "".join(f"probe{i + 1}={i}\n" for i in range(8))
            + "unitsize=1\n"
        )
        raw = self.samples.tobytes()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("version", "2")
            zf.writestr("metadata", meta)
            for n, i in enumerate(range(0, len(raw), chunk), start=1):
                zf.writestr(f"logic-1-{n}", raw[i : i + chunk])
        return path


@dataclass(frozen=True)
class Scenario:
    bus: SyntheticBus
    turnaround_s: float  # end of the Token -> DE rise
    usage_s: float  # end of the second Token -> DE rise
    reply_s: float  # end of the DER -> DE rise
    lead_s: float  # DE rise -> start bit
    tail_s: float  # end of stop bit -> DE fall


def scenario(baud: int, turnaround_bits: float = 60, lead_16ths: int = 17, tail_16ths: int = 19) -> Scenario:
    """Token to the DUT, DUT I-Am then token back; a bad-CRC DER (ignored); a DER that is
    answered; a Token the DUT uses for a Poll-For-Master. DE lead/tail are given in 1/16 bit,
    like the STM32 DEAT/DEDT fields, and deliberately not multiples of the sample period."""
    bit = 1.0 / baud
    sc = Scenario(
        SyntheticBus(baud, duration_s=0.002 + 12 * 400 * bit + 0.020),
        turnaround_s=turnaround_bits * bit, usage_s=70 * bit + 0.001, reply_s=90 * bit + 0.002,
        lead_s=lead_16ths * bit / 16, tail_s=tail_16ths * bit / 16,
    )  # fmt: skip
    b, lead, tail, frame_s = sc.bus, sc.lead_s, sc.tail_s, 400 * bit
    end = b.master(0.001, mstp.build_frame(FrameType.TOKEN, DUT, MASTER))
    iam = mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, 255, DUT, IAM)
    fall = b.dut(end + sc.turnaround_s, iam, lead, tail)
    fall = b.dut(fall + 30 * bit, mstp.build_frame(FrameType.TOKEN, MASTER, DUT), lead, tail)
    der = mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, DUT, MASTER, RP_REQ, bad_data_crc=True)
    end = b.master(fall + frame_s, der)
    end = b.master(
        end + frame_s, mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, DUT, MASTER, RP_REQ)
    )
    ack = mstp.build_frame(FrameType.BACNET_DATA_NOT_EXPECTING_REPLY, MASTER, DUT, RP_ACK)
    fall = b.dut(end + sc.reply_s, ack, lead, tail)
    end = b.master(fall + frame_s, mstp.build_frame(FrameType.TOKEN, DUT, MASTER))
    b.dut(end + sc.usage_s, mstp.build_frame(FrameType.POLL_FOR_MASTER, DUT + 1, DUT), lead, tail)
    return sc


# --- Signal and UART decoding ------------------------------------------------------------


def test_signal_levels_edges_and_intervals() -> None:
    low = la.Signal(0, np.array([1.0, 2.0, 3.0]))
    assert [low.level_at(t) for t in (0.5, 1.0, 1.5, 2.5, 3.5)] == [0, 1, 1, 0, 1]
    assert low.rising.tolist() == [1.0, 3.0] and low.falling.tolist() == [2.0]
    assert low.intervals() == [(1.0, 2.0), (3.0, np.inf)]
    assert low.intervals(level=0) == [(-np.inf, 1.0), (2.0, 3.0)]
    high = la.Signal(1, np.array([1.0]))
    assert high.intervals() == [(-np.inf, 1.0)] and high.falling.tolist() == [1.0]


def uart_signal(baud: int, bits: list[int], t0: float = 0.001) -> la.Signal:
    """Signal from a list of bit levels starting at t0 (idle high before)."""
    toggles, level = [], 1
    for i, b in enumerate([*bits, 1]):
        if b != level:
            toggles.append(t0 + i / baud)
            level = b
    return la.Signal(1, np.array(toggles))


def char(value: int, stop: int = 1) -> list[int]:
    return [0] + [(value >> j) & 1 for j in range(8)] + [stop]


def test_uart_decode_framing_error_glitch_and_break() -> None:
    baud = 9600
    bits = char(0x55) + char(0xA3, stop=0) + [1] * 3 + char(0x00) + [1] * 5
    octets = la.uart_decode(uart_signal(baud, bits), baud)
    assert [(o.value, o.framing_error) for o in octets] == [(0x55, False), (0xA3, True), (0x00, False)]
    assert octets[2].t == pytest.approx(0.001 + 23 / baud)
    glitch = la.Signal(1, np.array([0.001, 0.001 + 0.3 / baud]))  # 0.3-bit low pulse
    assert la.uart_decode(glitch, baud) == []
    brk = la.Signal(1, np.array([0.001, 0.001 + 25 / baud]))  # line held low for 25 bits
    assert [(o.value, o.framing_error) for o in la.uart_decode(brk, baud)] == [(0x00, True)]


# --- reading captures ----------------------------------------------------------------------


def test_read_sr_recovers_generated_edges(tmp_path: Path) -> None:
    sc = scenario(38400)
    b = sc.bus
    trace = la.read_sr(b.write_sr(tmp_path / "x.sr", chunk=4099), CH)  # odd chunk size: edges across chunks
    assert trace.samplerate == FS and trace.duration_s == pytest.approx(len(b.samples) / FS)
    assert trace["de"].intervals() == [(b.q(r), b.q(f)) for r, f in b.de]
    tx = trace.uart("tx", b.baud)
    assert [o.t for o in tx] == pytest.approx([b.q(t) for t in b.tx_starts], abs=1e-12)
    assert trace["mark"].toggles.size == 0 and trace["mark"].initial == 0
    with pytest.raises(KeyError, match="no LA channel"):
        trace["nrst"]


def test_read_sr_rejects_bad_channel_map(tmp_path: Path) -> None:
    path = scenario(115200).bus.write_sr(tmp_path / "x.sr")
    with pytest.raises(la.LaError, match="outside"):
        la.read_sr(path, {"de": 8})


@pytest.mark.parametrize("baud", [9600, 38400, 115200])
def test_mstp_timing_recovered_within_one_sample(tmp_path: Path, baud: int) -> None:
    sc = scenario(baud)
    b, lim, one = sc.bus, mstp.Limits(baud), 1.0 / FS
    trace = la.read_sr(b.write_sr(tmp_path / "x.sr"), CH)
    de = trace["de"].intervals()
    tx_octets = trace.uart("tx", baud)
    bus_octets = trace.uart("bus", baud)
    tx = mstp.parse_frames(tx_octets, baud)
    bus = mstp.parse_frames(bus_octets, baud)
    assert [f.error for f in bus].count(mstp.RxError.BAD_DATA_CRC) == 1 and len(bus) == 8
    assert [f.raw for f in tx] == [f.raw for f in mstp.split_by_de(bus, de)[0]]
    rx = mstp.split_by_de(bus, de)[1]

    timing = mstp.de_timing(de, tx_octets, lim)
    assert timing.ok, (str(timing.lead), str(timing.postdrive))
    assert timing.lead.values == pytest.approx([sc.lead_s] * 4, abs=one)
    assert timing.postdrive.values == pytest.approx([sc.tail_s] * 4, abs=one)

    turn = mstp.turnaround(de, bus_octets, lim)
    assert turn.ok
    assert [turn.values[i] for i in (0, 2, 3)] == pytest.approx(
        [sc.turnaround_s, sc.reply_s, sc.usage_s], abs=one
    )
    gap = mstp.frame_gap(tx, lim)
    assert gap.ok and max(gap.values) == pytest.approx(0.0, abs=one)
    usage = mstp.usage_delay(rx, tx, DUT, lim)
    assert usage.ok and usage.values == pytest.approx(
        [sc.turnaround_s + sc.lead_s, sc.usage_s + sc.lead_s], abs=one
    )
    reply = mstp.reply_delay(rx, tx, DUT, lim)
    assert reply.ok and reply.values == pytest.approx([sc.reply_s + sc.lead_s], abs=one)
    # the bad-CRC request got no answer: the next foreign frame came first
    bad = [pair for pair in mstp.first_responses(rx, tx) if pair[0].error]
    assert len(bad) == 1 and bad[0][1] is None


def test_turnaround_violation_detected(tmp_path: Path) -> None:
    baud = 115200
    trace = la.read_sr(scenario(baud, turnaround_bits=30).bus.write_sr(tmp_path / "x.sr"), CH)
    check = mstp.turnaround(trace["de"].intervals(), trace.uart("bus", baud), mstp.Limits(baud))
    assert not check.ok and check.violations[0].value_s == pytest.approx(30 / baud, abs=1 / FS)


# --- sigrok-cli -----------------------------------------------------------------------------


def parse_vcd(text: str) -> dict[str, list[tuple[int, int]]]:
    """sigrok VCD export -> {name: [(time in timescale units, level), ...]}."""
    ids = {m.group(1): m.group(2) for m in re.finditer(r"\$var wire 1 (\S+) (\S+) \$end", text)}
    changes: dict[str, list[tuple[int, int]]] = {name: [] for name in ids.values()}
    t = 0
    for tok in text.split("$enddefinitions $end", 1)[1].split():
        if tok.startswith("#"):
            t = int(tok[1:])
        elif tok[0] in "01" and tok[1:] in ids:
            changes[ids[tok[1:]]].append((t, int(tok[0])))
    return changes


def sigrok_vcd(path: Path) -> str:
    """All channels of a session file as VCD, exported by sigrok-cli."""
    cmd = ["sigrok-cli", "-i", str(path), "-O", "vcd"]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def vcd_unit(text: str) -> float:
    m = re.search(r"\$timescale (\d+) (s|ms|us|ns|ps) \$end", text)
    assert m, "no timescale in VCD"
    return int(m.group(1)) * {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12}[m.group(2)]


def assert_matches_vcd(
    trace: la.Trace, vcd: dict[str, list[tuple[int, int]]], names: dict[str, str], unit_s: float
) -> None:
    for ours, theirs in names.items():
        changes = vcd[theirs]
        assert trace[ours].initial == changes[0][1]
        levels = [lv for _, lv in changes]
        toggles = [t * unit_s for (t, lv), prev in zip(changes[1:], levels[:-1], strict=True) if lv != prev]
        assert trace[ours].toggles.tolist() == pytest.approx(toggles, abs=unit_s / 2)


@needs_sigrok
def test_sigrok_cli_agrees_with_read_sr(tmp_path: Path) -> None:
    b = scenario(115200).bus
    path = b.write_sr(tmp_path / "x.sr")
    show = subprocess.run(
        ["sigrok-cli", "-i", str(path), "--show"], check=True, capture_output=True, text=True
    ).stdout
    assert f"Samplerate: {FS}" in show and f"Logic sample count: {len(b.samples)}" in show
    vcd = sigrok_vcd(path)
    assert_matches_vcd(
        la.read_sr(path, CH), parse_vcd(vcd), {"de": "0", "tx": "1", "bus": "2"}, vcd_unit(vcd)
    )


@needs_sigrok
def test_binary_input_converted_by_sigrok_reads_the_same(tmp_path: Path) -> None:
    b = scenario(38400).bus
    (tmp_path / "x.bin").write_bytes(b.samples.tobytes())
    subprocess.run(
        ["sigrok-cli", "-I", f"binary:numchannels=8:samplerate={FS}", "-i", str(tmp_path / "x.bin"),
         "-o", str(tmp_path / "conv.sr")], check=True, capture_output=True,
    )  # fmt: skip
    ours = la.read_sr(b.write_sr(tmp_path / "x.sr"), CH)
    theirs = la.read_sr(tmp_path / "conv.sr", CH)
    for name in CH:
        assert theirs[name].initial == ours[name].initial
        assert np.array_equal(theirs[name].toggles, ours[name].toggles)


@needs_sigrok
def test_bacnet_mstp_decoder_in_sigrok_cli(tmp_path: Path) -> None:
    baud = 38400
    path = scenario(baud).bus.write_sr(tmp_path / "x.sr")
    out = subprocess.run(
        ["sigrok-cli", "-i", str(path), "-P", f"uart:rx=2:baudrate={baud},bacnet_mstp:baudrate={baud}",
         "-A", "bacnet_mstp=frame", "--protocol-decoder-samplenum"],
        check=True, capture_output=True, text=True, env={**os.environ, "SIGROKDECODE_DIR": str(DECODERS)},
    ).stdout  # fmt: skip
    got = []
    for line in out.splitlines():
        m = re.match(r"(\d+)-(\d+) bacnet_mstp-1: (FRAME .*)", line)
        if m:
            fields = dict(kv.split("=", 1) for kv in m.group(3).split()[1:])
            got.append(
                (int(m.group(1)), int(fields["ft"]), int(fields["src"]), int(fields["dst"]), fields["err"])
            )
    frames = mstp.parse_frames(la.read_sr(path, CH).uart("bus", baud), baud)
    expected = [(round(f.t_start * FS), f.ftype, f.src, f.dst, f.error or "-") for f in frames]
    assert [g[1:] for g in got] == [e[1:] for e in expected]
    assert all(abs(g[0] - e[0]) <= 1 for g, e in zip(got, expected, strict=True))
    assert "hcrc=ok dcrc=bad err=bad_data_crc" in out


@needs_sigrok
def test_bacnet_mstp_decoder_receive_errors_and_extended_frames(tmp_path: Path) -> None:
    baud = 115200
    bit = 1.0 / baud
    b = SyntheticBus(baud, duration_s=0.2)
    end = b.master(
        0.001, mstp.build_frame(FrameType.BACNET_DATA_EXPECTING_REPLY, DUT, MASTER, RP_REQ, truncate=12)
    )
    starts, end = b.uart(CH["bus"], end + 0.005, mstp.build_frame(FrameType.TOKEN, DUT, MASTER))
    b.paint(CH["bus"], starts[4] + 9 * bit, starts[4] + 10 * bit, 0)  # framing error in the header
    ext = mstp.build_frame(
        FrameType.BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY, MASTER, DUT, bytes(300) + RP_ACK * 10
    )
    end = b.master(end + 0.005, ext)
    b.master(end + 0.005, mstp.build_frame(FrameType.TOKEN, DUT, MASTER, bad_header_crc=True))
    path = b.write_sr(tmp_path / "errors.sr")
    out = subprocess.run(
        ["sigrok-cli", "-i", str(path), "-P", f"uart:rx=2:baudrate={baud},bacnet_mstp:baudrate={baud}",
         "-A", "bacnet_mstp"],
        check=True, capture_output=True, text=True, env={**os.environ, "SIGROKDECODE_DIR": str(DECODERS)},
    ).stdout  # fmt: skip
    errors = re.findall(r"FRAME .* err=(\S+)", out)
    expected = [f.error or "-" for f in mstp.parse_frames(la.read_sr(path, CH).uart("bus", baud), baud)]
    assert errors == expected == ["timeout", "receive_error", "-", "bad_header_crc"]
    for text in ("Preamble", "Dst 5", "Src 7", "Len 13", "Header CRC ok", "Header CRC bad", "Data CRC ok"):
        assert f"bacnet_mstp-1: {text}" in out, text
    assert "type=BACNET_EXTENDED_DATA_NOT_EXPECTING_REPLY ft=33" in out


@needs_sigrok
def test_sigrok_la_demo_acquisition(tmp_path: Path) -> None:
    """A real sigrok-cli acquisition (demo driver), read back and checked against its VCD export."""
    channels = {"d0": 0, "d1": 1, "d7": 7}
    analyzer = la.open_analyzer("demo:analog_channels=0", channels, samplerate=1_000_000, settle_s=0.05)
    assert isinstance(analyzer, la.SigrokLA)
    with analyzer.acquire(0.2, tmp_path / "cap") as acq:
        pass
    trace = acq.trace
    assert trace is not None and trace.samplerate == 1_000_000
    assert trace.duration_s == pytest.approx(0.2, rel=0.1)
    assert trace["d0"].toggles.size > 10
    sr = tmp_path / "cap" / "capture.sr"
    vcd = sigrok_vcd(sr)
    assert_matches_vcd(trace, parse_vcd(vcd), {"d0": "D0", "d1": "D1", "d7": "D7"}, vcd_unit(vcd))


@needs_sigrok
def test_channel_subset_capture_rejected(tmp_path: Path) -> None:
    sr = tmp_path / "subset.sr"
    demo = ["sigrok-cli", "-d", "demo:analog_channels=0", "--config", "samplerate=1000000"]
    subprocess.run(
        [*demo, "--samples", "1000", "-C", "D0,D2", "-o", str(sr)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    with pytest.raises(la.LaError, match="channel-subset"):
        la.read_sr(sr, {"d0": 0})


@needs_sigrok
def test_sigrok_la_failure_and_abort(tmp_path: Path) -> None:
    broken = la.SigrokLA("no-such-driver", {"d0": 0}, settle_s=0.0)
    with pytest.raises(la.LaError, match="sigrok-cli failed"):
        broken.acquire(0.1, tmp_path / "a").wait()
    long = la.SigrokLA("demo:analog_channels=0", {"d0": 0}, samplerate=100_000, settle_s=0.05)
    acq = long.acquire(60.0, tmp_path / "b")
    with pytest.raises(RuntimeError, match="test body failed"), acq:
        raise RuntimeError("test body failed")
    assert isinstance(acq, la._SigrokAcquisition)
    assert acq.trace is None and acq.proc.poll() is not None  # sigrok-cli was stopped, not left running


def test_sigrok_command_captures_all_channels(tmp_path: Path) -> None:
    analyzer = la.open_analyzer("fx2lafw", CH)
    assert isinstance(analyzer, la.SigrokLA)
    cmd = analyzer.command(2.0, tmp_path / "c.sr")
    assert cmd == ["sigrok-cli", "-d", "fx2lafw", "--config", "samplerate=24000000", "--time", "2000ms",
                   "-o", str(tmp_path / "c.sr")]  # fmt: skip
    assert "-C" not in cmd  # never select channels: libsigrok 0.5.2 mislabels them
