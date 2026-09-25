# SPDX-License-Identifier: Apache-2.0
"""examples/systems/sim-demo.yaml on two real native_sim nodes in network
namespaces, driven through the ``bacnet-uc`` CLI (and so the MCP tools):
sim up, validate, plan, apply, re-plan, test, IO drift, sim down."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness import cli
from bacnet_uc_harness.bacnet.client import BacnetClient
from bacnet_uc_harness.errors import BacnetError

pytestmark = pytest.mark.e2e

MANIFEST = Path(__file__).resolve().parents[2] / "examples" / "systems" / "sim-demo.yaml"
PASSWORD = "sim-demo-pw"  # bacnet.password of both nodes in the manifest


class Cli:
    """In-process ``bacnet-uc --json --home <home> ...``."""

    def __init__(self, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self.home = home
        self.capsys = capsys

    def __call__(self, *args: str) -> tuple[int, Any]:
        rc = cli.main(["--json", "--home", str(self.home), *args])
        out = self.capsys.readouterr()
        text = out.out.strip() or out.err.strip()
        try:
            return rc, json.loads(text) if text else None
        except json.JSONDecodeError:
            return rc, text


def actions(plan: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(a["node"], a["kind"], a["target"]) for a in plan["actions"]}


@pytest.fixture
def run(firmware_exe: Path, netns_required: None, tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> Iterator[Cli]:
    if shutil.which("clang") is None:
        pytest.skip("clang required (the apps are built from source)")
    c = Cli(tmp_path, capsys)
    rc, up = c("sim", "up", str(MANIFEST), "--exe", str(firmware_exe), "--erase")
    try:
        assert rc == 0, up
        assert up["addresses"] == {"sim-a": "10.47.0.1", "sim-b": "10.47.0.2"}
        assert all(up["reachable"].values()), up
        yield c
    finally:
        rc_down, down = c("sim", "down", str(MANIFEST))
        assert rc_down == 0 and sorted(down["stopped"]) == ["sim-a", "sim-b"], down


async def _dcc(address: tuple[str, int]) -> None:
    async with BacnetClient(apdu_timeout=1.0, retries=2) as client:
        await client.device_communication_control(address, "enable", password=PASSWORD)
        with pytest.raises(BacnetError) as exc:
            await client.device_communication_control(address, "enable", password="wrong")
        assert exc.value.error_code_name == "password-failure"


def test_sim_demo(run: Cli, tmp_path: Path) -> None:
    rc, report = run("system", "validate", str(MANIFEST))
    assert rc == 0 and report["ok"], report
    assert report["wamr_pool"]["sim-b"]["status"] == "ok", report["wamr_pool"]
    # a fresh simulation: everything is planned
    rc, plan = run("system", "plan", str(MANIFEST))
    assert rc == 0 and not plan["in_sync"]
    assert actions(plan) >= {
        ("sim-a", "push_config", "device"), ("sim-a", "push_config", "io"),
        ("sim-b", "push_config", "device"), ("sim-b", "push_config", "io"),
        ("sim-b", "deploy_app", "thermostat"), ("sim-b", "deploy_app", "link")}
    # apply: staged documents, apps, then both nodes restart (instance, address)
    rc, rep = run("system", "apply", str(MANIFEST), "--no-dry-run")
    assert rc == 0 and rep["ok"], rep
    assert sorted(rep["rebooted"]) == ["sim-a", "sim-b"], rep
    rc, plan = run("system", "plan", str(MANIFEST))
    assert rc == 0 and plan["in_sync"], plan
    # the acceptance tests of the manifest
    rc, res = run("system", "test", str(MANIFEST))
    assert rc == 0 and res["ok"] and res["passed"] == 3, res
    # device.json passed bacnet.password through
    asyncio.run(_dcc(("10.47.0.1", 47808)))
    asyncio.run(_dcc(("10.47.0.2", 47808)))
    # drift: io.json of sim-b replaced on the node; the plan pushes it back and
    # restarts the apps that use sim-b's re-created IO objects
    points = tmp_path / "points.json"
    points.write_text(json.dumps([{"channel": "do0", "type": "binary-output", "instance": 1,
                                   "name": "Lamp 2"}]))
    rc, out = run("io", "configure", "sim-b", str(points))
    assert rc == 0 and out["result"]["activated"], out
    assert sorted(out["restarted_apps"]) == ["link", "thermostat"], out
    rc, plan = run("system", "plan", str(MANIFEST))
    assert actions(plan) == {("sim-b", "push_config", "io"), ("sim-b", "restart_app", "link"),
                             ("sim-b", "restart_app", "thermostat")}, plan
    rc, rep = run("system", "apply", str(MANIFEST), "--no-dry-run")
    assert rc == 0 and rep["ok"] and not rep["rebooted"], rep
    rc, plan = run("system", "plan", str(MANIFEST))
    assert plan["in_sync"], plan
    rc, res = run("system", "test", str(MANIFEST), "switch-drives-lamp")
    assert rc == 0 and res["ok"], res
    rc, status = run("system", "status", str(MANIFEST))
    assert rc == 0 and status["in_sync"], status
    assert status["nodes"]["sim-b"]["apps"] == {"thermostat": "running", "link": "running"}
