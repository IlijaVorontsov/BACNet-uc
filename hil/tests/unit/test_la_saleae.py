"""Offline tests of the Saleae backend of hilrig.la.

Logic 2 is not needed: the raw-CSV reader runs on written files, and the acquisition sequence
runs against a stand-in Manager/Capture whose every call is bound to the signature of the real
logic2-automation 1.0.11 API (skipped when the 'saleae' extra is not installed).
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from hilrig import la, mstp

BAUD = 115200


def write_csv(path: Path, header: list[str], rows: list[list[float | int]]) -> Path:
    path.write_text("\n".join([",".join(header)] + [",".join(str(v) for v in row) for row in rows]) + "\n")
    return path


def test_read_saleae_csv(tmp_path: Path) -> None:
    rows = [[0.0, 1, 0, 0], [0.001, 1, 1, 0], [0.002, 0, 1, 0], [0.003, 0, 0, 1]]
    path = write_csv(tmp_path / "digital.csv", ["Time [s]", "Channel 0", "Channel 2", "Channel 5"], rows)
    trace = la.read_saleae_csv(path, {"tx": 0, "de": 2, "m0": 5}, 500_000_000, 0.01)
    assert trace["tx"].initial == 1 and trace["tx"].toggles.tolist() == [0.002]
    assert trace["de"].intervals() == [(0.001, 0.003)]
    assert trace["m0"].rising.tolist() == [0.003]
    assert trace.samplerate == 500_000_000 and trace.duration_s == 0.01
    with pytest.raises(la.LaError, match="Channel 3"):
        la.read_saleae_csv(path, {"x": 3}, 1, 1.0)
    empty = write_csv(tmp_path / "empty.csv", ["Time [s]", "Channel 0"], [])
    with pytest.raises(la.LaError, match="no rows"):
        la.read_saleae_csv(empty, {"tx": 0}, 1, 1.0)


def frame_csv(path: Path, raw: bytes, t0: float) -> Path:
    """digital.csv of a UART line on channel 1 carrying ``raw`` (8N1, idle high) and its driver
    enable on channel 0 (high from 1 us before to 1 us after), one row per change."""
    bits = [b for octet in raw for b in [0] + [(octet >> j) & 1 for j in range(8)] + [1]]
    rows: list[list[float | int]] = [[0.0, 0, 1], [t0 - 1e-6, 1, 1]]
    for i, bit in enumerate(bits):
        if bit != rows[-1][2]:
            rows.append([t0 + i / BAUD, 1, bit])
    rows.append([t0 + len(bits) / BAUD + 1e-6, 0, 1])
    return write_csv(path, ["Time [s]", "Channel 0", "Channel 1"], rows)


def test_saleae_csv_feeds_the_mstp_analysis(tmp_path: Path) -> None:
    raw = mstp.build_frame(mstp.FrameType.TEST_REQUEST, 5, 7, bytes(range(40)))
    trace = la.read_saleae_csv(frame_csv(tmp_path / "digital.csv", raw, 0.001), {"tx": 1}, 100_000_000, 0.02)
    [frame] = mstp.parse_frames(trace.uart("tx", BAUD), BAUD)
    assert frame.ok and frame.raw == raw and frame.t_start == pytest.approx(0.001)


# --- acquisition against the real API signatures ------------------------------------------


@pytest.fixture
def automation() -> Any:
    return pytest.importorskip("saleae.automation", reason="logic2-automation (hilrig[saleae]) not installed")


class Recorder:
    """Stand-in for Manager and Capture: checks each call against the real method's signature."""

    def __init__(self, real: type, calls: list[str], hooks: dict[str, Any] | None = None) -> None:
        self.real, self.calls, self.hooks = real, calls, hooks or {}

    def __getattr__(self, name: str) -> Any:
        method = getattr(self.real, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            inspect.signature(method).bind(self, *args, **kwargs)  # TypeError on a wrong name
            self.calls.append(name)
            hook = self.hooks.get(name)
            return hook(*args, **kwargs) if hook else None

        return call


def fake_logic2(
    monkeypatch: pytest.MonkeyPatch, automation: Any, tmp_path: Path, calls: list[str]
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def export(directory: str, **kwargs: Any) -> None:
        seen["export"] = kwargs
        frame_csv(Path(directory) / "digital.csv", mstp.build_frame(0, 5, 7), 0.0005)

    capture = Recorder(automation.Capture, calls, {
        "export_raw_data_csv": export,
        "save_capture": lambda filepath: Path(filepath).write_bytes(b"sal"),
    })  # fmt: skip

    def start_capture(**kwargs: Any) -> Recorder:
        seen["start"] = kwargs
        return capture

    manager = Recorder(automation.Manager, calls, {"start_capture": start_capture})

    def connect(**kwargs: Any) -> Recorder:
        inspect.signature(automation.Manager.connect).bind(**kwargs)
        seen["connect"] = kwargs
        return manager

    monkeypatch.setattr(automation.Manager, "connect", staticmethod(connect))
    return seen


def test_saleae_acquisition_sequence(
    monkeypatch: pytest.MonkeyPatch, automation: Any, tmp_path: Path
) -> None:
    calls: list[str] = []
    seen = fake_logic2(monkeypatch, automation, tmp_path, calls)
    analyzer = la.open_analyzer("saleae", {"tx": 1, "de": 0}, samplerate=100_000_000, port=10431)
    assert isinstance(analyzer, la.SaleaeLA)
    with analyzer.acquire(0.5, tmp_path / "cap") as acq:
        pass
    assert calls == ["start_capture", "wait", "export_raw_data_csv", "save_capture", "close", "close"]
    assert seen["connect"] == {"address": "127.0.0.1", "port": 10431}
    device, capture = seen["start"]["device_configuration"], seen["start"]["capture_configuration"]
    assert isinstance(device, automation.LogicDeviceConfiguration)
    assert device.enabled_digital_channels == [0, 1] and device.digital_sample_rate == 100_000_000
    assert isinstance(capture.capture_mode, automation.TimedCaptureMode)
    assert capture.capture_mode.duration_seconds == 0.5
    assert seen["export"] == {"digital_channels": [0, 1]}
    assert (tmp_path / "cap" / "capture.sal").exists()
    assert acq.trace is not None
    [frame] = mstp.parse_frames(acq.trace.uart("tx", BAUD), BAUD)
    assert frame.ok and frame.ftype == 0
    timing = mstp.de_timing(acq.trace["de"].intervals(), acq.trace.uart("tx", BAUD), mstp.Limits(BAUD))
    assert timing.lead.values == pytest.approx([1e-6]) and timing.postdrive.values == pytest.approx([1e-6])


def test_saleae_abort_closes_everything(
    monkeypatch: pytest.MonkeyPatch, automation: Any, tmp_path: Path
) -> None:
    calls: list[str] = []
    fake_logic2(monkeypatch, automation, tmp_path, calls)
    acq = la.SaleaeLA({"tx": 1}).acquire(10.0, tmp_path / "cap")
    with pytest.raises(RuntimeError), acq:
        raise RuntimeError("test body failed")
    assert calls == ["start_capture", "stop", "close", "close"] and acq.trace is None


def test_saleae_without_the_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "saleae", None)  # import saleae.automation now fails
    monkeypatch.setitem(sys.modules, "saleae.automation", None)
    with pytest.raises(la.LaError, match=r"hilrig\[saleae\]"):
        la.SaleaeLA({"tx": 1}).acquire(1.0, tmp_path)
