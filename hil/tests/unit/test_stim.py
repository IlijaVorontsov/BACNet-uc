"""Stimulus driver, protocol v0 (hilrig.stim), against the pty fake in fake_stim.py."""

from __future__ import annotations

import os
import time
import tty
from collections.abc import Iterator
from pathlib import Path

import pytest

from fake_stim import FakeStim
from hilrig import stim as stim_mod
from hilrig.stim import (
    ETIMEDOUT,
    AdcReading,
    ChanInfo,
    DacReading,
    Edges,
    Latency,
    PowerState,
    Pulse,
    RstMon,
    Stim,
    StimBusy,
    StimError,
    StimLinkError,
    StimNoDevice,
    StimNotSupported,
    StimRebooted,
    StimTimeout,
    parse_reply,
)


@pytest.fixture
def fake() -> Iterator[FakeStim]:
    with FakeStim() as board:
        yield board


@pytest.fixture
def stim(fake: FakeStim, tmp_path: Path) -> Iterator[Stim]:
    with Stim(fake.port, log=tmp_path / "stim.log", timeout=0.5) as driver:
        yield driver


def test_parse_reply_ignores_echo_logs_prompts_and_escapes() -> None:
    lines = [
        "stim din di1",
        "\x1b[1;32mstim:~$ \x1b[m<wrn> not a reply",
        "[00:00:01.000] <inf> x: OKAY",
        "\x1b[1;32mstim:~$ \x1b[mOK v=1 extra=a,b",
        "stim:~$ ",
    ]
    assert parse_reply(lines, "stim din di1") == {"v": "1", "extra": "a,b"}
    assert parse_reply(["OK"], "stim safe") == {}


@pytest.mark.parametrize(
    ("lines", "problem"),
    [
        ([], "expected one OK/ERR line"),
        (["OK v=1", "OK v=1"], "expected one OK/ERR line"),
        (["OK v"], "malformed field"),
        (["ERR x text"], "malformed ERR line"),
    ],
)
def test_parse_reply_rejects_broken_replies(lines: list[str], problem: str) -> None:
    with pytest.raises(StimLinkError, match=problem):
        parse_reply(lines, "stim x")


def test_err_reply_carries_negative_errno() -> None:
    with pytest.raises(StimError) as err:
        parse_reply(["ERR -116 timeout"], "stim lat do3 1 di2 1 10")
    assert (err.value.errno, err.value.text, err.value.cmd) == (-116, "timeout", "stim lat do3 1 di2 1 10")


@pytest.mark.parametrize(
    ("errno", "cls"),
    [(-116, StimTimeout), (-19, StimNoDevice), (-134, StimNotSupported), (-16, StimBusy), (-2, StimError)],
)
def test_err_codes_map_to_subclasses(errno: int, cls: type[StimError]) -> None:
    with pytest.raises(StimError) as err:
        parse_reply([f"ERR {errno} text"], "stim x")
    assert type(err.value) is cls and err.value.errno == errno


def test_typed_commands(stim: Stim, fake: FakeStim) -> None:
    info = stim.info()
    assert (info.proto, info.board, info.chans) == (0, "fake", fake.chans)
    assert (info.vdda_mv, info.pwr, info.rst_n, info.caps, info.profile) == (3300, 1, 0, ("rstmon",), None)
    assert info.uptime_ms >= 0
    assert stim.dout("di1", 1) > 0  # t_ns of the edge
    assert stim.din("do3") == 1  # di1 reaches do3 through the (fake) DUT
    stim.dout("di1", "z")
    assert stim.din("do3") == 0
    assert stim.pulse("di1", 100, 5, 1000, active=0) == Pulse(n=5, t0_ns=1_000_000, mode="busy")
    assert stim.edges("m0", 50) == Edges(t_ns=(1000, 2000, 3000), levels=(1, 0, 1), n=3, t0_ns=500)
    assert stim.lat("di1", 1, "do3", 1, 100) == Latency(lat_ns=1500, t0_ns=2_000_000)
    assert stim.dac("ai0", 1650) == DacReading(mv=1650, code=2047, vdda_mv=3300)
    assert stim.adc("ai0", 8) == AdcReading(mv=1650.0, raw=2048.0, n=8, vdda_mv=3300)
    assert stim.reset().held_ms == 10
    assert stim.reset(20).held_ms == 20
    assert stim.power("cycle", 100).pwr == 1
    stim.safe()
    assert fake.levels["di1"] == "z"
    assert "stim reset 20" in fake.commands and "stim power cycle 100" in fake.commands


def test_new_commands_and_arguments(stim: Stim, fake: FakeStim) -> None:
    """Follow-up 4 (HIL.md 5.4): chan, rstmon, dac z, pulse active, edges max, lat z."""
    assert stim.chan("ai0") == ChanInfo("ai0", "ai", True, True, False, "P?", "D?")
    assert stim.dac("ai0", "z") == DacReading(mv=None)
    stim.reset()
    assert stim.rstmon() == RstMon(n=1, last_ns=0, in_reset=False)
    assert stim.rstmon(clear=True).n == 1
    assert stim.rstmon().n == 0
    assert stim.pulse("di1", 5000, active=0).mode == "busy"
    assert "stim pulse di1 5000 1 10000 0" in fake.commands  # period defaults to 2 x width
    fake.edges_reply = (10, 20, 30, 40, 50)
    edges = stim.edges("m0", 10, max_edges=2)
    assert (edges.n, edges.t_ns, edges.levels, edges.truncated) == (5, (10, 20), (1, 0), True)
    assert stim.lat("di1", "z", "do3", 0, 10).lat_ns == 1500
    off = stim.power("off")
    assert isinstance(off, PowerState) and off.pwr == 0
    with pytest.raises(StimError) as err:
        stim.dac("ai0", 1000)  # the DUT is unpowered: analog sources refuse
    assert err.value.errno == -1


def test_device_errors_raise_stimerror(stim: Stim) -> None:
    with pytest.raises(StimTimeout) as err:
        stim.lat("di2", 1, "do4", 1, 10)  # not wired: the fake times out like the board
    assert err.value.errno == ETIMEDOUT
    with pytest.raises(StimError) as err2:
        stim.dout("di9", 1)
    assert err2.value.errno == -2
    with pytest.raises(StimNotSupported):
        stim.command("stim frob")


def test_no_device_error_is_its_own_class(stim: Stim, fake: FakeStim) -> None:
    fake.override["dac"] = "ERR -19 source device"
    with pytest.raises(StimNoDevice):
        stim.dac("ai0", 500)


def test_reply_without_ok_or_err_is_a_link_error(stim: Stim, fake: FakeStim) -> None:
    fake.silent.update({"din", "safe"})
    with pytest.raises(StimLinkError, match="no reply"):
        stim.safe()
    assert stim.din("do3") == 0  # the link is still usable; read-only din was retried
    assert fake.commands.count("stim din do3") == 2


def test_state_changing_command_is_never_retried(stim: Stim, fake: FakeStim) -> None:
    fake.garble.add("dout")
    with pytest.raises(StimLinkError):
        stim.dout("di1", 1)
    assert fake.commands.count("stim dout di1 1") == 1


def test_read_only_command_is_retried_once(stim: Stim, fake: FakeStim) -> None:
    fake.garble.add("din")
    assert stim.din("do3") == 0
    assert fake.commands.count("stim din do3") == 2


def test_sync_discards_the_late_reply_of_a_timed_out_command(stim: Stim, fake: FakeStim) -> None:
    fake.delay["safe"] = 1.0
    with pytest.raises(StimLinkError, match="no reply within"):
        stim.safe()
    stim.sync()
    stim.dout("di1", 1)
    assert stim.din("do3") == 1


def test_next_command_resyncs_after_a_link_error(stim: Stim, fake: FakeStim) -> None:
    """Regression: the late OK of a timed-out command was taken as the next command's reply."""
    fake.delay["safe"] = 1.0
    with pytest.raises(StimLinkError, match="no reply within"):
        stim.safe()
    assert stim.command("stim din do3") == {"v": "0"}  # no sync() by the caller, no retry


def test_resync_never_executes_a_half_typed_line(stim: Stim, fake: FakeStim) -> None:
    """Regression (follow-up 1): the bare-CR resync executed a half-typed line."""
    stim._write("stim dout di1 1")  # a line left without CR, as by a crashed host process
    stim.sync()
    assert "stim dout di1 1" not in fake.commands
    assert stim.din("do3") == 0


def test_connect_discards_a_half_typed_line(fake: FakeStim) -> None:
    fd = os.open(fake.port, os.O_WRONLY | os.O_NOCTTY)
    try:
        os.write(fd, b"stim power off")
    finally:
        os.close(fd)
    with Stim(fake.port, timeout=0.5):
        pass
    assert "stim power off" not in fake.commands and fake.pwr == 1


def test_reboot_during_a_command_raises_stim_rebooted(stim: Stim, fake: FakeStim) -> None:
    """Regression (follow-up 2): a mid-session banner was flushed by reset_input_buffer()."""
    stim.dout("di1", 1)
    fake.reboot_on.add("din")
    with pytest.raises(StimRebooted) as err:
        stim.din("do3")  # read-only, but a reboot is never retried
    assert err.value.banner.startswith("STIM READY proto=0")
    assert fake.commands.count("stim din do3") == 1
    assert stim.din("do3") == 0  # the link resyncs; the board state was reset (di1 is Hi-Z)


def test_banner_waiting_before_the_next_command_raises(stim: Stim, fake: FakeStim) -> None:
    fake.reboot()
    time.sleep(0.3)  # the banner is already in the input buffer when the command is sent
    with pytest.raises(StimRebooted):
        stim.info()
    assert stim.info().proto == 0


def test_reopen_after_a_reboot(stim: Stim, fake: FakeStim) -> None:
    fake.reboot()
    stim.reopen(connect_timeout=2)
    assert stim.info().proto == 0


def test_line_limits(stim: Stim, fake: FakeStim) -> None:
    """Follow-up 3: 511 characters are accepted, longer or non-ASCII lines are refused unsent."""
    line = "stim din do3"
    assert stim.command(line + " " * (511 - len(line))) == {"v": "0"}
    with pytest.raises(ValueError, match="512 characters"):
        stim.command(line + " " * (512 - len(line)))
    with pytest.raises(ValueError, match="printable ASCII"):
        stim.command("stim din dö3")
    with pytest.raises(ValueError, match="printable ASCII"):
        stim.command("stim din do3\rstim power off")
    assert fake.commands.count(line + " " * (511 - len(line))) == 1 and len(fake.commands) == 2  # + info


def test_port_is_opened_exclusively(stim: Stim, fake: FakeStim) -> None:
    """Follow-up 7: a second process cannot open (and write into) the stimulus shell."""
    with pytest.raises(StimLinkError, match="cannot open"):
        Stim(fake.port, timeout=0.5)


def test_unusable_reply_fields_are_link_errors(stim: Stim, fake: FakeStim) -> None:
    """Regression: a reply lacking a field or with a non-numeric one raised KeyError/ValueError."""
    fake.corrupt.add("din")
    assert stim.din("do3") == 0  # read-only: retried
    assert fake.commands.count("stim din do3") == 2
    fake.corrupt.add("dac")
    with pytest.raises(StimLinkError, match="unusable reply fields"):
        stim.dac("ai0", 1000)
    assert fake.commands.count("stim dac ai0 1000") == 1  # state-changing: never retried
    with pytest.raises(ValueError, match="must be 0 or 1"):
        stim_mod._edges({"n": "1", "t_ns": "10", "lv": "2"})
    with pytest.raises(ValueError, match="trunc=0"):
        stim_mod._edges({"n": "3", "t_ns": "10", "lv": "1"})  # untruncated but short


@pytest.mark.timeout(30)
def test_board_that_stops_reading_fails_the_write(stim: Stim, fake: FakeStim) -> None:
    """Regression: writes had no timeout and flush() waited in tcdrain() for ever."""
    fake.stalled = True
    with pytest.raises(StimLinkError, match="serial write failed"):
        stim._write("x" * 1_000_000)  # lines are capped at 511 characters, so write directly


def test_unplugged_board_is_a_link_error(stim: Stim, fake: FakeStim) -> None:
    """Regression: a vanished port raised SerialException/termios.error, not StimLinkError."""
    fake.hangup()
    with pytest.raises(StimLinkError, match="serial"):
        stim.info()


def test_traffic_is_logged(stim: Stim, tmp_path: Path) -> None:
    stim.din("do3")
    log = (tmp_path / "stim.log").read_text()
    assert "> 'stim din do3\\r'" in log and "OK v=0" in log


def test_banner_is_parsed_on_boot(tmp_path: Path) -> None:
    with FakeStim(boot_delay=0.3) as fake, Stim(fake.port, timeout=0.5) as stim:
        assert stim.banner == "STIM READY proto=0 fw=0.0.0-fake board=fake rc=0x1 init=0"


def test_protocol_mismatch_in_banner_is_refused() -> None:
    with FakeStim(proto=1, boot_delay=0.3) as fake, pytest.raises(StimLinkError, match="proto=1"):
        Stim(fake.port, timeout=0.5)


def test_protocol_mismatch_in_info_is_refused() -> None:
    with FakeStim(proto=1) as fake, pytest.raises(StimLinkError, match="proto=1"):
        Stim(fake.port, timeout=0.5)


def test_silent_port_is_refused(tmp_path: Path) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)
    try:
        with pytest.raises(StimLinkError, match="no banner or prompt"):
            Stim(os.ttyname(slave), connect_timeout=0.3)
    finally:
        os.close(master)
        os.close(slave)


def test_long_pulse_trains_and_edge_lists_get_their_reply_time(
    stim: Stim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the host waited count x period + 2 s for a pulse train, but in sleep mode each
    of a pulse's two k_usleep phases ends up to 2 ticks late (+400 us a pulse: +40 s for 100000),
    and an edges reply of 1024 edges takes about 2 s on the wire; both timed out while the board
    was still working."""
    seen: list[float] = []

    def query(line: str, build: object, *, timeout: float | None = None, read_only: bool = False) -> None:
        seen.append(timeout or 0.0)

    monkeypatch.setattr(stim, "_query", query)
    stim.pulse("di1", 500, 100_000, 1000, active=0)  # 100 s nominal
    assert seen[-1] >= 100.0 + 100_000 * stim_mod.PULSE_SLACK_S + 2.0
    stim.edges("m0", 100, max_edges=1024)  # 1024 x 24 characters at 115200 baud: 2.1 s
    assert seen[-1] >= 0.1 + 2.0 + 1024 * 24 * 10 / 115200
    stim.edges("m0", 100)  # 64 edges by default
    assert 2.1 < seen[-1] < 2.4
