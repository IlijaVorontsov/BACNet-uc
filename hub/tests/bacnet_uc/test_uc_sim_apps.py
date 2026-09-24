from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from uc_hub.drivers.bacnet_uc.client import SmpNodeClient
from uc_hub.sim import EXAMPLE_IO, LinkSpec, ManualClock, RoomModel, SimNetwork, SimNode

from .conftest import make_client

WASM = b"\0asm\x01\0\0\0"
LINK_PERMS = ["bacnet.local", "bacnet.remote"]


@pytest.fixture
async def pair(net: SimNetwork) -> AsyncIterator[tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient]]:
    ahu = SimNode(name="ahu1-ctl", instance=1001, io=EXAMPLE_IO, network=net)
    room = SimNode(name="r204-ctl", instance=2041, io=EXAMPLE_IO, network=net)
    for sim in (ahu, room):
        await sim.start()
    ahu_client, room_client = make_client(ahu), make_client(room)
    yield ahu, ahu_client, room, room_client
    await ahu_client.close()
    await room_client.close()


async def install(client: SmpNodeClient, name: str, file: str, params: dict[str, str],
                  perms: list[str], period_ms: int = 1000) -> None:
    await client.file_upload(file, WASM)
    await client.app_install({"name": name, "file": file, "perms": perms, "params": params,
                              "period_ms": period_ms})


def test_link_spec_parsing() -> None:
    spec = LinkSpec.parse("1001 0 1 2 10 cov 1000 0 1 0")
    assert (spec.src_device, spec.src_type, spec.src_instance) == (1001, 0, 1)
    assert (spec.dst_type, spec.dst_instance, spec.mode) == (2, 10, "cov")
    assert (spec.period_ms, spec.priority, spec.scale, spec.offset) == (1000, 0, 1.0, 0.0)
    assert LinkSpec.parse(spec.format()) == spec
    for bad in ("", "1001 0 1 2 10 cov 1000 0 1", "1001 0 1 2 10 push 1000 0 1 0",
                "1001 0 1 2 10 cov 1000 17 1 0", "a 0 1 2 10 cov 1000 0 1 0",
                "1001 0 1 2 10 cov 1000 0 nan 0"):
        with pytest.raises(ValueError):
            LinkSpec.parse(bad)


async def test_uc_link_poll_between_nodes(pair: tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient],
                                          net: SimNetwork) -> None:
    _, ahu_client, _, room_client = pair
    await ahu_client.file_upload("/lfs/cfg/io.json", b'{"schema": 1, "points": [{"channel": "ao1", '
                                 b'"type": "analog-value", "instance": 3, "name": "Supply Temp SP"}]}')
    await ahu_client.reload("io")
    await ahu_client.prop_write("analog-value", 3, 18.0, priority=16)
    # analog-value:3 of 1001 -> analog-value:10 here, every 500 ms, * 2 + 1
    await install(room_client, "link", "/lfs/apps/uc-link.wasm",
                  {"count": "1", "l0": "1001 2 3 2 10 poll 500 0 2 1"}, LINK_PERMS)
    net.advance(1.0, step_s=0.5)
    assert await room_client.prop_read("analog-value", 10) == pytest.approx(37.0)
    assert await room_client.prop_read("analog-value", 10, "object-name") == "link-0"
    await ahu_client.prop_write("analog-value", 3, 20.0, priority=16)
    net.advance(0.5, step_s=0.5)
    assert await room_client.prop_read("analog-value", 10) == pytest.approx(41.0)
    status = await room_client.app_status("link")
    assert status["state"] == "running" and status["errors"] == 0
    assert status["emulation"] == "uc-link"
    objects = {(o["type"], o["instance"]): o["owner"] for o in await room_client.objects()}
    assert objects[("analog-value", 10)] == "app:link"
    await room_client.app_stop("link")
    with pytest.raises(Exception, match="NOT_FOUND"):
        await room_client.prop_read("analog-value", 10)  # app objects go with the app


async def test_uc_link_cov_to_commandable_output(
    pair: tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient], net: SimNetwork,
) -> None:
    _, ahu_client, _, room_client = pair
    # analog-input:1 of the AHU (room temp, degC) -> analog-output:1 here at priority 10
    await install(room_client, "link", "/lfs/apps/uc-link.wasm",
                  {"count": "2", "l0": "1001 0 1 1 1 cov 1000 10 1 0", "l1": "garbage"}, LINK_PERMS)
    net.step()
    assert await room_client.prop_read("analog-output", 1) == pytest.approx(20.0)
    assert await room_client.prop_read("analog-output", 1, "priority-array", index=10) == pytest.approx(20.0)
    await ahu_client.io_force("ai0", 800.0)  # 30 degC
    net.step()
    assert await room_client.prop_read("analog-output", 1) == pytest.approx(30.0)
    assert (await room_client.app_status("link"))["events"] >= 2


async def test_uc_link_retries_unreachable_source(
    pair: tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient], net: SimNetwork,
) -> None:
    ahu, _, _, room_client = pair
    ahu.online = False
    await install(room_client, "link", "/lfs/apps/uc-link.wasm",
                  {"count": "1", "l0": "1001 0 1 2 10 cov 1000 0 1 0"}, LINK_PERMS, period_ms=200)
    net.advance(0.4, step_s=0.2)
    status = await room_client.app_status("link")
    assert status["state"] == "running" and status["errors"] >= 1
    assert "does not answer" in status["last_error"]
    ahu.online = True
    net.advance(0.4, step_s=0.2)
    assert await room_client.prop_read("analog-value", 10) == pytest.approx(20.0)


async def test_uc_link_needs_remote_permission(
    pair: tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient], net: SimNetwork,
) -> None:
    _, _, _, room_client = pair
    await install(room_client, "link", "/lfs/apps/uc-link.wasm",
                  {"count": "1", "l0": "1001 0 1 2 10 poll 100 0 1 0"}, ["bacnet.local"])
    net.advance(0.5, step_s=0.1)
    status = await room_client.app_status("link")
    assert status["errors"] >= 1 and "bacnet.remote" in status["last_error"]


async def test_uc_link_bad_count_fails_start(
    pair: tuple[SimNode, SmpNodeClient, SimNode, SmpNodeClient],
) -> None:
    _, _, _, room_client = pair
    await install(room_client, "link", "/lfs/apps/uc-link.wasm", {"count": "9"}, LINK_PERMS)
    status = await room_client.app_status("link")
    assert status["state"] == "failed" and "uc_app_init returned -1" in status["last_error"]


async def test_thermostat_converges(net: SimNetwork, clock: ManualClock) -> None:
    room = RoomModel(temp_c=16.0, outdoor_c=5.0)
    sim = SimNode(name="r204-ctl", instance=2041, io=EXAMPLE_IO, room=room, network=net)
    await sim.start()
    c = make_client(sim)
    try:
        await install(c, "thermostat", "/lfs/apps/thermostat.wasm", {"setpoint": "21.5"},
                      ["bacnet.local"])
        assert await c.prop_read("analog-value", 1) == 21.5  # setpoint object
        assert await c.prop_read("analog-value", 1, "object-name") == "Setpoint"
        net.advance(3600, step_s=1.0)
        assert room.temp_c == pytest.approx(21.5, abs=0.2)
        assert await c.prop_read("analog-input", 1) == pytest.approx(21.5, abs=0.2)
        valve = await c.prop_read("analog-output", 1, "priority-array", index=12)
        assert valve == pytest.approx(100 * (21.5 - 5.0) / 36.0, abs=3)  # steady-state heat balance
        # An operator changes the setpoint at priority 8; the loop follows.
        await c.prop_write("analog-value", 1, 19.0, priority=8)
        net.advance(3600, step_s=1.0)
        assert room.temp_c == pytest.approx(19.0, abs=0.2)
        status = await c.app_status("thermostat")
        assert status["errors"] == 0 and status["ticks"] >= 7000
        await c.app_stop("thermostat")
        assert await c.prop_read("analog-output", 1, "priority-array", index=12) is None
    finally:
        await c.close()


async def test_thermostat_opens_valve_when_cold(net: SimNetwork) -> None:
    sim = SimNode(name="r204-ctl", instance=2041, io=EXAMPLE_IO, network=net)
    await sim.start()
    c = make_client(sim)
    try:
        await install(c, "thermostat", "/lfs/apps/thermostat.wasm", {"setpoint": "21.5"},
                      ["bacnet.local"])
        await c.io_force("ai0", 650.0)  # 15 degC
        net.advance(2.0, step_s=1.0)
        assert await c.prop_read("analog-output", 1) > 50
        assert (await c.io_read("ao0"))["ao0"] > 50
        await c.io_force("ai0", 800.0)  # 30 degC
        net.advance(2.0, step_s=1.0)
        assert await c.prop_read("analog-output", 1) == 0.0
    finally:
        await c.close()


async def test_thermostat_with_remote_sensor(net: SimNetwork) -> None:
    sensor = SimNode(name="sensor", instance=1001, io=EXAMPLE_IO, network=net)
    ctl = SimNode(name="ctl", instance=2041, io=[EXAMPLE_IO[3]], network=net)
    await sensor.start()
    await ctl.start()
    c = make_client(ctl)
    try:
        await install(c, "thermostat", "/lfs/apps/thermostat.wasm",
                      {"setpoint": "21.5", "sensor_device": "1001", "sensor_instance": "1"},
                      ["bacnet.local", "bacnet.remote"])
        sensor.force("ai0", 650.0)
        net.advance(2.0, step_s=1.0)
        assert await c.prop_read("analog-output", 1) > 50
        assert sensor.bacnet_packets > 0
    finally:
        await c.close()


async def test_trapping_app_fails_and_cleans_up(net: SimNetwork, monkeypatch: pytest.MonkeyPatch) -> None:
    from uc_hub.sim.network import ThermostatApp

    def boom(self: ThermostatApp, now_ms: int) -> None:
        raise RuntimeError("unreachable executed")

    monkeypatch.setattr(ThermostatApp, "tick", boom)
    sim = SimNode(name="r204-ctl", instance=2041, io=EXAMPLE_IO, network=net)
    await sim.start()
    c = make_client(sim)
    try:
        await install(c, "thermostat", "/lfs/apps/thermostat.wasm", {}, ["bacnet.local"])
        net.advance(1.0)
        status = await c.app_status("thermostat")
        assert status["state"] == "failed" and "unreachable executed" in status["last_error"]
        assert (2, 1) not in sim.objects  # its setpoint object is gone
    finally:
        await c.close()
