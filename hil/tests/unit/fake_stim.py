"""pty-based fake of the stimulus board (protocol v0), for tests without hardware.

It behaves like the Zephyr shell the real board runs: it echoes what is typed, answers after
CR, prints a VT100-coloured prompt, puts a log line in front of every reply and, every third
command, reprints the prompt around an asynchronous log line. Faults can be injected per
command word (``garble``, ``corrupt``, ``delay``) or for the whole link (``stall``,
``hangup``), and ``commands`` records every line received, so tests can check that the host
driver filters noise, resynchronises, never retries writes and never blocks for ever.
"""

from __future__ import annotations

import os
import re
import select
import threading
import time
import tty

PROMPT = b"\x1b[1;32mstim:~$ \x1b[m"
CHANS = tuple("di1 di2 do3 do4 ai0 ao0 nrst nrst_sense pwr sync m0 m1 m2 m3".split())


class FakeStim:
    """A fake stimulus board on a pty; ``port`` is the device path to open."""

    def __init__(
        self,
        *,
        proto: int = 0,
        boot_delay: float = 0.0,
        chans: tuple[str, ...] = CHANS,
        wiring: dict[str, str] | None = None,
    ) -> None:
        self.proto = proto
        self.chans = chans
        self.wiring = wiring or {"do3": "di1"}  # stimulus output -> input it reaches via the DUT
        self.levels: dict[str, str] = dict.fromkeys(chans, "z")
        self.commands: list[str] = []
        self.garble: set[str] = set()  # command words whose next reply has two OK lines
        self.corrupt: set[str] = set()  # command words whose next reply has unusable values
        self.delay: dict[str, float] = {}  # command word -> seconds to wait before replying
        self.stalled = False  # stop reading input, like a board that no longer drains USB
        self._t0 = time.monotonic()
        self._boot_delay = boot_delay
        self._master, self._slave = os.openpty()
        tty.setraw(self._slave)
        self.port = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-stim", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop the fake and close the pty."""
        self.hangup()
        os.close(self._slave)

    def hangup(self) -> None:
        """Stop answering and close the pty master: the port then fails, as when unplugged."""
        if not self._stop.is_set():
            self._stop.set()
            self._thread.join(timeout=2)
            os.close(self._master)

    def __enter__(self) -> FakeStim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- shell emulation ---------------------------------------------------------------
    def _out(self, data: bytes) -> None:
        os.write(self._master, data)

    def _run(self) -> None:
        if self._boot_delay:
            self._drain_until(time.monotonic() + self._boot_delay)  # input is lost while booting
            self._out(f"STIM READY proto={self.proto} fw=0.0.0-fake board=fake rc=0x1\r\n".encode())
            self._out(PROMPT)
        line = b""
        while not self._stop.is_set():
            if self.stalled or not select.select([self._master], [], [], 0.05)[0]:
                time.sleep(0.01 if self.stalled else 0)
                continue
            for byte in os.read(self._master, 1024):
                char = bytes([byte])
                if char in b"\r\n":
                    if char == b"\r" or line:
                        self._out(b"\r\n")
                        self._execute(line.decode())
                    line = b""
                else:
                    line += char
                    self._out(char)

    def _drain_until(self, deadline: float) -> None:
        while time.monotonic() < deadline and not self._stop.is_set():
            if select.select([self._master], [], [], 0.02)[0]:
                os.read(self._master, 1024)

    def _execute(self, line: str) -> None:
        words = line.split()
        if words:
            self.commands.append(line)
            word = words[1] if len(words) > 1 else words[0]
            time.sleep(self.delay.pop(word, 0.0))
            self._out(b"[00:00:01.000,000] <wrn> stim: fake log line\r\n")
            reply = self._reply(words)
            if word in self.garble:
                self.garble.discard(word)
                reply = f"{reply}\r\n{reply}"
            if word in self.corrupt:
                self.corrupt.discard(word)
                reply = re.sub(r"=[^ ]*", "=?", reply)
            self._out(reply.encode() + b"\r\n")
        self._out(PROMPT)
        if len(self.commands) % 3 == 0 and words:
            self._out(b"\r\x1b[K[00:00:02.000,000] <inf> stim: async log line\r\n" + PROMPT)

    def _reply(self, words: list[str]) -> str:
        if words[0] != "stim" or len(words) < 2:
            return f"{words[0]}: command not found"
        cmd, args = words[1], words[2:]
        if args and cmd in ("dout", "din", "pulse", "edges", "dac", "adc") and args[0] not in self.chans:
            return f"ERR -2 unknown channel {args[0]}"
        if cmd == "info":
            uptime = int((time.monotonic() - self._t0) * 1000)
            return (
                f"OK proto={self.proto} fw=0.0.0-fake board=fake uptime_ms={uptime} "
                f"chans={','.join(self.chans)}"
            )
        if cmd == "safe":
            self.levels = dict.fromkeys(self.chans, "z")
            return "OK"
        if cmd == "dout" and len(args) == 2 and args[1] in ("0", "1", "z"):
            self.levels[args[0]] = args[1]
            return "OK"
        if cmd == "din" and len(args) == 1:
            driver = next((o for o, i in self.wiring.items() if i == args[0]), None)
            level = self.levels.get(driver, "0") if driver else self.levels[args[0]]
            return f"OK v={level if level in ('0', '1') else '0'}"
        if cmd == "pulse" and 2 <= len(args) <= 4:
            return f"OK n={args[2] if len(args) > 2 else 1} t0_ns=1000000"
        if cmd == "edges" and len(args) == 2:
            return "OK n=3 t_ns=1000,2000,3000 lv=1,0,1"
        if cmd == "lat" and len(args) == 5:
            return "OK lat_ns=1500" if self.wiring.get(args[0]) == args[2] else "ERR -116 timeout"
        if cmd == "dac" and len(args) == 2:
            return f"OK mv={min(max(int(args[1]), 0), 3300)}"
        if cmd == "adc" and 1 <= len(args) <= 2:
            return f"OK mv=1650 raw=2048 n={args[1] if len(args) > 1 else 16}"
        if cmd == "reset" and len(args) <= 1:
            return "OK"
        if cmd == "power" and args and args[0] in ("on", "off", "cycle"):
            if args[0] != "on":
                self.levels = dict.fromkeys(self.chans, "z")
            return "OK"
        if cmd in ("dout", "din", "pulse", "edges", "lat", "dac", "adc", "reset", "power"):
            return "ERR -22 bad arguments"
        return "stim: wrong parameter"
