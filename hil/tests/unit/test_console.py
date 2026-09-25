"""DUT console recording (hilrig.console): lines, waits, tiers, serial and process sources."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import tty
from pathlib import Path

import pytest

from hilrig.console import Console, ConsoleError, ProcessConsole, SerialConsole


def test_lines_are_recorded_with_escapes_and_cr_removed(tmp_path: Path) -> None:
    console = Console(tmp_path / "c.log")
    console.feed("\x1b[1;32mHIL-BO")
    console.feed("OT board=nucleo_f767zi\r\npartial")
    assert [x.text for x in console.lines()] == ["HIL-BOOT board=nucleo_f767zi"]
    mark = console.mark()
    console.feed(" line\n<wrn> uc_storage: formatting /lfs\n")
    assert [x.text for x in console.lines(mark)] == ["partial line", "<wrn> uc_storage: formatting /lfs"]
    assert [x.text for x in console.lines(pattern="formatting /lfs")] == ["<wrn> uc_storage: formatting /lfs"]
    console.shutdown()
    log = (tmp_path / "c.log").read_text()
    assert "HIL-BOOT board=nucleo_f767zi" in log and "formatting /lfs" in log


def test_wait_for_sees_only_lines_after_the_mark(tmp_path: Path) -> None:
    console = Console(tmp_path / "c.log")
    console.feed("HIL-READY ip=192.0.2.10 mac=02:80:e1:00:00:01\n")
    mark = console.mark()
    threading.Timer(0.1, console.feed, ["HIL-READY ip=192.0.2.10 mac=02:80:e1:67:44:68\n"]).start()
    line, m = console.wait_for(r"HIL-READY ip=(\S+) mac=(\S+)", 2.0, mark)
    assert m.groups() == ("192.0.2.10", "02:80:e1:67:44:68") and line.t > 0
    with pytest.raises(TimeoutError, match="no line matching"):
        console.wait_for("never", 0.1)


def test_release_tier_console_takes_no_input(tmp_path: Path) -> None:
    """HIL.md 9: in the release tier the console is recorded, never typed into."""
    with pytest.raises(ConsoleError, match="release tier"):
        Console(tmp_path / "c.log").send("hil panic")


def test_serial_console_reads_and_writes_a_pty(tmp_path: Path) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)
    console = SerialConsole(os.ttyname(slave), tmp_path / "c.log", writable=True)
    try:
        console.open()
        os.write(master, b"BACnet-uc 0.1.0 on nucleo_f767zi\r\n")
        console.wait_for("BACnet-uc 0.1.0 on", 2.0)
        console.send("hil panic")
        deadline = time.monotonic() + 2
        typed = b""
        while b"\r" not in typed and time.monotonic() < deadline:
            typed += os.read(master, 64)
        assert typed == b"hil panic\r"
        console.close()  # the power-cycle sequence closes the port, then reopens it
        console.open(retry_s=1)
        os.write(master, b"after reopen\n")
        console.wait_for("after reopen", 2.0)
    finally:
        console.shutdown()
        os.close(master)
        os.close(slave)


def test_serial_console_that_cannot_open_says_so(tmp_path: Path) -> None:
    console = SerialConsole(str(tmp_path / "no-such-tty"), tmp_path / "c.log")
    with pytest.raises(ConsoleError, match="cannot open the DUT console"):
        console.open(retry_s=0.3)


def test_process_console_records_stdout(tmp_path: Path) -> None:
    console = ProcessConsole(tmp_path / "c.log", writable=True)
    # the shell ends a line with CR, like the Zephyr shell expects: read the 8 bytes "echo me\r"
    script = "import sys; print('booting', flush=True); print(sys.stdin.buffer.read(8).decode().strip())"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    console.attach(proc)
    console.wait_for("booting", 5.0)
    console.send("echo me")
    console.wait_for("echo me", 5.0)
    assert proc.wait(timeout=5) == 0
    console.shutdown()
