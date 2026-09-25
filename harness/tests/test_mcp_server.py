# SPDX-License-Identifier: Apache-2.0
"""MCP server: tool catalogue, resources, prompts and end-to-end calls against FakeNode."""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness.mcp_server import HarnessContext, check_io_points, create_server
from bacnet_uc_harness.planner import ArtifactBuilder
from bacnet_uc_harness.testing import FakeNode

REPO = Path(__file__).resolve().parents[2]
needs_clang = pytest.mark.skipif(shutil.which("clang") is None, reason="clang required")

EXPECTED_TOOLS = {
    "list_nodes", "add_node", "remove_node", "discover_devices", "node_info", "get_config",
    "set_config", "reload_config", "io_catalog", "configure_io", "io_read", "io_write",
    "io_force", "io_release", "bacnet_read", "bacnet_write", "list_objects", "sdk_info",
    "build_app", "deploy_app", "app_control", "list_apps", "app_status", "read_logs",
    "node_shell", "build_firmware", "flash_firmware", "update_firmware", "validate_system",
    "plan_system", "apply_system", "run_system_tests", "system_status", "sim_start",
    "sim_stop", "sim_status",
}


def _get(obj: Any, *names: str) -> Any:
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    raise AttributeError(names)


class ToolCallError(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        start = text.find("{")
        self.payload = json.loads(text[start:]) if start >= 0 else {"message": text}


class McpTestClient:
    """Uniform access to the SDK's in-memory client (mcp 2.x ``Client``, 1.x session)."""

    def __init__(self, session: Any) -> None:
        self.s = session

    async def tools(self) -> list[Any]:
        return list((await self.s.list_tools()).tools)

    async def call(self, tool: str, /, **args: Any) -> Any:
        res = await self.s.call_tool(tool, args)
        if _get(res, "is_error", "isError"):
            raise ToolCallError(res.content[0].text)
        data = _get(res, "structured_content", "structuredContent")
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data

    async def read(self, uri: str) -> str:
        res = await self.s.read_resource(uri)
        return res.contents[0].text


@asynccontextmanager
async def connect(server: Any) -> AsyncIterator[McpTestClient]:
    try:
        from mcp import Client
    except ImportError:  # mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server._mcp_server) as s:
            yield McpTestClient(s)
        return
    async with Client(server) as c:
        yield McpTestClient(c)


@pytest.fixture(scope="session")
def build_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("build-cache")


@pytest.fixture
def ctx(tmp_path: Path, build_cache: Path) -> HarnessContext:
    c = HarnessContext(tmp_path, smp_timeout=1.0)
    c._builder = ArtifactBuilder(build_cache)
    return c


@pytest.fixture
async def server(ctx: HarnessContext) -> AsyncIterator[Any]:
    """The server; tests connect inside their own task (the SDK's task groups must be
    entered and left in the same task, which async fixtures do not guarantee)."""
    yield create_server(ctx)
    await ctx.close()


async def add(client: McpTestClient, name: str, node: FakeNode) -> dict[str, Any]:
    return await client.call("add_node", name=name, transport="udp", host=node.host,
                             port=node.smp_port,
                             bacnet_address=f"{node.host}:{node.bacnet_port}")


# --- catalogue --------------------------------------------------------------------------------


async def test_tool_catalogue(server: Any) -> None:
    async with connect(server) as client:
        tools = {t.name: t for t in await client.tools()}
        assert set(tools) == EXPECTED_TOOLS
        for t in tools.values():
            assert t.description and len(t.description) > 30, t.name
            assert t.annotations is not None, t.name
        ro = _get(tools["list_nodes"].annotations, "read_only_hint", "readOnlyHint")
        assert ro is True
        destructive = _get(tools["apply_system"].annotations, "destructive_hint",
                           "destructiveHint")
        assert destructive is True
        schema = _get(tools["io_force"], "input_schema", "inputSchema")
        assert set(schema["required"]) == {"node", "channel", "value"}
        assert "description" in schema["properties"]["value"]
        apply_schema = _get(tools["apply_system"], "input_schema", "inputSchema")
        assert apply_schema["properties"]["dry_run"]["default"] is True


async def test_resources_and_prompts(ctx: HarnessContext) -> None:
    async with connect(create_server(ctx)) as c:
        text = await c.read("bacnet-uc://docs/management-protocol.md")
        assert "Group 64 `uc_app`" in text
        assert await c.read("bacnet-uc://docs/management-protocol") == text
        assert "uc-link" in await c.read("bacnet-uc://docs/uc-link")
        schema = json.loads(await c.read("bacnet-uc://schemas/system"))
        assert schema["title"].startswith("BACnet-uc distributed")
        example = json.loads(await c.read("bacnet-uc://schemas/example-io"))
        assert example["schema"] == 1
        assert "UC_IMPORT(uc_log)" in await c.read("bacnet-uc://sdk/bacnet_uc.h")
        with pytest.raises(Exception):  # noqa: B017 - SDK specific error type
            await c.read("bacnet-uc://docs/nonexistent")
        templates = await c.s.list_resource_templates()
        uris = {_get(t, "uri_template", "uriTemplate") for t in templates.resource_templates}
        assert {"bacnet-uc://docs/{name}", "bacnet-uc://schemas/{name}",
                "bacnet-uc://nodes/{name}/info"} <= uris
        prompts = {p.name for p in (await c.s.list_prompts()).prompts}
        assert prompts == {"design_distributed_app", "commission_node", "debug_app"}
        res = await c.s.get_prompt("debug_app", {"node": "n1", "app": "thermo"})
        assert "app_status" in res.messages[0].content.text
        assert "'thermo' on node 'n1'" in res.messages[0].content.text
        res = await c.s.get_prompt("design_distributed_app", {"goal": "heat the lab"})
        assert "{{ nodes.<name>.device.instance }}" in res.messages[0].content.text


async def test_sdk_info(server: Any) -> None:
    async with connect(server) as client:
        info = await client.call("sdk_info")
        assert info["api_version"] == "1.0"
        assert "thermostat" in info["examples"]
        assert any(f["name"] == "uc_remote_read" for f in info["host_functions"])


# --- inventory + node tools against FakeNode --------------------------------------------------


async def test_inventory_and_node_tools(server: Any, fake_node: FakeNode,
                                        ctx: HarnessContext) -> None:
    async with connect(server) as client:
        res = await add(client, "n1", fake_node)
        assert res["probe"]["ok"] and res["node"]["board"] == "native_sim/native/64"
        assert Path(res["saved"]).parent.parent == ctx.home
        listed = await client.call("list_nodes")
        assert [n["name"] for n in listed["nodes"]] == ["n1"]
        with pytest.raises(ToolCallError) as exc:
            await add(client, "n1", fake_node)
        assert "already in the inventory" in exc.value.payload["message"]
        info = await client.call("node_info", node="n1")
        assert info["info"]["device"]["instance"] == 1001
        cat = await client.call("io_catalog", node="n1")
        assert [c["name"] for c in cat["channels"]][:2] == ["di0", "di1"]
        with pytest.raises(ToolCallError) as exc:
            await client.call("node_info", node="ghost")
        assert exc.value.payload["error"] == "HarnessError"
        await client.call("remove_node", name="n1")
        assert (await client.call("list_nodes"))["nodes"] == []


async def test_config_and_io_tools(server: Any, fake_node: FakeNode) -> None:
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        got = await client.call("get_config", node="n1", doc="io")
        assert got["exists"] is False and got["content"] is None
        res = await client.call("configure_io", node="n1", points=[
            {"channel": "ai0", "type": "analog-input", "instance": 1, "scale": 0.01,
             "units": "degrees-celsius"},
            {"channel": "do0", "type": "binary-output", "instance": 1}])
        assert res["changes"]["added"] == ["ai0", "do0"] and res["result"]["uploaded"]
        res = await client.call("configure_io", node="n1", points=[
            {"channel": "di0", "type": "binary-input", "instance": 1}])
        assert res["changes"] == {"added": ["di0"], "removed": [], "changed": []}
        assert [p["channel"] for p in res["io"]["points"]] == ["ai0", "do0", "di0"]
        # invalid: channel not in the catalog, wrong type for the channel kind
        with pytest.raises(ToolCallError) as exc:
            await client.call("configure_io", node="n1", dry_run=True, points=[
                {"channel": "ai7", "type": "analog-input", "instance": 2},
                {"channel": "do1", "type": "analog-input", "instance": 3}])
        paths = {i["path"] for i in exc.value.payload["issues"]}
        assert "/points/3/channel" in paths and "/points/4/type" in paths
        # IO + BACnet
        await client.call("io_force", node="n1", channel="ai0", value=2350)
        val = await client.call("bacnet_read", node="n1", object="analog-input:1")
        assert val["value"] == pytest.approx(23.5) and val["via"] == "bacnet"
        val = await client.call("bacnet_read", node="n1", object="analog-input:1", via="smp")
        assert val["value"] == pytest.approx(23.5) and val["via"] == "smp"
        await client.call("io_release", node="n1", channel="ai0")
        w = await client.call("bacnet_write", node="n1", object="binary-output:1", value=1,
                              priority=8)
        assert w["readback"] == 1
        assert (await client.call("io_read", node="n1", channel="do0"))["values"] == {"do0": 1.0}
        await client.call("bacnet_write", node="n1", object="binary-output:1", value=None,
                          priority=8)
        assert (await client.call("io_read", node="n1", channel="do0"))["values"] == {"do0": 0.0}
        objs = await client.call("list_objects", node="n1")
        assert {o["owner"] for o in objs["objects"]} >= {"system", "io"}
        # set_config: schema validation error, then a valid device document
        with pytest.raises(ToolCallError) as exc:
            await client.call("set_config", node="n1", doc="device",
                              content={"schema": 1, "device": {"instance": -1, "name": "x"}})
        assert exc.value.payload["error"] == "ManifestError"
        assert exc.value.payload["issues"][0]["path"] == "/device/instance"
        res = await client.call("set_config", node="n1", doc="device",
                                content={"schema": 1,
                                         "device": {"instance": 1001, "name": "renamed"},
                                         "log": {"level": "dbg"}})
        assert res["uploaded"] and res["reboot_required"] is False
        reloaded = await client.call("reload_config", node="n1", doc="all")
        assert reloaded["reboot_required"] is False
        info = await client.call("node_info", node="n1")
        assert info["info"]["device"]["name"] == "renamed"


async def test_shell_disabled_and_enabled(server: Any, fake_node: FakeNode,
                                          ctx: HarnessContext) -> None:
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        with pytest.raises(ToolCallError) as exc:
            await client.call("node_shell", node="n1", command="uc info")
        assert "BACNET_UC_ALLOW_SHELL" in exc.value.payload["message"]
        ctx.allow_shell = True
        res = await client.call("node_shell", node="n1", command="uc info")
        assert res["rc"] == 0 and "device 1001" in res["output"]


async def test_logs(server: Any, fake_node: FakeNode) -> None:
    async with connect(server) as client:
        fake_node.files["/lfs/log/log.0003"] = b"old 1\nold 2\n"
        fake_node.files["/lfs/log/log.0004"] = b"<inf> app: hello\n<err> app: bad\n"
        await add(client, "n1", fake_node)
        res = await client.call("read_logs", node="n1", lines=3)
        assert res["files"] == ["/lfs/log/log.0003", "/lfs/log/log.0004"]
        assert res["lines"] == ["old 2", "<inf> app: hello", "<err> app: bad"]
        res = await client.call("read_logs", node="n1", grep="<err>")
        assert res["lines"] == ["<err> app: bad"]


@needs_clang
async def test_build_and_deploy_app(server: Any, fake_node: FakeNode,
                                    tmp_path: Path) -> None:
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        src = REPO / "wasm" / "examples" / "blinky" / "blinky.c"
        built = await client.call("build_app", name="blinky", source_path=str(src))
        wasm = built["wasm"]
        assert wasm["path"].endswith("blinky.wasm") and "uc_app_api_version" in wasm["exports"]
        res = await client.call("deploy_app", node="n1", name="blinky", module_path=wasm["path"],
                                params={"instance": 3, "period_ms": 500})
        assert res["uploaded"] and res["status"]["state"] == "running"
        assert res["perms_derived"] == ["bacnet.local", "io"]
        assert fake_node.apps["blinky"]["params"] == {"instance": "3", "period_ms": "500"}
        again = await client.call("deploy_app", node="n1", name="blinky", module_path=wasm["path"])
        assert again["uploaded"] is False
        apps = await client.call("list_apps", node="n1")
        assert [a["name"] for a in apps["apps"]] == ["blinky"]
        st = await client.call("app_control", node="n1", name="blinky", action="stop")
        assert st["state"] == "stopped"
        st = await client.call("app_control", node="n1", name="blinky", action="restart")
        assert st["state"] == "running"
        assert (await client.call("app_status", node="n1", name="blinky"))["name"] == "blinky"
        await client.call("app_control", node="n1", name="blinky", action="remove")
        assert "/lfs/apps/blinky.wasm" not in fake_node.files
        with pytest.raises(ToolCallError) as exc:
            await client.call("app_status", node="n1", name="blinky")
        assert "not installed" in exc.value.payload["message"]
        # compile errors come back structured
        with pytest.raises(ToolCallError) as exc:
            await client.call("build_app", name="bad", source="int x = ;")
        assert exc.value.payload["error"] == "WasmBuildError" and exc.value.payload["output"]


@needs_clang
async def test_deploy_aot_derives_perms_from_its_wasm(
        server: Any, make_fake_node: Callable[..., Awaitable[FakeNode]], tmp_path: Path) -> None:
    """An .aot file has no readable imports: the permissions come from the
    .wasm next to it, never a fixed guess (HAR-6)."""
    node = await make_fake_node(1005, wasm_aot=True)
    async with connect(server) as client:
        await add(client, "n1", node)
        src = REPO / "wasm" / "examples" / "blinky" / "blinky.c"
        built = await client.call("build_app", name="blinky", source_path=str(src),
                                  output_dir=str(tmp_path))
        aot = tmp_path / "blinky.aot"
        aot.write_bytes(b"\0aot" + b"\0" * 60)  # stands in for build_app(aot_board=...)
        res = await client.call("deploy_app", node="n1", name="blinky", module_path=str(aot))
        assert res["perms_derived"] == built["wasm"]["perms"] == ["bacnet.local", "io"]
        assert res["perms_derived_from"] == str(tmp_path / "blinky.wasm")
        assert node.apps["blinky"]["perms"] == ["bacnet.local", "io"]
        # without the .wasm the permissions must be given
        lone = tmp_path / "lone" / "x.aot"
        lone.parent.mkdir()
        lone.write_bytes(aot.read_bytes())
        with pytest.raises(ToolCallError) as exc:
            await client.call("deploy_app", node="n1", name="x", module_path=str(lone))
        assert "pass perms" in exc.value.payload["message"]
        res = await client.call("deploy_app", node="n1", name="x", module_path=str(lone),
                                perms=["bacnet.local"])
        assert node.apps["x"]["perms"] == ["bacnet.local"] and "perms_derived" not in res


async def test_deploy_rejects_bad_module(server: Any, fake_node: FakeNode,
                                         tmp_path: Path) -> None:
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        mod = tmp_path / "empty.wasm"
        mod.write_bytes(b"\0asm\1\0\0\0")
        with pytest.raises(ToolCallError) as exc:
            await client.call("deploy_app", node="n1", name="empty", module_path=str(mod))
        assert "uc_app_api_version" in json.dumps(exc.value.payload)
        res = await client.call("deploy_app", node="n1", name="empty", module_path=str(mod),
                                perms=["kv"], autostart=False)
        assert res["status"]["state"] == "stopped"


async def test_firmware_tools_need_confirm(server: Any, tmp_path: Path) -> None:
    async with connect(server) as client:
        bdir = tmp_path / "build"
        (bdir / "zephyr").mkdir(parents=True)
        (bdir / "zephyr" / "zephyr.elf").write_bytes(b"\x7fELF" + b"\0" * 60)
        res = await client.call("flash_firmware", build_dir=str(bdir))
        assert res["confirm_required"] is True and res["would_run"][:2] == ["west", "flash"]
        with pytest.raises(ToolCallError) as exc:
            await client.call("update_firmware", node="x", build_dir=str(bdir))
        assert "zephyr.signed.bin" in exc.value.payload["message"]


# --- systems ----------------------------------------------------------------------------------


def fake_manifest(path: Path, a: FakeNode, b: FakeNode) -> Path:
    thermostat = REPO / "wasm" / "examples" / "thermostat" / "thermostat.c"
    text = f"""
apiVersion: bacnet-uc/v1
kind: System
metadata: {{name: fk}}
nodes:
  - name: a
    board: native_sim/native/64
    transport: {{kind: udp, host: 127.0.0.1, port: {a.smp_port}}}
    bacnet_address: 127.0.0.1:{a.bacnet_port}
    bacnet: {{udp_port: {a.bacnet_port}}}
    device: {{instance: {a.device_instance}, name: fa}}
    io:
      - {{channel: ai0, type: analog-input, instance: 1, scale: 0.01}}
      - {{channel: di0, type: binary-input, instance: 1}}
  - name: b
    board: native_sim/native/64
    transport: {{kind: udp, host: 127.0.0.1, port: {b.smp_port}}}
    bacnet_address: 127.0.0.1:{b.bacnet_port}
    bacnet: {{udp_port: {b.bacnet_port}}}
    device: {{instance: {b.device_instance}, name: fb}}
    io:
      - {{channel: do0, type: binary-output, instance: 1}}
apps:
  - name: thermostat
    node: b
    source: {thermostat}
    params: {{sensor_device: "{{{{ nodes.a.device.instance }}}}"}}
links:
  - {{from: a/binary-input:1, to: b/binary-output:1}}
tests:
  - name: sensor
    steps:
      - force: {{node: a, channel: ai0, value: 2500}}
      - expect: {{point: a/analog-input:1, op: approx, value: 25, within_ms: 1000}}
      - write: {{point: b/binary-output:1, value: 1, priority: 8}}
      - expect: {{point: b/binary-output:1, op: eq, value: 1}}
      - write: {{point: b/binary-output:1, value: null, priority: 8}}
  - name: failing
    steps:
      - expect: {{point: a/analog-input:1, op: gt, value: 1000, within_ms: 300}}
"""
    path.write_text(text)
    return path


@needs_clang
async def test_system_workflow(server: Any,
                               make_fake_node: Callable[..., Awaitable[FakeNode]],
                               tmp_path: Path) -> None:
    async with connect(server) as client:
        fa = await make_fake_node(4001)
        fb = await make_fake_node(4002)
        mf = str(fake_manifest(tmp_path / "fk.yaml", fa, fb))
        val = await client.call("validate_system", system=mf)
        assert val["ok"] and val["documents"]["b"]["apps"] == ["thermostat", "link"]
        pool = val["wamr_pool"]["b"]
        assert pool["status"] == "ok" and pool["pool"] == 262144
        assert [a["app"] for a in pool["apps"]] == ["thermostat", "link"]
        assert pool["apps"][1]["linear_memory"] == 8192  # uc-link: heap_kb 0
        assert "a" not in val["wamr_pool"]  # no apps there
        val = await client.call("validate_system", system=mf, live_catalogs=True)
        assert val["ok"] and val["catalogs_from"] == ["a", "b"]
        plan = await client.call("plan_system", system=mf)
        assert len(plan["actions"]) == 6 and plan["nodes"]["a"]["reachable"]
        dry = await client.call("apply_system", system=mf)
        assert dry["dry_run"] and {r["status"] for r in dry["results"]} == {"dry-run"}
        assert not fb.apps
        res = await client.call("apply_system", system=mf, dry_run=False)
        assert res["ok"], res
        assert fb.apps["thermostat"]["state"] == "running"
        status = await client.call("system_status", system=mf)
        assert status["in_sync"] and status["nodes"]["b"]["apps"] == {"thermostat": "running",
                                                                     "link": "running"}
        # node tools work by name for nodes of a loaded system (not in the inventory)
        assert (await client.call("node_info", node="a"))["info"]["device"]["instance"] == 4001
        tests = await client.call("run_system_tests", system=mf)
        assert tests["passed"] == 1 and tests["failed"] == 1 and not tests["ok"]
        failing = tests["tests"][1]
        assert failing["steps"][0]["attempts"] > 1 and "expected gt 1000" in \
            failing["steps"][0]["message"]
        assert not fa.io_channels["ai0"].forced  # released after the test
        only = await client.call("run_system_tests", system=mf, tests=["sensor"])
        assert only["ok"] and only["passed"] == 1
        # an io.json change on b restarts the link that drives b's binary-output:1
        started = fb.apps["link"]["started_at"]
        res = await client.call("configure_io", node="b", points=[
            {"channel": "do0", "type": "binary-output", "instance": 1, "name": "Lamp"}])
        assert res["restarted_apps"] == ["link"] and fb.apps["link"]["started_at"] != started
        plan = await client.call("plan_system", system=mf)
        assert [(a["node"], a["kind"], a["target"]) for a in plan["actions"]] == [
            ("b", "push_config", "io"), ("b", "restart_app", "link")]
        res = await client.call("reload_config", node="b", doc="io")
        assert res["restarted_apps"] == ["link"]


async def test_validate_system_errors(server: Any, tmp_path: Path) -> None:
    async with connect(server) as client:
        bad = tmp_path / "bad.yaml"
        bad.write_text("apiVersion: bacnet-uc/v1\nkind: System\nmetadata: {name: x}\nnodes: []\n")
        rep = await client.call("validate_system", system=str(bad))
        assert not rep["ok"] and rep["errors"][0]["path"] == "/nodes"
        with pytest.raises(ToolCallError):
            await client.call("plan_system", system=str(bad))


async def test_sim_tools_without_simulation(server: Any) -> None:
    async with connect(server) as client:
        assert (await client.call("sim_status"))["simulations"] == {}
        with pytest.raises(ToolCallError) as exc:
            await client.call("sim_stop", system="nothing-here")
        assert "no simulation" in exc.value.payload["message"]
        with pytest.raises(ToolCallError) as exc:
            await client.call("sim_start", system=str(
                REPO / "harness" / "examples" / "systems" / "sim-demo.yaml"))
        assert "native_sim firmware" in exc.value.payload["message"]


def test_check_io_points() -> None:
    cat = {"channels": [{"name": "ai0", "kind": "ai"}, {"name": "do0", "kind": "do"}]}
    ok = [{"channel": "ai0", "type": "analog-input", "instance": 1}]
    assert not check_io_points(ok, cat)
    issues = check_io_points(ok + [{"channel": "ai0", "type": "analog-input", "instance": 1}],
                             cat, {("analog-input", 1)})
    msgs = " ".join(i.message for i in issues)
    assert "bound twice" in msgs and "also bound" in msgs and "owned by an app" in msgs
    assert check_io_points([{"channel": "x"}], cat)[0].path == "/points/0"


async def test_discover_devices_unicast(server: Any, fake_node: FakeNode) -> None:
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        res = await client.call("discover_devices", timeout_s=1.0,
                                target=f"127.0.0.1:{fake_node.bacnet_port}")
        assert res["devices"] == [{
            "device": 1001, "address": f"127.0.0.1:{fake_node.bacnet_port}",
            "max_apdu": 1476, "segmentation": 3, "vendor_id": fake_node.vendor_id,
            "inventory_node": "n1"}]


async def test_update_firmware_flow(server: Any, fake_node: FakeNode, tmp_path: Path) -> None:
    import struct
    import sys

    body = b"\xaa" * 3000
    tlvs = struct.pack("<HH", 0x10, 32) + bytes(32)
    image = (struct.pack("<IIHHI", 0x96F3B83D, 0, 32, 0, len(body)).ljust(32, b"\0") + body +
             struct.pack("<HH", 0x6907, 4 + len(tlvs)) + tlvs)
    bdir = tmp_path / "fw" / "zephyr"
    bdir.mkdir(parents=True)
    shutil.copy(Path(sys.executable).resolve(), bdir / "zephyr.elf")
    (bdir / "zephyr.signed.bin").write_bytes(image)
    async with connect(server) as client:
        await add(client, "n1", fake_node)
        preview = await client.call("update_firmware", node="n1", build_dir=str(bdir.parent))
        assert preview["confirm_required"] and preview["size"] == len(image)
        res = await client.call("update_firmware", node="n1", build_dir=str(bdir.parent),
                                confirm=True, timeout_s=5)
    assert fake_node.resets == 1
    slot1 = next(i for i in res["images"] if i["slot"] == 1)
    assert slot1["pending"] is True and slot1["hash"] == res["slot_hash"]
    # the fake node does not swap images on reset
    assert res["running_new_image"] is False and res["confirmed"] is False
