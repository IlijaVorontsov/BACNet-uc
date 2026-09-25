"""S-03 the MQTT session's e2e_native_sim.sh, run as-is (sil; slow).

Catalogue S-03: the script exits 0 (9 builds + 20 checks). It runs unmodified until the MQTT
session agrees to a port.

It lives here rather than in tests/sil because that directory belongs to the implemented
S-01/S-02 files. The script builds native_sim images with ``west build`` from its own app
directory, so the MQTT checkout (``--mqtt-checkout`` / ``$HIL_MQTT_CHECKOUT``) must sit in a
west workspace whose manifest it is, with the Zephyr venv active.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.sil, pytest.mark.slow]

SCRIPT = Path("apps/mqtt_tls/scripts/e2e_native_sim.sh")


@pytest.mark.timeout(3600)
def test_s03_mqtt_e2e_native_sim(pytestconfig: pytest.Config, artifacts: Path) -> None:
    checkout = pytestconfig.getoption("--mqtt-checkout")
    if not checkout:
        pytest.skip("S-03 needs the MQTT branch checkout: --mqtt-checkout DIR or $HIL_MQTT_CHECKOUT")
    script = Path(checkout) / SCRIPT
    if not script.is_file():
        pytest.skip(f"{script} not found (not the MQTT branch?)")
    missing = [
        t for t in ("west", "mosquitto", "mosquitto_sub", "mosquitto_pub", "openssl") if not shutil.which(t)
    ]
    if missing:
        pytest.skip(f"the script needs {', '.join(missing)} on PATH")
    topdir = subprocess.run(
        ["west", "topdir"], cwd=script.parent, capture_output=True, text=True, check=False
    )
    if topdir.returncode != 0:
        pytest.skip(f"{checkout} is not inside a west workspace: {topdir.stderr.strip()}")
    work = artifacts / "s03"
    work.mkdir(parents=True, exist_ok=True)
    log = work / "e2e_native_sim.log"
    with log.open("w") as out:
        proc = subprocess.run(
            ["bash", str(script), str(work)],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=dict(os.environ),
            check=False,
        )
    tail = log.read_text()[-2000:]
    assert proc.returncode == 0, f"e2e_native_sim.sh exited {proc.returncode}; see {log}:\n{tail}"
    assert "=== PASS" in tail
