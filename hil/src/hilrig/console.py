"""The DUT console, recorded line by line with host timestamps (HIL.md 8.2, tiers in 9).

A :class:`Console` keeps every line it is fed, in memory and in a log file, and lets tests
wait for a pattern after a :meth:`~Console.mark`. The line source is one of:

- :class:`SerialConsole`: pyserial on the DUT's ST-LINK VCP from bench.yml (local runs);
- :class:`TwisterConsole`: the Twister harness ``dut`` device adapter (Twister mode, D31),
  which owns the serial port and the flash;
- :class:`ProcessConsole`: the stdout of a native_sim ``zephyr.exe`` (SIL).

In the release tier the console is recorded for diagnosis only and console lines are no
pass/fail criteria, except ``formatting /lfs`` (FW-03); :meth:`Console.send` therefore
refuses unless the console was opened ``writable`` (instrumented images: ``hil`` shell).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import serial  # pyserial

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ConsoleError(RuntimeError):
    """The console could not be opened, or input was refused (release tier)."""


@dataclass(frozen=True)
class ConsoleLine:
    """One console line (escapes and CR removed) and ``time.monotonic()`` when it arrived."""

    t: float
    text: str


class Console:
    """A recorded console; subclasses feed it and implement :meth:`_write`."""

    def __init__(self, log: Path, *, writable: bool = False) -> None:
        self.log = log
        self.writable = writable
        self._lines: list[ConsoleLine] = []
        self._partial = ""
        self._cond = threading.Condition()
        log.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] | None = log.open("a", encoding="utf-8")

    # ---- recording ---------------------------------------------------------------------
    def feed(self, text: str) -> None:
        """Add received text (any chunking); complete lines are recorded (thread-safe)."""
        with self._cond:
            self._partial += text
            *complete, self._partial = self._partial.split("\n")
            now = time.monotonic()
            for raw in complete:
                line = _ANSI.sub("", raw).replace("\r", "").rstrip()
                self._lines.append(ConsoleLine(now, line))
                if self._file:
                    self._file.write(f"{time.time():.6f} {line}\n")
            if complete and self._file:
                self._file.flush()
            self._cond.notify_all()

    def mark(self) -> int:
        """Return a position in the line log, for ``since``."""
        with self._cond:
            return len(self._lines)

    def lines(self, since: int = 0, pattern: str | None = None) -> list[ConsoleLine]:
        """Return the lines received after ``since`` (matching ``pattern`` if given)."""
        with self._cond:
            found = self._lines[since:]
        return [x for x in found if pattern is None or re.search(pattern, x.text)]

    def wait_for(self, pattern: str, timeout: float, since: int = 0) -> tuple[ConsoleLine, re.Match[str]]:
        """Return the first line after ``since`` that matches ``pattern`` and its match."""
        regex = re.compile(pattern)

        def find() -> tuple[ConsoleLine, re.Match[str]] | None:
            for line in self._lines[since:]:
                m = regex.search(line.text)
                if m:
                    return line, m
            return None

        with self._cond:
            found = self._cond.wait_for(find, timeout)
        if found is None:
            raise TimeoutError(f"console: no line matching {pattern!r} within {timeout} s (see {self.log})")
        return found

    # ---- input and lifetime --------------------------------------------------------------
    def send(self, line: str) -> None:
        """Type ``line`` and CR; only on a writable (instrumented-tier) console."""
        if not self.writable:
            raise ConsoleError("console input is not allowed in the release tier (recorded only)")
        self._write((line + "\r").encode())

    def _write(self, data: bytes) -> None:
        raise ConsoleError(f"{type(self).__name__} takes no input")

    def open(self, retry_s: float = 0.0) -> None:
        """(Re)connect the source; retries for ``retry_s`` (the VCP after a power cycle)."""

    def close(self) -> None:
        """Disconnect the source; the recording stays available (:meth:`shutdown` ends it)."""

    def shutdown(self) -> None:
        """Close the source and the log file."""
        self.close()
        with self._cond:
            if self._file:
                if self._partial:
                    self._file.write(f"{time.time():.6f} {self._partial}\n")
                self._file.close()
                self._file = None


class SerialConsole(Console):
    """A console on a serial port, read by a background thread."""

    def __init__(self, port: str, log: Path, *, baud: int = 115200, writable: bool = False) -> None:
        super().__init__(log, writable=writable)
        self.port, self.baud = port, baud
        self._ser: serial.Serial | None = None
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None

    def open(self, retry_s: float = 0.0) -> None:
        if self._ser is not None:
            return
        deadline = time.monotonic() + retry_s
        while True:
            try:
                self._ser = serial.Serial(self.port, self.baud, timeout=0.1, exclusive=True)
                break
            except (OSError, ValueError) as e:
                if time.monotonic() >= deadline:
                    raise ConsoleError(f"cannot open the DUT console {self.port}: {e}") from None
                time.sleep(0.2)
        self._stop.clear()
        self._reader = threading.Thread(target=self._read, args=(self._ser,), name="dut-console", daemon=True)
        self._reader.start()

    def _read(self, ser: serial.Serial) -> None:
        while not self._stop.is_set():
            try:
                data = ser.read(ser.in_waiting or 1)
            except (OSError, TypeError):  # port closed or gone (power cycle, unplug)
                return
            if data:
                self.feed(data.decode("utf-8", "replace"))

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=2)
            self._reader = None
        if self._ser is not None:
            self._ser.close()
            self._ser = None

    def _write(self, data: bytes) -> None:
        if self._ser is None:
            raise ConsoleError("the DUT console is closed")
        self._ser.write(data)


class TwisterConsole(Console):
    """The console of the Twister harness ``dut`` (a DeviceAdapter), polled by a thread."""

    def __init__(self, dut: Any, log: Path, *, writable: bool = False) -> None:
        super().__init__(log, writable=writable)
        self.dut = dut
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self.open()

    def open(self, retry_s: float = 0.0) -> None:
        if self._reader is not None:
            return
        if retry_s and hasattr(self.dut, "connect"):
            self.dut.connect(retry_s=int(retry_s))
        self._stop.clear()
        self._reader = threading.Thread(target=self._read, name="dut-console", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        while not self._stop.is_set():
            try:
                line = self.dut.readline(timeout=0.2, print_output=False)
            except Exception:  # the adapter raises its own timeout types
                continue
            if line:
                self.feed(line + "\n")

    def close(self) -> None:
        self._stop.set()
        if self._reader:
            self._reader.join(timeout=2)
            self._reader = None
        if hasattr(self.dut, "disconnect"):
            self.dut.disconnect()

    def _write(self, data: bytes) -> None:
        self.dut.write(data)


class ProcessConsole(Console):
    """The stdout (and stderr) of a process, typically native_sim's ``zephyr.exe``."""

    def __init__(self, log: Path, *, writable: bool = False) -> None:
        super().__init__(log, writable=writable)
        self.proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None

    def attach(self, proc: subprocess.Popen[bytes]) -> None:
        """Record ``proc``'s output from now on (it needs stdout=PIPE, stderr=STDOUT)."""
        self.proc = proc
        self._reader = threading.Thread(target=self._read, args=(proc,), name="sil-console", daemon=True)
        self._reader.start()

    def _read(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        while chunk := os.read(fd, 4096):  # b"" once the process has exited
            self.feed(chunk.decode("utf-8", "replace"))

    def close(self) -> None:
        if self._reader:
            self._reader.join(timeout=2)
            self._reader = None

    def _write(self, data: bytes) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise ConsoleError("no process attached")
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
