# SPDX-License-Identifier: Apache-2.0
"""Node: staged configuration documents, property reads with LIMIT, app
restarts after IO reloads (against FakeNode)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from bacnet_uc_harness.errors import HarnessError, SmpError
from bacnet_uc_harness.manifest import ManifestError
from bacnet_uc_harness.node import Node, SmpTarget
from bacnet_uc_harness.render import doc_bytes
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.testing import FakeNode

IO = {"schema": 1, "points": [
    {"channel": "ai0", "type": "analog-input", "instance": 1, "scale": 0.01},
    {"channel": "ao0", "type": "analog-output", "instance": 1},
]}


@pytest.fixture
async def node(fake_node: FakeNode) -> AsyncIterator[Node]:
    n = Node("fake", f"udp:{fake_node.host}:{fake_node.smp_port}",
             f"{fake_node.host}:{fake_node.bacnet_port}", smp_timeout=1.0)
    try:
        yield n
    finally:
        await n.close()


def uploads(fake: FakeNode) -> list[str]:
    """Paths of the FS uploads the fake node received (first chunks)."""
    return list(fake.upload_paths)


@pytest.fixture(autouse=True)
def _record_uploads(fake_node: FakeNode) -> None:
    fake_node.upload_paths = []  # type: ignore[attr-defined]
    handler = fake_node._smp_handlers[(g.GROUP_FS, g.FS_FILE)]

    def upload(req: dict[str, Any]) -> dict[str, Any]:
        if req.get("off") == 0:
            fake_node.upload_paths.append(req.get("name"))  # type: ignore[attr-defined]
        return handler[1](req)

    fake_node._smp_handlers[(g.GROUP_FS, g.FS_FILE)] = (handler[0], upload)


async def test_push_config_is_staged(node: Node, fake_node: FakeNode) -> None:
    res = await node.push_config("io", IO)
    assert res["uploaded"] and res["reloaded"] and res["activated"]
    assert res["staged_path"] == "/lfs/cfg/io.json.new"
    assert uploads(fake_node) == ["/lfs/cfg/io.json.new"]
    assert "/lfs/cfg/io.json.new" not in fake_node.files
    assert fake_node.files["/lfs/cfg/io.json"] == doc_bytes(IO)
    assert (1, 1) in fake_node.objects
    # unchanged: nothing is sent, the document counts as active
    again = await node.push_config("io", IO)
    assert not again["uploaded"] and not again["reloaded"] and again["activated"]
    assert uploads(fake_node) == ["/lfs/cfg/io.json.new"]
    # force: staged and reloaded again
    forced = await node.push_config("io", IO, force=True)
    assert forced["uploaded"] and forced["reloaded"]


async def test_push_config_rejected_by_node(node: Node, fake_node: FakeNode) -> None:
    await node.push_config("io", IO)
    # valid for the schema, rejected by the firmware's parser (object twice)
    dup = {"schema": 1, "points": [
        {"channel": "ai0", "type": "analog-input", "instance": 1},
        {"channel": "ai1", "type": "analog-input", "instance": 1}]}
    with pytest.raises(SmpError) as exc:
        await node.push_config("io", dup)
    assert exc.value.rc_name == "INVALID" and "rejected by the node" in str(exc.value)
    assert "/lfs/cfg/io.json.new" not in fake_node.files
    assert fake_node.files["/lfs/cfg/io.json"] == doc_bytes(IO)
    with pytest.raises(ManifestError):  # schema errors never reach the node
        await node.push_config("io", {"schema": 1, "points": [{"channel": "ai0"}]})


async def test_push_config_without_reload(node: Node, fake_node: FakeNode) -> None:
    res = await node.push_config("io", IO, reload=False)
    assert res["uploaded"] and not res["reloaded"] and not res["activated"]
    assert fake_node.files["/lfs/cfg/io.json.new"] == doc_bytes(IO)
    assert "/lfs/cfg/io.json" not in fake_node.files
    assert await node.reload("io") is False  # activates the staged document
    assert fake_node.files["/lfs/cfg/io.json"] == doc_bytes(IO)


async def test_push_in_sync_removes_a_stale_staged_document(node: Node,
                                                            fake_node: FakeNode) -> None:
    """A document staged without reload must not win at the next reload or
    boot after the active one was pushed again (HAR-2)."""
    await node.push_config("io", IO)
    other = {"schema": 1, "points": [{"channel": "ai1", "type": "analog-input", "instance": 7}]}
    staged = await node.push_config("io", other, reload=False)
    assert staged["uploaded"] and "/lfs/cfg/io.json.new" in fake_node.files
    again = await node.push_config("io", IO)
    assert not again["uploaded"] and again["activated"] and again["staged_cleared"]
    assert "/lfs/cfg/io.json.new" not in fake_node.files
    fake_node._reset()  # a reboot activates staged documents
    assert fake_node.files["/lfs/cfg/io.json"] == doc_bytes(IO)
    assert (0, 7) not in fake_node.objects


async def test_clear_staged_without_shell_overwrites(node: Node, fake_node: FakeNode,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the shell group the stale staged file becomes the active one."""
    await node.push_config("io", IO)
    other = {"schema": 1, "points": []}
    await node.push_config("io", other, reload=False)

    async def no_shell(argv: list[str]) -> tuple[int, str]:
        raise SmpError(None, g.MGMT_ERR_ENOTSUP)

    monkeypatch.setattr(node, "shell", no_shell)
    res = await node.clear_staged("io")
    assert res["cleared"] and res["how"] == "replaced by the active document"
    assert fake_node.files["/lfs/cfg/io.json.new"] == fake_node.files["/lfs/cfg/io.json"]
    assert await node.reload("io") is False
    assert fake_node.files["/lfs/cfg/io.json"] == doc_bytes(IO)
    assert (await node.clear_staged("io"))["cleared"] is False  # nothing staged now


async def test_push_config_needs_staging_firmware(node: Node, fake_node: FakeNode) -> None:
    """A node that ignores ``<doc>.json.new`` keeps its old document: error."""
    real = fake_node._reload

    def old_firmware(doc: str, boot: bool = False) -> bool:
        fake_node.files.pop(f"/lfs/cfg/{doc}.json.new", None)
        return real(doc, boot)

    fake_node._reload = old_firmware  # type: ignore[method-assign]
    with pytest.raises(HarnessError) as exc:
        await node.push_config("io", IO)
    assert "was not activated" in str(exc.value)


async def test_push_device_reports_reboot(node: Node, fake_node: FakeNode) -> None:
    doc = {"schema": 1, "device": {"instance": 1001, "name": "renamed"},
           "bacnet": {"password": "pw"}}
    res = await node.push_config("device", doc)
    assert res["reboot_required"] is False and fake_node.device_name == "renamed"
    assert fake_node.bacnet_password == "pw"
    res = await node.push_config("device", {**doc, "bacnet": {"udp_port": 47809}})
    assert res["reboot_required"] is True and fake_node.bacnet_password == ""


async def test_prop_read_limit_fallback(node: Node, fake_node: FakeNode) -> None:
    for i in range(60):
        fake_node.add_object("binary-value", i)
    objects = await node.prop_read("device:1001", "object-list")
    assert len(objects) == 62 and objects[0] == "(binary-value, 0)"
    reads = [e for e in fake_node.smp_log if e[1:] == (g.GROUP_UC_NODE, g.UC_NODE_PROP_READ)]
    assert len(reads) == 1 + 1 + 62  # LIMIT, size, elements
    with pytest.raises(SmpError):
        await node.prop_read("device:1001", "object-list", 99)


async def test_restart_app(node: Node, fake_node: FakeNode, wasm_module: bytes) -> None:
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    await (await node.smp()).app_install({"name": "a", "file": "/lfs/apps/a.wasm"})
    started = fake_node.apps["a"]["started_at"]
    res = await node.restart_app("a")
    assert res["state"] == "running" and fake_node.apps["a"]["started_at"] != started
    await node.stop_app("a")
    assert (await node.restart_app("a"))["state"] == "running"
    with pytest.raises(HarnessError):
        await node.restart_app("nope")


async def test_restart_io_dependents(node: Node, fake_node: FakeNode,
                                     wasm_module: bytes) -> None:
    await node.push_config("io", IO)
    for f in ("link", "thermostat", "other"):
        fake_node.files[f"/lfs/apps/{f}.wasm"] = wasm_module
    apps = {"schema": 1, "apps": [
        # generated uc-link instance: local AI:1 -> AO:1
        {"name": "link", "file": "/lfs/apps/link.wasm", "params": [
            {"key": "count", "value": "1"},
            {"key": "l0", "value": "1001 0 1 1 1 cov 1000 8 1 0"}]},
        # thermostat with a remote sensor and a remote output: not affected
        {"name": "thermostat", "file": "/lfs/apps/thermostat.wasm", "params": [
            {"key": "sensor_device", "value": "7"}, {"key": "out_device", "value": "7"}]},
        {"name": "other", "file": "/lfs/apps/other.wasm"},
    ]}
    await node.push_config("apps", apps)
    assert {a["state"] for a in await node.list_apps()} == {"running"}
    assert await node.restart_io_dependents() == ["link"]
    # a stopped app is not started
    await node.stop_app("link")
    assert await node.restart_io_dependents() == []
    assert fake_node.apps["link"]["state"] == "stopped"


async def test_logs_reopen_the_file(node: Node, fake_node: FakeNode) -> None:
    """Every download closes a kept FS handle first (stale length otherwise)."""
    fake_node.files["/lfs/log/log.0000"] = b"one\r\n"
    assert (await node.logs())["lines"] == ["one"]
    fake_node.files["/lfs/log/log.0000"] += b"two\r\n"
    assert (await node.logs())["lines"] == ["one", "two"]
    closes = [e for e in fake_node.smp_log if e[1:] == (g.GROUP_FS, g.FS_OPENED_FILE)]
    assert len(closes) >= 2
    assert json.loads(json.dumps((await node.logs(1, grep="t"))["lines"])) == ["two"]


async def test_deploy_large_manifest_via_apps_json(node: Node, fake_node: FakeNode,
                                                   wasm_module: bytes) -> None:
    """An install request over the SMP request limit goes through apps.json."""
    small = await node.deploy_app("a", wasm_module, params={"k": "v"})
    assert small["installed_via"] == "install"
    params = {f"key{i}": "v" * 90 for i in range(16)}
    res = await node.deploy_app("big", wasm_module, params=params, perms=["bacnet.local"])
    assert res["installed_via"] == "apps.json" and res["status"]["state"] == "running"
    assert fake_node.apps["big"]["params"] == params
    doc = json.loads(fake_node.files["/lfs/cfg/apps.json"])
    assert [e["name"] for e in doc["apps"]] == ["a", "big"]
    assert fake_node.apps["a"]["state"] == "running"
    # replacing it (running) keeps the order and restarts it
    params["key0"] = "w"
    res = await node.deploy_app("big", wasm_module, params=params, perms=["bacnet.local"])
    assert res["installed_via"] == "apps.json" and res["status"]["state"] == "running"
    assert fake_node.apps["big"]["params"]["key0"] == "w"


@pytest.mark.parametrize(("spec", "device", "baud"), [
    ("serial:/dev/ttyACM0", "/dev/ttyACM0", 115200),
    ("serial:/dev/ttyACM0:57600", "/dev/ttyACM0", 57600),
    ("/dev/ttyUSB1:9600", "/dev/ttyUSB1", 9600),
    ("serial:socket://localhost:7777", "socket://localhost:7777", 115200),
    ("serial:rfc2217://bench:4000", "rfc2217://bench:4000", 115200),
    ("serial:socket://localhost:7777:57600", "socket://localhost:7777", 57600),
    ("serial:loop://", "loop://", 115200),
])
def test_smp_target_serial_urls(spec: str, device: str, baud: int) -> None:
    """A pyserial URL keeps its port; the baud rate follows it (HAR-9)."""
    t = SmpTarget.parse(spec)
    assert (t.kind, t.device, t.baud) == ("serial", device, baud)
    assert SmpTarget.parse(str(t)) == t
