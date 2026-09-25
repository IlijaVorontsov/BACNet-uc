"""pty-based fake of the stimulus board (protocol v0), for tests without hardware.

It behaves like the Zephyr shell the real board runs (apps/hil_stimulus, reply formats of
stim_cmds.c): it echoes what is typed, keeps at most 511 characters of a line (the 512-byte
shell buffer drops the rest), clears the line on Ctrl-C and prints a new prompt, answers
after CR, prints a VT100-coloured prompt, puts a log line in front of every reply and, every
third command, reprints the prompt around an asynchronous log line. Faults can be injected
per command word (``garble``, ``corrupt``, ``delay``, ``silent``, ``override``) or for the
whole link (``stall``, ``hangup``, ``reboot``), and ``commands`` records every line
executed, so tests can check that the host driver filters noise, resynchronises, never
retries writes, refuses lines the shell would cut and never blocks for ever.
"""

from __future__ import annotations

import os
import re
import select
import threading
import time
import tty

PROMPT = b"\x1b[1;32mstim:~$ \x1b[m"
LINE_MAX = 511
CHANS = tuple("di1 di2 do3 do4 ai0 ao0 nrst nrst_sense pwr sync m0 m1 m2 m3 lb v3v3 v5".split())
KINDS = {"di": "di", "do": "do", "ai": "ai", "ao": "ao", "m": "marker", "v": "sense"}
OUTPUTS = ("di", "sync")  # kinds the stimulus drives
INPUTS = ("do", "marker", "nrst-sense")  # kinds edges/lat accept (never ao)


def kind_of(chan: str) -> str:
    """The stimulus channel kind of a channel name (as the firmware's devicetree has it)."""
    special = {"nrst": "nrst", "nrst_sense": "nrst-sense", "pwr": "pwr", "sync": "sync", "lb": "marker"}
    return special.get(chan) or KINDS.get(chan[:2], KINDS.get(chan[:1], "?"))


class FakeStim:
    """A fake stimulus board on a pty; ``port`` is the device path to open."""

    def __init__(
        self,
        *,
        proto: int = 0,
        boot_delay: float = 0.0,
        chans: tuple[str, ...] = CHANS,
        wiring: dict[str, str] | None = None,
        board: str = "fake",
        profile: str | None = None,
    ) -> None:
        self.proto = proto
        self.chans = chans
        self.board = board
        self.profile = profile
        # stimulus output -> stimulus input it reaches (through the DUT, or the lb jumper)
        self.wiring = wiring or {"di1": "do3", "sync": "lb"}
        self.levels: dict[str, str] = dict.fromkeys(chans, "z")
        self.adc_mv: dict[str, int] = {"v3v3": 1650, "v5": 2550}
        self.pwr = 1
        self.rst_n = 0
        self.edges_reply: tuple[int, ...] = (1000, 2000, 3000)  # t_ns of the next edges reply
        self.commands: list[str] = []
        self.garble: set[str] = set()  # command words whose next reply has two OK lines
        self.corrupt: set[str] = set()  # command words whose next reply has unusable values
        self.silent: set[str] = set()  # command words whose next reply is not an OK/ERR line
        self.override: dict[str, str] = {}  # command word -> next reply line, verbatim
        self.delay: dict[str, float] = {}  # command word -> seconds to wait before replying
        self.reboot_on: set[str] = set()  # command words during which the board resets
        self.stalled = False  # stop reading input, like a board that no longer drains USB
        self._t0 = time.monotonic()
        self._boot_delay = boot_delay
        self._reboot = threading.Event()
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

    def reboot(self) -> None:
        """Reset the board: state back to safe, line discarded, banner and prompt printed."""
        self._reboot.set()

    def __enter__(self) -> FakeStim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- shell emulation ---------------------------------------------------------------
    def _out(self, data: bytes) -> None:
        os.write(self._master, data)

    def _banner(self) -> None:
        self._out(
            f"STIM READY proto={self.proto} fw=0.0.0-fake board={self.board} rc=0x1 init=0\r\n".encode()
        )
        self._out(PROMPT)

    def _run(self) -> None:
        if self._boot_delay:
            self._drain_until(time.monotonic() + self._boot_delay)  # input is lost while booting
            self._banner()
        line = b""
        while not self._stop.is_set():
            if self._reboot.is_set():
                self._reboot.clear()
                line = b""
                self.levels = dict.fromkeys(self.chans, "z")
                self.pwr, self.rst_n, self._t0 = 1, 0, time.monotonic()
                self._banner()
            if self.stalled or not select.select([self._master], [], [], 0.05)[0]:
                time.sleep(0.01 if self.stalled else 0)
                continue
            for byte in os.read(self._master, 1024):
                char = bytes([byte])
                if char == b"\x03":  # Ctrl-C: the shell clears the line and prints a prompt
                    line = b""
                    self._out(b"\r\n" + PROMPT)
                elif char in b"\r\n":
                    if char == b"\r" or line:
                        self._out(b"\r\n")
                        self._execute(line.decode())
                    line = b""
                elif len(line) < LINE_MAX:  # a full shell buffer drops further characters
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
            if word in self.reboot_on:  # reset while executing: no reply, a banner instead
                self.reboot_on.discard(word)
                self._reboot.set()
                return
            self._out(b"[00:00:01.000,000] <wrn> stim: fake log line\r\n")
            reply = self.override.pop(word, None) or self._reply(words)
            if word in self.silent:
                self.silent.discard(word)
                reply = "stim: usage printed without a reply line"
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

    def _now_ns(self) -> int:
        return int((time.monotonic() - self._t0) * 1e9)

    def _level(self, chan: str) -> str:
        """The level an input sees: its driver's through the wiring, else its own (z = 0)."""
        driver = next((o for o, i in self.wiring.items() if i == chan), None)
        level = self.levels.get(driver, "0") if driver else self.levels.get(chan, "0")
        return level if level in ("0", "1") else "0"

    def _reply(self, words: list[str]) -> str:
        """One branch per command, with the firmware's reply format and error codes."""
        if words[0] != "stim":
            return f"{words[0]}: command not found"
        if len(words) < 2:
            return "ERR -22 usage: stim <command> ..."
        cmd, args = words[1], words[2:]
        if (
            args
            and cmd in ("chan", "dout", "din", "pulse", "edges", "dac", "adc")
            and args[0] not in self.chans
        ):
            return "ERR -2 channel"
        kind = kind_of(args[0]) if args else ""
        if cmd == "info" and not args:
            uptime = int((time.monotonic() - self._t0) * 1000)
            profile = f" profile={self.profile}" if self.profile else ""
            return (
                f"OK proto={self.proto} fw=0.0.0-fake board={self.board}{profile} uptime_ms={uptime} "
                f"vdda_mv=3300 pwr={self.pwr} rst_n={self.rst_n} caps=rstmon chans={','.join(self.chans)}"
            )
        if cmd == "chan" and len(args) == 1:
            src, sense = int(args[0] in ("ai0", "ai3")), int(kind in ("ai", "sense"))
            return (
                f"OK name={args[0]} kind={kind} src={src} sense={sense} cap={int(kind == 'ao')} pin=P? dut=D?"
            )
        if cmd == "safe" and not args:
            self.levels, self.pwr = dict.fromkeys(self.chans, "z"), 1
            return "OK pwr=1"
        if cmd == "dout" and len(args) == 2:
            if kind not in OUTPUTS:
                return "ERR -1 not an output channel"
            if args[1] not in ("0", "1", "z"):
                return "ERR -22 level"
            self.levels[args[0]] = args[1]
            return f"OK t_ns={self._now_ns()}"
        if cmd == "din" and len(args) == 1:
            return f"OK v={self._level(args[0])}"
        if cmd == "pulse" and 2 <= len(args) <= 5:
            if kind not in OUTPUTS:
                return "ERR -1 not an output channel"
            if self.levels.get(args[0]) == "z" and len(args) < 5:
                return "ERR -22 active level required when z"
            total_us = int(args[1]) * 2 * int(args[2] if len(args) > 2 else 1)
            mode = "busy" if total_us <= 50_000 else "sleep"
            return f"OK n={args[2] if len(args) > 2 else 1} t0_ns=1000000 mode={mode}"
        if cmd == "edges" and len(args) in (2, 3):
            if kind not in INPUTS:
                return "ERR -1 not an input channel"
            limit = int(args[2]) if len(args) > 2 else 64
            listed = self.edges_reply[:limit]
            levels = ",".join(str((i + 1) % 2) for i in range(len(listed)))
            trunc = int(len(self.edges_reply) > limit)
            times = ",".join(map(str, listed))
            return f"OK n={len(self.edges_reply)} t0_ns=500 trunc={trunc} t_ns={times} lv={levels}"
        if cmd == "lat" and len(args) == 5:
            if self.wiring.get(args[0]) != args[2]:
                return "ERR -116 timeout"
            self.levels[args[0]] = args[1]
            return "OK lat_ns=1500 t0_ns=2000000"
        if cmd == "dac" and len(args) == 2:
            if args[1] == "z":
                return "OK mv=z"
            if not self.pwr and int(args[1]) > 0:
                return "ERR -1 dut unpowered"
            if not 200 <= int(args[1]) <= 3100:
                return "ERR -34 outside usable range"
            return f"OK mv={args[1]} code={int(args[1]) * 4095 // 3300} vdda_mv=3300"
        if cmd == "adc" and 1 <= len(args) <= 2:
            mv = self.adc_mv.get(args[0], 1650) if self.pwr else 0
            return f"OK mv={mv} raw=2048 n={args[1] if len(args) > 1 else 16} vdda_mv=3300"
        if cmd == "reset" and len(args) <= 1:
            self.rst_n += 1
            return f"OK held_ms={args[0] if args else 10} t_ns={self._now_ns()}"
        if cmd == "power" and 1 <= len(args) <= 2 and args[0] in ("on", "off", "cycle"):
            if args[0] != "on":
                self.levels = dict.fromkeys(self.chans, "z")
            self.pwr = 0 if args[0] == "off" else 1
            return f"OK pwr={self.pwr} t_ns={self._now_ns()}"
        if cmd == "rstmon" and len(args) <= 1:
            reply = f"OK n={self.rst_n} last_ns=0 in_reset=0"
            if args == ["clear"]:
                self.rst_n = 0
            return reply
        if cmd in ("info", "chan", "dout", "din", "pulse", "edges", "lat", "dac", "adc", "reset", "power"):
            return f"ERR -22 usage: stim {cmd} ..."
        return "ERR -134 unknown command"
