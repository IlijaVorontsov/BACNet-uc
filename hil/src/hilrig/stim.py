"""Host driver for the stimulus board, protocol v0 (Zephyr shell group ``stim``).

Every command gets exactly one reply line, ``OK k=v ...`` or ``ERR <negative errno> <text>``,
followed by the prompt ``stim:~$ ``. Everything else on the line (command echo, log lines,
VT100 escapes, prompts reprinted around log output) is ignored, and so are unknown keys. The
board prints ``STIM READY proto=0 fw=<semver> board=<board> rc=0x<reset cause> init=<rc>``
when it boots; a banner seen after the link is up raises :class:`StimRebooted`, because the
board has lost its state (docs/hil/stimulus-protocol.md section 2).

State-changing commands are never retried: a lost reply leaves the board state unknown,
which the caller must see. Read-only commands (``info``, ``chan``, ``din``, ``edges``,
``adc``, ``rstmon`` without ``clear``) are retried once. After any link error the next
command first resynchronises: Ctrl-C clears a half-typed line (a bare CR would execute it),
then CR, then the line must stay quiet, so a late reply is never taken for the reply of a
later command. Lines are at most 511 printable ASCII characters (the shell's 512-byte
buffer); longer ones are refused before anything is sent. Everything sent and received is
appended to a traffic log.
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
MAX_LINE = 511  # SHELL_CMD_BUFF_SIZE=512 including the terminating NUL
CTRL_C = "\x03"
DEFAULT_PROFILE = "P1"  # It1 firmware serves the P1 map without a profile= key (HIL.md 5.2)

# Zephyr/newlib errno numbering (a BUILD_ASSERT in the firmware pins it).
EPERM, ENOENT, EBUSY, ENODEV, EINVAL = -1, -2, -16, -19, -22
ETIMEDOUT, ENOTSUP = -116, -134

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_BANNER = re.compile(r"STIM READY proto=(\d+)[^\r\n]*")

T = TypeVar("T")
Level = Literal[0, 1, "z"]


class StimError(Exception):
    """The board answered ``ERR <errno> <text>``; ``errno`` is negative (Zephyr style)."""

    def __init__(self, errno: int, text: str, cmd: str = "") -> None:
        super().__init__(f"{cmd!r}: ERR {errno} {text}" if cmd else f"ERR {errno} {text}")
        self.errno = errno
        self.text = text
        self.cmd = cmd


class StimTimeout(StimError):
    """ERR -116 (ETIMEDOUT): the awaited edge or signal did not come."""


class StimNoDevice(StimError):
    """ERR -19 (ENODEV): the hardware behind the channel is absent (the conftest skips)."""


class StimNotSupported(StimError):
    """ERR -134 (ENOTSUP): unknown command, or the channel lacks that function."""


class StimBusy(StimError):
    """ERR -16 (EBUSY): the edge engine or a capture unit is in use."""


_ERRORS: dict[int, type[StimError]] = {
    ETIMEDOUT: StimTimeout,
    ENODEV: StimNoDevice,
    ENOTSUP: StimNotSupported,
    EBUSY: StimBusy,
}


class StimLinkError(Exception):
    """The serial link failed: no prompt, no single usable OK/ERR line, or a protocol mismatch."""


class StimRebooted(StimLinkError):
    """A ``STIM READY`` banner arrived mid-session: the board reset and lost its state."""

    def __init__(self, banner: str) -> None:
        super().__init__(f"stimulus rebooted mid-session: {banner!r}")
        self.banner = banner


@dataclass(frozen=True)
class StimInfo:
    """Reply of ``stim info``; keys added after the baseline are None or empty when absent."""

    proto: int
    fw: str
    board: str
    uptime_ms: int
    chans: tuple[str, ...]
    profile: str | None = None
    vdda_mv: int | None = None
    pwr: int | None = None
    rst_n: int | None = None
    caps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChanInfo:
    """Reply of ``stim chan``: kind, which functions it has, and the pins at both ends."""

    name: str
    kind: str
    src: bool
    sense: bool
    cap: bool
    pin: str
    dut: str


@dataclass(frozen=True)
class Pulse:
    """Reply of ``stim pulse``: pulses generated, first edge (stimulus timebase), busy/sleep."""

    n: int
    t0_ns: int
    mode: str | None = None


@dataclass(frozen=True)
class Edges:
    """Reply of ``stim edges``: edge timestamps (ns) and the level after each edge.

    ``n`` is the number of edges seen; with ``truncated`` only the first ``max`` are listed.
    """

    t_ns: tuple[int, ...]
    levels: tuple[int, ...]
    n: int = 0
    t0_ns: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class Latency:
    """Reply of ``stim lat``: ns from driving ``out`` to the edge on ``in``, and the drive time."""

    lat_ns: int
    t0_ns: int | None = None


@dataclass(frozen=True)
class DacReading:
    """Reply of ``stim dac``: the value set (None for Hi-Z), the DAC code and VDDA."""

    mv: int | None
    code: int | None = None
    vdda_mv: int | None = None


@dataclass(frozen=True)
class AdcReading:
    """Reply of ``stim adc``: mean in mV and raw counts over ``n`` samples."""

    mv: float
    raw: float
    n: int
    vdda_mv: int | None = None


@dataclass(frozen=True)
class ResetPulse:
    """Reply of ``stim reset``: hold time and the release time (stimulus timebase)."""

    held_ms: int
    t_ns: int


@dataclass(frozen=True)
class PowerState:
    """Reply of ``stim power``: DUT power after the command (1 = on) and when."""

    pwr: int
    t_ns: int


@dataclass(frozen=True)
class RstMon:
    """Reply of ``stim rstmon``: DUT resets seen since boot or clear, the last one, NRST now."""

    n: int
    last_ns: int
    in_reset: bool


def _clean(line: str) -> str:
    """Strip VT100 escapes, whitespace and any prompt the shell reprinted before the text."""
    line = _ANSI.sub("", line).strip()
    while line.startswith(PROMPT):
        line = line[len(PROMPT) :].strip()
    return line


def _is_reply(line: str) -> bool:
    return line == "OK" or line.startswith(("OK ", "ERR "))


def check_line(line: str) -> None:
    """Refuse a command line the stimulus shell cannot take whole (ValueError).

    The shell keeps 511 characters; anything longer would be cut and a control or
    non-ASCII character would edit or split the line, so either would run a different
    command than the one written.
    """
    if len(line) > MAX_LINE:
        raise ValueError(f"stimulus line of {len(line)} characters (at most {MAX_LINE})")
    bad = sorted({c for c in line if not " " <= c <= "~"})
    if bad:
        raise ValueError(f"stimulus lines are printable ASCII only, got {bad!r}")


def parse_reply(lines: Sequence[str], cmd: str) -> dict[str, str]:
    """Return the key/value pairs of the single OK line in ``lines``.

    Raises :class:`StimError` (or its errno subclass) for an ERR line and
    :class:`StimLinkError` when there is not exactly one OK/ERR line.
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
        raise _ERRORS.get(errno, StimError)(errno, parts[2] if len(parts) > 2 else "", cmd)
    fields = {}
    for token in reply[2:].split():
        key, sep, value = token.partition("=")
        if not sep:
            raise StimLinkError(f"{cmd!r}: malformed field {token!r} in {reply!r}")
        fields[key] = value
    return fields


def _ints(value: str) -> tuple[int, ...]:
    return tuple(int(v) for v in value.split(",") if v)


def _opt_int(r: dict[str, str], key: str) -> int | None:
    return int(r[key]) if key in r else None


def _flag(value: str) -> bool:
    if value not in ("0", "1"):
        raise ValueError(f"expected 0 or 1, got {value!r}")
    return value == "1"


def _levels(value: str) -> tuple[int, ...]:
    """Parse edge levels, written either comma-separated (``0,1,0``) or packed (``010``)."""
    levels = tuple(int(c) for c in value.replace(",", ""))
    if any(level not in (0, 1) for level in levels):
        raise ValueError(f"edge levels must be 0 or 1: {value!r}")
    return levels


def _edges(r: dict[str, str]) -> Edges:
    """Build :class:`Edges` from a ``stim edges`` reply; ValueError if its parts disagree.

    ``n`` is the total number of edges; a truncated reply lists fewer (the first ``max``).
    """
    t_ns, levels, n = _ints(r.get("t_ns", "")), _levels(r.get("lv", "")), int(r["n"])
    truncated = _flag(r.get("trunc", "0"))
    listed_ok = len(t_ns) < n if truncated else len(t_ns) == n
    if not listed_ok or len(levels) != len(t_ns):
        raise ValueError(f"n={n} trunc={int(truncated)} but {len(t_ns)} times and {len(levels)} levels")
    return Edges(t_ns, levels, n, _opt_int(r, "t0_ns"), truncated)


def _info(r: dict[str, str]) -> StimInfo:
    return StimInfo(
        int(r["proto"]),
        r["fw"],
        r["board"],
        int(r["uptime_ms"]),
        tuple(c for c in r["chans"].split(",") if c),
        profile=r.get("profile"),
        vdda_mv=_opt_int(r, "vdda_mv"),
        pwr=_opt_int(r, "pwr"),
        rst_n=_opt_int(r, "rst_n"),
        caps=tuple(c for c in r.get("caps", "").split(",") if c),
    )


def _chan(r: dict[str, str]) -> ChanInfo:
    return ChanInfo(
        r["name"], r["kind"], _flag(r["src"]), _flag(r["sense"]), _flag(r["cap"]), r["pin"], r["dut"]
    )


def _dac(r: dict[str, str]) -> DacReading:
    if r["mv"] == "z":
        return DacReading(None)
    return DacReading(int(r["mv"]), _opt_int(r, "code"), _opt_int(r, "vdda_mv"))


class Stim:
    """Stimulus board on a serial port (``/dev/hil/stim0``, 115200 8N1), opened exclusively."""

    def __init__(
        self,
        port: str,
        *,
        baud: int = 115200,
        log: Path | None = None,
        timeout: float = 2.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.port, self.baud = port, baud
        self.timeout = timeout
        self.banner: str | None = None
        self._log = None
        self._rx_line = ""  # received text after the last newline, not logged yet
        self._stale = False  # a link error happened: resynchronise before the next command
        self._up = False  # the link is up: a banner from now on means a stimulus reset
        self._ser = self._open()
        try:
            self._log = log.open("a", encoding="utf-8") if log else None
            self._connect(connect_timeout)
        except Exception:
            self.close()
            raise

    # ---- link ----------------------------------------------------------------------
    def _open(self) -> serial.Serial:
        """Open the port exclusively, so no other process can write into the shell."""
        try:
            return serial.Serial(
                self.port, self.baud, timeout=0.02, write_timeout=self.timeout, exclusive=True
            )
        except (OSError, ValueError) as e:  # SerialException is an OSError
            raise StimLinkError(f"cannot open {self.port}: {e}") from None

    def close(self) -> None:
        """Close the port and the traffic log."""
        self._ser.close()
        self._up = False
        if self._log:
            if self._rx_line:
                self._log_line("<", self._rx_line)
                self._rx_line = ""
            self._log.close()
            self._log = None

    def reopen(self, connect_timeout: float = 10.0) -> None:
        """Reopen the port after the board reset or re-enumerated (PWR-02), keeping the log.

        Retries opening until ``connect_timeout``, since a re-enumerated tty takes a moment.
        """
        self._ser.close()
        self._up = False
        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                self._ser = self._open()
                break
            except StimLinkError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        self._connect(max(0.5, deadline - time.monotonic()))

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
        self._log.write(f"{time.monotonic():.6f} {direction} {text!r}\n")

    def _connect(self, timeout: float) -> None:
        """Wait for the boot banner or a prompt, then check the protocol version.

        Ctrl-C and CR are sent first, so a board that booted long ago answers with a prompt
        and a half-typed line left by an earlier process is discarded, not executed.
        """
        self._write(CTRL_C + "\r")
        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            buf += self._read_chunk()
            text = _ANSI.sub("", buf)
            m = _BANNER.search(text)
            if m:
                self.banner = m[0].strip()
                if int(m[1]) != PROTO:
                    raise StimLinkError(f"stimulus speaks proto={m[1]}, host supports {PROTO}")
                break
            if text.rstrip().endswith(PROMPT):
                break
        else:
            raise StimLinkError(f"no banner or prompt within {timeout} s (got {buf[-200:]!r})")
        self.sync()
        self._up = True
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

    def _check_banner(self, text: str) -> None:
        """Raise :class:`StimRebooted` if ``text`` holds a boot banner while the link is up."""
        m = _BANNER.search(_ANSI.sub("", text))
        if m and self._up:
            self.banner = m[0].strip()
            self._stale = True
            raise StimRebooted(self.banner)

    def _drain(self) -> None:
        """Read and drop pending input, but never a boot banner (unlike reset_input_buffer)."""
        try:
            waiting = self._ser.in_waiting
        except (OSError, termios.error) as e:
            raise StimLinkError(f"serial port failed: {e}") from None
        pending = ""
        while waiting:
            pending += self._read_chunk()
            try:
                waiting = self._ser.in_waiting
            except (OSError, termios.error) as e:
                raise StimLinkError(f"serial port failed: {e}") from None
        self._check_banner(pending)

    def sync(self, attempts: int = 5, quiet: float = 0.1) -> None:
        """Resynchronise with the prompt and drop any late output.

        Sends Ctrl-C (clears a partial line) and CR until a prompt appears, then keeps
        reading until the line has been quiet for ``quiet`` seconds, so the reply of a
        command that timed out earlier cannot be taken for the reply of the next one.
        A boot banner on the way raises :class:`StimRebooted`.
        """
        for _ in range(attempts):
            self._drain()
            self._write(CTRL_C + "\r")
            buf = ""
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and not _ANSI.sub("", buf).rstrip().endswith(PROMPT):
                buf += self._read_chunk()
            self._check_banner(buf)
            if not _ANSI.sub("", buf).rstrip().endswith(PROMPT):
                continue
            last = time.monotonic()
            while time.monotonic() - last < quiet:
                chunk = self._read_chunk()
                if chunk:
                    buf += chunk
                    last = time.monotonic()
            self._check_banner(buf)
            self._stale = False
            return
        raise StimLinkError("stimulus shell prompt not found")

    def _exchange(self, line: str, timeout: float) -> list[str]:
        """Send one command line and return the lines received up to the reply's prompt."""
        if self._stale:
            self.sync()
        self._drain()
        self._write(line + "\r")
        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            buf += self._read_chunk()
            self._check_banner(buf)
            text = _ANSI.sub("", buf)
            lines = text.splitlines()
            has_reply = any(_is_reply(_clean(x)) for x in lines[:-1])
            if has_reply and text.rstrip().endswith(PROMPT):
                return lines
        raise StimLinkError(f"{line!r}: no reply within {timeout:.1f} s (got {buf[-200:]!r})")

    def command(self, line: str, *, timeout: float | None = None, read_only: bool = False) -> dict[str, str]:
        """Send a raw ``stim ...`` line and return the reply fields.

        Only ``read_only`` commands are repeated (once, after :meth:`sync`) when the link
        garbles or loses the reply; a device ERR and a stimulus reboot are never retried.
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
        check_line(line)
        limit = max(self.timeout, timeout or 0.0)
        try:
            return self._reply(line, limit, build)
        except StimRebooted:
            raise
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
        """``stim info``: protocol, firmware, board, profile, uptime, power, caps, channels."""
        return self._query("stim info", _info, read_only=True)

    def chan(self, name: str) -> ChanInfo:
        """``stim chan``: a channel's kind, functions and pins."""
        return self._query(f"stim chan {name}", _chan, read_only=True)

    def safe(self) -> None:
        """``stim safe``: every DUT-facing line Hi-Z, NRST released, DUT power on."""
        self.command("stim safe")

    def dout(self, chan: str, level: Level) -> int:
        """``stim dout``: drive a channel low, high or Hi-Z; return the edge time (ns)."""
        return self._query(f"stim dout {chan} {level}", lambda r: int(r["t_ns"]))

    def din(self, chan: str) -> int:
        """``stim din``: read a channel's level."""
        return self._query(f"stim din {chan}", lambda r: int(r["v"]), read_only=True)

    def pulse(
        self,
        chan: str,
        width_us: int,
        count: int = 1,
        period_us: int | None = None,
        active: Literal[0, 1] | None = None,
    ) -> Pulse:
        """``stim pulse``: ``count`` pulses of ``width_us`` every ``period_us`` (2 x width).

        ``active`` is the level during the pulse; it is required when the channel idles Hi-Z.
        """
        if active is not None and period_us is None:
            period_us = 2 * width_us
        args = f"{width_us} {count}" + ("" if period_us is None else f" {period_us}")
        args += "" if active is None else f" {active}"
        duration = count * (period_us or 2 * width_us) / 1e6
        return self._query(
            f"stim pulse {chan} {args}",
            lambda r: Pulse(int(r["n"]), int(r["t0_ns"]), r.get("mode")),
            timeout=duration + 2.0,
        )

    def edges(self, chan: str, window_ms: int, max_edges: int | None = None) -> Edges:
        """``stim edges``: record the edges on a channel for ``window_ms`` (at most ``max_edges``)."""
        arg = "" if max_edges is None else f" {max_edges}"
        return self._query(
            f"stim edges {chan} {window_ms}{arg}", _edges, timeout=window_ms / 1000 + 2.0, read_only=True
        )

    def lat(self, out: str, out_level: Level, inp: str, in_level: Literal[0, 1], timeout_ms: int) -> Latency:
        """``stim lat``: drive ``out`` and measure until ``inp`` reaches ``in_level``.

        A missing response raises :class:`StimTimeout` (ERR -116).
        """
        return self._query(
            f"stim lat {out} {out_level} {inp} {in_level} {timeout_ms}",
            lambda r: Latency(int(r["lat_ns"]), _opt_int(r, "t0_ns")),
            timeout=timeout_ms / 1000 + 2.0,
        )

    def dac(self, chan: str, mv: int | Literal["z"]) -> DacReading:
        """``stim dac``: set an analog source in mV, or ``"z"`` to switch it off (Hi-Z)."""
        value = "z" if mv == "z" else str(int(mv))
        return self._query(f"stim dac {chan} {value}", _dac)

    def adc(self, chan: str, n: int | None = None) -> AdcReading:
        """``stim adc``: average ``n`` samples of an analog input."""
        return self._query(
            f"stim adc {chan}" + ("" if n is None else f" {n}"),
            lambda r: AdcReading(float(r["mv"]), float(r["raw"]), int(r["n"]), _opt_int(r, "vdda_mv")),
            read_only=True,
        )

    def reset(self, hold_ms: int | None = None) -> ResetPulse:
        """``stim reset``: pulse the DUT's NRST (10 ms unless ``hold_ms`` is given)."""
        arg = "" if hold_ms is None else f" {hold_ms}"
        return self._query(
            "stim reset" + arg,
            lambda r: ResetPulse(int(r["held_ms"]), int(r["t_ns"])),
            timeout=(hold_ms or 10) / 1000 + 2.0,
        )

    def power(self, action: Literal["on", "off", "cycle"], off_ms: int | None = None) -> PowerState:
        """``stim power``: switch the DUT supply (off and cycle run ``safe`` first).

        The host allows 2 s beyond the off time of a cycle (2 s assumed when not given).
        """
        arg = "" if off_ms is None else f" {off_ms}"
        wait = (2000 if off_ms is None else off_ms) / 1000 if action == "cycle" else 0.0
        return self._query(
            f"stim power {action}{arg}",
            lambda r: PowerState(int(r["pwr"]), int(r["t_ns"])),
            timeout=wait + 2.0,
        )

    def rstmon(self, clear: bool = False) -> RstMon:
        """``stim rstmon``: DUT resets counted on nrst_sense (the count is read, then cleared)."""
        return self._query(
            "stim rstmon" + (" clear" if clear else ""),
            lambda r: RstMon(int(r["n"]), int(r["last_ns"]), _flag(r["in_reset"])),
            read_only=not clear,
        )
