"""Flashing and the flash guard R-07 for P1 (HIL.md 8.4), through OpenOCD by probe serial.

Both boards are ST-LINK V2-1 (0483:374b) on the same platform, and ``west flash --serial``
does not pick the probe on nucleo_f767zi (its openocd.cfg ignores ``_ZEPHYR_BOARD_SERIAL``),
so every OpenOCD call selects the probe with ``adapter serial <SN>`` (D3). Before any flash
the guard reads, over SWD and without writing anything:

- IDCODE at 0xE0042000: 0x10016451 (rev Z) passes, 0x10006451 (rev A) passes with a warning;
- FLASH_OPTCR[15:8] at 0x40023C14: 0xAA (RDP level 0). Option bytes are never written;
- the UID words at 0x1FF0F420: the D29 MAC computed from them must equal bench.yml dut.mac.

Any mismatch means the wrong board is on the probe: no flash, rig fault.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from hilrig.bench import Bench, stm32_mac, stm32_uid

IDCODE_ADDR = 0xE0042000  # DBGMCU_IDCODE
OPTCR_ADDR = 0x40023C14  # FLASH_OPTCR
UID_ADDR = 0x1FF0F420  # UID_BASE on STM32F7
IDCODE_REV_Z, IDCODE_REV_A = 0x10016451, 0x10006451
RDP_LEVEL_0 = 0xAA
OPENOCD_ENV = "HIL_OPENOCD"
BOARD_CFG = "board/st_nucleo_f7.cfg"
_HOSTTOOLS = "hosttools/sysroots/x86_64-pokysdk-linux/usr/bin/openocd"
_MDW = re.compile(r"^0x([0-9a-fA-F]+):((?:\s+[0-9a-fA-F]{8})+)\s*$", re.MULTILINE)


class FlashError(RuntimeError):
    """OpenOCD or west failed, or the guard refused the board."""


def find_openocd() -> str | None:
    """OpenOCD: ``$HIL_OPENOCD``, the SDK 1.0.1 host tools, or PATH."""
    if path := os.environ.get(OPENOCD_ENV):
        return path
    sdk = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    if sdk and (Path(sdk) / _HOSTTOOLS).exists():
        return str(Path(sdk) / _HOSTTOOLS)
    return shutil.which("openocd")


def openocd_argv(openocd: str, probe: str, commands: Sequence[str], board_cfg: str = BOARD_CFG) -> list[str]:
    """An OpenOCD command line on the probe with serial ``probe`` that runs ``commands``."""
    argv = [openocd, "-f", board_cfg, "-c", f"adapter serial {probe}", "-c", "init"]
    for command in commands:
        argv += ["-c", command]
    return [*argv, "-c", "shutdown"]


def parse_mdw(text: str) -> dict[int, int]:
    """Words printed by OpenOCD ``mdw`` (``0xe0042000: 10016451``), by address."""
    words: dict[int, int] = {}
    for m in _MDW.finditer(text):
        base = int(m[1], 16)
        for i, word in enumerate(m[2].split()):
            words[base + 4 * i] = int(word, 16)
    return words


@dataclass(frozen=True)
class Identity:
    """What the guard reads from the DUT over SWD."""

    idcode: int
    optcr: int
    uid_words: tuple[int, int, int]

    @property
    def rdp(self) -> int:
        """Read-out protection level byte (0xAA = level 0)."""
        return (self.optcr >> 8) & 0xFF

    @property
    def uid(self) -> str:
        """The UID as hwinfo returns it (hex), the MQTT info 'hwid'."""
        return stm32_uid(*self.uid_words).hex()

    @property
    def mac(self) -> str:
        """The D29 MAC of this chip."""
        return stm32_mac(*self.uid_words)


@dataclass
class GuardResult:
    """The R-07 verdict: errors refuse the flash, warnings are reported."""

    identity: Identity
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def identity_from_words(words: dict[int, int]) -> Identity:
    """Build :class:`Identity` from :func:`parse_mdw` output; FlashError if a word is missing."""
    needed = [IDCODE_ADDR, OPTCR_ADDR, UID_ADDR, UID_ADDR + 4, UID_ADDR + 8]
    missing = [f"0x{a:08x}" for a in needed if a not in words]
    if missing:
        raise FlashError(f"OpenOCD did not print {', '.join(missing)}")
    uid = (words[UID_ADDR], words[UID_ADDR + 4], words[UID_ADDR + 8])
    return Identity(words[IDCODE_ADDR], words[OPTCR_ADDR], uid)


def read_identity(probe: str, openocd: str | None = None, timeout: float = 30.0) -> tuple[Identity, str]:
    """Read IDCODE, FLASH_OPTCR and the UID through OpenOCD; return them and OpenOCD's output."""
    tool = openocd or find_openocd()
    if tool is None:
        raise FlashError(f"OpenOCD not found (set {OPENOCD_ENV} or ZEPHYR_SDK_INSTALL_DIR)")
    commands = [f"mdw 0x{IDCODE_ADDR:08x}", f"mdw 0x{OPTCR_ADDR:08x}", f"mdw 0x{UID_ADDR:08x} 3"]
    proc = subprocess.run(
        openocd_argv(tool, probe, commands), capture_output=True, text=True, timeout=timeout, check=False
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise FlashError(f"OpenOCD on probe {probe} failed ({proc.returncode}): {output.strip()[-500:]}")
    return identity_from_words(parse_mdw(output)), output


def check_identity(identity: Identity, bench: Bench) -> GuardResult:
    """Compare what the DUT reports with bench.yml (R-07 criteria)."""
    result = GuardResult(identity)
    if identity.idcode == IDCODE_REV_A:
        result.warnings.append("silicon rev A (IDCODE 0x10006451): Ethernet instability on cut A")
    elif identity.idcode != IDCODE_REV_Z:
        result.errors.append(f"IDCODE 0x{identity.idcode:08x} is not an STM32F76x rev Z/A")
    if identity.rdp != RDP_LEVEL_0:
        result.errors.append(f"FLASH_OPTCR[15:8] = 0x{identity.rdp:02x}, not 0xAA (RDP level 0)")
    if identity.mac != bench.dut.mac:
        result.errors.append(
            f"UID-derived MAC {identity.mac} != bench dut.mac {bench.dut.mac} (wrong board?)"
        )
    if bench.dut.uid is not None and identity.uid != bench.dut.uid:
        result.errors.append(f"UID {identity.uid} != bench dut.uid {bench.dut.uid}")
    return result


def usb_serial_of(tty: str) -> str | None:
    """The USB serial number of the device behind a tty (the ST-LINK behind the VCP)."""
    name = Path(os.path.realpath(tty)).name
    device = Path(f"/sys/class/tty/{name}/device")
    for parent in [device.resolve(), *device.resolve().parents]:
        serial = parent / "serial"
        if serial.is_file() and (parent / "idVendor").is_file():
            return serial.read_text().strip()
    return None


def usb_devnum_of(tty: str) -> tuple[str, int] | None:
    """(bus-port path, devnum) of the USB device behind a tty: a re-enumeration changes it."""
    name = Path(os.path.realpath(tty)).name
    device = Path(f"/sys/class/tty/{name}/device")
    if not device.exists():
        return None
    for parent in [device.resolve(), *device.resolve().parents]:
        devnum = parent / "devnum"
        if devnum.is_file() and (parent / "idVendor").is_file():
            return parent.name, int(devnum.read_text())
    return None


def west_flash_argv(build_dir: Path, probe: str, *, erase: bool = False) -> list[str]:
    """``west flash`` with the probe selected by serial (D3), optionally mass-erasing (D25)."""
    argv = ["west", "flash", "-d", str(build_dir), "-r", "openocd"]
    if erase:
        argv.append("--erase")
    return [*argv, "--", "--cmd-pre-init", f"adapter serial {probe}"]


@dataclass(frozen=True)
class FlashResult:
    """One flash: exit status, duration (s), end time (``time.time()``) and output."""

    returncode: int
    seconds: float
    ended: float
    output: str


def flash(build_dir: Path, probe: str, *, erase: bool = False, timeout: float = 120.0) -> FlashResult:
    """Flash ``build_dir`` through the probe ``probe`` (SYS-01: exit 0 within 120 s)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            west_flash_argv(build_dir, probe, erase=erase),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        rc, output = proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        rc, output = -1, f"west flash timed out after {timeout} s: {e.output!r}"
    return FlashResult(rc, time.monotonic() - start, time.time(), output)


def reset_target(probe: str, *, halt: bool = False, openocd: str | None = None) -> None:
    """Reset a board through its probe: ``reset run``, or ``reset halt`` to hold it (PWR-02).

    With ``halt`` the core stays at its reset vector after OpenOCD exits (``shutdown`` does
    not resume a halted target), so the board's pins stay in their reset state.
    """
    tool = openocd or find_openocd()
    if tool is None:
        raise FlashError(f"OpenOCD not found (set {OPENOCD_ENV} or ZEPHYR_SDK_INSTALL_DIR)")
    command = "reset halt" if halt else "reset run"
    proc = subprocess.run(
        openocd_argv(tool, probe, [command]), capture_output=True, text=True, timeout=30, check=False
    )
    if proc.returncode != 0:
        raise FlashError(
            f"OpenOCD '{command}' on {probe} failed: {(proc.stdout + proc.stderr).strip()[-500:]}"
        )
