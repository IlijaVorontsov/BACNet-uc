from __future__ import annotations

import asyncio
import json
import sys

import pytest

from uc_hub.core.errors import DeviceError, NotFound, Unsupported
from uc_hub.drivers.bacnet_uc.client import SmpNodeClient, parse_address
from uc_hub.sim import (
    EXAMPLE_IO,
    ManualClock,
    RoomModel,
    SimFeatures,
    SimNetwork,
    SimNode,
    start_sim_nodes,
)

from .conftest import make_client

WASM = b"\0asm\x01\0\0\0"


async def upload_io(client: SmpNodeClient, points: list[dict[str, object]]) -> None:
    await client.file_upload("/lfs/cfg/io.json", json.dumps({"schema": 1, "points": points}).encode())
    await client.reload("io")


async def test_reload_io_replaces_io_objects(client: SmpNodeClient, node: SimNode) -> None:
    assert (1, 1) in node.objects  # analog-output:1 from the example io.json
    await upload_io(client, [
        {"channel": "ao0", "type": "analog-value", "instance": 7, "name": "Valve AV"},
        {"channel": "do1", "type": "binary-value", "instance": 3},
        {"channel": "zz", "type": "binary-input", "instance": 9},  # unknown channel: skipped
        {"channel": "ai1", "type": "binary-output", "instance": 9},  # wrong kind: skipped
    ])
    keys = {(o["type"], o["instance"], o["owner"]) for o in await client.objects()}
    assert keys == {("device", 2041, "system"), ("network-port", 1, "system"),
                    ("analog-value", 7, "io"), ("binary-value", 3, "io")}
    assert await client.prop_read("binary-value", 3, "object-name") == "do1"  # name defaults to the channel


async def test_ai_force_is_scaled(client: SmpNodeClient) -> None:
    await client.io_force("ai0", 650.0)  # 650 mV * 0.1 - 50 = 15 degC
    assert await client.prop_read("analog-input", 1) == pytest.approx(15.0)
    assert await client.io_read("ai0") == {"ai0": 650.0}
    await client.io_force("ai0", None)
    assert await client.prop_read("analog-input", 1) == pytest.approx(20.0)


async def test_ao_inverse_scaling_and_clamp(client: SmpNodeClient) -> None:
    await upload_io(client, [
        {"channel": "ao0", "type": "analog-output", "instance": 1, "scale": 0.5, "offset": 10,
         "min": 10, "max": 50},
    ])
    await client.prop_write("analog-output", 1, 30.0, priority=8)
    assert await client.io_read("ao0") == {"ao0": pytest.approx(40.0)}  # (30 - 10) / 0.5
    await client.prop_write("analog-output", 1, 90.0, priority=8)
    assert await client.io_read("ao0") == {"ao0": pytest.approx(80.0)}  # clamped to max 50 first
    await client.io_force("ao0", 12.0)  # a forced output ignores its object
    await client.prop_write("analog-output", 1, 20.0, priority=8)
    assert await client.io_read("ao0") == {"ao0": 12.0}
    await client.io_force("ao0", None)
    assert await client.io_read("ao0") == {"ao0": pytest.approx(20.0)}


async def test_binary_invert_and_multistate(client: SmpNodeClient) -> None:
    await upload_io(client, [
        {"channel": "di0", "type": "binary-input", "instance": 1, "invert": True},
        {"channel": "di1", "type": "multi-state-input", "instance": 1},
        {"channel": "do0", "type": "binary-output", "instance": 1, "invert": True},
    ])
    assert await client.prop_read("binary-input", 1) == 1  # raw 0 inverted
    await client.io_force("di1", 1)
    assert await client.prop_read("multi-state-input", 1) == 2
    assert await client.io_read("do0") == {"do0": 1.0}  # PV inactive, inverted
    await client.prop_write("binary-output", 1, 1, priority=16)
    assert await client.io_read("do0") == {"do0": 0.0}
    with pytest.raises(DeviceError):
        await client.io_force("di0", 0.5)


async def test_out_of_service_input_takes_writes(client: SmpNodeClient) -> None:
    await client.prop_write("analog-input", 1, True, prop="out-of-service")
    await client.prop_write("analog-input", 1, 33.0)
    await client.io_force("ai0", 100.0)
    assert await client.prop_read("analog-input", 1) == 33.0


async def test_io_force_lease_expires(client: SmpNodeClient, node: SimNode, clock: ManualClock) -> None:
    await client.io_force("ai0", 650.0, lease_ms=5000)
    clock.advance(4.9)
    assert await client.prop_read("analog-input", 1) == pytest.approx(15.0)
    clock.advance(0.2)
    assert await client.prop_read("analog-input", 1) == pytest.approx(20.0)
    assert (await client.io_catalog())["channels"][4]["forced"] is False


async def test_prop_write_lease_expires(client: SmpNodeClient, clock: ManualClock) -> None:
    await client.prop_write("analog-output", 1, 55.0, priority=12, lease_ms=1000)
    await client.prop_write("analog-output", 1, 20.0, priority=14)
    assert await client.prop_read("analog-output", 1) == 55.0
    clock.advance(1.5)
    assert await client.prop_read("analog-output", 1, "priority-array", index=12) is None
    assert await client.prop_read("analog-output", 1) == 20.0


async def test_rewrite_without_lease_keeps_value(client: SmpNodeClient, clock: ManualClock) -> None:
    await client.prop_write("analog-output", 1, 55.0, priority=12, lease_ms=1000)
    await client.prop_write("analog-output", 1, 60.0, priority=12)
    clock.advance(2)
    assert await client.prop_read("analog-output", 1) == 60.0


async def test_firmware_without_lease_ignores_it(net: SimNetwork, clock: ManualClock) -> None:
    sim = SimNode(name="old", instance=12, io=EXAMPLE_IO, network=net,
                  features=SimFeatures(lease_ms=False))
    await sim.start()
    c = make_client(sim)
    try:
        await c.prop_write("analog-output", 1, 55.0, priority=12, lease_ms=1000)
        await c.io_force("ai0", 650.0, lease_ms=1000)
        clock.advance(5)
        assert await c.prop_read("analog-output", 1) == 55.0
        assert await c.prop_read("analog-input", 1) == pytest.approx(15.0)
    finally:
        await c.close()


async def test_priority_array_semantics(client: SmpNodeClient) -> None:
    await client.prop_write("analog-output", 1, 7.0, prop="relinquish-default")
    assert await client.prop_read("analog-output", 1) == 7.0
    await client.prop_write("analog-output", 1, 50.0)  # no priority: slot 16
    assert await client.prop_read("analog-output", 1, "priority-array", index=16) == 50.0
    with pytest.raises(DeviceError) as err:
        await client.prop_write("analog-output", 1, 1.0, priority=6)
    assert err.value.rc == 10
    # Like the firmware, an array read without index answers slot 1 only.
    await client.prop_write("analog-output", 1, 80.0, priority=1)
    assert await client.prop_read("analog-output", 1, "priority-array") == 80.0


async def test_array_read_feature_returns_the_list(net: SimNetwork) -> None:
    sim = SimNode(name="new", instance=13, io=EXAMPLE_IO, network=net, features=SimFeatures(array_read=True))
    await sim.start()
    c = make_client(sim)
    try:
        await c.prop_write("binary-output", 1, 1, priority=9)
        slots = await c.prop_read("binary-output", 1, "priority-array")
        assert slots == [None] * 8 + [1] + [None] * 7
    finally:
        await c.close()


async def test_hwid_feature(net: SimNetwork) -> None:
    sim = SimNode(name="old", instance=14, network=net, features=SimFeatures(hwid=False))
    await sim.start()
    c = make_client(sim)
    try:
        info = await c.info()
        assert "hwid" not in info and "mac" not in info
    finally:
        await c.close()


async def test_reset_reboots_ram_state(client: SmpNodeClient, node: SimNode) -> None:
    await client.io_force("ai0", 650.0)
    await client.prop_write("analog-output", 1, 55.0, priority=12)
    await client.reset()
    assert await client.prop_read("analog-output", 1) == 0.0
    assert await client.prop_read("analog-input", 1) == pytest.approx(20.0)


async def test_apps_json_reload_and_boot(client: SmpNodeClient, node: SimNode) -> None:
    await client.file_upload("/lfs/apps/idle.wasm", WASM)
    doc = {"schema": 1, "apps": [{"name": "idle", "file": "/lfs/apps/idle.wasm",
                                  "params": [{"key": "a", "value": "1"}]}]}
    await client.file_upload("/lfs/cfg/apps.json", json.dumps(doc).encode())
    await client.reload("apps")
    assert [(a["name"], a["state"]) for a in await client.app_list()] == [("idle", "running")]
    await client.reset()
    assert [(a["name"], a["state"]) for a in await client.app_list()] == [("idle", "running")]
    await client.file_upload("/lfs/cfg/apps.json", b'{"schema": 1, "apps": []}')
    await client.reload("all")
    assert await client.app_list() == []


async def test_missing_document_reload(client: SmpNodeClient) -> None:
    with pytest.raises(NotFound):
        await client.reload("apps")
    assert await client.reload("all") is False  # skips documents that do not exist


async def test_app_ticks_follow_the_clock(client: SmpNodeClient, node: SimNode, clock: ManualClock) -> None:
    await client.file_upload("/lfs/apps/idle.wasm", WASM)
    await client.app_install({"name": "idle", "file": "/lfs/apps/idle.wasm", "period_ms": 100})
    for _ in range(10):
        clock.advance(0.1)
        node.step()
    status = await client.app_status("idle")
    assert status["ticks"] == 10
    assert status["uptime_ms"] == 1000
    assert status["emulation"] == "idle"


async def test_objects_page_fits_small_mtu(net: SimNetwork) -> None:
    io = [{"channel": ch, "type": t, "instance": i, "name": f"A rather long object name {i:02d}"}
          for i, (ch, t) in enumerate([("ai0", "analog-input"), ("ai1", "analog-input"),
                                       ("ao0", "analog-output"), ("ao1", "analog-value"),
                                       ("di0", "binary-input"), ("di1", "binary-input"),
                                       ("do0", "binary-output"), ("do1", "binary-value")], 1)]
    sim = SimNode(name="small", instance=15, io=io, mtu=160, network=net)
    await sim.start()
    c = make_client(sim, objects_page=50)
    try:
        objects = await c.objects()
        assert len(objects) == 10
        assert c._mtu is None  # paging does not need the MTU
    finally:
        await c.close()


async def test_room_model_closes_the_loop(net: SimNetwork, clock: ManualClock) -> None:
    room = RoomModel(temp_c=15.0)
    sim = SimNode(name="room", instance=16, io=EXAMPLE_IO, room=room, network=net)
    sim.local_write(1, 1, 85, 100.0, 8)  # valve fully open
    net.advance(600, step_s=5)
    assert room.temp_c > 25.0
    assert sim.local_read(0, 1, 85) == pytest.approx(room.temp_c)
    sim.local_write(1, 1, 85, None, 8)
    net.advance(1800, step_s=5)
    assert room.temp_c < 12.0


def test_room_model_math() -> None:
    room = RoomModel(temp_c=20.0, outdoor_c=5.0, tau_s=900.0, valve_gain_c_per_s=0.04)
    assert room.steady_state_c(100.0) == pytest.approx(41.0)
    room.advance(20 * 900, 100.0, False)
    assert room.temp_c == pytest.approx(41.0, abs=0.01)
    assert room.sensor_mv() == pytest.approx((41.0 + 50.0) / 0.1, abs=1)
    with pytest.raises(ValueError):
        RoomModel(tau_s=0)


async def test_start_sim_nodes_with_real_clock() -> None:
    net, nodes = await start_sim_nodes([
        {"name": "a", "instance": 1},
        {"name": "b", "instance": 2, "io": EXAMPLE_IO, "scan_interval_s": 0.01},
    ])
    async with net:
        a, b = nodes
        assert net.addresses() == {"a": a.address, "b": b.address}
        assert a.port and b.port and a.port != b.port
        assert b.autorun
        c = make_client(b)
        try:
            await c.io_force("ai0", 650.0, lease_ms=50)
            assert await c.prop_read("analog-input", 1) == pytest.approx(15.0)
            await asyncio.sleep(0.2)
            assert b.channels["ai0"].forced is None  # the scan task expired it, no request needed
            assert b.objects[(0, 1)].pv == pytest.approx(20.0)
        finally:
            await c.close()
    assert net.nodes == []
    assert not b.reachable


async def test_cli_serves_a_node() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uc_hub.sim.smp_node", "--port", "0", "--instance", "2041",
        "--name", "r204-ctl", "--io", "example", "--room", "--without", "identify",
        "--log-level", "WARNING",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        line = (await asyncio.wait_for(proc.stdout.readline(), 30)).decode()
        assert "device 2041" in line
        host, port = parse_address(line.rsplit(" ", 1)[-1])
        c = SmpNodeClient(host, port, timeout_s=1.0, retries=2)
        try:
            info = await c.info()
            assert info["device"] == {"instance": 2041, "name": "r204-ctl"}
            assert await c.prop_read("analog-input", 1, "object-name") == "Room Temperature"
            with pytest.raises(Unsupported):
                await c.identify()
        finally:
            await c.close()
    finally:
        if proc.returncode is None:
            proc.terminate()
        assert await asyncio.wait_for(proc.wait(), 10) == 0
