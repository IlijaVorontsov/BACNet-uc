"""The simulated AHU controller's own behaviour."""

from __future__ import annotations

import pytest

from uc_hub.core.errors import InvalidRequest
from uc_hub.core.types import Quality
from uc_hub.drivers.bacnet_ip import BacnetIpDriver
from uc_hub.sim.bacnet_ip_device import MODES, SimBacnetIpDevice

from .conftest import Recorder, add_sim, make_driver, ref

pytestmark = pytest.mark.network


async def test_binds_a_free_port_and_stops_cleanly() -> None:
    device = SimBacnetIpDevice()
    assert device.port == 0
    await device.start()
    await device.start()
    try:
        assert device.port > 0 and device.address == f"127.0.0.1:{device.port}"
    finally:
        await device.stop()
        await device.stop()
    assert device.app is None


async def test_address_of_a_device_on_all_interfaces() -> None:
    async with SimBacnetIpDevice(host="0.0.0.0") as device:
        assert device.address == f"127.0.0.1:{device.port}"


async def test_supply_air_temp_drifts(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=True, tick_s=0.05) as device:
        driver = make_driver(rec)
        try:
            await add_sim(driver, device)
            await driver.start()
            await driver.watch([ref("analog-value:3")])

            def drifted() -> bool:
                last = rec.latest("analog-value:3")
                return last is not None and last.quality == Quality.GOOD and last.value not in (None, 18.0)

            await rec.wait(drifted, timeout=3.0)
            value = rec.latest("analog-value:3").value  # type: ignore[union-attr]
            assert isinstance(value, float) and 18.0 < value < 19.5
        finally:
            await driver.stop()


async def test_fan_status_follows_the_fan_commands(ahu: BacnetIpDriver, rec: Recorder) -> None:
    await ahu.watch([ref("binary-input:1")])
    await rec.wait_value("binary-input:1", 0)
    assert (await ahu.write(ref("analog-output:1"), 60.0, 12)).ok
    await rec.wait_value("binary-input:1", 1)
    assert (await ahu.write(ref("binary-output:1"), "inactive", 8)).ok  # stop
    await rec.wait_value("binary-input:1", 0)
    assert (await ahu.relinquish(ref("binary-output:1"), 8)).ok  # back to the controller: start
    await rec.wait_value("binary-input:1", 1)
    assert (await ahu.relinquish(ref("analog-output:1"), 12)).ok
    await rec.wait_value("binary-input:1", 0)


async def test_supply_air_temp_drifts_around_the_setpoint(rec: Recorder) -> None:
    async with SimBacnetIpDevice(drift=True, tick_s=0.05) as device:
        driver = make_driver(rec)
        try:
            await add_sim(driver, device)
            await driver.start()
            assert (await driver.write(ref("analog-value:4"), 25.0, 12)).ok
            await driver.watch([ref("analog-value:3")])

            def near_setpoint() -> bool:
                last = rec.latest("analog-value:3")
                return last is not None and isinstance(last.value, float) and 23.5 <= last.value <= 26.5

            await rec.wait(near_setpoint, timeout=3.0)
        finally:
            await driver.stop()


async def test_measurements_are_read_only(ahu: BacnetIpDriver) -> None:
    for obj in ("analog-input:1", "analog-value:3", "binary-input:1"):
        result = await ahu.write(ref(obj), 1, None)
        assert not result.ok and "write-access-denied" in (result.error or ""), obj


async def test_mode_states(ahu: BacnetIpDriver, sim: SimBacnetIpDevice) -> None:
    assert len(MODES) == 4
    for state in (1, 4):
        assert (await ahu.write(ref("multi-state-value:1"), state, None)).ok
        assert int(sim.mode.presentValue) == state
    result = await ahu.write(ref("multi-state-value:1"), 5, None)
    assert not result.ok and "value-out-of-range" in (result.error or "")
    with pytest.raises(InvalidRequest):  # state numbers start at 1; refused before sending
        await ahu.write(ref("multi-state-value:1"), 0, None)
