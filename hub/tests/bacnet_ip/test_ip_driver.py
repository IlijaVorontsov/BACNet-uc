"""The BACnet/IP driver against simulated devices over real UDP on loopback."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import socket
import time
from typing import Any

import pytest
from bacpypes3.apdu import ReadPropertyMultipleRequest, ReadPropertyRequest, SubscribeCOVRequest
from bacpypes3.basetypes import Reliability
from bacpypes3.local.analog import AnalogValueObject
from bacpypes3.primitivedata import Null

from uc_hub.core.errors import DeviceTimeout, HubError, InvalidRequest, NotFound
from uc_hub.core.types import DeviceRecord, PointKind, ProtocolName, Quality
from uc_hub.drivers.bacnet_ip import BacnetIpDriver
from uc_hub.sim.bacnet_ip_device import SimBacnetIpDevice

from .conftest import DEVICE, Recorder, add_sim, make_driver, ref

pytestmark = pytest.mark.network


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class NullPresentValue(AnalogValueObject):
    """An analog value whose present value reads as NULL, as some devices
    answer for a point without a value: bacpypes3 cannot decode that inside
    a ReadPropertyMultiple answer (it can in a ReadProperty answer)."""

    async def read_property(self, attr: Any, index: int | None = None) -> Any:
        if attr in ("presentValue", "present-value", 85):
            return Null(())
        return await super().read_property(attr, index)


def count_rpm(sim: SimBacnetIpDevice) -> list[int]:
    """Count the ReadPropertyMultiple requests ``sim`` receives."""
    assert sim.app is not None
    original = sim.app.do_ReadPropertyMultipleRequest
    count = [0]

    async def counted(apdu: ReadPropertyMultipleRequest) -> None:
        count[0] += 1
        await original(apdu)

    sim.app.do_ReadPropertyMultipleRequest = counted  # type: ignore[method-assign]
    return count


# -- describe -------------------------------------------------------------------------
async def test_describe_maps_point_objects(ahu: BacnetIpDriver, rec: Recorder) -> None:
    desc = await ahu.describe(DEVICE)
    points = {p.ref.obj: p for p in desc.points}
    assert set(points) == {"analog-input:1", "analog-value:3", "analog-value:4", "analog-output:1",
                           "binary-input:1", "binary-output:1", "binary-output:9", "multi-state-value:1"}

    sat = points["analog-value:3"]
    assert (sat.name, sat.kind, sat.datatype, sat.units) == ("Supply Air Temp", PointKind.VALUE, "real",
                                                            "degrees-celsius")
    assert sat.description == "Supply air temperature after the heating coil"
    assert sat.writable and not sat.commandable
    assert str(sat.ref) == "hq/ahu1-ctl/analog-value:3"
    setpoint = points["analog-value:4"]  # a value object with a priority array
    assert (setpoint.kind, setpoint.units, setpoint.writable, setpoint.commandable) == (
        PointKind.VALUE, "degrees-celsius", True, True)
    fan = points["analog-output:1"]
    assert (fan.kind, fan.units, fan.writable, fan.commandable) == (PointKind.OUTPUT, "percent", True, True)
    damper = points["binary-output:9"]
    assert (damper.datatype, damper.units, damper.commandable) == ("enum", None, True)
    mode = points["multi-state-value:1"]
    assert (mode.datatype, mode.writable, mode.commandable) == ("int", True, False)
    oat = points["analog-input:1"]
    assert (oat.kind, oat.writable, oat.commandable) == (PointKind.INPUT, False, False)
    assert points["binary-input:1"].kind is PointKind.INPUT

    device = desc.device
    assert (device.instance, device.model, device.firmware, device.online) == (100, "SIM-AHU-200", "2.4.1", True)
    assert device.address.startswith("127.0.0.1:")
    assert desc.extra["vendor"] == "Simulated Controls"
    assert desc.extra["skipped_objects"] == {"notification-class": 1}
    assert desc.extra["object_count"] == 10 and desc.extra["truncated"] is False
    assert desc.extra["read_property_multiple"] is True
    assert desc.to_json()["device"]["points"] == 8

    assert rec.online == [(DEVICE, True)]
    assert rec.latest("analog-value:3") is not None and rec.latest("analog-value:3").value == 18.0  # type: ignore[union-attr]


async def test_describe_learns_the_instance(driver: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    record = await add_sim(driver, sim, device_instance=None)
    assert record.instance is None
    await driver.start()
    desc = await driver.describe(DEVICE)
    assert record.instance == 100 and len(desc.points) == 8


async def test_describe_with_allow_list(driver: BacnetIpDriver, sim: SimBacnetIpDevice, rec: Recorder) -> None:
    await add_sim(driver, sim, points=[
        {"obj": "analog-value:3", "name": "SAT"},
        {"obj": "analog-output:1", "units": "percent-per-second"},
        {"obj": "analog-value:77"},
        {"obj": "not-a-point"},
    ])
    await driver.start()
    desc = await driver.describe(DEVICE)
    points = {p.ref.obj: p for p in desc.points}
    assert list(points) == ["analog-value:3", "analog-output:1", "analog-value:77"]
    assert points["analog-value:3"].name == "SAT"
    assert points["analog-output:1"].name == "Supply Fan Speed"
    assert points["analog-output:1"].units == "percent-per-second"
    assert desc.extra["missing_objects"] == ["analog-value:77"]

    other, allowed = await driver.read([ref("binary-output:9"), ref("analog-value:3")])
    assert other.quality == Quality.FAULT and "allow-list" in (other.error or "")
    assert allowed.value == 18.0
    with pytest.raises(NotFound):
        await driver.write(ref("binary-output:9"), 1, 12)


async def test_describe_reads_a_long_object_list_by_index(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=False, segmentation=False, max_apdu=480, extra_objects=300) as big:
        driver = make_driver(rec)
        try:
            await add_sim(driver, big)
            await driver.start()
            desc = await driver.describe(DEVICE)
        finally:
            await driver.stop()
    assert len(desc.points) == 256
    assert desc.extra["truncated"] is True
    assert desc.extra["object_count"] == 10 + 300
    assert desc.extra["segmentation"] == "no-segmentation"
    assert rec.online == [(DEVICE, True)]


async def test_describe_unknown_device(ahu: BacnetIpDriver) -> None:
    with pytest.raises(NotFound):
        await ahu.describe("nope")


# -- read -----------------------------------------------------------------------------
async def test_read_values_and_per_point_errors(ahu: BacnetIpDriver, rec: Recorder) -> None:
    refs = [ref("analog-value:3"), ref("analog-output:1"), ref("binary-output:9"), ref("multi-state-value:1"),
            ref("binary-input:1"), ref("analog-value:99"), ref("notification-class:1"), ref("co2"),
            ref("analog-value:3", device="nope"), ref("analog-value:3")]
    readings = await ahu.read(refs)
    assert [r.ref for r in readings] == refs
    values = [(r.value, r.quality) for r in readings]
    assert values[:5] == [(18.0, Quality.GOOD), (0.0, Quality.GOOD), (0, Quality.GOOD), (2, Quality.GOOD),
                          (0, Quality.GOOD)]
    assert isinstance(readings[0].value, float) and type(readings[2].value) is int
    assert readings[5].quality == Quality.FAULT and "unknown-object" in (readings[5].error or "")
    assert readings[6].quality == Quality.FAULT and "not points" in (readings[6].error or "")
    assert readings[7].quality == Quality.FAULT
    assert readings[8].quality == Quality.FAULT and "unknown bacnet-ip device" in (readings[8].error or "")
    assert readings[9].value == 18.0
    assert rec.readings[-len(refs):] == readings


async def test_read_falls_back_to_read_property(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=False, rpm=False) as plain:
        driver = make_driver(rec)
        try:
            await add_sim(driver, plain)
            await driver.start()
            first = await driver.read([ref("analog-value:3"), ref("analog-value:99")])
            again = await driver.read([ref("multi-state-value:1")])
            desc = await driver.describe(DEVICE)
        finally:
            await driver.stop()
    assert first[0].value == 18.0 and first[1].quality == Quality.FAULT
    assert again[0].value == 2
    assert desc.extra["read_property_multiple"] is False and len(desc.points) == 8
    assert {p.ref.obj for p in desc.points if p.commandable} == {"analog-value:4", "analog-output:1",
                                                                 "binary-output:1", "binary-output:9"}


async def test_read_shrinks_batches_the_device_cannot_answer(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=False, segmentation=False, max_apdu=480, extra_objects=300) as big:
        driver = make_driver(rec, rpm_max_properties=1024)
        try:
            await add_sim(driver, big)
            await driver.start()
            refs = [ref(f"analog-value:{1000 + i}") for i in range(300)]
            readings = await driver.read(refs)
        finally:
            await driver.stop()
    assert [r.value for r in readings] == [float(i) for i in range(300)]


async def test_an_undecodable_value_does_not_shrink_later_batches(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    assert sim.app is not None
    sim.app.add_object(NullPresentValue(objectIdentifier=("analog-value", 50), objectName="No Value",
                                        presentValue=1.0, covIncrement=1.0))
    refs = [ref("analog-value:3"), ref("analog-value:50"), ref("analog-output:1"), ref("binary-output:9")]
    readings = await ahu.read(refs)
    assert [(r.value, r.quality) for r in readings] == [
        (18.0, Quality.GOOD), (None, Quality.GOOD), (0.0, Quality.GOOD), (0, Quality.GOOD)]

    requests = count_rpm(sim)
    good = [ref(obj) for obj in ("analog-value:3", "analog-value:4", "analog-output:1", "binary-output:1",
                                 "binary-output:9", "multi-state-value:1", "analog-input:1", "binary-input:1")]
    assert all(r.quality == Quality.GOOD for r in await ahu.read(good))
    assert requests[0] == 1  # 16 properties in one request, as before the bad value


async def test_read_before_start_is_offline(driver: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    await add_sim(driver, sim)
    (reading,) = await driver.read([ref("analog-value:3")])
    assert reading.quality == Quality.OFFLINE and "not started" in (reading.error or "")
    with pytest.raises(HubError):
        await driver.describe(DEVICE)


# -- write and priority array -------------------------------------------------------
async def test_write_with_priority_and_relinquish(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    fan = ref("analog-output:1")
    result = await ahu.write(fan, 55.5, 12)
    assert (result.ok, result.value, result.priority, result.error) == (True, 55.5, 12, None)
    assert (await ahu.read([fan]))[0].value == 55.5
    slots = await ahu.priority_array(fan)
    assert slots is not None and len(slots) == 16
    assert slots[11] == 55.5 and slots.count(None) == 15

    assert (await ahu.write(fan, 30, 8)).ok
    assert (await ahu.read([fan]))[0].value == 30.0
    relinquished = await ahu.relinquish(fan, 8)
    assert relinquished.ok and relinquished.value is None and relinquished.priority == 8
    assert (await ahu.read([fan]))[0].value == 55.5
    assert (await ahu.relinquish(fan, 12)).ok
    assert (await ahu.read([fan]))[0].value == 0.0
    assert await ahu.priority_array(fan) == [None] * 16
    assert float(sim.fan_speed.presentValue) == 0.0


async def test_write_binary_output(ahu: BacnetIpDriver) -> None:
    damper = ref("binary-output:9")
    assert (await ahu.write(damper, "active", 8)).ok
    assert (await ahu.read([damper]))[0].value == 1
    slots = await ahu.priority_array(damper)
    assert slots is not None and slots[7] == 1
    assert (await ahu.write(damper, False, 8)).ok
    assert (await ahu.read([damper]))[0].value == 0
    assert (await ahu.relinquish(damper, 8)).ok
    assert await ahu.priority_array(damper) == [None] * 16


async def test_write_commandable_value_object(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    await ahu.describe(DEVICE)
    setpoint = ref("analog-value:4")
    result = await ahu.write(setpoint, 16.5, 12)
    assert (result.ok, result.priority) == (True, 12)
    slots = await ahu.priority_array(setpoint)
    assert slots is not None and slots[11] == 16.5 and slots.count(None) == 15
    assert (await ahu.read([setpoint]))[0].value == 16.5
    assert (await ahu.relinquish(setpoint, 12)).ok
    assert await ahu.priority_array(setpoint) == [None] * 16
    assert float(sim.supply_air_setpoint.presentValue) == 18.0  # the relinquish default


async def test_write_non_commandable_value(ahu: BacnetIpDriver) -> None:
    await ahu.describe(DEVICE)
    mode = ref("multi-state-value:1")
    result = await ahu.write(mode, 3, 12)
    assert result.ok and result.priority is None
    assert (await ahu.read([mode]))[0].value == 3
    with pytest.raises(InvalidRequest):
        await ahu.relinquish(mode, 12)


async def test_write_errors(ahu: BacnetIpDriver, rec: Recorder) -> None:
    denied = await ahu.write(ref("analog-value:3"), 20.0, 12)
    assert not denied.ok and "write-access-denied" in (denied.error or "")
    out_of_range = await ahu.write(ref("multi-state-value:1"), 7, None)
    assert not out_of_range.ok and "value-out-of-range" in (out_of_range.error or "")
    missing = await ahu.write(ref("analog-output:5"), 1.0, 12)
    assert not missing.ok and "unknown-object" in (missing.error or "")
    with pytest.raises(InvalidRequest):
        await ahu.write(ref("analog-output:1"), "hot", 12)
    with pytest.raises(InvalidRequest):
        await ahu.write(ref("analog-output:1"), 1.0, 17)
    with pytest.raises(InvalidRequest):
        await ahu.write(ref("analog-output:1"), 1.0, "12")  # type: ignore[arg-type]
    with pytest.raises(NotFound):
        await ahu.write(ref("analog-output:1", device="nope"), 1.0, 12)
    with pytest.raises(NotFound):
        await ahu.write(ref("device:100"), 1.0, 12)
    assert rec.online == [(DEVICE, True)]


async def test_priority_array_of_non_commandable_points(ahu: BacnetIpDriver) -> None:
    assert await ahu.priority_array(ref("analog-value:3")) is None
    assert await ahu.priority_array(ref("multi-state-value:1")) is None
    assert await ahu.priority_array(ref("analog-input:1")) is None


# -- live values ------------------------------------------------------------------------
async def test_watch_publishes_cov_notifications(ahu: BacnetIpDriver, sim: SimBacnetIpDevice, rec: Recorder) -> None:
    await ahu.watch([ref("analog-value:3"), ref("analog-output:1")])
    await rec.wait_value("analog-value:3", 18.0)
    await rec.wait_value("analog-output:1", 0.0)
    sim.set_supply_air_temp(21.5)
    reading = await rec.wait_value("analog-value:3", 21.5, timeout=2.0)
    assert reading.quality == Quality.GOOD
    assert (await ahu.write(ref("analog-output:1"), 40.0, 12)).ok
    await rec.wait_value("analog-output:1", 40.0, timeout=2.0)
    # refresh_s is 30 s and neither point is polled: only COV can explain these.


async def test_cov_refused_falls_back_to_polling(ahu: BacnetIpDriver, sim: SimBacnetIpDevice, rec: Recorder) -> None:
    await ahu.watch([ref("multi-state-value:1")])
    await rec.wait_value("multi-state-value:1", 2)
    sim.set_mode(4)
    await rec.wait_value("multi-state-value:1", 4, timeout=2.0)


async def test_device_without_cov_is_polled(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=False, cov=False) as no_cov:
        driver = make_driver(rec)
        try:
            await add_sim(driver, no_cov)
            await driver.start()
            await driver.watch([ref("analog-value:3"), ref("binary-output:9")])
            await rec.wait_value("analog-value:3", 18.0)
            no_cov.set_supply_air_temp(19.25)
            await rec.wait_value("analog-value:3", 19.25, timeout=2.0)
            count = len(rec.readings)
            await asyncio.sleep(0.35)
            # Polls publish changes only.
            assert all(r.ref.obj != "binary-output:9" for r in rec.readings[count:])
        finally:
            await driver.stop()


async def test_cov_subscriptions_are_renewed(rec: Recorder, sim: SimBacnetIpDevice) -> None:
    driver = make_driver(rec, cov_lifetime_s=1)
    try:
        await add_sim(driver, sim)
        await driver.start()
        await driver.watch([ref("analog-value:3")])
        await rec.wait_value("analog-value:3", 18.0)
        await asyncio.sleep(1.6)  # past the lifetime: without renewal the device forgets us
        sim.set_supply_air_temp(17.0)
        await rec.wait_value("analog-value:3", 17.0, timeout=2.0)
    finally:
        await driver.stop()


async def test_confirmed_cov_notifications(rec: Recorder, sim: SimBacnetIpDevice) -> None:
    driver = make_driver(rec, cov_confirmed=True)
    try:
        await add_sim(driver, sim)
        await driver.start()
        await driver.watch([ref("analog-value:3")])
        await rec.wait_value("analog-value:3", 18.0)
        sim.set_supply_air_temp(22.0)
        await rec.wait_value("analog-value:3", 22.0, timeout=2.0)
    finally:
        await driver.stop()


async def test_unwatch_stops_updates(ahu: BacnetIpDriver, sim: SimBacnetIpDevice, rec: Recorder) -> None:
    await ahu.watch([ref("analog-value:3"), ref("multi-state-value:1")])
    await rec.wait_value("analog-value:3", 18.0)
    await rec.wait_value("multi-state-value:1", 2)
    assert sim.cov_subscriptions() == [(f"{ahu.local_address[0]}:{ahu.local_address[1]}",  # type: ignore[index]
                                        "analog-value:3")]
    await ahu.unwatch([ref("analog-value:3"), ref("multi-state-value:1")])
    assert sim.cov_subscriptions() == []
    count = len(rec.readings)
    sim.set_supply_air_temp(25.0)
    sim.set_mode(1)
    await asyncio.sleep(0.4)
    assert rec.readings[count:] == []


async def test_a_fault_in_status_flags_is_a_fault_reading(ahu: BacnetIpDriver, sim: SimBacnetIpDevice,
                                                         rec: Recorder) -> None:
    sim.supply_air_temp.reliability = Reliability.overRange
    (reading,) = await ahu.read([ref("analog-value:3")])
    assert (reading.value, reading.quality) == (18.0, Quality.FAULT) and "fault" in (reading.error or "")
    await ahu.watch([ref("analog-value:3")])
    sim.supply_air_temp.reliability = Reliability.noFaultDetected
    sim.set_supply_air_temp(18.5)  # a notification with the cleared flags
    reading = await rec.wait_value("analog-value:3", 18.5, timeout=2.0)
    assert reading.error is None


async def test_unwatch_while_subscribing_cancels_the_subscription(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    """The last unwatch cancels the monitor while the device is still
    creating the subscription: it must not stay in the device's table."""
    assert sim.app is not None
    original = sim.app.do_SubscribeCOVRequest
    arrived = asyncio.Event()

    async def slow(apdu: SubscribeCOVRequest) -> None:
        arrived.set()
        await asyncio.sleep(0.2)
        await original(apdu)

    sim.app.do_SubscribeCOVRequest = slow  # type: ignore[method-assign]
    await ahu.watch([ref("analog-value:3")])
    await asyncio.wait_for(arrived.wait(), 2.0)
    await ahu.unwatch([ref("analog-value:3")])
    await asyncio.sleep(0.4)  # both delayed requests are handled by now
    assert sim.cov_subscriptions() == []


async def test_stop_ends_requests_in_flight(rec: Recorder) -> None:
    """Stopping the driver fails the requests in flight at once, as OFFLINE
    readings, without calling the device offline, and bacpypes3 does not
    retry them on the closed socket."""
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    driver = make_driver(rec, timeout_s=0.5, retries=3)
    try:
        await driver.add_device(DeviceRecord("hq", "gone", ProtocolName.BACNET_IP, ""),
                                {"address": f"127.0.0.1:{free_port()}", "device_instance": 5})
        await driver.start()
        pending = asyncio.create_task(driver.read([ref("analog-value:1", device="gone")]))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        await driver.stop()
        (reading,) = await asyncio.wait_for(pending, 1.5)
        assert time.monotonic() - started < 1.0  # not the 2 s bacpypes3 would retry for
        assert reading.quality == Quality.OFFLINE and "stopped" in (reading.error or "")
        assert rec.online == []
        await asyncio.sleep(0.6)  # past the first retry
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []
    finally:
        await driver.stop()
        loop.set_exception_handler(None)


async def test_stop_during_read_property_fallback(rec: Recorder) -> None:
    """Requests that run side by side (ReadProperty, one per property) end
    as one OFFLINE reading each when the driver stops, not as an error."""
    async with SimBacnetIpDevice(drift=False, rpm=False) as plain:
        assert plain.app is not None
        original = plain.app.do_ReadPropertyRequest

        async def slow(apdu: ReadPropertyRequest) -> None:
            await asyncio.sleep(0.3)
            await original(apdu)

        plain.app.do_ReadPropertyRequest = slow  # type: ignore[method-assign]
        driver = make_driver(rec, timeout_s=1.0)
        try:
            await add_sim(driver, plain)
            await driver.start()
            await driver.read([ref("analog-value:3")])  # learns that the device has no RPM
            pending = asyncio.create_task(driver.read([ref("analog-value:3"), ref("analog-output:1")]))
            await asyncio.sleep(0.1)
            await driver.stop()
            readings = await asyncio.wait_for(pending, 1.0)
        finally:
            await driver.stop()
    assert [r.quality for r in readings] == [Quality.OFFLINE, Quality.OFFLINE]


async def test_watch_rejects_bad_refs(ahu: BacnetIpDriver, rec: Recorder) -> None:
    await ahu.watch([ref("co2"), ref("analog-value:3", device="nope")])
    assert [r.quality for r in rec.readings] == [Quality.FAULT, Quality.FAULT]


async def test_watched_device_goes_offline_and_recovers(rec: Recorder) -> None:
    device = SimBacnetIpDevice(drift=False)
    await device.start()
    port = device.port
    driver = make_driver(rec, refresh_s=0.3)
    try:
        await add_sim(driver, device)
        await driver.start()
        await driver.watch([ref("analog-value:3"), ref("multi-state-value:1")])
        await rec.wait_value("analog-value:3", 18.0)
        await device.stop()

        def offline() -> bool:
            last = rec.latest("analog-value:3")
            return last is not None and last.quality == Quality.OFFLINE

        await rec.wait(offline, timeout=3.0)
        assert rec.online[-1] == (DEVICE, False)
        assert rec.latest("multi-state-value:1").quality == Quality.OFFLINE  # type: ignore[union-attr]

        device = SimBacnetIpDevice(drift=False, port=port)
        await device.start()
        device.set_supply_air_temp(16.5)
        await rec.wait_value("analog-value:3", 16.5, timeout=3.0)
        await rec.wait_value("multi-state-value:1", 2, timeout=3.0)
        assert rec.online[-1] == (DEVICE, True)
        device.set_supply_air_temp(16.0)  # the subscription was renewed
        await rec.wait_value("analog-value:3", 16.0, timeout=2.0)
    finally:
        await driver.stop()
        await device.stop()


# -- offline and unreachable devices -------------------------------------------------
async def test_offline_device(driver: BacnetIpDriver, rec: Recorder) -> None:
    record = DeviceRecord("hq", "gone", ProtocolName.BACNET_IP, "")
    await driver.add_device(record, {"address": f"127.0.0.1:{free_port()}", "device_instance": 5})
    await driver.start()
    (reading,) = await driver.read([ref("analog-value:1", device="gone")])
    assert reading.quality == Quality.OFFLINE and "did not answer" in (reading.error or "")
    assert rec.online == [("gone", False)] and record.online is False
    with pytest.raises(DeviceTimeout):
        await driver.describe("gone")
    result = await driver.write(ref("analog-output:1", device="gone"), 10.0, 12)
    assert not result.ok and "did not answer" in (result.error or "")
    with pytest.raises(DeviceTimeout):
        await driver.priority_array(ref("analog-output:1", device="gone"))
    assert rec.online == [("gone", False)]


async def test_device_with_a_bad_address(driver: BacnetIpDriver, rec: Recorder) -> None:
    await driver.add_device(DeviceRecord("hq", "bad", ProtocolName.BACNET_IP, ""), {"address": "ahu.local"})
    await driver.start()
    assert rec.online == [("bad", False)]
    (reading,) = await driver.read([ref("analog-value:1", device="bad")])
    assert reading.quality == Quality.OFFLINE and "bad address" in (reading.error or "")
    await driver.watch([ref("analog-value:1", device="bad")])
    assert rec.readings[-1].quality == Quality.OFFLINE
    with pytest.raises(DeviceTimeout):
        await driver.describe("bad")


# -- discovery ------------------------------------------------------------------------
async def test_discover_manifest_devices(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    (found,) = await ahu.discover(timeout_s=0.3)
    assert found.protocol is ProtocolName.BACNET_IP
    assert (found.address, found.instance) == (sim.address, 100)
    assert (found.name, found.model) == ("AHU-1 Controller", "SIM-AHU-200")
    assert found.extra["vendor"] == "Simulated Controls" and found.extra["vendor_id"] == 999


async def test_discover_targets(rec: Recorder, sim: SimBacnetIpDevice) -> None:
    async with SimBacnetIpDevice(drift=False, instance=101, name="VAV-3") as other:
        driver = make_driver(rec, discover_targets=[sim.address, other.address])
        try:
            await driver.start()
            found = await driver.discover(timeout_s=0.3)
        finally:
            await driver.stop()
    assert [(d.instance, d.name) for d in found] == [(100, "AHU-1 Controller"), (101, "VAV-3")]


async def test_discover_by_broadcast_on_loopback(rec: Recorder) -> None:
    """Broadcasts on loopback reach sockets bound to 0.0.0.0 on the
    destination port, so the device binds 0.0.0.0 and the hub broadcasts to
    127.255.255.255 on the device's port."""
    async with SimBacnetIpDevice(host="0.0.0.0", drift=False) as device:
        driver = make_driver(rec, broadcast=f"127.255.255.255:{device.port}")
        try:
            await driver.start()
            found = await driver.discover(timeout_s=0.3)
        finally:
            await driver.stop()
    assert [(d.instance, d.name) for d in found] == [(100, "AHU-1 Controller")]


# -- life cycle -------------------------------------------------------------------------
async def test_start_stop_releases_the_socket(rec: Recorder) -> None:
    driver = make_driver(rec)
    await driver.start()
    await driver.start()
    local = driver.local_address
    assert local is not None and local[0] == "127.0.0.1" and local[1] > 0
    await driver.stop()
    await driver.stop()
    assert driver.local_address is None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(local)


async def test_a_cancelled_start_leaves_nothing_behind(rec: Recorder) -> None:
    """Cancelled while bacpypes3 is still creating the transport in a task
    of its own: the transport must not stay registered on the closed file
    descriptor, which the next socket gets."""
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        for spins in range(1, 4):
            driver = make_driver(rec)
            starting = asyncio.create_task(driver.start())
            for _ in range(spins):
                await asyncio.sleep(0)
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await starting
            await driver.stop()
            other = make_driver(rec, timeout_s=0.5)
            async with asyncio.timeout(1.0):
                await other.start()
            await other.stop()
        assert errors == []
    finally:
        loop.set_exception_handler(None)


async def test_port_in_use_fails_loudly(rec: Recorder) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        driver = make_driver(rec, port=s.getsockname()[1])
        with pytest.raises(HubError, match="cannot open BACnet/IP"):
            await driver.start()


async def test_remove_device(ahu: BacnetIpDriver, sim: SimBacnetIpDevice, rec: Recorder) -> None:
    await ahu.watch([ref("analog-value:3")])
    await rec.wait_value("analog-value:3", 18.0)
    await rec.wait(lambda: sim.cov_subscriptions() != [])
    await ahu.remove_device(DEVICE)
    assert sim.cov_subscriptions() == []
    with pytest.raises(NotFound):
        await ahu.describe(DEVICE)
    (reading,) = await ahu.read([ref("analog-value:3")])
    assert reading.quality == Quality.FAULT
