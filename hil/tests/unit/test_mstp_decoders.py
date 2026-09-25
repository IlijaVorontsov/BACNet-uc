"""The two self-contained MS/TP decoders agree with hilrig.mstp on the same octet streams.

hil/decoders/bacnet_mstp/core.py (the sigrok decoder's frame assembly) and
hil/saleae/bacnet_mstp/HighLevelAnalyzer.py (Logic 2 HLA) each repeat the CRC, COBS and
receive-FSM code, because sigrok and Logic 2 load them as standalone directories. This test
feeds all three the same streams, faults included, and requires identical frames and errors.
The HLA runs against a stub of Logic 2's ``saleae.analyzers`` module.
"""

from __future__ import annotations

import importlib.util
import random
import sys
import types
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hilrig import mstp

HIL = Path(__file__).resolve().parents[2]
BAUD = 38400
FS = 4_000_000  # sample rate for the sigrok core's sample numbers
BIT = 1.0 / BAUD


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=None)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def corpus(seed: int) -> list[mstp.Octet]:
    """Frames of all kinds with faults, separated by gaps well away from the abort boundary."""
    rng = random.Random(seed)
    octets: list[mstp.Octet] = []
    t = 0.0
    for _ in range(300):
        kind = rng.randrange(9)
        ftype = rng.choice([0, 1, 2, 3, 4, 5, 6, 7, 32, 33, 200])
        data = bytes(rng.choice([0, 0x55, 0xFF, rng.randrange(256)]) for _ in range(rng.randrange(1, 40)))
        if ftype in (0, 1, 2) and kind != 3:
            data = b""
        raw = mstp.build_frame(
            ftype, rng.randrange(256), rng.randrange(256), data,
            bad_header_crc=kind == 1, bad_data_crc=kind == 2 and bool(data),
            truncate=rng.randrange(1, 12) if kind == 3 else None,
            length=rng.randrange(0, 80) if kind == 4 else None,
        )  # fmt: skip
        if kind == 5:
            raw = bytes(rng.randrange(256) for _ in range(5)) + raw  # line noise first
        chunk = mstp.to_octets(raw, t, BAUD, gap_s=rng.choice([0.0, 0.0, 15 * BIT]))
        if kind == 6 and len(chunk) > 3:  # framing error somewhere
            i = rng.randrange(len(chunk))
            chunk[i] = chunk[i]._replace(framing_error=True)
        if kind == 7 and len(chunk) > 4:  # stall inside the frame: longer than Tframe_abort
            i = rng.randrange(2, len(chunk))
            chunk[i:] = [o._replace(t=o.t + 200 * BIT) for o in chunk[i:]]
        octets += chunk
        t = chunk[-1].t + mstp.OCTET_BITS * BIT + rng.choice([30, 100, 400]) * BIT
    return octets


def reference(octets: list[mstp.Octet]) -> list[tuple[bytes, str | None, bytes]]:
    parser = mstp.FrameParser(BAUD)
    return [(f.raw, f.error, f.data) for f in map(parser.feed, octets) if f is not None]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sigrok_core_matches_hilrig(seed: int) -> None:
    core = load(HIL / "decoders/bacnet_mstp/core.py", "bacnet_mstp_core")
    asm = core.Assembler(abort=mstp.FRAME_ABORT_BITS * FS / BAUD)
    got = []
    for o in corpus(seed):
        ss = round(o.t * FS)
        record = asm.feed(
            core.Octet(ss, ss + round(mstp.OCTET_BITS * FS / BAUD), o.value, not o.framing_error)
        )
        if record is not None:
            got.append((bytes(x.value for x in record.octets), record.error, record.data))
    expected = reference(corpus(seed))
    assert {e for _, e, _ in expected} >= {None, "timeout", "receive_error", "bad_header_crc", "bad_data_crc"}
    assert got == expected


@pytest.fixture
def hla_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """HighLevelAnalyzer.py imported against a minimal stand-in for Logic 2's API."""
    api = types.ModuleType("saleae.analyzers")

    class AnalyzerFrame:
        def __init__(
            self, type: str, start_time: float, end_time: float, data: dict[str, object] | None = None
        ) -> None:
            self.type, self.start_time, self.end_time, self.data = type, start_time, end_time, data or {}

    class ChoicesSetting:
        def __init__(self, choices: tuple[str, ...]) -> None:
            self.choices = choices

    vars(api).update(AnalyzerFrame=AnalyzerFrame, ChoicesSetting=ChoicesSetting, HighLevelAnalyzer=object)
    package = types.ModuleType("saleae")
    vars(package)["analyzers"] = api
    monkeypatch.setitem(sys.modules, "saleae", package)
    monkeypatch.setitem(sys.modules, "saleae.analyzers", api)
    return load(HIL / "saleae/bacnet_mstp/HighLevelAnalyzer.py", "bacnet_mstp_hla")


def make_hla(module: ModuleType, baud: str) -> Any:
    hla = module.MstpHla.__new__(module.MstpHla)
    hla.baud = baud  # Logic 2 sets settings on the instance before __init__
    hla.__init__()
    return hla


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_saleae_hla_matches_hilrig(hla_module: ModuleType, seed: int) -> None:
    assert str(BAUD) in hla_module.MstpHla.baud.choices
    hla = make_hla(hla_module, str(BAUD))
    frame_cls = sys.modules["saleae.analyzers"].AnalyzerFrame
    got = []
    for o in corpus(seed):
        data: dict[str, object] = {"data": bytes([o.value])}
        if o.framing_error:
            data["error"] = "Framing"
        out = hla.decode(frame_cls("data", o.t, o.t + mstp.OCTET_BITS * BIT, data))
        if out is not None:
            d = out.data
            got.append((d["src"], d["dst"], d["length"], d["status"].split(",")[0], out.start_time))
    ref = mstp.FrameParser(BAUD)
    expected = [
        (f.src if f.src is not None else -1, f.dst if f.dst is not None else -1,
         f.length if f.length is not None else -1, f.error or "ok", f.t_start)
        for f in map(ref.feed, corpus(seed)) if f is not None
    ]  # fmt: skip
    assert got == expected


def test_saleae_hla_reports_frame_gap(hla_module: ModuleType) -> None:
    hla = make_hla(hla_module, "9600")
    frame_cls = sys.modules["saleae.analyzers"].AnalyzerFrame
    octets = mstp.to_octets(mstp.build_frame(0, 5, 7), 0.0, 9600, gap_s=25 / 9600)
    outs = [hla.decode(frame_cls("data", o.t, o.t + 10 / 9600, {"data": bytes([o.value])})) for o in octets]
    [frame] = [o for o in outs if o is not None]
    assert frame.data["type"] == "TOKEN" and frame.data["status"] == "ok, Tframe_gap exceeded"
    assert frame.data["max_gap_bits"] == "25.0"
    assert hla.decode(frame_cls("error", 0.0, 0.1, {})) is None  # non-data frames are ignored
