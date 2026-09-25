"""The suite's own rules (tests/conftest.py), checked by running a nested pytest.

A throwaway test file runs with this conftest loaded as a plugin and a hardware bench that
has nothing attached, so the session start has nothing to flash or check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HIL = Path(__file__).resolve().parents[2]
BENCH = """\
name: nested
mode: hil
dut: {profile: P1, board: nucleo_f767zi, mac: "02:80:e1:67:44:68", bacnet_instance: 260001}
net: {dut_iface: enx000000000001}
"""
TESTS = """\
import time
import pytest
from hilrig.stim import StimNoDevice

@pytest.mark.rig(gate=True)
def test_gate():
    assert GATE_OK

@pytest.mark.release
def test_dut():
    pass

@pytest.mark.release
@pytest.mark.slow
def test_slow_dut():
    pass

def test_no_device():
    raise StimNoDevice(-19, "source device", "stim dac ai1 500")

@pytest.mark.cycles(1, 2)
def test_cycles_timeout(request):
    assert request.node.get_closest_marker("timeout").args == (1 + 2 * 3,)
"""


def nested(tmp_path: Path, *args: str, gate_ok: bool = True, bench: bool = True) -> tuple[int, str]:
    (tmp_path / "bench.yml").write_text(BENCH)
    (tmp_path / "test_nested.py").write_text(TESTS.replace("GATE_OK", str(gate_ok)))
    argv = [sys.executable, "-m", "pytest", "-c", str(HIL / "pyproject.toml"), "--rootdir", str(HIL)]
    # no:twister_harness: the host venv has the Twister plugin installed (D2), and it refuses to
    # load without ZEPHYR_BASE, which this minimal environment leaves out on purpose
    argv += ["-p", "conftest", "-p", "no:cacheprovider", "-p", "no:twister_harness"]
    argv += ["-rs", str(tmp_path / "test_nested.py")]
    argv += ["--bench", str(tmp_path / "bench.yml")] if bench else []
    argv += ["--artifacts", str(tmp_path / "artifacts"), *args]
    # hil/src first: the nested run tests this checkout's hilrig, not an installed copy
    path = ":".join(str(p) for p in (HIL / "src", HIL / "tests", HIL / "tests" / "unit"))
    env = {"PYTHONPATH": path, "PATH": "/usr/bin:/bin"}
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path, check=False
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_gate_runs_first_and_a_failure_is_a_rig_fault(tmp_path: Path) -> None:
    rc, out = nested(tmp_path, "--hil", "--cycles", "3", "-v", gate_ok=False)
    assert rc == 1
    verdicts = ("PASSED", "FAILED", "SKIPPED")
    order = [
        x.split("::")[1].split()[0]
        for x in out.splitlines()
        if "::test_" in x and any(v in x for v in verdicts)
    ]
    assert order[0] == "test_gate"
    assert "rig fault: gate" in out and "RIG FAULT (not a DUT failure)" in out
    fault = json.loads((tmp_path / "artifacts" / "rig" / "rig-fault.json").read_text())
    assert fault["rig_fault"].startswith("gate ") and "test_gate" in fault["rig_fault"]
    assert order[:5] == ["test_gate", "test_dut", "test_slow_dut", "test_no_device", "test_cycles_timeout"]


def test_healthy_run_passes_and_enodev_skips(tmp_path: Path) -> None:
    rc, out = nested(tmp_path, "--hil", "--cycles", "3")
    assert rc == 0, out
    assert "stimulus hardware absent" in out and "slow: needs --enable-slow" in out


def test_a_hil_run_with_no_executed_test_fails(tmp_path: Path) -> None:
    """D31: an all-skipped HIL run must not report green."""
    rc, out = nested(tmp_path, "--hil", "-k", "slow_dut")
    assert rc == 1 and "1 skipped" in out
    rc, out = nested(tmp_path, "-k", "slow_dut", bench=False)  # unit runs (no bench): no such rule
    assert rc == 0, out


def test_a_hil_run_that_selects_nothing_fails(tmp_path: Path) -> None:
    """Regression: a -k/-m that deselected every test left exit 5, which Twister reports as a skip."""
    rc, out = nested(tmp_path, "--hil", "-k", "no_such_test")
    assert rc == 1 and "5 deselected" in out, out
    rc, out = nested(tmp_path, "--hil", "--collect-only", "-k", "no_such_test")  # a listing is no run
    assert rc == 5, out


def test_hil_select_narrows_the_selection(tmp_path: Path) -> None:
    rc, out = nested(tmp_path, "--hil", "--cycles", "3", "--hil-select", "rig or (release and not slow)")
    assert rc == 0, out
    assert "2 passed" in out and "3 deselected" in out
    rc, out = nested(tmp_path, "--hil", "--hil-select", "rig or")
    assert rc != 0 and "expression ends early" in out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--hil", "--sil"], "--hil and --sil are exclusive"),
        (["--sil-dut", "bacnet=/nonexistent/zephyr.exe", "--sil"], "is not a file"),
        (["--sil-dut", "bogus", "--sil"], "expected bacnet=<zephyr.exe>"),
        (["--sil"], "is a hil bench, but --sil was given"),
    ],
)
def test_bad_option_combinations_are_usage_errors(tmp_path: Path, args: list[str], message: str) -> None:
    rc, out = nested(tmp_path, *args)
    assert rc == 4 and message in out


def test_no_test_records_junit_properties() -> None:
    """Regression: BIP-01 and R-01 used record_property for informational numbers.

    Under the default junit family (xunit2) pytest drops those properties and warns, and under
    xunit1 they become the first child of <testcase>, which Twister's pytest harness
    (harness.py ``_parse_report_file``: ``elem_tc.find('*')``) takes as the verdict, so a pass
    is recorded as an error. Informational values go to ``<artifacts>/rig/<id>.json``.
    """
    users = [
        f"{path.relative_to(HIL)}:{n}"
        for path in sorted((HIL / "tests").rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if "record_property" in line and path.name != Path(__file__).name
    ]
    assert not users, f"record_property in {users}"
