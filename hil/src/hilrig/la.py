"""Logic analyzer captures for HIL timing tests: acquisition, per-channel edges, UART decoding.

Two backends share one result type, :class:`Trace` (named digital signals on the analyzer's
timebase), so the MS/TP analysis in :mod:`hilrig.mstp` does not care where samples came from:

- :class:`SigrokLA` runs ``sigrok-cli`` (fx2lafw, dreamsourcelab-dslogic, ...; ``demo`` for
  offline tests), always on every channel, and reads the ``.sr`` session file itself instead of
  going through a sigrok export: sigrok-cli 0.7.2 / libsigrok 0.5.2 mislabel channels when
  ``-C`` selects a subset (v1 review), so :func:`read_sr` also refuses channel-subset files
  rather than guess their bit layout.
- :class:`SaleaeLA` drives Logic 2 through logic2-automation 1.0.11 (install the ``saleae``
  extra) and reads its raw digital CSV export.

Channel names come from bench.yml (``la.channels: {de: 0, tx: 1, bus: 2, ...}``): the number is
the analyzer's channel index (sigrok probe N+1, Saleae "Channel N").

UART decoding is done here in Python on the edge lists, identically for both backends, so an
octet's time is the sample of its start-bit edge and no decoder-specific annotation parsing is
involved.
"""

from __future__ import annotations

import configparser
import csv
import re
import signal
import subprocess
import time
import zipfile
from abc import ABC, abstractmethod
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import TracebackType
from typing import Any

import numpy as np

from hilrig.mstp import OCTET_BITS, Octet


class LaError(RuntimeError):
    """A capture could not be made or read."""


@dataclass(frozen=True, eq=False)
class Signal:
    """One digital channel: its level at the first sample and the times (s) it toggles."""

    initial: int
    toggles: np.ndarray  # float64 seconds, ascending

    def level_at(self, t: float) -> int:
        """Level at time ``t`` (a toggle at exactly ``t`` has already happened)."""
        return self.initial ^ (int(np.searchsorted(self.toggles, t, side="right")) & 1)

    @property
    def rising(self) -> np.ndarray:
        return self.toggles[0::2] if self.initial == 0 else self.toggles[1::2]

    @property
    def falling(self) -> np.ndarray:
        return self.toggles[1::2] if self.initial == 0 else self.toggles[0::2]

    def intervals(self, level: int = 1) -> list[tuple[float, float]]:
        """(start, end) of every stretch at ``level``; -inf/inf where it runs past the capture."""
        edges = [-np.inf] if self.initial == level else []
        edges += self.toggles.tolist()
        if len(edges) % 2:
            edges.append(np.inf)
        return list(zip(edges[0::2], edges[1::2], strict=True))


@dataclass(frozen=True, eq=False)
class Trace:
    """A finished capture: named signals, sample rate and length."""

    samplerate: int
    duration_s: float
    signals: Mapping[str, Signal]

    def __getitem__(self, name: str) -> Signal:
        try:
            return self.signals[name]
        except KeyError:
            raise KeyError(f"no LA channel {name!r}; have {sorted(self.signals)}") from None

    def uart(self, name: str, baud: int) -> list[Octet]:
        """8N1 octets on channel ``name`` (see :func:`uart_decode`)."""
        return uart_decode(self[name], baud)


def uart_decode(sig: Signal, baud: int) -> list[Octet]:
    """Decode 8N1 UART characters (idle high, LSB first) from a signal's edges.

    A start bit is a falling edge that is still low half a bit later; each bit is sampled at
    its centre, and a low stop bit is reported as a framing error. The search for the next
    start bit resumes at the middle of the stop bit, as a UART does.
    """
    bit = 1.0 / baud
    toggles = sig.toggles.tolist()
    octets: list[Octet] = []

    def level(t: float, lo: int) -> int:
        return sig.initial ^ (bisect_right(toggles, t, lo) & 1)

    k = 0
    while k < len(toggles):
        if sig.initial ^ ((k + 1) & 1):  # level after toggle k is high: not a start bit
            k += 1
            continue
        t0 = toggles[k]
        bits = [level(t0 + (j + 0.5) * bit, k) for j in range(OCTET_BITS)]
        if bits[0]:
            k += 1  # glitch shorter than half a bit
            continue
        value = sum(b << j for j, b in enumerate(bits[1:9]))
        octets.append(Octet(t0, value, framing_error=not bits[9]))
        k = bisect_right(toggles, t0 + (OCTET_BITS - 0.5) * bit, k)
    return octets


# --- sigrok ------------------------------------------------------------------------------

_BLOCK = 1 << 24  # sample bytes processed per numpy pass


def _parse_samplerate(text: str) -> int:
    """libsigrok's sr_samplerate_string() format, e.g. '24 MHz', '12.5 MHz', '500 kHz'."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kMGT]?)Hz\s*", text)
    if not m:
        raise LaError(f"unrecognised samplerate {text!r}")
    scale = {"": 1, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}[m.group(2)]
    return int(Fraction(m.group(1)) * scale)


def read_sr(path: Path | str, channels: Mapping[str, int]) -> Trace:
    """Read a sigrok session file (format version 2) into a :class:`Trace`.

    ``channels`` maps names to probe indices (probe ``i+1`` in the metadata, bit ``i`` of each
    sample). Files captured with a channel subset are rejected, see the module docstring.
    """
    with zipfile.ZipFile(path) as zf:
        meta = configparser.ConfigParser(interpolation=None)
        meta.read_string(zf.read("metadata").decode())
        dev = meta["device 1"]
        total = int(dev["total probes"])
        missing = [i for i in range(1, total + 1) if f"probe{i}" not in dev]
        if missing:
            raise LaError(f"{path}: probes {missing} absent (channel-subset capture); capture all channels")
        for name, index in channels.items():
            if not 0 <= index < total:
                raise LaError(f"{path}: channel {name}={index} outside the {total} probes")
        unitsize = int(dev["unitsize"])
        if unitsize not in (1, 2, 4, 8):
            raise LaError(f"{path}: unsupported unitsize {unitsize}")
        samplerate = _parse_samplerate(dev["samplerate"])
        prefix = dev["capturefile"]
        chunks = sorted(
            (n for n in zf.namelist() if n == prefix or n.startswith(prefix + "-")),
            key=lambda n: int(n.rpartition("-")[2]) if n != prefix else 0,
        )
        edges, first, count = _edges(zf, chunks, unitsize, channels)
    signals = {
        name: Signal(int(first >> index & 1), edges[name].astype(np.float64) / samplerate)
        for name, index in channels.items()
    }
    return Trace(samplerate, count / samplerate, signals)


def _edges(
    zf: zipfile.ZipFile, chunks: list[str], unitsize: int, channels: Mapping[str, int]
) -> tuple[dict[str, np.ndarray], int, int]:
    """Toggle sample indices per channel, the first sample and the sample count."""
    dtype = np.dtype(f"<u{unitsize}")
    found: dict[str, list[np.ndarray]] = {name: [] for name in channels}
    prev: np.ndarray | None = None
    first = 0
    offset = 0  # index of the first sample in the current block
    pending: list[bytes] = []

    def process(block: bytes) -> None:
        nonlocal prev, first, offset
        samples = np.frombuffer(block, dtype=dtype)
        if prev is None:
            first = int(samples[0])
            prev = samples[:1]
        changes = np.bitwise_xor(samples, np.concatenate((prev, samples[:-1])))
        where = np.flatnonzero(changes)
        for name, index in channels.items():
            hits = where[((changes[where] >> index) & 1).astype(bool)]
            found[name].append(hits.astype(np.int64) + offset)
        prev = samples[-1:]
        offset += len(samples)

    size = 0
    for name in chunks:
        pending.append(zf.read(name))
        size += len(pending[-1])
        if size >= _BLOCK:
            process(b"".join(pending))
            pending, size = [], 0
    if size:
        process(b"".join(pending))
    if prev is None:
        raise LaError("capture contains no samples")
    return {n: np.concatenate(v) for n, v in found.items()}, first, offset


# --- acquisition interface ---------------------------------------------------------------


class Acquisition(ABC):
    """A capture in progress. As a context manager it waits on exit and stores ``trace``."""

    trace: Trace | None = None

    @abstractmethod
    def wait(self) -> Trace:
        """Block until the capture has finished and return it."""

    @abstractmethod
    def abort(self) -> None:
        """Stop the capture and discard it."""

    def __enter__(self) -> Acquisition:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if exc_type is None:
            self.trace = self.wait()
        else:
            self.abort()


class LogicAnalyzer(ABC):
    """A logic analyzer with named channels."""

    def __init__(self, channels: Mapping[str, int]) -> None:
        self.channels = dict(channels)

    @abstractmethod
    def acquire(self, duration_s: float, workdir: Path) -> Acquisition:
        """Start a timed capture of all channels; raw files are kept in ``workdir``.

        Returns once the analyzer is sampling, so the stimulus can start right away::

            with la.acquire(2.0, artifacts / "mstp06") as acq:
                drive_the_bus()
            trace = acq.trace
        """


class SigrokLA(LogicAnalyzer):
    """sigrok-cli backend. ``driver`` is a sigrok driver spec, e.g. ``fx2lafw`` or ``demo``.

    ``settle_s`` is how long sigrok-cli needs before the device streams (about 0.3 s for
    fx2lafw); sigrok-cli prints nothing when it starts, so this is a fixed wait.
    """

    def __init__(
        self,
        driver: str,
        channels: Mapping[str, int],
        *,
        samplerate: int = 24_000_000,
        settle_s: float = 0.5,
        sigrok_cli: str = "sigrok-cli",
    ) -> None:
        super().__init__(channels)
        self.driver = driver
        self.samplerate = samplerate
        self.settle_s = settle_s
        self.sigrok_cli = sigrok_cli

    def command(self, duration_s: float, out: Path) -> list[str]:
        """The sigrok-cli command line for a capture of ``duration_s`` into ``out``."""
        return [
            self.sigrok_cli,
            "-d", self.driver,
            "--config", f"samplerate={self.samplerate}",
            "--time", f"{round(duration_s * 1000)}ms",
            "-o", str(out),
        ]  # fmt: skip

    def acquire(self, duration_s: float, workdir: Path) -> Acquisition:
        workdir.mkdir(parents=True, exist_ok=True)
        out = workdir / "capture.sr"
        proc = subprocess.Popen(
            self.command(duration_s, out), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        time.sleep(self.settle_s)
        return _SigrokAcquisition(proc, out, self.channels, timeout_s=duration_s + self.settle_s + 30)


class _SigrokAcquisition(Acquisition):
    def __init__(
        self, proc: subprocess.Popen[str], out: Path, channels: Mapping[str, int], timeout_s: float
    ) -> None:
        self.proc, self.out, self.channels, self.timeout_s = proc, out, channels, timeout_s

    def wait(self) -> Trace:
        try:
            _, err = self.proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self.abort()
            raise LaError(f"sigrok-cli did not finish within {self.timeout_s:.0f}s") from None
        if self.proc.returncode != 0 or not self.out.exists():
            raise LaError(f"sigrok-cli failed ({self.proc.returncode}): {err.strip()}")
        return read_sr(self.out, self.channels)

    def abort(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()


# --- Saleae ------------------------------------------------------------------------------


def read_saleae_csv(
    path: Path | str, channels: Mapping[str, int], samplerate: int, duration_s: float
) -> Trace:
    """Read Logic 2's raw digital CSV export (``Time [s]``, ``Channel N`` ...; one row per change)."""
    times: list[float] = []
    levels: dict[str, list[int]] = {name: [] for name in channels}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        absent = [f"Channel {i}" for i in channels.values() if f"Channel {i}" not in header]
        if "Time [s]" not in header or absent:
            raise LaError(f"{path}: expected 'Time [s]' and {absent or 'channel'} columns, got {header}")
        for row in reader:
            times.append(float(row["Time [s]"]))
            for name, index in channels.items():
                levels[name].append(int(row[f"Channel {index}"]))
    if not times:
        raise LaError(f"{path}: no rows")
    t = np.asarray(times)
    signals = {}
    for name, values in levels.items():
        v = np.asarray(values)
        changed = np.flatnonzero(v[1:] != v[:-1]) + 1
        signals[name] = Signal(int(v[0]), t[changed])
    return Trace(samplerate, duration_s, signals)


class SaleaeLA(LogicAnalyzer):
    """Saleae Logic 2 backend over logic2-automation (gRPC, Logic 2 started with ``--automation``)."""

    def __init__(
        self,
        channels: Mapping[str, int],
        *,
        samplerate: int = 50_000_000,
        address: str = "127.0.0.1",
        port: int = 10430,
        device_id: str | None = None,
        threshold_v: float | None = None,
    ) -> None:
        super().__init__(channels)
        self.samplerate = samplerate
        self.address, self.port = address, port
        self.device_id = device_id
        self.threshold_v = threshold_v  # Logic Pro 8/16 only

    def configuration(self, automation: Any, duration_s: float) -> tuple[Any, Any]:
        """(LogicDeviceConfiguration, CaptureConfiguration) for a timed capture of our channels."""
        device = automation.LogicDeviceConfiguration(
            enabled_digital_channels=sorted(set(self.channels.values())),
            digital_sample_rate=self.samplerate,
            digital_threshold_volts=self.threshold_v,
        )
        capture = automation.CaptureConfiguration(
            capture_mode=automation.TimedCaptureMode(duration_seconds=duration_s)
        )
        return device, capture

    def acquire(self, duration_s: float, workdir: Path) -> Acquisition:
        try:
            import saleae.automation as automation
        except ImportError as exc:
            raise LaError("Saleae backend needs logic2-automation: pip install 'hilrig[saleae]'") from exc
        workdir.mkdir(parents=True, exist_ok=True)
        device, capture = self.configuration(automation, duration_s)
        manager = automation.Manager.connect(address=self.address, port=self.port)
        try:
            cap = manager.start_capture(
                device_id=self.device_id, device_configuration=device, capture_configuration=capture
            )
        except BaseException:
            manager.close()
            raise
        return _SaleaeAcquisition(manager, cap, workdir, self.channels, self.samplerate, duration_s)


class _SaleaeAcquisition(Acquisition):
    def __init__(
        self,
        manager: Any,
        cap: Any,
        workdir: Path,
        channels: Mapping[str, int],
        samplerate: int,
        duration_s: float,
    ) -> None:
        self.manager, self.cap, self.workdir = manager, cap, workdir
        self.channels, self.samplerate, self.duration_s = channels, samplerate, duration_s

    def wait(self) -> Trace:
        try:
            self.cap.wait()
            channels = sorted(set(self.channels.values()))
            self.cap.export_raw_data_csv(directory=str(self.workdir), digital_channels=channels)
            self.cap.save_capture(filepath=str(self.workdir / "capture.sal"))
        finally:
            self.cap.close()
            self.manager.close()
        return read_saleae_csv(self.workdir / "digital.csv", self.channels, self.samplerate, self.duration_s)

    def abort(self) -> None:
        try:
            self.cap.stop()
        finally:
            self.cap.close()
            self.manager.close()


def open_analyzer(driver: str, channels: Mapping[str, int], **options: Any) -> LogicAnalyzer:
    """The analyzer named by bench.yml ``la.driver``: ``saleae``, or any sigrok driver spec.

    ``options`` go to the backend (``samplerate``; sigrok: ``settle_s``; Saleae: ``address``,
    ``port``, ``device_id``, ``threshold_v``).
    """
    if driver == "saleae":
        return SaleaeLA(channels, **options)
    return SigrokLA(driver, channels, **options)
