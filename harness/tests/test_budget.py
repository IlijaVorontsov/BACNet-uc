# SPDX-License-Identifier: Apache-2.0
"""WAMR pool budget: board pool sizes, per-app estimates, validate_system."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bacnet_uc_harness import budget
from bacnet_uc_harness.manifest import load_system_dict
from bacnet_uc_harness.planner import ArtifactBuilder
from bacnet_uc_harness.wasm_build import build_c

REPO = Path(__file__).resolve().parents[2]
needs_clang = pytest.mark.skipif(shutil.which("clang") is None, reason="clang required")


def test_board_pool_sizes() -> None:
    confs = {b: (REPO / "firmware" / "boards" / f"{k}.conf").read_text()
             for b, k in (("nucleo_f767zi", "nucleo_f767zi"),
                          ("frdm_mcxn947/mcxn947/cpu0", "frdm_mcxn947_mcxn947_cpu0"),
                          ("native_sim/native/64", "native_sim_native_64"))}
    for board, text in confs.items():
        size, source = budget.board_pool_size(board)
        assert f"CONFIG_UC_APP_POOL_SIZE={size}" in text and source.endswith(".conf")
    assert budget.board_pool_size("native_sim") == budget.board_pool_size("native_sim/native/64")
    # prj.conf does not set it: the Kconfig default
    assert "CONFIG_UC_APP_POOL_SIZE" not in (REPO / "firmware" / "prj.conf").read_text()
    assert budget.board_pool_size("some_other_board") == (budget.KCONFIG_POOL_DEFAULT,
                                                          "firmware/Kconfig default")


@needs_clang
def test_estimates_match_measurements(tmp_path: Path) -> None:
    """The model reproduces the pool use measured on native_sim (+-3 %)."""
    measured = {"thermostat": (8, 54920), "blinky": (8, 38776), "alarm": (8, 50376),
                "uc-link": (0, 39496)}
    for name, (heap, used) in measured.items():
        src = REPO / "wasm" / "examples" / name / f"{name.replace('-', '_')}.c"
        wasm = build_c([src], tmp_path / f"{name}.wasm", opt="-Oz", heap_kb=heap).path
        est = budget.app_pool_estimate(name, wasm, heap_kb=heap)
        assert est.linear == (16384 if heap else 8192)
        assert abs(est.total - used) <= 0.03 * used, (name, est.to_dict(), used)


@needs_clang
def test_system_budget_warning(tmp_path: Path) -> None:
    thermostat = REPO / "wasm" / "examples" / "thermostat" / "thermostat.c"
    apps = [{"name": f"t{i}", "node": "n", "source": str(thermostat), "heap_kb": 16,
             "params": {"sp_instance": i}} for i in range(3)]
    doc = {"apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "b"},
           "nodes": [{"name": "n", "board": "frdm_mcxn947/mcxn947/cpu0",
                      "transport": {"kind": "udp", "host": "10.0.0.1"},
                      "device": {"instance": 1, "name": "n"}}],
           "apps": apps}
    system = load_system_dict(doc)
    builder = ArtifactBuilder(tmp_path / "cache")
    budgets = budget.system_budgets(system, builder.wasm_for, builder.build_system(system))
    b = budgets["n"]
    assert b.pool == 98304 and len(b.apps) == 3 and b.status == "exceeded"
    msg = b.message()
    assert msg is not None and "CONFIG_UC_APP_POOL_SIZE is 98304" in msg and "NO_MEM" in msg
    assert b.to_dict()["estimate"] == sum(a.total for a in b.apps)
    small = budget.node_budget("n", "native_sim/native/64",
                               [{"name": "x", "wasm": builder.wasm_for(system.apps[0])},
                                {"name": "gone", "wasm": None}])
    assert small.status == "ok" and small.message() is None
    assert small.unknown and small.unknown[0].startswith("gone")
