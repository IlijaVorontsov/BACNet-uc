from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from uc_hub.core.driver import DriverContext
from uc_hub.core.errors import DeviceTimeout, InvalidRequest, NotFound, Unsupported
from uc_hub.core.ids import PointRef
from uc_hub.core.types import DeviceRecord, PointKind, ProtocolName, Quality, Reading
from uc_hub.drivers.bacnet_uc import BacnetUcDriver
from uc_hub.drivers.bacnet_uc.api import NodeApi
from uc_hub.sim import EXAMPLE_IO, SimFeatures, SimNetwork, SimNode

from .conftest import make_client

WASM = b"\0asm\x01\0\0\0"


@dataclass
class Sink:
    readings: list[Reading] = field(default_factory=list)
    online: list[tuple[str, bool]] = field(default_factory=list)

    def publish(self, reading: Reading) -> None:
        self.readings.append(reading)

    def set_online(self, device: str, online: bool) -> None:
        self.online.append((device, online))

    def values(self, obj: str) -> list[Any]:
        return [r.value for r in self.readings if r.ref.obj == obj]


async def wait_until(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def ref(obj: str, device: str = "r204-ctl") -> PointRef:
    return PointRef("hq", device, obj)


def spec(sim: SimNode, **transport: Any) -> dict[str, Any]:
    return {
        "name": sim.name, "board": "native_sim/native/64",
        "transport": transport or {"kind": "udp", "host": "127.0.0.1", "port": sim.port},
        "device": {"instance": sim.instance, "name": sim.name},
    }


def record(name: str) -> DeviceRecord:
    return DeviceRecord(site="hq", name=name, protocol=ProtocolName.BACNET_UC, address="", space="r204")


def make_driver(sink: Sink, **settings: Any) -> BacnetUcDriver:
    base = {"timeout_s": 0.3, "retries": 1, "poll_interval_s": 0.05, "heartbeat_s": 0.05,
            "discover_broadcast": ""}
    base.update(settings)
    return BacnetUcDriver(DriverContext("hq", sink.publish, sink.set_online, base))


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
async def driver(node: SimNode, sink: Sink) -> AsyncIterator[BacnetUcDriver]:
    drv = make_driver(sink)
    await drv.add_device(record(node.name), spec(node))
    yield drv
    await drv.stop()


async def test_describe(driver: BacnetUcDriver, node: SimNode) -> None:
    c = make_client(node)
    try:
        await c.file_upload("/lfs/apps/thermostat.wasm", WASM)
        await c.app_install({"name": "thermostat", "file": "/lfs/apps/thermostat.wasm",
                             "perms": ["bacnet.local"], "params": {"setpoint": "21"}})
    finally:
        await c.close()
    desc = await driver.describe("r204-ctl")
    points = {p.ref.obj: p for p in desc.points}
    assert set(points) == {"analog-input:1", "analog-output:1", "binary-input:1",
                           "binary-output:1", "analog-value:1"}
    ai, ao = points["analog-input:1"], points["analog-output:1"]
    assert (ai.name, ai.kind, ai.datatype, ai.units) == ("Room Temperature", PointKind.INPUT, "real", "degrees-celsius")
    assert (ai.writable, ai.commandable, ai.source, ai.space) == (False, False, "io:ai0", "r204")
    assert (ao.kind, ao.units, ao.writable, ao.commandable, ao.source) == (
        PointKind.OUTPUT, "percent", True, True, "io:ao0")
    bi, bo = points["binary-input:1"], points["binary-output:1"]
    assert (bi.datatype, bi.units, bi.kind) == ("enum", None, PointKind.INPUT)
    assert (bo.datatype, bo.commandable, bo.source) == ("enum", True, "io:do0")
    av = points["analog-value:1"]
    assert (av.kind, av.writable, av.commandable, av.source, av.name) == (
        PointKind.VALUE, True, True, "app:thermostat", "Setpoint")
    assert str(ai.ref) == "hq/r204-ctl/analog-input:1"
    dev = desc.device
    assert dev.online and dev.managed
    assert (dev.instance, dev.model, dev.firmware, dev.hwid) == (2041, "native_sim/native/64", "0.1.0", node.hwid)
    assert dev.address == f"127.0.0.1:{node.port}"
    assert [a["name"] for a in desc.apps] == ["thermostat"]
    assert desc.extra["info"]["device"]["instance"] == 2041
    assert desc.to_json()["device"]["points"] == 5
    before = node.requests
    await driver.describe("r204-ctl")
    assert node.requests - before == 4  # info, objects, catalog, apps: units come from the cache


async def test_datatypes_are_canonical(net: SimNetwork, sink: Sink) -> None:
    """Analog -> real, binary -> enum (0/1), multi-state -> int (state number)."""
    io = [*EXAMPLE_IO, {"channel": "di1", "type": "multi-state-input", "instance": 1, "name": "Mode"}]
    sim = SimNode(name="r205-ctl", instance=2051, io=io, network=net)
    await sim.start()
    drv = make_driver(sink)
    try:
        await drv.add_device(record(sim.name), spec(sim))
        points = {p.ref.obj: p.datatype for p in (await drv.describe("r205-ctl")).points}
        assert points == {"analog-input:1": "real", "analog-output:1": "real", "binary-input:1": "enum",
                          "binary-output:1": "enum", "multi-state-input:1": "int"}
        (mode,) = await drv.read([ref("multi-state-input:1", "r205-ctl")])
        assert (mode.value, type(mode.value)) == (1, int)
    finally:
        await drv.stop()


async def test_describe_tolerates_units_timeouts(driver: BacnetUcDriver, node: SimNode) -> None:
    original = node.handle_datagram

    def no_units(data: bytes) -> bytes | None:
        return None if b"units" in data else original(data)

    node.handle_datagram = no_units  # type: ignore[method-assign]
    desc = await driver.describe("r204-ctl")
    assert len(desc.points) == 4
    assert all(p.units is None for p in desc.points)
    node.handle_datagram = original  # type: ignore[method-assign]
    desc = await driver.describe("r204-ctl")  # failures were not cached
    assert {p.ref.obj: p.units for p in desc.points}["analog-input:1"] == "degrees-celsius"


async def test_describe_tolerates_units_errors(driver: BacnetUcDriver, node: SimNode) -> None:
    from uc_hub.sim.smp_node import RcError

    original = node._node_prop_read

    def broken_units(body: dict[str, Any]) -> dict[str, Any]:
        if body.get("prop") == "units":
            raise RcError(7, "io error")
        return original(body)

    node._handlers[(66, 3)] = (broken_units, None)
    desc = await driver.describe("r204-ctl")
    assert all(p.units is None for p in desc.points)


async def test_read(driver: BacnetUcDriver, sink: Sink) -> None:
    refs = [ref("analog-input:1"), ref("binary-output:1"), ref("analog-value:42"),
            ref("co2.ppm"), ref("analog-input:1", device="nope")]
    readings = await driver.read(refs)
    ai, bo, missing, not_bacnet, unknown = readings
    assert ai.value == pytest.approx(20.0) and ai.quality is Quality.GOOD
    assert bo.value == 0 and isinstance(bo.value, int)
    assert missing.quality is Quality.FAULT and "NOT_FOUND" in (missing.error or "")
    assert not_bacnet.quality is Quality.FAULT
    assert unknown.quality is Quality.FAULT and "nope" in (unknown.error or "")
    assert sink.readings == readings
    assert sink.online == [("r204-ctl", True)]


async def test_read_offline_and_back(driver: BacnetUcDriver, node: SimNode, sink: Sink) -> None:
    node.online = False
    (reading,) = await driver.read([ref("analog-input:1")])
    assert reading.quality is Quality.OFFLINE and reading.value is None
    assert sink.online == [("r204-ctl", False)]
    node.online = True
    (reading,) = await driver.read([ref("analog-input:1")])
    assert reading.quality is Quality.GOOD
    assert sink.online == [("r204-ctl", False), ("r204-ctl", True)]


async def test_read_of_a_silent_node_costs_one_timeout(node: SimNode, sink: Sink) -> None:
    drv = make_driver(sink, timeout_s=0.2, retries=0)
    await drv.add_device(record(node.name), spec(node))
    original = node.handle_datagram
    sent = 0

    def count(data: bytes) -> bytes | None:
        nonlocal sent
        sent += 1
        return original(data)

    node.handle_datagram = count  # type: ignore[method-assign]
    node.online = False
    try:
        readings = await drv.read([ref("analog-input:1")] * 16)
        assert {r.quality for r in readings} == {Quality.OFFLINE}
        assert all(r.error for r in readings)
        # One wave of max_inflight requests, not 16 / 4 waves of timeouts.
        assert sent <= 4
    finally:
        await drv.stop()


async def test_bad_settings_are_rejected(sink: Sink) -> None:
    for bad in ({"read_concurrency": 0}, {"poll_interval_s": 0}, {"heartbeat_s": -1},
                {"refresh_s": 0}, {"max_inflight": 0}):
        with pytest.raises(ValueError):
            make_driver(sink, **bad)


async def test_write_relinquish_priority_array(driver: BacnetUcDriver) -> None:
    ao = ref("analog-output:1")
    result = await driver.write(ao, 42, 12)
    assert result.ok and result.priority == 12
    await driver.write(ao, 60.0, 9)
    slots = await driver.priority_array(ao)
    assert slots is not None and len(slots) == 16
    assert (slots[8], slots[11]) == (60.0, 42.0)
    assert [s for i, s in enumerate(slots) if i not in (8, 11)] == [None] * 14
    assert (await driver.read([ao]))[0].value == 60.0
    assert (await driver.relinquish(ao, 9)).ok
    assert (await driver.read([ao]))[0].value == 42.0
    bo = ref("binary-output:1")
    assert (await driver.write(bo, "on", 12)).ok
    assert (await driver.read([bo]))[0].value == 1
    assert await driver.priority_array(ref("analog-input:1")) is None
    with pytest.raises(NotFound):
        await driver.priority_array(ref("analog-value:77"))


async def test_priority_array_whole_read(net: SimNetwork, sink: Sink) -> None:
    sim = SimNode(name="new-fw", instance=5, io=EXAMPLE_IO, network=net, features=SimFeatures(array_read=True))
    await sim.start()
    drv = make_driver(sink)
    await drv.add_device(record(sim.name), spec(sim))
    try:
        ao = ref("analog-output:1", device="new-fw")
        await drv.write(ao, 12.5, 16)
        before = sim.requests
        slots = await drv.priority_array(ao)
        assert sim.requests - before == 1
        assert slots == [None] * 15 + [12.5]
    finally:
        await drv.stop()


async def test_write_failures(driver: BacnetUcDriver, node: SimNode) -> None:
    result = await driver.write(ref("analog-input:1"), 3.0, None)
    assert not result.ok and "PERM" in (result.error or "")
    with pytest.raises(InvalidRequest):
        await driver.write(ref("binary-output:1"), 2, 12)
    with pytest.raises(InvalidRequest):
        await driver.write(ref("multi-state-value:1"), 1.5, 12)
    with pytest.raises(InvalidRequest):
        await driver.write(ref("co2"), 1, None)
    with pytest.raises(NotFound):
        await driver.write(ref("analog-output:1", device="nope"), 1, None)
    node.online = False
    result = await driver.write(ref("analog-output:1"), 3.0, 12)
    assert not result.ok and "no answer" in (result.error or "")


async def test_write_with_lease(driver: BacnetUcDriver, node: SimNode, clock: Any) -> None:
    ao = ref("analog-output:1")
    assert (await driver.write(ao, 70.0, 12, lease_ms=2000)).ok
    clock.advance(3)
    assert (await driver.read([ao]))[0].value == 0.0


async def test_watch_publishes_changes_and_tracks_online(driver: BacnetUcDriver, node: SimNode,
                                                         sink: Sink) -> None:
    await driver.start()
    ai, ao = ref("analog-input:1"), ref("analog-output:1")
    await driver.watch([ai, ao])
    await wait_until(lambda: sink.values("analog-output:1") == [0.0])
    await driver.write(ao, 33.0, 12)
    await wait_until(lambda: 33.0 in sink.values("analog-output:1"))
    await asyncio.sleep(0.2)  # several polls, nothing changes
    assert sink.values("analog-input:1") == [pytest.approx(20.0)]
    node.online = False
    await wait_until(lambda: ("r204-ctl", False) in sink.online)
    await wait_until(lambda: sink.readings[-1].quality is Quality.OFFLINE)
    node.online = True
    await wait_until(lambda: sink.online[-1] == ("r204-ctl", True))
    await wait_until(lambda: sink.readings[-1].quality is Quality.GOOD)
    await driver.unwatch([ai, ao])
    count = len(sink.readings)
    await asyncio.sleep(0.2)
    assert len(sink.readings) == count


async def test_rewatch_after_unwatch_during_a_poll_publishes(driver: BacnetUcDriver, sink: Sink) -> None:
    ai = ref("analog-input:1")
    read_many = driver._read_many
    unwatched = asyncio.Event()

    async def unwatch_while_polling(refs: list[PointRef]) -> list[Reading]:
        readings = await read_many(refs)
        if not unwatched.is_set():
            await driver.unwatch([ai])  # the poll is still in flight
            unwatched.set()
        return readings

    driver._read_many = unwatch_while_polling  # type: ignore[method-assign]
    await driver.start()
    await driver.watch([ai])
    await asyncio.wait_for(unwatched.wait(), 3)
    count = len(sink.values("analog-input:1"))
    await driver.watch([ai])  # unchanged value: a new watcher still gets it
    await wait_until(lambda: len(sink.values("analog-input:1")) > count)


async def test_watch_full_refresh(node: SimNode, sink: Sink) -> None:
    drv = make_driver(sink, refresh_s=0.1)
    await drv.add_device(record(node.name), spec(node))
    try:
        await drv.watch([ref("analog-input:1")])
        await wait_until(lambda: len(sink.values("analog-input:1")) >= 3)
    finally:
        await drv.stop()


async def test_heartbeat_marks_nodes_online(driver: BacnetUcDriver, node: SimNode, sink: Sink) -> None:
    await driver.start()
    await wait_until(lambda: sink.online == [("r204-ctl", True)])
    node.online = False
    await wait_until(lambda: sink.online[-1] == ("r204-ctl", False))


async def test_identify_and_force(driver: BacnetUcDriver, node: SimNode) -> None:
    await driver.identify("r204-ctl", 10)
    assert node.identifying
    await driver.force("r204-ctl", "ai0", 650.0, lease_ms=60000)
    assert (await driver.read([ref("analog-input:1")]))[0].value == pytest.approx(15.0)
    await driver.force("r204-ctl", "ai0", None)
    assert (await driver.read([ref("analog-input:1")]))[0].value == pytest.approx(20.0)
    with pytest.raises(NotFound):
        await driver.force("r204-ctl", "zz9", 1.0)


async def test_identify_unsupported_is_explained(net: SimNetwork, sink: Sink) -> None:
    sim = SimNode(name="old-fw", instance=6, network=net, features=SimFeatures(identify=False, hwid=False))
    await sim.start()
    drv = make_driver(sink)
    await drv.add_device(record(sim.name), spec(sim))
    try:
        with pytest.raises(Unsupported, match="cannot identify"):
            await drv.identify("old-fw")
        desc = await drv.describe("old-fw")
        assert desc.device.hwid == ""  # fallback: no hardware id reported
    finally:
        await drv.stop()


async def test_node_accessor(driver: BacnetUcDriver) -> None:
    api = driver.node("r204-ctl")
    assert isinstance(api, NodeApi)
    assert (await api.info())["device"]["instance"] == 2041
    with pytest.raises(NotFound):
        driver.node("nope")


async def test_serial_transport_is_offline(sink: Sink) -> None:
    drv = make_driver(sink)
    await drv.add_device(record("r9"), {"name": "r9", "board": "nucleo_f767zi",
                                        "transport": {"kind": "serial", "device": "/dev/ttyACM0"},
                                        "device": {"instance": 9, "name": "r9"}})
    try:
        assert sink.online == [("r9", False)]
        (reading,) = await drv.read([ref("analog-input:1", device="r9")])
        assert reading.quality is Quality.OFFLINE and "serial transport" in (reading.error or "")
        with pytest.raises(DeviceTimeout, match="not implemented"):
            await drv.describe("r9")
        with pytest.raises(DeviceTimeout):
            drv.node("r9")
        result = await drv.write(ref("analog-output:1", device="r9"), 1.0, 12)
        assert not result.ok and "serial" in (result.error or "")
        await drv.start()
        await drv.watch([ref("analog-input:1", device="r9")])
    finally:
        await drv.stop()


async def test_sim_transport_addresses(node: SimNode, sink: Sink) -> None:
    drv = make_driver(sink, sim_addresses={"r204-ctl": node.address, "r205-ctl": ("127.0.0.1", node.port)})
    await drv.add_device(record("r204-ctl"), spec(node, kind="sim"))
    await drv.add_device(record("r205-ctl"), {**spec(node, kind="sim"), "name": "r205-ctl"})
    await drv.add_device(record("r206-ctl"), {**spec(node, kind="sim"), "name": "r206-ctl"})
    try:
        (reading,) = await drv.read([ref("analog-input:1")])
        assert reading.quality is Quality.GOOD
        (reading,) = await drv.read([ref("analog-input:1", device="r205-ctl")])
        assert reading.quality is Quality.GOOD
        (reading,) = await drv.read([ref("analog-input:1", device="r206-ctl")])
        assert reading.quality is Quality.OFFLINE and "sim_addresses" in (reading.error or "")
    finally:
        await drv.stop()


async def test_discover(net: SimNetwork, node: SimNode, sink: Sink) -> None:
    stranger = SimNode(name="new-board", instance=3001, network=net)
    old = SimNode(name="old-board", instance=3002, network=net, features=SimFeatures(hwid=False))
    await stranger.start()
    await old.start()
    drv = make_driver(sink, discover_broadcast=[stranger.address, old.address, "not a host:1"])
    await drv.add_device(record(node.name), spec(node))
    try:
        found = {d.address: d for d in await drv.discover(timeout_s=0.3)}
        assert set(found) == {node.address, stranger.address, old.address}
        new = found[stranger.address]
        assert (new.instance, new.name, new.model, new.bacnet_uc) == (3001, "new-board", "native_sim/native/64", True)
        assert new.hwid == stranger.hwid and new.protocol is ProtocolName.BACNET_UC
        assert new.extra["fw"] == "0.1.0"
        assert found[old.address].hwid == ""
    finally:
        await drv.stop()


async def test_discover_survives_lost_first_answer(net: SimNetwork, sink: Sink) -> None:
    shy = SimNode(name="shy", instance=3003, network=net)
    await shy.start()
    shy.drop = 1
    drv = make_driver(sink, discover_broadcast=shy.address)
    try:
        found = await drv.discover(timeout_s=0.4)
        assert [d.instance for d in found] == [3003]
    finally:
        await drv.stop()


async def test_remove_device_and_stop(driver: BacnetUcDriver, node: SimNode) -> None:
    await driver.start()
    await driver.watch([ref("analog-input:1")])
    await driver.remove_device("r204-ctl")
    with pytest.raises(NotFound):
        await driver.describe("r204-ctl")
    await driver.stop()
