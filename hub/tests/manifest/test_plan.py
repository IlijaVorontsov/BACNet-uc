"""Planning against in-memory nodes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from uc_hub.core.errors import DeviceError, InvalidRequest, NotFound
from uc_hub.manifest import GATEWAY, MemoryBackupStore, SiteManifest, SitePlan, apply_plan, compute_plan
from uc_hub.manifest.nodedocs import CFG_APPS, CFG_DEVICE, CFG_IO, UC_LINK_FILE
from uc_hub.manifest.plan import directory_resolver

from .conftest import THERMOSTAT, UC_LINK, resolver
from .fakes import FakeGateway, FakeNode, Nodes, wasm


@pytest.fixture
def uc_link(tmp_path: Path) -> Path:
    path = tmp_path / "uc-link.wasm"
    path.write_bytes(UC_LINK)
    return path


@pytest.fixture
def nodes() -> Nodes:
    return Nodes(FakeNode("r204-ctl", 2041), FakeNode("r205-ctl", 2051))


async def make_plan(site: SiteManifest, nodes: Nodes, uc_link: Path | None, app_files: dict[str, bytes],
                    live: SiteManifest | None = None) -> SitePlan:
    return await compute_plan(site, live, nodes, resolver(app_files), uc_link, revision=2, base_revision=1)


def kinds(plan: SitePlan, target: str | None = None) -> list[str]:
    return [c.kind if c.kind != "upload-doc" else f"upload-doc {c.payload['doc']}"
            for c in plan.changes if target is None or c.target == target]


async def converge(site: SiteManifest, nodes: Nodes, uc_link: Path | None, app_files: dict[str, bytes]) -> None:
    plan = await make_plan(site, nodes, uc_link, app_files)
    results = await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore())
    assert all(r.ok for r in results), results


# -- new nodes -----------------------------------------------------------------------
async def test_new_nodes(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    plan = await make_plan(site, nodes, uc_link, app_files)
    assert plan.id == "p2" and (plan.revision, plan.base_revision) == (2, 1)
    assert plan.targets == ["r204-ctl", "r205-ctl", GATEWAY]
    assert kinds(plan, "r204-ctl") == [
        "upload-file", "upload-file", "upload-doc device", "upload-doc io", "reload", "reload",
        "install-app", "install-app",
    ]
    assert kinds(plan, "r205-ctl") == ["upload-doc device", "upload-doc io", "reload", "reload"]
    assert kinds(plan, GATEWAY) == ["tags", "bridge-add"]
    assert [c.id for c in plan.changes] == [f"c{i}" for i in range(1, len(plan.changes) + 1)]
    assert plan.warnings == ["gateway: life-safety mark added for hq/ahu1-ctl/binary-output:9; "
                             "only a site admin may approve this"]
    assert plan.blocked == {}
    assert all(c.tier == "C" for c in plan.changes)

    upload = plan.changes[0]
    assert upload.payload["path"] == "/lfs/apps/thermostat.wasm"
    assert base64.b64decode(upload.payload["data"]) == THERMOSTAT
    assert upload.payload["sha256"] == hashlib.sha256(THERMOSTAT).hexdigest()
    assert plan.changes[1].payload["path"] == UC_LINK_FILE
    device = plan.changes[2]
    assert device.summary == "device.json: new (instance 2041, 'R204 Controller')"
    assert device.payload["content"] == site.device_json("r204-ctl")
    assert device.diff.startswith("--- /dev/null\n+++ b/device.json\n")
    assert plan.changes[3].summary == "io.json: new, 2 points"
    assert [c.payload["doc"] for c in plan.changes[4:6]] == ["device", "io"]
    assert plan.changes[4].summary == "reload device"
    install = plan.changes[6].payload["manifest"]
    assert install["name"] == "thermostat" and install["params"] == {"setpoint": "21.5"}
    assert install["sha256"] == hashlib.sha256(THERMOSTAT).hexdigest() and "restart" not in install
    assert plan.changes[7].payload["manifest"]["params"]["l0"] == "100 2 3 2 10 cov 1000 0 1 0"
    tags = plan.changes[-2]
    assert tags.payload["tags"] == {"hq/r204-ctl/analog-input:1": ["Zone_Air_Temperature_Sensor"]}
    assert tags.payload["safety"] == {"hq/ahu1-ctl/binary-output:9": "life-safety"}
    assert tags.summary == "tags: 1 point, safety: 1 point"
    bridge = plan.changes[-1]
    assert bridge.summary == "add bridge hq/r204-co2/co2 -> hq/r204-ctl/analog-value:20"
    assert bridge.payload["bridges"] == [b.to_dict() for b in site.bridges]
    assert json.loads(json.dumps(plan.to_json()))["targets"] == ["r204-ctl", "r205-ctl", GATEWAY]


async def test_plan_is_deterministic(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                     app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    first = (await make_plan(site, nodes, uc_link, app_files)).to_json()
    second = (await make_plan(site, nodes, uc_link, app_files)).to_json()
    first.pop("created_at")
    second.pop("created_at")
    assert first == second


async def test_reboot_is_flagged(doc: dict[str, Any], uc_link: Path, app_files: dict[str, bytes]) -> None:
    nodes = Nodes(FakeNode("r204-ctl", instance=4194303), FakeNode("r205-ctl", 2051, bacnet_port=47809))
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files)
    reloads = [c for c in plan.changes if c.kind == "reload" and c.payload["doc"] == "device"]
    assert [c.summary for c in reloads] == ["reload device (reboot required: device.instance)",
                                            "reload device (reboot required: bacnet.udp_port)"]
    assert reloads[0].payload["reboot_expected"] is True
    assert any("r204-ctl: device.json changes device.instance" in w for w in plan.warnings)


# -- converged nodes ----------------------------------------------------------------------------
async def test_unchanged_site_has_no_changes(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                             app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    calls_before = {name: len(n.calls) for name, n in nodes.nodes.items()}
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.changes == [] and plan.warnings == []
    for name, node in nodes.nodes.items():
        assert "file_upload" not in node.methods()[calls_before[name]:]


async def test_semantic_comparison_ignores_defaults_and_order(doc: dict[str, Any], nodes: Nodes,
                                                              uc_link: Path, app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    node = nodes.nodes["r204-ctl"]
    device = node.get_json(CFG_DEVICE)
    device.update(log={"level": "inf"}, bacnet={"udp_port": 47808, "apdu_retries": 3})
    node.put_json(CFG_DEVICE, dict(reversed(list(device.items()))))
    io = node.get_json(CFG_IO)
    io["points"] = [dict(p, cov_increment=0.1, sample_ms=100, invert=False) for p in reversed(io["points"])]
    node.put_json(CFG_IO, io)
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.changes == []


async def test_unmanaged_device_sections_are_kept(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                  app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    node = nodes.nodes["r204-ctl"]
    network = {"dhcp": False, "ipv4": "10.0.2.51", "netmask": "255.255.255.0"}
    node.put_json(CFG_DEVICE, {**node.get_json(CFG_DEVICE), "network": network, "log": {"level": "dbg"}})
    doc["system"]["nodes"][0]["device"]["location"] = "Room 204 (north)"
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=site)
    assert kinds(plan) == ["upload-doc device", "reload"]
    change = plan.changes[0]
    assert change.summary == "device.json: device.location changed"
    assert change.payload["content"]["network"] == network
    assert change.payload["content"]["log"] == {"level": "dbg"}
    assert '-    "location": "Room 204",\n+    "location": "Room 204 (north)",\n' in change.diff
    assert plan.changes[1].summary == "reload device"


async def test_network_change_needs_reboot(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                           app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    doc["system"]["nodes"][1]["network"] = {"dhcp": False, "ipv4": "10.0.2.52"}
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=site)
    assert [c.summary for c in plan.changes] == [
        "device.json: network.dhcp, network.ipv4 changed (reboot required: network)",
        "reload device (reboot required: network)",
    ]


async def test_pending_reboot_is_reported(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                          app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    nodes.nodes["r205-ctl"].instance = 9
    nodes.nodes["r205-ctl"].bacnet_port = 47900
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.changes == []
    assert plan.warnings == ["r205-ctl: runs device.instance 9, bacnet.udp_port 47900, but its device.json has "
                             "2051, 47808; reboot the node to apply it"]


# -- io -------------------------------------------------------------------------------------------
async def test_io_change(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    io = doc["system"]["nodes"][0]["io"]
    io[0]["scale"] = 0.2
    io.append({"channel": "do0", "type": "binary-output", "instance": 1, "name": "R204 Heater"})
    doc["system"]["nodes"][1]["io"] = []
    changed = SiteManifest.from_dict(doc)
    plan = await make_plan(changed, nodes, uc_link, app_files, live=site)
    assert [(c.target, c.kind) for c in plan.changes] == [
        ("r204-ctl", "upload-doc"), ("r204-ctl", "reload"), ("r205-ctl", "upload-doc"), ("r205-ctl", "reload"),
    ]
    assert plan.changes[0].summary == "io.json: +1 (do0) ~1 (ai0)"
    assert plan.changes[0].payload == {"doc": "io", "path": CFG_IO, "content": changed.io_json("r204-ctl")}
    assert '-      "scale": 0.1,\n+      "scale": 0.2,\n' in plan.changes[0].diff
    assert plan.changes[1].payload == {"doc": "io", "reboot_expected": False}
    assert plan.changes[2].summary == "io.json: -1 (di0)"


async def test_invalid_document_on_node_is_replaced(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                    app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    nodes.nodes["r205-ctl"].files[CFG_IO] = b"{not json"
    nodes.nodes["r205-ctl"].files[CFG_DEVICE] = b"[1, 2]"
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert [c.summary for c in plan.changes] == [
        "device.json: replaced (the node's copy is not valid JSON)", "io.json: new, 1 point",
        "reload device", "reload io",
    ]
    assert "-{not json\n\\ No newline at end of file\n" in plan.changes[1].diff
    assert "r205-ctl: /lfs/cfg/io.json on the node is not a JSON object; it will be replaced" in plan.warnings


async def test_malformed_node_documents_do_not_break_the_plan(
        doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    r204 = nodes.nodes["r204-ctl"]
    apps = r204.get_json(CFG_APPS)
    apps["apps"][0].update(params=5, perms=7)
    r204.put_json(CFG_APPS, apps)
    io = r204.get_json(CFG_IO)
    io["points"] += [dict(io["points"][0], instance="1"), dict(io["points"][0])]
    r204.put_json(CFG_IO, io)
    r204.put_json(CFG_DEVICE, {**r204.get_json(CFG_DEVICE), "junk": True, "log": "dbg"})
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.blocked == {}
    assert [(c.kind, c.summary) for c in plan.changes] == [
        ("upload-doc", "device.json: junk, log changed"),
        ("upload-doc", "io.json: duplicate channels removed"),
        ("reload", "reload device"),
        ("reload", "reload io"),
        ("install-app", "update app thermostat: params.setpoint, perms changed (restart)"),
    ]
    assert plan.changes[0].payload["content"] == site.device_json("r204-ctl")
    assert plan.changes[1].payload["content"] == site.io_json("r204-ctl")


async def test_unexpected_node_errors_block_only_that_node(
        doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    nodes.nodes["r204-ctl"].fail["app_list"] = RuntimeError("client bug")
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files)
    assert plan.blocked == {"r204-ctl": "unexpected RuntimeError: client bug"}
    assert plan.targets == ["r205-ctl", GATEWAY]


# -- apps ------------------------------------------------------------------------------------------
async def test_app_param_change_restarts(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                         app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    doc["system"]["apps"][0]["params"]["setpoint"] = 22
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=site)
    (change,) = plan.changes
    assert change.kind == "install-app"
    assert change.summary == "update app thermostat: params.setpoint changed (restart)"
    assert change.payload["manifest"]["restart"] is True
    assert change.payload["manifest"]["params"] == {"setpoint": "22"}
    assert '-    "setpoint": "21.5"\n+    "setpoint": "22"\n' in change.diff


async def test_new_module_is_uploaded_and_installed(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                    app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    nodes.nodes["r204-ctl"].apps["thermostat"]["state"] = "stopped"
    new = {"apps/thermostat.wasm": wasm("thermostat v2")}
    plan = await make_plan(site, nodes, uc_link, new, live=site)
    assert kinds(plan) == ["upload-file", "install-app"]
    assert plan.changes[0].summary == f"upload /lfs/apps/thermostat.wasm ({len(new['apps/thermostat.wasm'])} bytes) " \
                                      "for app thermostat"
    old, sha = hashlib.sha256(THERMOSTAT).hexdigest(), hashlib.sha256(new["apps/thermostat.wasm"]).hexdigest()
    assert plan.changes[0].diff == f"/lfs/apps/thermostat.wasm: sha256 {old} -> {sha}\n"
    assert plan.changes[1].summary == "update app thermostat: new module"
    assert "restart" not in plan.changes[1].payload["manifest"]


async def test_app_add_and_remove(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                  app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    nodes.nodes["r205-ctl"].install_direct({"name": "legacy", "file": "/lfs/apps/legacy.wasm"}, wasm("legacy"))
    doc["system"]["apps"] = [{"name": "logger", "node": "r205-ctl", "wasm": "apps/logger.wasm",
                              "perms": ["io", "kv", "bacnet.remote"], "autostart": False}]
    files = {**app_files, "apps/logger.wasm": wasm("logger")}
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, files, live=site)
    assert [(c.target, c.kind, c.summary) for c in plan.changes] == [
        ("r204-ctl", "remove-app", "remove app thermostat"),
        ("r205-ctl", "upload-file", f"upload /lfs/apps/logger.wasm ({len(files['apps/logger.wasm'])} bytes) "
                                    "for app logger"),
        ("r205-ctl", "remove-app", "remove app legacy"),
        ("r205-ctl", "install-app", "install app logger"),
    ]
    assert plan.changes[0].payload == {"name": "thermostat"}
    assert plan.warnings == ["r205-ctl: app logger requests permission io",
                             "r205-ctl: app logger requests permission bacnet.remote"]


async def test_failed_app_is_reported_not_reinstalled(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                      app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    app = nodes.nodes["r204-ctl"].apps["thermostat"]
    app.update(state="failed", last_error="trap: out of bounds")
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.changes == []
    assert plan.warnings == ["r204-ctl: app thermostat is failed (trap: out of bounds); the plan does not change it"]


async def test_sha_fallback_without_fs_hash(doc: dict[str, Any], uc_link: Path, app_files: dict[str, bytes]) -> None:
    nodes = Nodes(FakeNode("r204-ctl", 2041, sha_supported=False), FakeNode("r205-ctl", 2051))
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    plan = await make_plan(site, nodes, uc_link, app_files, live=site)
    assert plan.changes == []
    plan = await make_plan(site, nodes, uc_link, {"apps/thermostat.wasm": wasm("v2")}, live=site)
    assert kinds(plan) == ["upload-file", "install-app"]


async def test_source_apps_and_links_without_module_are_skipped(
        doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    site = SiteManifest.from_dict(doc)
    await converge(site, nodes, uc_link, app_files)
    app = doc["system"]["apps"][0]
    app.pop("wasm")
    app["source"] = "logic/thermostat.c"
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, None, app_files, live=site)
    assert plan.changes == []
    assert plan.warnings == [
        "r204-ctl: app thermostat: building from C source (logic/thermostat.c) is not available yet; skipped",
        "r204-ctl: 1 link skipped: uc_link_wasm is not configured (drivers.bacnet_uc.uc_link_wasm)",
    ]


async def test_unreadable_modules_are_warnings(doc: dict[str, Any], nodes: Nodes, tmp_path: Path) -> None:
    bad_link = tmp_path / "uc-link.wasm"
    bad_link.write_bytes(b"not wasm")
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, bad_link, {"apps/thermostat.wasm": b"ELF"})
    assert "install-app" not in kinds(plan)
    assert plan.warnings[0] == f"uc_link_wasm {bad_link} is not a WebAssembly module; links are skipped"
    assert "r204-ctl: app thermostat: apps/thermostat.wasm is not a WebAssembly module; skipped" in plan.warnings
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, tmp_path / "missing.wasm", {})
    assert plan.warnings[0].startswith(f"uc_link_wasm {tmp_path / 'missing.wasm'}: cannot be read")
    assert any("cannot read apps/thermostat.wasm (apps/thermostat.wasm does not exist)" in w for w in plan.warnings)


# -- unreachable nodes ------------------------------------------------------------------------------
async def test_unreachable_node_blocks_the_plan(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                                app_files: dict[str, bytes]) -> None:
    nodes.nodes["r204-ctl"].unreachable = True
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files)
    assert "r204-ctl" not in plan.targets and "r205-ctl" in plan.targets
    assert plan.blocked == {"r204-ctl": "DeviceTimeout: r204-ctl: no answer"}
    assert plan.warnings[0] == ("r204-ctl: unreachable: DeviceTimeout: r204-ctl: no answer; no changes planned "
                                "for it, and the plan cannot be applied until it answers")
    assert plan.to_json()["blocked"] == plan.blocked
    with pytest.raises(InvalidRequest, match="cannot be applied; unreachable while planning: r204-ctl"):
        await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore())
    assert "file_upload" not in nodes.nodes["r205-ctl"].methods()


async def test_node_errors_and_missing_connections_block(doc: dict[str, Any], uc_link: Path,
                                                         app_files: dict[str, bytes]) -> None:
    node = FakeNode("r204-ctl", 2041)
    node.fail["app_list"] = DeviceError("busy", rc=5, group=64)
    plan = await make_plan(SiteManifest.from_dict(doc), Nodes(node), uc_link, app_files)
    assert plan.blocked == {"r204-ctl": "DeviceError: busy", "r205-ctl": "no management connection ('r205-ctl')"}
    assert plan.targets == [GATEWAY]


async def test_slow_node_times_out(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                   app_files: dict[str, bytes]) -> None:
    async def hang(path: str) -> bytes:
        await asyncio.sleep(10)
        raise NotFound(path)

    nodes.nodes["r205-ctl"].file_download = hang  # type: ignore[method-assign]
    plan = await compute_plan(SiteManifest.from_dict(doc), None, nodes, resolver(app_files), uc_link,
                              revision=3, base_revision=2, node_timeout_s=0.05)
    assert plan.blocked == {"r205-ctl": "planning did not finish within 0.05 s"}
    assert "r204-ctl" in plan.targets


# -- gateway ---------------------------------------------------------------------------------------
async def test_bridge_changes(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    live = SiteManifest.from_dict(doc)
    await converge(live, nodes, uc_link, app_files)
    doc["bridges"] = [
        {"from": "r204-co2/co2", "to": "r204-ctl/analog-value:20", "max_age_s": 60, "scale": 2},
        {"from": "r204-co2/co2", "to": "ahu1-ctl/analog-value:7"},
    ]
    live_doc = live.to_dict()
    live_doc["bridges"].append({"from": "r204-co2/co2", "to": "r205-ctl/analog-value:1"})
    live = SiteManifest.from_dict(live_doc)
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    assert [(c.kind, c.summary) for c in plan.changes] == [
        ("bridge-remove", "remove bridge hq/r204-co2/co2 -> hq/r205-ctl/analog-value:1"),
        ("bridge-update", "update bridge hq/r204-co2/co2 -> hq/r204-ctl/analog-value:20: max_age_s, scale changed"),
        ("bridge-add", "add bridge hq/r204-co2/co2 -> hq/ahu1-ctl/analog-value:7"),
    ]
    update = plan.changes[1].payload
    assert update["before"]["max_age_s"] == 120 and update["bridge"]["max_age_s"] == 60
    assert [b["to"] for b in update["bridges"]] == ["hq/r204-ctl/analog-value:20", "hq/ahu1-ctl/analog-value:7"]
    assert len(update["before_bridges"]) == 2
    assert all(c.target == GATEWAY for c in plan.changes)


async def test_tag_changes(doc: dict[str, Any], nodes: Nodes, uc_link: Path, app_files: dict[str, bytes]) -> None:
    doc["tags"]["r204-ctl/analog-output:1"] = ["Valve_Command", "space:r204"]
    live = SiteManifest.from_dict(doc)
    await converge(live, nodes, uc_link, app_files)
    doc["tags"]["r204-ctl/analog-output:1"] = ["space:r204", "Valve_Command"]
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    assert plan.changes == []
    doc["tags"]["hq/r204-ctl/analog-output:1"] = [*doc["tags"].pop("r204-ctl/analog-output:1"), "Extra"]
    doc["safety"]["r204-ctl/analog-output:1"] = "critical"
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    (change,) = plan.changes
    assert change.kind == "tags" and change.summary == "tags: 1 point, safety: 1 point"
    assert change.payload["safety"]["hq/r204-ctl/analog-output:1"] == "critical"
    assert change.payload["before"]["safety"] == {"hq/ahu1-ctl/binary-output:9": "life-safety"}
    assert '+    "hq/r204-ctl/analog-output:1": "critical"' in change.diff


async def test_life_safety_marks_need_an_admin(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                               app_files: dict[str, bytes]) -> None:
    live = SiteManifest.from_dict(doc)
    await converge(live, nodes, uc_link, app_files)
    doc["safety"] = {"ahu1-ctl/binary-output:9": "critical", "ahu1-ctl/binary-output:1": "life-safety"}
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    (change,) = plan.changes
    assert change.payload["life_safety"] == {"added": ["hq/ahu1-ctl/binary-output:1"],
                                             "removed": ["hq/ahu1-ctl/binary-output:9"]}
    assert plan.warnings == [
        "gateway: life-safety mark added for hq/ahu1-ctl/binary-output:1; only a site admin may approve this",
        "gateway: life-safety mark removed for hq/ahu1-ctl/binary-output:9; only a site admin may approve this",
    ]
    doc["tags"]["r204-ctl/analog-output:1"] = ["Valve_Command"]
    doc["safety"] = {"ahu1-ctl/binary-output:9": "life-safety", "r204-ctl/analog-output:1": "critical"}
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    assert plan.changes[0].payload["life_safety"] == {"added": [], "removed": []}
    assert plan.warnings == []


async def test_removed_node_is_left_alone(doc: dict[str, Any], nodes: Nodes, uc_link: Path,
                                          app_files: dict[str, bytes]) -> None:
    live = SiteManifest.from_dict(doc)
    await converge(live, nodes, uc_link, app_files)
    doc["system"]["nodes"].pop(1)
    del doc["placement"]["r205-ctl"]
    plan = await make_plan(SiteManifest.from_dict(doc), nodes, uc_link, app_files, live=live)
    assert plan.changes == []
    assert plan.warnings == ["r205-ctl: no longer in the manifest; its configuration is left unchanged"]


# -- module files ----------------------------------------------------------------------------------
def test_directory_resolver(tmp_path: Path) -> None:
    base = tmp_path / "site"
    (base / "apps").mkdir(parents=True)
    (base / "apps" / "t.wasm").write_bytes(b"\0asm")
    (tmp_path / "secret.wasm").write_bytes(b"secret")
    os.symlink(tmp_path / "secret.wasm", base / "apps" / "link.wasm")
    (base / "big.wasm").write_bytes(b"\0" * 20)
    read = directory_resolver(base, max_bytes=10)
    assert read("apps/t.wasm") == b"\0asm"
    assert read("apps/../apps/t.wasm") == b"\0asm"
    with pytest.raises(NotFound):
        read("apps/missing.wasm")
    for bad in ("../secret.wasm", "/etc/passwd", "apps/link.wasm", "big.wasm"):
        with pytest.raises(InvalidRequest):
            read(bad)
    os.mkfifo(base / "apps" / "pipe.wasm")
    with pytest.raises(InvalidRequest, match="not a regular file"):
        read("apps/pipe.wasm")
    with pytest.raises(InvalidRequest, match="not a regular file"):
        read("apps")


async def test_plan_with_demo_site(uc_link: Path) -> None:
    from .test_load import EXAMPLES

    site = SiteManifest.load(EXAMPLES / "site.yaml")
    nodes = Nodes(*(FakeNode(name, int(n["device"]["instance"]), bacnet_port=n["bacnet"]["udp_port"])
                    for name, n in site.nodes.items()))
    plan = await compute_plan(site, None, nodes, directory_resolver(EXAMPLES), uc_link, 1, 0)
    assert plan.blocked == {}
    assert plan.warnings == ["gateway: life-safety mark added for hq/ahu1-ctl/binary-output:9; "
                             "only a site admin may approve this"]
    assert plan.targets == [*site.nodes, GATEWAY]
    installs = [c.payload["manifest"]["name"] for c in plan.changes if c.kind == "install-app"]
    assert installs == ["thermostat", "link"]
    results = await apply_plan(plan, nodes, FakeGateway(), MemoryBackupStore())
    assert all(r.ok for r in results)
    assert json.loads(nodes.nodes["r204-ctl"].files[CFG_APPS])["apps"][0]["name"] == "thermostat"
