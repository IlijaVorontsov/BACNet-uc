"""Fixtures for the MQTT tests: throwaway mosquitto brokers (plain and mutual
TLS) on ephemeral ports, a recording driver context and simulated devices."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from support.mosquitto import Broker, Pki, make_pki, need_mosquitto

from uc_hub.core.driver import DriverContext
from uc_hub.core.types import DeviceRecord, ProtocolName, Reading
from uc_hub.drivers.mqtt import MqttDriver
from uc_hub.sim.mqtt_device import SimJsonSensor, SimMqttTlsDevice

__all__ = ["Broker", "Pki"]

SITE = "hq"


@pytest.fixture(scope="session")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    return make_pki(tmp_path_factory.mktemp("pki"))


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    need_mosquitto()
    b = Broker(tmp_path)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def tls_broker(tmp_path: Path, pki: Pki) -> Iterator[Broker]:
    need_mosquitto()
    b = Broker(tmp_path, pki)
    b.start()
    yield b
    b.stop()


@dataclass
class Recorder:
    """The driver context callbacks, recorded."""

    readings: list[Reading] = field(default_factory=list)
    online: list[tuple[str, bool]] = field(default_factory=list)

    def publish(self, reading: Reading) -> None:
        self.readings.append(reading)

    def set_online(self, device: str, online: bool) -> None:
        self.online.append((device, online))

    def last(self, point: str) -> Reading | None:
        for r in reversed(self.readings):
            if str(r.ref) == point:
                return r
        return None

    def is_online(self, device: str) -> bool | None:
        for name, state in reversed(self.online):
            if name == device:
                return state
        return None


async def eventually(check: Callable[[], Any], timeout_s: float = 5.0, what: str = "") -> Any:
    """Poll ``check`` until it returns something truthy."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or check}")
        await asyncio.sleep(0.02)


def record(name: str, address: str = "") -> DeviceRecord:
    return DeviceRecord(site=SITE, name=name, protocol=ProtocolName.MQTT, address=address)


DriverFactory = Callable[..., Awaitable[tuple[MqttDriver, Recorder]]]


@pytest.fixture
async def make_driver() -> AsyncIterator[DriverFactory]:
    """``await make_driver(settings, [(record, spec), ...], start=True)``."""
    drivers: list[MqttDriver] = []

    async def make(
        settings: dict[str, Any],
        devices: Iterable[tuple[DeviceRecord, dict[str, Any]]] = (),
        start: bool = True,
    ) -> tuple[MqttDriver, Recorder]:
        rec = Recorder()
        driver = MqttDriver(DriverContext(
            site=SITE, publish=rec.publish, set_online=rec.set_online, settings=settings))
        drivers.append(driver)
        for dev_record, spec in devices:
            await driver.add_device(dev_record, spec)
        if start:
            await driver.start()
            assert await driver.wait_connected(5.0), "driver did not connect"
        return driver, rec

    yield make
    for driver in drivers:
        await driver.stop()


@pytest.fixture
async def sims() -> AsyncIterator[list[SimMqttTlsDevice | SimJsonSensor]]:
    """Append simulated devices here; they are stopped after the test."""
    devices: list[SimMqttTlsDevice | SimJsonSensor] = []
    yield devices
    await asyncio.gather(*(d.stop() for d in devices), return_exceptions=True)
