"""Host driver for the stimulus board, protocol v0 (Zephyr shell group ``stim``).

Every command gets exactly one reply line, ``OK k=v ...`` or ``ERR <negative errno> <text>``,
followed by the prompt ``stim:~$ ``. Everything else on the line (command echo, log lines,
VT100 escapes, prompts reprinted around log output) is ignored. The board prints
``STIM READY proto=0 fw=<semver> board=<board> rc=<reset-cause>`` when it boots.

State-changing commands are never retried: a lost reply leaves the board state unknown,
which the caller must see. Read-only commands are retried once. After any link error the
next command first resynchronises with the prompt, so a late reply is never taken for the
reply of a later command. Everything sent and received is appended to a traffic log.
"""

from __future__ import annotations

import re
import termios
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

import serial  # pyserial

PROTO = 0
PROMPT = "stim:~$"
ETIMEDOUT = -116

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BANNER = re.compile(r"STIM READY proto=(\d+)")

T = TypeVar("T")


class StimError(Exception):
    """The board answered ``ERR <errno> <text>``; ``errno`` is negative (Zephyr style)."""

    def __init__(self, errno: int, text: str, cmd: str = "") -> None:
        super().__init__(f"{cmd!r}: ERR {errno} {text}" if cmd else f"ERR {errno} {text}")
        self.errno = errno
        self.text = text
        self.cmd = cmd


class StimLinkError(Exception):
    """The serial link failed: no prompt, no single usable OK/ERR line, or a protocol mismatch."""


@dataclass(frozen=True)
class StimInfo:
    """Reply of ``stim info``."""

    proto: int
    fw: str
    board: str
    uptime_ms: int
    chans: tuple[str, ...]


@dataclass(frozen=True)
class Pulse:
    """Reply of ``stim pulse``: pulses generated and the first edge (stimulus timebase)."""

    n: int
    t0_ns: int


@dataclass(frozen=True)
class Edges:
    """Reply of ``stim edges``: edge timestamps (ns) and the level after each edge."""

    t_ns: tuple[int, ...]
    levels: tuple[int, ...]


@dataclass(frozen=True)
class AdcReading:
    """Reply of ``stim adc``: mean in mV and raw counts over ``n`` samples."""

    mv: float
    raw: float
    n: int


def _clean(line: str) -> str:
    """Strip VT100 escapes, whitespace and any prompt the shell reprinted before the text."""
    line = _ANSI.sub("", line).strip()
    while line.startswith(PROMPT):
        line = line[len(PROMPT) :].strip()
    return line


def _is_reply(line: str) -> bool:
    return line == "OK" or line.startswith(("OK ", "ERR "))


def parse_reply(lines: Sequence[str], cmd: str) -> dict[str, str]:
    """Return the key/value pairs of the single OK line in ``lines``.

    Raises :class:`StimError` for an ERR line and :class:`StimLinkError` when there is not
    exactly one OK/ERR line.
    """
    replies = [c for c in map(_clean, lines) if _is_reply(c)]
    if len(replies) != 1:
        raise StimLinkError(f"{cmd!r}: expected one OK/ERR line, got {replies!r}")
    reply = replies[0]
    if reply.startswith("ERR "):
        parts = reply.split(maxsplit=2)
        try:
            errno = int(parts[1])
        except (IndexError, ValueError):
            raise StimLinkError(f"{cmd!r}: malformed ERR line {reply!r}") from None
        raise StimError(errno, parts[2] if len(parts) > 2 else "", cmd)
    fields = {}
    for token in reply[2:].split():
        key, sep, value = token.partition("=")
        if not sep:
            raise StimLinkError(f"{cmd!r}: malformed field {token!r} in {reply!r}")
        fields[key] = value
    return fields


def _ints(value: str) -> tuple[int, ...]:
    return tuple(int(v) for v in value.split(",") if v)


def _levels(value: str) -> tuple[int, ...]:
    """Parse edge levels, written either comma-separated (``0,1,0``) or packed (``010``)."""
    levels = tuple(int(c) for c in value.replace(",", ""))
    if any(level not in (0, 1) for level in levels):
        raise ValueError(f"edge levels must be 0 or 1: {value!r}")
    return levels


def _edges(r: dict[str, str]) -> Edges:
    """Build :class:`Edges` from a ``stim edges`` reply; ValueError if its parts disagree."""
    t_ns, levels = _ints(r.get("t_ns", "")), _levels(r.get("lv", ""))
    if len(t_ns) != int(r["n"]) or len(levels) != len(t_ns):
        raise ValueError(f"n={r['n']} but {len(t_ns)} times and {len(levels)} levels")
    return Edges(t_ns, levels)


class Stim:
    """Stimulus board on a serial port (``/dev/hil/stim0``, 115200 8N1)."""

    def __init__(
        self,
        port: str,
        *,
        baud: int = 115200,
        log: Path | None = None,
        timeout: float = 2.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.timeout = timeout
        self.banner: str | None = None
        self._log = None
        self._rx_line = ""  # received text after the last newline, not logged yet
        self._stale = False  # a link error happened: resynchronise before the next command
        self._ser = serial.Serial(port, baud, timeout=0.02, write_timeout=timeout)
        try:
            self._log = log.open("a", encoding="utf-8") if log else None
            self._connect(connect_timeout)
        except Exception:
            self.close()
            raise

    # ---- link ----------------------------------------------------------------------
    def close(self) -> None:
        """Close the port and the traffic log."""
        self._ser.close()
        if self._log:
            if self._rx_line:
                self._log_line("<", self._rx_line)
                self._rx_line = ""
            self._log.close()
            self._log = None

    def __enter__(self) -> Stim:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _trace(self, direction: str, text: str) -> None:
        """Log traffic one line per entry; received bytes are buffered until the newline.

        Reads return whatever has arrived (often a single byte), so logging each chunk
        would split replies such as ``OK v=0`` across entries.
        """
        if not self._log:
            return
        if direction == ">":
            self._log_line(">", text)
        else:
            self._rx_line += text
            *lines, self._rx_line = self._rx_line.split("\n")
            for line in lines:
                self._log_line("<", line + "\n")
        self._log.flush()

    def _log_line(self, direction: str, text: str) -> None:
        assert self._log is not None
        self._log.write(f"{time.time():.6f} {direction} {text!r}\n")

    def _connect(self, timeout: float) -> None:
        """Wait for the boot banner or a prompt, then check the protocol version.

        A CR is sent first, so a board that booted long ago answers with a prompt.
        """
        self._write("\r")
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            buf += self._read_chunk()
            text = _ANSI.sub("", buf)
            m = _BANNER.search(text)
            if m:
                self.banner = text[m.start() :].splitlines()[0].strip()
                if int(m[1]) != PROTO:
                    raise StimLinkError(f"stimulus speaks proto={m[1]}, host supports {PROTO}")
                break
            if text.rstrip().endswith(PROMPT):
                break
        else:
            raise StimLinkError(f"no banner or prompt within {timeout} s (got {buf[-200:]!r})")
        self.sync()
        info = self.info()
        if info.proto != PROTO:
            raise StimLinkError(f"stim info reports proto={info.proto}, host supports {PROTO}")

    def _read_chunk(self) -> str:
        """Return what has arrived; waits at most the port timeout (20 ms) for the first byte."""
        try:
            data = self._ser.read(self._ser.in_waiting or 1).decode("utf-8", "replace")
        except OSError as e:  # SerialException is one: board unplugged or port gone
            raise StimLinkError(f"serial read failed: {e}") from None
        if data:
            self._trace("<", data)
        return data

    def _write(self, text: str) -> None:
        """Queue ``text`` for sending; a board that stops reading fails after ``timeout``.

        There is deliberately no flush(): tcdrain() has no timeout and would hang on a board
        that no longer drains its USB endpoint.
        """
        self._trace(">", text)
        try:
            self._ser.write(text.encode("ascii"))
        except OSError as e:  # SerialException, SerialTimeoutException
            raise StimLinkError(f"serial write failed: {e}") from None

    def _drop_input(self) -> None:
        """Discard input that has not been read yet."""
        try:
            self._ser.reset_input_buffer()
        except (OSError, termios.error) as e:
            raise StimLinkError(f"serial port failed: {e}") from None

    def sync(self, attempts: int = 5, quiet: float = 0.1) -> None:
        """Resynchronise with the prompt and drop any late output.

        Sends a bare CR until a prompt appears, then keeps reading until the line has been
        quiet for ``quiet`` seconds, so the reply of a command that timed out earlier cannot
        be taken for the reply of the next one.
        """
        for _ in range(attempts):
            self._drop_input()
            self._write("\r")
            buf = ""
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not _ANSI.sub("", buf).rstrip().endswith(PROMPT):
                buf += self._read_chunk()
            if not _ANSI.sub("", buf).rstrip().endswith(PROMPT):
                continue
            last = time.monotonic()
            while time.monotonic() - last < quiet:
                if self._read_chunk():
                    last = time.monotonic()
            self._stale = False
            return
        raise StimLinkError("stimulus shell prompt not found")

    def _exchange(self, line: str, timeout: float) -> list[str]:
        """Send one command line and return the lines received up to the reply's prompt."""
        if self._stale:
            self.sync()
        self._drop_input()
        self._write(line + "\r")
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf += self._read_chunk()
            text = _ANSI.sub("", buf)
            lines = text.splitlines()
            has_reply = any(_is_reply(_clean(x)) for x in lines[:-1])
            if has_reply and text.rstrip().endswith(PROMPT):
                return lines
        raise StimLinkError(f"{line!r}: no reply within {timeout:.1f} s (got {buf[-200:]!r})")

    def command(self, line: str, *, timeout: float | None = None, read_only: bool = False) -> dict[str, str]:
        """Send a raw ``stim ...`` line and return the reply fields.

        Only ``read_only`` commands are repeated (once, after :meth:`sync`) when the link
        garbles or loses the reply; a device ERR is never retried.
        """
        return self._query(line, dict, timeout=timeout, read_only=read_only)

    def _query(
        self,
        line: str,
        build: Callable[[dict[str, str]], T],
        *,
        timeout: float | None = None,
        read_only: bool = False,
    ) -> T:
        """Send ``line`` and return ``build(fields)``; see :meth:`command` for retries.

        A reply that lacks a field or carries one that does not convert is a link error, like
        a garbled line, so read-only commands get their retry for it too.
        """
        limit = max(self.timeout, timeout or 0.0)
        try:
            return self._reply(line, limit, build)
        except StimLinkError:
            if not read_only:
                raise
            return self._reply(line, limit, build)

    def _reply(self, line: str, limit: float, build: Callable[[dict[str, str]], T]) -> T:
        """One attempt of :meth:`_query`; a link error marks the link for resynchronisation."""
        try:
            fields = parse_reply(self._exchange(line, limit), line)
            try:
                return build(fields)
            except (KeyError, ValueError) as e:
                raise StimLinkError(f"{line!r}: unusable reply fields {fields} ({e!r})") from None
        except StimLinkError:
            self._stale = True
            raise

    # ---- protocol v0 commands --------------------------------------------------------
    def info(self) -> StimInfo:
        """``stim info``: protocol version, firmware, board, uptime and channel names."""
        return self._query(
            "stim info",
            lambda r: StimInfo(
                int(r["proto"]),
                r["fw"],
                r["board"],
                int(r["uptime_ms"]),
                tuple(c for c in r["chans"].split(",") if c),
            ),
            read_only=True,
        )

    def safe(self) -> None:
        """``stim safe``: every DUT-facing line Hi-Z, NRST released, DUT power on."""
        self.command("stim safe")

    def dout(self, chan: str, level: Literal[0, 1, "z"]) -> None:
        """``stim dout``: drive a channel low, high or Hi-Z."""
        self.command(f"stim dout {chan} {level}")

    def din(self, chan: str) -> int:
        """``stim din``: read a channel's level."""
        return self._query(f"stim din {chan}", lambda r: int(r["v"]), read_only=True)

    def pulse(self, chan: str, width_us: int, count: int = 1, period_us: int | None = None) -> Pulse:
        """``stim pulse``: generate ``count`` pulses of ``width_us`` every ``period_us``."""
        args = f"{width_us} {count}" + ("" if period_us is None else f" {period_us}")
        duration = count * (period_us or 2 * width_us) / 1e6
        return self._query(
            f"stim pulse {chan} {args}",
            lambda r: Pulse(int(r["n"]), int(r["t0_ns"])),
            timeout=duration + 1.0,
        )

    def edges(self, chan: str, window_ms: int) -> Edges:
        """``stim edges``: record the edges on a channel for ``window_ms``."""
        return self._query(
            f"stim edges {chan} {window_ms}", _edges, timeout=window_ms / 1000 + 1.0, read_only=True
        )

    def lat(self, out: str, out_level: int, inp: str, in_level: int, timeout_ms: int) -> int:
        """``stim lat``: drive ``out`` and return ns until ``inp`` reaches ``in_level``.

        A missing response raises :class:`StimError` with errno :data:`ETIMEDOUT` (-116).
        """
        return self._query(
            f"stim lat {out} {out_level} {inp} {in_level} {timeout_ms}",
            lambda r: int(r["lat_ns"]),
            timeout=timeout_ms / 1000 + 1.0,
        )

    def dac(self, chan: str, mv: int) -> int:
        """``stim dac``: set an analog output; return the value actually set (mV)."""
        return self._query(f"stim dac {chan} {int(mv)}", lambda r: int(r["mv"]))

    def adc(self, chan: str, n: int | None = None) -> AdcReading:
        """``stim adc``: average ``n`` samples of an analog input."""
        return self._query(
            f"stim adc {chan}" + ("" if n is None else f" {n}"),
            lambda r: AdcReading(float(r["mv"]), float(r["raw"]), int(r["n"])),
            read_only=True,
        )

    def reset(self, hold_ms: int | None = None) -> None:
        """``stim reset``: pulse the DUT's NRST (10 ms unless ``hold_ms`` is given)."""
        arg = "" if hold_ms is None else f" {hold_ms}"
        self.command("stim reset" + arg, timeout=(hold_ms or 10) / 1000 + 1.0)

    def power(self, action: Literal["on", "off", "cycle"], off_ms: int | None = None) -> None:
        """``stim power``: switch the DUT supply (off and cycle run ``safe`` first).

        The host allows 2 s beyond the off time of a cycle (2 s assumed when not given).
        """
        arg = "" if off_ms is None else f" {off_ms}"
        wait = (2000 if off_ms is None else off_ms) / 1000 if action == "cycle" else 0.0
        self.command(f"stim power {action}{arg}", timeout=wait + 2.0)
