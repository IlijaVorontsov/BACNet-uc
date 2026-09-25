"""R-07 flash guard and identity (rig gate; hardware; runs before any flash in the session).

Catalogue R-07: IDCODE at 0xE0042000 is 0x10016451 (rev Z), or 0x10006451 (rev A: warning).
FLASH_OPTCR[15:8] is 0xAA. The MAC from D29 (02:80:E1 + low 3 bytes, little-endian, of crc32
over be32(w@0x1FF0F428) + be32(w@0x1FF0F424) + be32(w@0x1FF0F420)) equals bench.yml dut.mac.
The probe serial equals bench dut.probe. Any mismatch: no flash, rig fault. Option bytes are
never written.

The session start runs the same guard before it flashes (conftest ``run_guard``); this test
reports it. OpenOCD only reads memory here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hilrig import flash as flash_mod
from hilrig.bench import Bench

pytestmark = [pytest.mark.rig(gate=True), pytest.mark.hil_only]


def test_r07_flash_guard_and_identity(
    bench: Bench, rig_report: Callable[[str, dict[str, Any]], Path]
) -> None:
    if not bench.dut.probe:
        pytest.fail("bench.yml dut.probe is not set: the guard cannot select the DUT's probe")
    if flash_mod.find_openocd() is None:
        pytest.fail(f"OpenOCD not found (set {flash_mod.OPENOCD_ENV} or ZEPHYR_SDK_INSTALL_DIR): no guard")
    identity, output = flash_mod.read_identity(bench.dut.probe)  # fails if no probe has this serial
    result = flash_mod.check_identity(identity, bench)
    tty_probe = flash_mod.usb_serial_of(bench.dut.serial) if bench.dut.serial else None
    rig_report(
        "R-07",
        {
            "idcode": f"0x{identity.idcode:08x}",
            "rdp": f"0x{identity.rdp:02x}",
            "uid": identity.uid,
            "mac": identity.mac,
            "console_probe": tty_probe,
            "errors": result.errors,
            "warnings": result.warnings,
            "openocd": output[-2000:],
        },
    )
    for warning in result.warnings:
        print(f"R-07 warning: {warning}")
    assert result.ok, "; ".join(result.errors)
    if bench.dut.serial:
        assert tty_probe == bench.dut.probe, f"{bench.dut.serial} belongs to probe {tty_probe}, not dut.probe"
