"""Applying plans, verification, backups and rollback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from uc_hub.core.errors import DeviceError, DeviceTimeout, InvalidRequest
from uc_hub.core.types import Change, Plan
from uc_hub.manifest import (
    GATEWAY,
    ApplyProgress,
    MemoryBackupStore,
    SiteManifest,
    TargetBackup,
    apply_plan,
    compute_plan,
    rollback_target,
)
from uc_hub.manifest.nodedocs import CFG_APPS, CFG_DEVICE, CFG_IO, UC_LINK_FILE

from .conftest import THERMOSTAT, UC_LINK, resolver
from .fakes import FakeClock, FakeGateway, FakeNode, Nodes, wasm


@pytest.fixture
def uc_link(tmp_path: Path) -> Path:
    path = tmp_path / "uc-link.wasm"
    path.write_bytes(UC_LINK)
    return path


@pytest.fixture
def nodes() -> Nodes:
    return Nodes(FakeNode("r204-ctl", 2041), FakeNode("r205-ctl", 2051))


async def plan_for(doc: dict[str, Any], nodes: Nodes, uc_link: Path | None,
                   files: dict[str, bytes], live: SiteManifest | None = None) -> Plan:
    return await compute_plan(SiteManifest.from_dict(doc), live, nodes, resolver(files), uc_link, 2, 1)


def by_id(results: list[Any]) -> dict[str, Any]:
    return {r.change_id: r for r in results}


async def test_apply_new_site(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    gateway, backups, events = FakeGateway(), MemoryBackupStore(), []
    results = await apply_plan(plan, nodes, gateway, backups, events.append)
    assert all(r.ok for r in results), results
    ids = [r.change_id for r in results]
    assert ids == [*(c.id for c in plan.changes if c.target == "r204-ctl"), "r204-ctl/verify",
                   *(c.id for c in plan.changes if c.target == "r205-ctl"), "r205-ctl/verify",
                   *(c.id for c in plan.changes if c.target == GATEWAY)]
    site = SiteManifest.from_dict(doc)
    r204 = nodes.nodes["r204-ctl"]
    assert json.loads(r204.files[CFG_DEVICE]) == site.device_json("r204-ctl")
    assert json.loads(r204.files[CFG_IO]) == site.io_json("r204-ctl")
    assert r204.files["/lfs/apps/thermostat.wasm"] == THERMOSTAT and r204.files[UC_LINK_FILE] == UC_LINK
    assert {n: a["state"] for n, a in r204.apps.items()} == {"thermostat": "running", "link": "running"}
    assert [m for m, a in r204.calls if m == "reload"] == ["reload", "reload"]
    assert gateway.bridges == [b.to_dict() for b in site.bridges]
    assert gateway.tags == site.tags and gateway.safety == {"hq/ahu1-ctl/binary-output:9": "life-safety"}
    assert gateway.calls == ["tags", "bridges"]
    assert by_id(results)["r204-ctl/verify"].detail == "node matches the plan"

    backup = backups.get(plan.id, "r204-ctl")
    assert backup is not None
    assert backup.files == {CFG_DEVICE: None, CFG_IO: None, CFG_APPS: None,
                            "/lfs/apps/thermostat.wasm": None, UC_LINK_FILE: None}
    assert backups.get(plan.id, GATEWAY) == TargetBackup(
        GATEWAY, plan.id, gateway={"tags": {}, "safety": {}, "bridges": []},
        created_at=backups.get(plan.id, GATEWAY).created_at)  # type: ignore[union-attr]
    assert events[0] == ApplyProgress("r204-ctl", "r204-ctl/backup", "running")
    assert ("r204-ctl/verify", "ok") in [(e.step, e.state) for e in events]
    assert {e.state for e in events} == {"running", "ok"}


async def test_install_uses_bytes_sha(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                      app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore())
    installs = [a[0] for m, a in nodes.nodes["r204-ctl"].calls if m == "app_install"]
    assert all(isinstance(i["sha256"], bytes) and len(i["sha256"]) == 32 for i in installs)


async def test_failure_stops_the_target_and_later_stages(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                         app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    r204 = nodes.nodes["r204-ctl"]
    r204.fail["reload"] = DeviceError("io.json rejected", rc=2, group=66)
    gateway, events = FakeGateway(), []
    results = await apply_plan(plan, nodes, gateway, MemoryBackupStore(), events.append, max_devices_per_stage=1)
    status = {r.change_id: (r.ok, r.detail) for r in results}
    r204_changes = [c for c in plan.changes if c.target == "r204-ctl"]
    reload = next(c for c in r204_changes if c.kind == "reload")
    assert status[reload.id] == (False, "DeviceError: io.json rejected")
    after = r204_changes[r204_changes.index(reload) + 1:]
    assert all(status[c.id] == (False, f"not run: {reload.id} failed") for c in after)
    assert "r204-ctl/verify" not in status
    for c in plan.changes:
        if c.target != "r204-ctl":
            assert status[c.id] == (False, "not run: an earlier stage failed")
    assert "file_upload" not in nodes.nodes["r205-ctl"].methods()
    assert gateway.calls == []
    assert "app_install" not in r204.methods()
    assert ApplyProgress("r204-ctl", reload.id, "failed", "DeviceError: io.json rejected") in events
    assert len(results) == len(plan.changes)


async def test_targets_in_one_stage_run_concurrently(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                     app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    nodes.nodes["r204-ctl"].fail["app_install"] = DeviceTimeout("no answer")
    results = by_id(await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore(), max_devices_per_stage=5))
    assert results["r205-ctl/verify"].ok
    assert not any(r.ok for cid, r in results.items() if cid in {c.id for c in plan.changes if c.target == GATEWAY})
    first_install = next(c for c in plan.changes if c.kind == "install-app")
    assert results[first_install.id].detail == "DeviceTimeout: no answer"


async def test_unexpected_error_fails_its_target_without_orphaning_the_stage(
        doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    r204, r205 = nodes.nodes["r204-ctl"], nodes.nodes["r205-ctl"]
    r204.fail["file_upload"] = RuntimeError("driver bug")
    upload = r205.file_upload

    async def slow_upload(path: str, data: bytes) -> None:
        await asyncio.sleep(0.01)
        await upload(path, data)

    r205.file_upload = slow_upload  # type: ignore[method-assign]
    results = by_id(await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore()))
    first = next(c for c in plan.changes if c.target == "r204-ctl")
    assert (results[first.id].ok, results[first.id].detail) == (False, "RuntimeError: driver bug")
    assert results["r205-ctl/verify"].ok
    assert len(results) == len(plan.changes) + 1
    files = dict(r205.files)
    await asyncio.sleep(0.05)
    assert r205.files == files


async def test_verification_failures(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                     app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    r204 = nodes.nodes["r204-ctl"]
    r204.broken.add("thermostat")
    r204.corrupt[CFG_IO] = b'{"schema": 1, "points": []}'
    results = by_id(await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore()))
    verify = results["r204-ctl/verify"]
    assert not verify.ok
    assert verify.detail == ("/lfs/cfg/io.json on the node differs from the uploaded document; "
                             "app thermostat failed: trap: unreachable")
    assert all(r.ok for cid, r in results.items() if cid.startswith("c") and
               next(c for c in plan.changes if c.id == cid).target == "r204-ctl")
    assert results["r205-ctl/verify"].ok
    assert not any(r.ok for cid, r in results.items()
                   if cid in {c.id for c in plan.changes if c.target == GATEWAY})


async def test_app_that_never_starts(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                     app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, None, app_files)
    nodes.nodes["r204-ctl"].slow.add("thermostat")
    clock = FakeClock()
    results = by_id(await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore(), verify_timeout_s=2.0,
                                     poll_interval_s=0.5, sleep=clock.sleep, clock=clock))
    assert results["r204-ctl/verify"].detail == "app thermostat is starting after 2 s, not running"
    assert clock.sleeps == [0.5, 0.5, 0.5, 0.5]


async def test_removed_app_is_verified(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                       app_files: dict[str, bytes]) -> None:
    nodes.nodes["r205-ctl"].install_direct({"name": "legacy", "file": "/lfs/apps/legacy.wasm"}, wasm("legacy"))
    plan = await plan_for(doc, nodes, uc_link, app_files)
    remove = next(c for c in plan.changes if c.kind == "remove-app")
    results = by_id(await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore()))
    assert results[remove.id].ok and results["r205-ctl/verify"].ok
    assert "legacy" not in nodes.nodes["r205-ctl"].apps


async def test_backup_failure_runs_nothing(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                           app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)

    class FailingStore(MemoryBackupStore):
        async def save(self, backup: TargetBackup) -> None:
            raise OSError("disk full")

    results = await apply_plan(plan, nodes, FakeGateway(), FailingStore(), max_devices_per_stage=1)
    assert results[0].change_id == "r204-ctl/backup"
    assert results[0].detail == "backup failed: OSError: disk full"
    assert not any(r.ok for r in results)
    assert "file_upload" not in nodes.nodes["r204-ctl"].methods()


async def test_gateway_failure(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    gateway = FakeGateway()
    gateway.fail = DeviceError("broker down")
    results = by_id(await apply_plan(plan, nodes, gateway, MemoryBackupStore()))
    tags, bridge = [c for c in plan.changes if c.target == GATEWAY]
    assert results[tags.id].ok
    assert (results[bridge.id].ok, results[bridge.id].detail) == (False, "DeviceError: broker down")


async def test_several_bridge_changes_apply_as_one_set(doc: dict[str, Any], nodes: Nodes) -> None:
    doc["bridges"].append({"from": "r204-co2/co2", "to": "ahu1-ctl/analog-value:7"})
    live = doc.copy()
    live["bridges"] = [{"from": "r204-co2/co2", "to": "r205-ctl/analog-value:1"}]
    plan = Plan("p9", 9, 8, changes=[
        c for c in (await plan_for(doc, nodes, None, {}, SiteManifest.from_dict(live))).changes
        if c.target == GATEWAY
    ])
    gateway = FakeGateway()
    results = await apply_plan(plan, nodes, gateway, MemoryBackupStore())
    assert [r.detail for r in results] == ["bridge set applied (2 bridges)", f"applied with {plan.changes[0].id}",
                                           f"applied with {plan.changes[0].id}"]
    assert gateway.calls == ["bridges"]
    assert [b["to"] for b in gateway.bridges] == ["hq/r204-ctl/analog-value:20", "hq/ahu1-ctl/analog-value:7"]


async def test_refusals(nodes: Nodes) -> None:
    plan = Plan("p1", 1, 0, changes=[Change("c1", GATEWAY, "tags", "tags", payload={})])
    with pytest.raises(InvalidRequest, match="no gateway applier"):
        await apply_plan(plan, nodes, None, MemoryBackupStore())
    assert await apply_plan(Plan("p0", 1, 0), nodes, None, MemoryBackupStore()) == []


async def test_bad_payload_fails_the_change(nodes: Nodes) -> None:
    change = Change("c1", "r204-ctl", "upload-file", "upload", payload={
        "path": "/lfs/apps/x.wasm", "sha256": "00" * 32, "data": "AGFzbQ==", "size": 4})
    results = await apply_plan(Plan("p1", 1, 0, changes=[change]), nodes, None, MemoryBackupStore())
    assert results[0].detail == "ValueError: the planned content of /lfs/apps/x.wasm does not match its sha256"
    odd = Change("c1", "r204-ctl", "device-config", "writes", payload={})
    results = await apply_plan(Plan("p2", 1, 0, changes=[odd]), nodes, None, MemoryBackupStore())
    assert results[0].detail == "Unsupported: device-config is not a node change"


async def test_async_progress_callback_errors_are_ignored(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                          app_files: dict[str, bytes]) -> None:
    plan = await plan_for(doc, nodes, uc_link, app_files)
    seen: list[ApplyProgress] = []

    async def progress(event: ApplyProgress) -> None:
        seen.append(event)
        raise RuntimeError("ui gone")

    results = await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore(), progress)
    assert all(r.ok for r in results) and len(seen) > len(plan.changes)


# -- rollback ---------------------------------------------------------------------------------------
async def test_rollback_restores_the_node(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                          app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    first = await plan_for(doc, nodes, uc_link, app_files)
    await apply_plan(first, nodes, FakeGateway(), MemoryBackupStore())
    r204 = nodes.nodes["r204-ctl"]
    before = dict(r204.files)
    doc["system"]["nodes"][0]["io"].pop()
    doc["system"]["nodes"][0]["device"]["name"] = "Renamed"
    doc["system"]["apps"][0]["params"]["setpoint"] = 19
    backups = MemoryBackupStore()
    second = await plan_for(doc, nodes, uc_link, {"apps/thermostat.wasm": wasm("v2")}, live=site)
    results = await apply_plan(second, nodes, FakeGateway(), backups)
    assert all(r.ok for r in results)
    assert r204.files != before
    backup = backups.get(second.id, "r204-ctl")
    assert backup is not None and backup.files[CFG_DEVICE] == before[CFG_DEVICE]
    restored = TargetBackup.from_json(json.loads(json.dumps(backup.to_json())))
    assert restored == backup

    r204.calls.clear()
    result = await rollback_target(restored, nodes)
    assert result.ok and result.change_id == "r204-ctl/rollback"
    assert result.detail == ("/lfs/apps/thermostat.wasm restored; /lfs/cfg/device.json restored; "
                             "/lfs/cfg/io.json restored; /lfs/cfg/apps.json restored")
    for path in (CFG_DEVICE, CFG_IO, CFG_APPS, "/lfs/apps/thermostat.wasm"):
        assert r204.files[path] == before[path]
    assert r204.calls[-1] == ("reload", ("all",))
    assert r204.apps["thermostat"]["params"] == {"setpoint": "21.5"}


async def test_rollback_of_a_new_node(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                      app_files: dict[str, bytes]) -> None:
    backups = MemoryBackupStore()
    plan = await plan_for(doc, nodes, uc_link, app_files)
    await apply_plan(plan, nodes, FakeGateway(), backups)
    r204 = nodes.nodes["r204-ctl"]
    r204.reboot_required = True
    result = await rollback_target(backups.get(plan.id, "r204-ctl"), nodes)  # type: ignore[arg-type]
    assert result.ok
    assert result.detail == ("/lfs/cfg/device.json did not exist before and was left in place; "
                             "/lfs/cfg/io.json restored; /lfs/cfg/apps.json restored; "
                             "the node reports that a reboot is required")
    assert r204.get_json(CFG_IO) == {"schema": 1, "points": []}
    assert r204.apps == {}


async def test_rollback_gateway_and_failures(doc: dict[str, Any], nodes: Nodes) -> None:
    backup = TargetBackup(GATEWAY, "p3", gateway={"tags": {"hq/a/b": ["X"]}, "safety": {}, "bridges": []})
    gateway = FakeGateway()
    result = await rollback_target(backup, nodes, gateway)
    assert (result.ok, result.detail) == (True, "tags restored; 0 bridges restored")
    assert gateway.tags == {"hq/a/b": ["X"]}
    result = await rollback_target(backup, nodes)
    assert (result.ok, result.detail) == (False, "InvalidRequest: a gateway applier is needed to roll back the gateway")
    nodes.nodes["r204-ctl"].unreachable = True
    result = await rollback_target(TargetBackup("r204-ctl", "p3", files={CFG_IO: b"{}"}), nodes)
    assert (result.ok, result.detail) == (False, "DeviceTimeout: r204-ctl: no answer")
    result = await rollback_target(TargetBackup("r999-ctl", "p3"), nodes)
    assert not result.ok and result.detail.startswith("KeyError")
