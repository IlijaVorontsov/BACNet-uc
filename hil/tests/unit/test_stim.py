"""Stimulus driver, protocol v0 (hilrig.stim), against the pty fake in fake_stim.py."""

from __future__ import annotations

import os
import tty
from collections.abc import Iterator
from pathlib import Path

import pytest

from fake_stim import FakeStim
from hilrig import stim as stim_mod
from hilrig.stim import ETIMEDOUT, AdcReading, Edges, Pulse, Stim, StimError, StimLinkError, parse_reply


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


def test_typed_commands(stim: Stim, fake: FakeStim) -> None:
    info = stim.info()
    assert (info.proto, info.board, info.chans) == (0, "fake", fake.chans)
    assert info.uptime_ms >= 0
    stim.dout("do3", 1)
    assert stim.din("di1") == 1  # do3 reaches di1 through the (fake) DUT
    stim.dout("do3", "z")
    assert stim.din("di1") == 0
    assert stim.pulse("do3", 100, 5, 1000) == Pulse(n=5, t0_ns=1_000_000)
    assert stim.edges("m0", 50) == Edges(t_ns=(1000, 2000, 3000), levels=(1, 0, 1))
    assert stim.lat("do3", 1, "di1", 1, 100) == 1500
    assert stim.dac("ai0", 5000) == 3300
    assert stim.adc("ai0", 8) == AdcReading(mv=1650.0, raw=2048.0, n=8)
    stim.reset()
    stim.reset(20)
    stim.power("cycle", 100)
    stim.safe()
    assert fake.levels["do3"] == "z"
    assert "stim reset 20" in fake.commands and "stim power cycle 100" in fake.commands


def test_device_errors_raise_stimerror(stim: Stim) -> None:
    with pytest.raises(StimError) as err:
        stim.lat("do3", 1, "di2", 1, 10)  # not wired: the fake times out like the board
    assert err.value.errno == ETIMEDOUT
    with pytest.raises(StimError) as err:
        stim.dout("do9", 1)
    assert err.value.errno == -2


def test_reply_without_ok_or_err_is_a_link_error(stim: Stim) -> None:
    with pytest.raises(StimLinkError, match="no reply"):
        stim.command("stim bogus")
    assert stim.din("di1") == 0  # the link is still usable


def test_state_changing_command_is_never_retried(stim: Stim, fake: FakeStim) -> None:
    fake.garble.add("dout")
    with pytest.raises(StimLinkError):
        stim.dout("do3", 1)
    assert fake.commands.count("stim dout do3 1") == 1


def test_read_only_command_is_retried_once(stim: Stim, fake: FakeStim) -> None:
    fake.garble.add("din")
    assert stim.din("di1") == 0
    assert fake.commands.count("stim din di1") == 2


def test_sync_discards_the_late_reply_of_a_timed_out_command(stim: Stim, fake: FakeStim) -> None:
    fake.delay["safe"] = 1.0
    with pytest.raises(StimLinkError, match="no reply within"):
        stim.safe()
    stim.sync()
    stim.dout("do3", 1)
    assert stim.din("di1") == 1


def test_next_command_resyncs_after_a_link_error(stim: Stim, fake: FakeStim) -> None:
    """Regression: the late OK of a timed-out command was taken as the next command's reply."""
    fake.delay["safe"] = 1.0
    with pytest.raises(StimLinkError, match="no reply within"):
        stim.safe()
    assert stim.command("stim din di1") == {"v": "0"}  # no sync() by the caller, no retry


def test_unusable_reply_fields_are_link_errors(stim: Stim, fake: FakeStim) -> None:
    """Regression: a reply lacking a field or with a non-numeric one raised KeyError/ValueError."""
    fake.corrupt.add("din")
    assert stim.din("di1") == 0  # read-only: retried
    assert fake.commands.count("stim din di1") == 2
    fake.corrupt.add("dac")
    with pytest.raises(StimLinkError, match="unusable reply fields"):
        stim.dac("ao0", 1000)
    assert fake.commands.count("stim dac ao0 1000") == 1  # state-changing: never retried
    with pytest.raises(ValueError, match="must be 0 or 1"):
        stim_mod._edges({"n": "1", "t_ns": "10", "lv": "2"})


@pytest.mark.timeout(30)
def test_board_that_stops_reading_fails_the_write(stim: Stim, fake: FakeStim) -> None:
    """Regression: writes had no timeout and flush() waited in tcdrain() for ever."""
    fake.stalled = True
    with pytest.raises(StimLinkError, match="serial write failed"):
        stim.command("stim " + "x" * 1_000_000)


def test_unplugged_board_is_a_link_error(stim: Stim, fake: FakeStim) -> None:
    """Regression: a vanished port raised SerialException/termios.error, not StimLinkError."""
    fake.hangup()
    with pytest.raises(StimLinkError, match="serial"):
        stim.info()


def test_traffic_is_logged(stim: Stim, tmp_path: Path) -> None:
    stim.din("di1")
    log = (tmp_path / "stim.log").read_text()
    assert "> 'stim din di1\\r'" in log and "OK v=0" in log


def test_banner_is_parsed_on_boot(tmp_path: Path) -> None:
    with FakeStim(boot_delay=0.3) as fake, Stim(fake.port, timeout=0.5) as stim:
        assert stim.banner == "STIM READY proto=0 fw=0.0.0-fake board=fake rc=0x1"


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
