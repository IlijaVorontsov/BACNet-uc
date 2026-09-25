#!/bin/bash
# pre-flash.sh - Twister pre_script of the HIL DUT (map.yml): the R-07 flash guard, OpenOCD only.
#
# Installed root-owned as /opt/hil/bin/pre-flash.sh by install-runner.sh. The Twister pytest
# harness runs it with no arguments right before every flash of the DUT, inside the pytest
# session (after the console is connected), with that session's environment. It repeats the
# guard that the hil_session fixture already ran (defence in depth, docs/HIL.md 8.4):
#   - IDCODE (0xE0042000) is an STM32F76x rev Z, or rev A with a warning;
#   - FLASH_OPTCR[15:8] (0x40023C14) is 0xAA, RDP level 0. Option bytes are never written;
#   - the UID-derived MAC (D29) equals bench.yml dut.mac (and the UID dut.uid when set).
# It reads over SWD only, through the DUT probe selected by serial ('adapter serial'), and it
# never opens the stimulus tty: the pytest session holds it, exclusively.
#
# Environment (run-hil sets it): HIL_BENCH (default /etc/hil/bench1/bench.yml), HIL_PYTHON
# (default /opt/hil/venv/bin/python), PYTHONPATH (the run's hilrig), ZEPHYR_SDK_INSTALL_DIR or
# HIL_OPENOCD (OpenOCD from the SDK host tools).
# Exit status: 0 the board is the bench's DUT, 1 refused (wrong board, RDP, OpenOCD error).
set -euo pipefail

bench=${HIL_BENCH:-/etc/hil/bench1/bench.yml}
py=${HIL_PYTHON:-/opt/hil/venv/bin/python}

exec "$py" - "$bench" <<'EOF'
import sys

from hilrig.bench import Bench, BenchError
from hilrig.flash import FlashError, check_identity, read_identity

try:
    bench = Bench.load(sys.argv[1])
    if not bench.dut.probe or not bench.dut.mac:
        raise BenchError(f"{sys.argv[1]}: dut.probe and dut.mac are needed by the flash guard")
    identity, _ = read_identity(bench.dut.probe)
except (BenchError, FlashError) as e:
    sys.exit(f"pre-flash: R-07 guard: {e}")
result = check_identity(identity, bench)
for warning in result.warnings:
    print(f"pre-flash: warning: {warning}")
if not result.ok:
    sys.exit("pre-flash: R-07 guard refused the flash: " + "; ".join(result.errors))
print(f"pre-flash: probe {bench.dut.probe}: IDCODE 0x{identity.idcode:08x}, RDP 0x{identity.rdp:02x}, "
      f"MAC {identity.mac}: bench {bench.name} DUT")
EOF
