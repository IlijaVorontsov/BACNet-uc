"""Flash guard R-07 and flashing commands (hilrig.flash), with a fake OpenOCD."""

from __future__ import annotations

import dataclasses
import stat
from pathlib import Path

import pytest

from hilrig import flash
from hilrig.bench import Bench
from hilrig.flash import (
    IDCODE_REV_A,
    IDCODE_REV_Z,
    FlashError,
    Identity,
    check_identity,
    identity_from_words,
    openocd_argv,
    parse_mdw,
    read_identity,
    west_flash_argv,
)

HOST = Path(__file__).resolve().parents[2] / "host"
WORDS = (0x00290038, 0x33385114, 0x34373932)  # the bench.yml.example commissioning values
OUTPUT = """\
Open On-Chip Debugger 0.12.0+dev
Info : clock speed 2000 kHz
Info : STLINK V2J40M27 (API v2) VID:PID 0483:374B
0xe0042000: 10016451
0x40023c14: c0ffaafd
0x1ff0f420: 00290038 33385114 34373932
"""


def bench() -> Bench:
    return Bench.load(HOST / "bench.yml.example")


def test_parse_mdw_reads_every_word() -> None:
    words = parse_mdw(OUTPUT)
    assert words[0xE0042000] == IDCODE_REV_Z and words[0x40023C14] == 0xC0FFAAFD
    assert (words[0x1FF0F420], words[0x1FF0F424], words[0x1FF0F428]) == WORDS
    identity = identity_from_words(words)
    assert identity.rdp == 0xAA and identity.mac == "02:80:e1:67:44:68"
    assert identity.uid == "343739323338511400290038"
    with pytest.raises(FlashError, match="0x1ff0f428"):
        identity_from_words({0xE0042000: 1, 0x40023C14: 2, 0x1FF0F420: 3, 0x1FF0F424: 4})


def test_guard_passes_the_commissioned_board() -> None:
    result = check_identity(Identity(IDCODE_REV_Z, 0xC0FFAAFD, WORDS), bench())
    assert result.ok and result.warnings == []


@pytest.mark.parametrize(
    ("identity", "problem"),
    [
        (Identity(0x10016413, 0xC0FFAAFD, WORDS), "IDCODE 0x10016413"),  # an F4, not an F76x
        (Identity(IDCODE_REV_Z, 0xC0FFBBFD, WORDS), "FLASH_OPTCR[15:8] = 0xbb"),  # RDP level 1
        (Identity(IDCODE_REV_Z, 0xC0FFAAFD, (1, 2, 3)), "wrong board"),  # another chip on the probe
    ],
)
def test_guard_refuses_a_mismatch(identity: Identity, problem: str) -> None:
    result = check_identity(identity, bench())
    assert not result.ok and any(problem in e for e in result.errors)


def test_guard_warns_on_rev_a_and_checks_the_bench_uid() -> None:
    assert check_identity(Identity(IDCODE_REV_A, 0xC0FFAAFD, WORDS), bench()).warnings
    other_uid = dataclasses.replace(bench(), dut=dataclasses.replace(bench().dut, uid="00" * 12))
    assert any(
        "UID" in e for e in check_identity(Identity(IDCODE_REV_Z, 0xC0FFAAFD, WORDS), other_uid).errors
    )


def test_commands_select_the_probe_by_serial() -> None:
    argv = openocd_argv("openocd", "SN1", ["mdw 0xe0042000"])
    assert argv[:7] == ["openocd", "-f", "board/st_nucleo_f7.cfg", "-c", "adapter serial SN1", "-c", "init"]
    assert argv[-4:] == ["-c", "mdw 0xe0042000", "-c", "shutdown"]
    assert west_flash_argv(Path("/b"), "SN1") == [
        "west", "flash", "-d", "/b", "-r", "openocd", "--", "--cmd-pre-init", "adapter serial SN1",
    ]  # fmt: skip
    assert "--erase" in west_flash_argv(Path("/b"), "SN1", erase=True)


def fake_openocd(tmp_path: Path, output: str, rc: int = 0) -> str:
    script = tmp_path / "openocd"
    script.write_text(f"#!/bin/sh\necho \"$@\" > {tmp_path}/args\ncat <<'EOF' >&2\n{output}EOF\nexit {rc}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_read_identity_runs_openocd(tmp_path: Path) -> None:
    identity, _ = read_identity("SN1", fake_openocd(tmp_path, OUTPUT))
    assert identity == Identity(IDCODE_REV_Z, 0xC0FFAAFD, WORDS)
    assert "adapter serial SN1" in (tmp_path / "args").read_text()
    with pytest.raises(FlashError, match="failed"):
        read_identity("SN1", fake_openocd(tmp_path, "Error: open failed\n", rc=1))


def test_find_openocd_prefers_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(flash.OPENOCD_ENV, "/opt/x/openocd")
    assert flash.find_openocd() == "/opt/x/openocd"
    monkeypatch.delenv(flash.OPENOCD_ENV)
    tool = tmp_path / flash._HOSTTOOLS
    tool.parent.mkdir(parents=True)
    tool.write_text("")
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", str(tmp_path))
    assert flash.find_openocd() == str(tool)


def test_tty_without_usb_device_has_no_serial(tmp_path: Path) -> None:
    assert flash.usb_serial_of(str(tmp_path / "ttyNOPE")) is None
    assert flash.usb_devnum_of(str(tmp_path / "ttyNOPE")) is None
