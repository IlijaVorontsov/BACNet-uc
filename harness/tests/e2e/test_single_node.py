# SPDX-License-Identifier: Apache-2.0
"""One real native_sim node: SMP management, staged configuration, IO,
BACnet/IP, WebAssembly apps (see conftest.py for the requirements)."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness import budget
from bacnet_uc_harness.bacnet.client import BacnetClient
from bacnet_uc_harness.errors import BacnetError, SmpError
from bacnet_uc_harness.manifest import ManifestError
from bacnet_uc_harness.node import Node
from bacnet_uc_harness.planner import wait_reachable
from bacnet_uc_harness.sim import SimManager
from bacnet_uc_harness.smp.client import SmpClient
from bacnet_uc_harness.smp.transport import SerialTransport
from bacnet_uc_harness.wasm_build import build_c

pytestmark = pytest.mark.e2e
needs_clang = pytest.mark.skipif(shutil.which("clang") is None, reason="clang required")

REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "wasm" / "examples"
SINGLE_INSTANCE = 3100  # conftest.SINGLE_INSTANCE: the device.json pushed here
BACNET = ("127.0.0.1", 47808)
PASSWORD = "e2e-Pass word!"
DEVICE = {"schema": 1, "device": {"instance": SINGLE_INSTANCE, "name": "e2e-node",
                                  "location": "ci"},
          "bacnet": {"password": PASSWORD}, "log": {"level": "inf"}}
IO = {"schema": 1, "points": [
    {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "Temp",
     "units": "degrees-celsius", "scale": 0.01},
    {"channel": "di0", "type": "binary-input", "instance": 1, "debounce_ms": 0},
    {"channel": "do0", "type": "binary-output", "instance": 1},
    {"channel": "ao0", "type": "analog-output", "instance": 1, "units": "percent"},
    # skipped by the firmware (unknown channel), the other points are bound
    {"channel": "nope", "type": "analog-input", "instance": 9},
]}


async def eventually(read: Callable[[], Awaitable[Any]], check: Callable[[Any], bool],
                     timeout: float = 10.0, interval: float = 0.2) -> Any:
    """Poll ``read`` until ``check`` holds; returns the last value (assert on it)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    value = await read()
    while not check(value) and loop.time() < deadline:
        await asyncio.sleep(interval)
        value = await read()
    return value


@pytest.fixture
async def node(single_node: SimManager) -> AsyncIterator[Node]:
    n = Node("e2e", "udp:127.0.0.1:1337", f"{BACNET[0]}:{BACNET[1]}", smp_timeout=2.0)
    assert await wait_reachable(n, 30.0, 0.2), single_node.log_tail("e2e", 30)
    try:
        yield n
    finally:
        await n.close()


@pytest.fixture
async def bacnet() -> AsyncIterator[BacnetClient]:
    client = BacnetClient(apdu_timeout=1.0, retries=2)
    await client.start()
    try:
        yield client
    finally:
        await client.close()


async def test_node_info(node: Node) -> None:
    info = await node.info()
    assert info["board"] == "native_sim/native/64"
    assert info["api"] == 0x10000
    assert info["fs"]["ready"] and info["fs"]["total"] > 0
    assert info["wasm"]["interp"] and info["wasm"]["pool_total"] > 200000
    assert info["bacnet"]["objects"] >= 2  # device, network-port
    smp = await node.smp()
    assert (await smp.mcumgr_params())["buf_size"] == 1152
    assert await smp.max_frame() == 1024  # UDP request limit
    assert await node.echo("e2e") == "e2e"


async def test_staged_device_config(node: Node, bacnet: BacnetClient) -> None:
    smp = await node.smp()
    booted = (await node.info())["device"]["instance"]
    res = await node.push_config("device", DEVICE)
    assert res["uploaded"] and res["reloaded"] and res["activated"], res
    assert res["reboot_required"] is (booted != SINGLE_INSTANCE)
    # the name applies at once (node_info reads the BACnet thread's snapshot)
    info = await eventually(node.info, lambda i: i["device"]["name"] == "e2e-node", 3.0)
    assert info["device"]["name"] == "e2e-node"
    with pytest.raises(SmpError) as exc:
        await smp.fs_status("/lfs/cfg/device.json.new")
    assert exc.value.rc_name == "FILE_NOT_FOUND"  # renamed over device.json
    # the same document again: nothing is sent
    again = await node.push_config("device", DEVICE)
    assert not again["uploaded"] and again["activated"]
    # an invalid staged document is deleted, the active one stays
    await smp.fs_upload("/lfs/cfg/device.json.new", b'{"schema":1,"device":{"instance":')
    with pytest.raises(SmpError) as exc:
        await node.reload("device")
    assert (exc.value.group, exc.value.rc_name) == (66, "INVALID")
    with pytest.raises(SmpError) as exc:
        await smp.fs_status("/lfs/cfg/device.json.new")
    assert exc.value.rc_name == "FILE_NOT_FOUND"
    assert await node.file_sha256("/lfs/cfg/device.json") == res["sha256"]
    # the harness validates before uploading (password: 1..20 printable ASCII)
    bad = {**DEVICE, "bacnet": {"password": "x" * 21}}
    with pytest.raises(ManifestError):
        await node.push_config("device", bad)
    # the new instance needs a reboot: native_sim restarts the process in place
    if res["reboot_required"]:
        await node.reboot()
        await asyncio.sleep(0.5)
        assert await wait_reachable(node, 30.0, 0.2)
    info = await node.info()
    assert info["device"] == {"instance": SINGLE_INSTANCE, "name": "e2e-node"}
    assert await bacnet.read_property(BACNET, "device", SINGLE_INSTANCE, "location") == "ci"


async def test_io_force_and_bacnet(node: Node, bacnet: BacnetClient) -> None:
    res = await node.push_config("io", IO)
    assert res["activated"]
    objs = {(o["type"], o["instance"]): o for o in await node.objects()}
    assert objs[("analog-input", 1)]["owner"] == "io"
    assert ("analog-input", 9) not in objs  # point on the unknown channel skipped
    cat = {c["name"]: c for c in (await node.io_catalog())["channels"]}
    assert cat["ao0"]["object"] == {"type": "analog-output", "instance": 1}
    # force an input: the BACnet object follows (2500 mV * 0.01 = 25.0)
    await node.io_force("ai0", 2500)
    pv = await eventually(lambda: bacnet.read_property(BACNET, "analog-input", 1),
                          lambda v: math.isclose(v, 25.0, abs_tol=0.01))
    assert pv == pytest.approx(25.0, abs=0.01)
    assert await bacnet.read_property(BACNET, "analog-input", 1, "units") == 62
    assert (await node.io_catalog())["channels"][4]["forced"] is True
    await node.io_force("di0", 1)
    assert await eventually(lambda: bacnet.read_property(BACNET, "binary-input", 1),
                            lambda v: v == 1) == 1
    # a BACnet write drives the output channel; relinquish returns it
    await bacnet.write_property(BACNET, "binary-output", 1, "present-value", 1, priority=8)
    assert await eventually(lambda: node.io_read("do0"), lambda v: v["do0"] == 1.0) == {
        "do0": 1.0}
    await bacnet.write_property(BACNET, "binary-output", 1, "present-value", None, priority=8)
    assert await eventually(lambda: node.io_read("do0"), lambda v: v["do0"] == 0.0) == {
        "do0": 0.0}
    # force: exactly one of value and release
    smp = await node.smp()
    with pytest.raises(SmpError) as exc:
        await smp.write(65, 3, {"name": "ai0", "value": 1.0, "release": True})
    assert exc.value.rc_name == "INVALID"
    await node.io_release("di0")
    await node.io_release("ai0")
    # a released simulated input keeps the forced value (io.md section 5)
    assert await node.io_read("ai0") == {"ai0": 2500.0}


async def test_prop_read_write(node: Node) -> None:
    await node.push_config("io", IO)
    dev = (await node.info())["device"]["instance"]
    objects = await node.prop_read(f"device:{dev}", "object-list")
    assert isinstance(objects, list) and f"(device, {dev})" in objects
    assert await node.prop_read(f"device:{dev}", "object-list", 0) == len(objects)
    assert await node.prop_read("analog-output:1", "priority-array") == [None] * 16
    await node.prop_write("analog-output:1", "present-value", 42.0, priority=8)
    pa = await node.prop_read("analog-output:1", "priority-array")
    assert pa[7] == 42.0 and await node.prop_read("analog-output:1") == 42.0
    await node.prop_write("analog-output:1", "present-value", None, priority=8)
    assert await node.prop_read("analog-output:1") == 0.0
    assert await node.prop_read("analog-input:1", "status-flags") == "{false,false,false,false}"
    for args, rc_name in (
        (("analog-output:1", "present-value", 1.0, 6), "PERM"),  # priority 6 reserved
        (("binary-output:1", "present-value", 3, 8), "INVALID"),
        (("binary-output:1", "present-value", 0.5, 8), "INVALID"),
        (("analog-output:1", "present-value", float("nan"), 8), "INVALID"),
        (("analog-output:1", "present-value", "text", 8), "INVALID"),
        (("analog-output:1", "units", -1, None), "INVALID"),
        (("analog-input:1", "present-value", 5.0, None), "PERM"),
    ):
        with pytest.raises(SmpError) as exc:
            await node.prop_write(*args)
        assert exc.value.rc_name == rc_name, args
    with pytest.raises(SmpError) as exc:
        await node.prop_read("analog-input:1", "present-value", 1)
    assert exc.value.rc_name == "INVALID"  # index on a property that is not an array
    with pytest.raises(SmpError) as exc:
        await node.prop_read("analog-input:77")
    assert exc.value.rc_name == "NOT_FOUND"


async def test_bacnet_password(node: Node, bacnet: BacnetClient) -> None:
    await node.push_config("device", DEVICE)
    await bacnet.device_communication_control(BACNET, "enable", password=PASSWORD)
    for call in (lambda: bacnet.device_communication_control(BACNET, "enable", password="no"),
                 lambda: bacnet.device_communication_control(BACNET, "enable"),
                 lambda: bacnet.reinitialize_device(BACNET, "warmstart", password="no")):
        with pytest.raises(BacnetError) as exc:
            await call()
        assert exc.value.error_code_name == "password-failure"
    # without a password the firmware refuses DCC and ReinitializeDevice
    no_pw = {k: v for k, v in DEVICE.items() if k != "bacnet"}
    await node.push_config("device", no_pw)
    with pytest.raises(BacnetError) as exc:
        await bacnet.device_communication_control(BACNET, "enable", password=PASSWORD)
    assert exc.value.error_code_name == "password-failure"
    await node.push_config("device", DEVICE)
    await bacnet.device_communication_control(BACNET, "enable", password=PASSWORD)


async def test_serial_console(single_node: SimManager) -> None:
    """SMP over the node's console pty with full-size frames and no pacing."""
    pytest.importorskip("serial")
    ptys = [m.group(1) for ln in single_node.log_tail("e2e", 400)
            if (m := re.search(r"uart connected to pseudotty: (\S+)", ln))]
    if not ptys:
        pytest.skip("no console pty in the node log")
    client = SmpClient(SerialTransport(ptys[-1]), timeout=5.0, retries=1)
    try:
        assert await client.os_echo("over the pty") == "over the pty"
        assert await client.max_frame() == 1152
        data = bytes(range(256)) * 12
        await client.fs_upload("/lfs/data/serial.bin", data)
        assert await client.fs_hash("/lfs/data/serial.bin") == hashlib.sha256(data).digest()
        assert (await client.node_info())["board"] == "native_sim/native/64"
    finally:
        await client.close()


def _build(tmp: Path, name: str, src: str, heap_kb: int = 8) -> Path:
    return build_c([EXAMPLES / src], tmp / f"{name}.wasm", opt="-Oz", heap_kb=heap_kb).path


@needs_clang
async def test_deploy_apps(node: Node, tmp_path: Path) -> None:
    await node.push_config("io", IO)
    thermostat = _build(tmp_path, "thermostat", "thermostat/thermostat.c")
    blinky = _build(tmp_path, "blinky", "blinky/blinky.c")
    for name in ("thermostat", "blinky"):
        if await node.app_status(name) is not None:
            await node.remove_app(name)
    free0 = (await node.info())["wasm"]["pool_free"]
    res = await node.deploy_app(
        "thermostat", thermostat.read_bytes(), heap_kb=8, perms=["bacnet.local"],
        params={"sensor_instance": "1", "out_instance": "1", "setpoint": "21", "kp": "20",
                "ti_s": "0", "out_deadband": "0", "poll_ms": "1000"})
    assert res["uploaded"] and res["status"]["state"] == "running", res
    used = free0 - (await node.info())["wasm"]["pool_free"]
    est = budget.app_pool_estimate("thermostat", thermostat, heap_kb=8, stack_kb=4).total
    assert abs(est - used) <= 0.1 * used, (est, used)  # validate_system's pool model
    res = await node.deploy_app("blinky", blinky.read_bytes(), period_ms=200,
                                perms=["bacnet.local"], params={"instance": "5"})
    assert res["status"]["state"] == "running"
    again = await node.deploy_app("blinky", blinky.read_bytes(), period_ms=200,
                                  perms=["bacnet.local"], params={"instance": "5"})
    assert not again["uploaded"]  # same module: install only
    # thermostat (P only, kp 20 %/K): 15 degC -> valve open, 25 degC -> closed
    await node.io_force("ai0", 1500)
    assert await eventually(lambda: node.prop_read("analog-output:1"), lambda v: v >= 99) >= 99
    await node.io_force("ai0", 2500)
    assert await eventually(lambda: node.prop_read("analog-output:1"), lambda v: v <= 1) <= 1
    await node.io_release("ai0")
    # blinky toggles its binary-value:5
    seen = set()
    for _ in range(12):
        seen.add(await node.prop_read("binary-value:5"))
        await asyncio.sleep(0.1)
    assert seen == {0, 1}
    # the thermostat's setpoint analog-value:1 is a value object: no priority array
    await node.bacnet_write("analog-value:1", "present-value", 22.0, priority=8)
    await node.bacnet_write("analog-value:1", "present-value", None, priority=8)
    assert await node.bacnet_read("analog-value:1") == 22.0
    with pytest.raises(BacnetError) as exc:
        await node.bacnet_read("analog-value:1", "priority-array")
    assert exc.value.error_code_name == "unknown-property"
    status = await node.app_status("thermostat")
    assert status is not None and status["state"] == "running" and status["ticks"] > 0
    assert {a["name"] for a in await node.list_apps()} >= {"thermostat", "blinky"}
    # the log thread writes /lfs/log/log.NNNN asynchronously; every read
    # reopens the file (a kept FS download handle would have a stale length).
    # The FS backend syncs when the log thread goes idle: generate one more
    # message so the setpoint line is flushed (see harness README, e2e notes).
    await node.io_force("ai1", 1)
    await node.io_release("ai1")
    logs = await eventually(lambda: node.logs(300, grep="thermostat"),
                            lambda r: any("setpoint" in ln for ln in r["lines"]), 5.0, 0.2)
    assert logs["files"] and any("thermostat: running" in ln for ln in logs["lines"]), logs
    if not any("setpoint 21.00 -> 22.00" in ln for ln in logs["lines"]):
        smp = await node.smp()
        size = await smp.fs_status(logs["files"][-1])
        raise AssertionError(f"setpoint line not in {logs['files']} ({size} B): "
                             f"{(await node.logs(4))['lines']}")
    # removing an app deletes the objects it created and (here) its module
    await node.remove_app("blinky", delete_file=True)
    assert await node.app_status("blinky") is None
    assert ("binary-value", 5) not in {(o["type"], o["instance"]) for o in await node.objects()}
    assert await node.file_sha256("/lfs/apps/blinky.wasm") is None


@needs_clang
async def test_large_manifest_via_apps_json(node: Node, tmp_path: Path) -> None:
    """16 long parameters do not fit one install request: apps.json is used."""
    blinky = _build(tmp_path, "blinky", "blinky/blinky.c")
    params = {"instance": "6", **{f"note{i}": f"{i:02d}" + "x" * 88 for i in range(15)}}
    res = await node.deploy_app("big", blinky.read_bytes(), perms=["bacnet.local"],
                                params=params)
    assert res["installed_via"] == "apps.json", res
    assert res["status"] is not None and res["status"]["state"] == "running", res
    cfg = await node.get_config("apps")
    assert cfg is not None
    entry = next(e for e in cfg["apps"] if e["name"] == "big")
    assert {p["key"]: p["value"] for p in entry["params"]} == params
    objs = await eventually(node.objects, lambda objs: ("binary-value", 6) in {
        (o["type"], o["instance"]) for o in objs}, 3.0)
    assert ("binary-value", 6) in {(o["type"], o["instance"]) for o in objs}
    await node.remove_app("big")


@needs_clang
async def test_io_reload_restarts_link(node: Node, tmp_path: Path) -> None:
    """After an io.json reload re-created the IO objects, restarting the
    uc-link instance brings its destination back to the linked value."""
    await node.push_config("io", IO)
    if await node.app_status("thermostat") is not None:
        await node.remove_app("thermostat")  # it writes analog-output:1 too
    link = _build(tmp_path, "link", "uc-link/uc_link.c", heap_kb=0)
    dev = (await node.info())["device"]["instance"]
    # poll mode: uc-link writes only when the source value changes
    line = f"{dev} 0 1 1 1 poll 300 8 1 0"  # local AI:1 -> AO:1 @8
    res = await node.deploy_app("link", link.read_bytes(), heap_kb=0, period_ms=500,
                                perms=["bacnet.local", "bacnet.remote"],
                                params={"count": "1", "l0": line})
    assert res["status"]["state"] == "running", res
    await node.io_force("ai0", 3300)
    assert await eventually(lambda: node.prop_read("analog-output:1"),
                            lambda v: math.isclose(v, 33.0, abs_tol=0.01)) == pytest.approx(33.0)
    assert await node.reload("io") is False
    # the re-created analog-output:1 sits at Relinquish_Default: the source
    # did not change, so the link does not write it again ...
    await asyncio.sleep(1.5)
    assert await node.prop_read("analog-output:1") == 0.0
    # ... until the link is restarted
    restarted = await node.restart_io_dependents()
    assert restarted == ["link"]
    assert await eventually(lambda: node.prop_read("analog-output:1"),
                            lambda v: math.isclose(v, 33.0, abs_tol=0.01),
                            timeout=5.0) == pytest.approx(33.0)
    await node.io_release("ai0")
    await node.remove_app("link")
