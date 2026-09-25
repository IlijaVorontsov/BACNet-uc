"""The driver and simulated devices as separate bacpypes3 applications on
127.0.0.1, each on its own ephemeral port."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from uc_hub.core.driver import DriverContext
from uc_hub.core.ids import PointRef
from uc_hub.core.types import DeviceRecord, ProtocolName, Quality, Reading, Value
from uc_hub.drivers.bacnet_ip import BacnetIpDriver
from uc_hub.sim.bacnet_ip_device import SimBacnetIpDevice

SITE = "hq"
DEVICE = "ahu1-ctl"

FAST = {
    "interface": "127.0.0.1",
    "port": 0,
    "timeout_s": 0.3,
    "retries": 1,
    "poll_interval_s": 0.1,
    "refresh_s": 30.0,
    "cov_lifetime_s": 60,
}


def ref(obj: str, device: str = DEVICE) -> PointRef:
    return PointRef(SITE, device, obj)


class Recorder:
    """The site runtime's side of ``DriverContext``."""

    def __init__(self) -> None:
        self.readings: list[Reading] = []
        self.online: list[tuple[str, bool]] = []

    def publish(self, reading: Reading) -> None:
        self.readings.append(reading)

    def set_online(self, device: str, online: bool) -> None:
        self.online.append((device, online))

    def latest(self, obj: str) -> Reading | None:
        for reading in reversed(self.readings):
            if reading.ref.obj == obj:
                return reading
        return None

    async def wait(self, predicate: Callable[[], bool], timeout: float = 3.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)

    async def wait_value(self, obj: str, value: Value, timeout: float = 3.0) -> Reading:
        def seen() -> bool:
            last = self.latest(obj)
            return last is not None and last.quality == Quality.GOOD and last.value == value

        await self.wait(seen, timeout)
        reading = self.latest(obj)
        assert reading is not None
        return reading


def make_driver(rec: Recorder, **settings: Any) -> BacnetIpDriver:
    ctx = DriverContext(site=SITE, publish=rec.publish, set_online=rec.set_online,
                        settings={**FAST, **settings})
    return BacnetIpDriver(ctx)


async def add_sim(driver: BacnetIpDriver, sim: SimBacnetIpDevice, name: str = DEVICE, **spec: Any) -> DeviceRecord:
    record = DeviceRecord(SITE, name, ProtocolName.BACNET_IP, "")
    entry: dict[str, Any] = {"name": name, "protocol": "bacnet-ip", "address": sim.address,
                             "device_instance": sim.instance}
    entry.update(spec)
    await driver.add_device(record, {k: v for k, v in entry.items() if v is not None})
    return record


@pytest.fixture
async def sim() -> AsyncIterator[SimBacnetIpDevice]:
    async with SimBacnetIpDevice(drift=False, tick_s=0.05) as device:
        yield device


@pytest.fixture
def rec() -> Recorder:
    return Recorder()


@pytest.fixture
async def driver(rec: Recorder) -> AsyncIterator[BacnetIpDriver]:
    drv = make_driver(rec)
    try:
        yield drv
    finally:
        await drv.stop()


@pytest.fixture
async def ahu(sim: SimBacnetIpDevice, driver: BacnetIpDriver) -> BacnetIpDriver:
    """The driver, started, with the simulated AHU controller as ``ahu1-ctl``."""
    await add_sim(driver, sim)
    await driver.start()
    return driver
