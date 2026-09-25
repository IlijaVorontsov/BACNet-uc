"""A simulated third-party BACnet/IP controller (an AHU controller), for the
``bacnet-ip`` driver's tests and the demo.

Objects:

- ``analog-input:1`` "Outside Air Temp" (degrees-celsius)
- ``analog-value:3`` "Supply Air Temp" (degrees-celsius), drifts slowly
  around the setpoint; a measurement, so its present value is read-only
- ``analog-value:4`` "Supply Air Temp Setpoint" (degrees-celsius, 18.0),
  a commandable value object
- ``analog-output:1`` "Supply Fan Speed" (percent), commandable
- ``binary-output:1`` "Supply Fan Command", commandable (relinquish default
  active: the controller's own sequence runs the fan)
- ``binary-input:1`` "Supply Fan Status", active while the fan is commanded
  on at a speed above 0
- ``binary-output:9`` "Smoke Damper", commandable (inactive = closed)
- ``multi-state-value:1`` "AHU Mode" (Off, Occupied, Unoccupied, Night Purge),
  writable without a priority array and without COV support, like many
  vendors' mode selectors
- ``notification-class:1``, a non-point object
- ``extra_objects`` spare analog values (``analog-value:1000`` ...)

Options mimic devices the driver must cope with: ``cov=False`` refuses every
SubscribeCOV, ``rpm=False`` rejects ReadPropertyMultiple, and
``segmentation=False`` with a small ``max_apdu`` makes large answers (the
object list of a device with many objects) fail with an abort.

With ``network`` (a ``sim.network.SimNetwork``) the device is also a node of
the simulated BACnet network between BACnet-uc nodes, so their emulated
apps (uc-link) can read and subscribe to its present values, as boards do
over BACnet/IP; writes from there are refused.

Run ``python -m uc_hub.sim.bacnet_ip_device --port 47809`` for the demo.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
from typing import Any

from bacpypes3.apdu import ReadPropertyMultipleRequest, SubscribeCOVRequest
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, Segmentation, ServicesSupported
from bacpypes3.errors import ExecutionError, PropertyError, UnrecognizedService
from bacpypes3.local.analog import (
    AnalogInputObject,
    AnalogOutputObject,
    AnalogValueObject,
    AnalogValueObjectCmd,
)
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject
from bacpypes3.local.device import DeviceObject
from bacpypes3.local.multistate import MultiStateValueObject
from bacpypes3.object import NotificationClassObject
from bacpypes3.primitivedata import Null

from ..core.ids import OBJECT_TYPES
from ..drivers.bacnet_ip.stack import BoundApplication, open_application
from .network import PROP_PRESENT_VALUE, UC_ERR_BACNET, UC_ERR_NOT_FOUND, SimNetwork, UcError

logger = logging.getLogger(__name__)

MODES = ("Off", "Occupied", "Unoccupied", "Night Purge")
#: How bacpypes3 may name the present value when a write reaches an object.
_PRESENT_VALUE = ("presentValue", "present-value", 85)


class _MeasuredMixin:
    """Present value writable only while out of service, as on real
    controllers; the simulation itself sets the attribute directly."""

    async def write_property(
        self, attr: str | int, value: Any, index: int | None = None, priority: int | None = None
    ) -> None:
        if attr in _PRESENT_VALUE and not getattr(self, "outOfService", False):
            raise PropertyError("writeAccessDenied")
        await super().write_property(attr, value, index, priority)  # type: ignore[misc]


class MeasuredAnalogValue(_MeasuredMixin, AnalogValueObject):
    pass


class MeasuredAnalogInput(_MeasuredMixin, AnalogInputObject):
    pass


class MeasuredBinaryInput(_MeasuredMixin, BinaryInputObject):
    pass


class ModeValue(MultiStateValueObject):
    """A mode selector: no priority array, no COV, state checked on write."""

    _cov_criteria = None

    async def write_property(
        self, attr: str | int, value: Any, index: int | None = None, priority: int | None = None
    ) -> None:
        if attr in _PRESENT_VALUE:
            if isinstance(value, Null):
                raise PropertyError("invalidDataType")  # nothing to relinquish
            state = int(value)
            if not 1 <= state <= int(self.numberOfStates):
                raise PropertyError("valueOutOfRange")
        await super().write_property(attr, value, index, priority)


class SimApplication(BoundApplication):
    """The simulated device's application, with services it can be told to lack."""

    cov_enabled = True
    rpm_enabled = True

    async def do_SubscribeCOVRequest(self, apdu: SubscribeCOVRequest) -> None:
        if not self.cov_enabled:
            raise ExecutionError(errorClass="services", errorCode="covSubscriptionFailed")
        await super().do_SubscribeCOVRequest(apdu)

    async def do_ReadPropertyMultipleRequest(self, apdu: ReadPropertyMultipleRequest) -> None:
        if not self.rpm_enabled:
            raise UnrecognizedService("ReadPropertyMultiple is not supported")
        await super().do_ReadPropertyMultipleRequest(apdu)

    def get_services_supported(self) -> ServicesSupported:
        supported = super().get_services_supported()
        if not self.rpm_enabled:
            supported[ServicesSupported.readPropertyMultiple] = 0
        return supported


class SimBacnetIpDevice:
    """An AHU controller on its own BACnet/IP socket. ``start()`` binds
    (``port=0`` picks a free port, see ``port``), ``stop()`` closes."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        instance: int = 100,
        name: str = "AHU-1 Controller",
        vendor_identifier: int = 999,
        drift: bool = True,
        tick_s: float = 1.0,
        cov: bool = True,
        rpm: bool = True,
        segmentation: bool = True,
        max_apdu: int = 1476,
        extra_objects: int = 0,
        network: SimNetwork | None = None,
    ) -> None:
        self.host = host
        self.instance = instance
        self.name = name
        self.vendor_identifier = vendor_identifier
        self.drift = drift
        self.tick_s = tick_s
        self.cov = cov
        self.rpm = rpm
        self.segmentation = segmentation
        self.max_apdu = max_apdu
        self.extra_objects = extra_objects
        self.network = network
        self._view = NetworkView(self)
        self._objects: dict[tuple[int, int], Any] = {}
        self._port = port
        self.app: SimApplication | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._elapsed = 0.0
        self.supply_air_temp: MeasuredAnalogValue
        self.supply_air_setpoint: AnalogValueObjectCmd
        self.outside_air_temp: MeasuredAnalogInput
        self.fan_command: BinaryOutputObject
        self.fan_speed: AnalogOutputObject
        self.fan_status: MeasuredBinaryInput
        self.smoke_damper: BinaryOutputObject
        self.mode: ModeValue

    @property
    def port(self) -> int:
        return self.app.bound[1] if self.app is not None else self._port

    @property
    def address(self) -> str:
        """``host:port`` as a manifest ``address``."""
        host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        return f"{host}:{self.port}"

    async def __aenter__(self) -> SimBacnetIpDevice:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        if self.app is not None:
            return
        objects = self._build_objects()
        app = await open_application(SimApplication, objects, self.host, self._port)
        app.cov_enabled = self.cov
        app.rpm_enabled = self.rpm
        self.app = app
        self._port = app.bound[1]
        self._objects = {}
        for obj in objects:
            type_name, instance = str(obj.objectIdentifier[0]), int(obj.objectIdentifier[1])
            if type_name in OBJECT_TYPES and type_name != "device":
                self._objects[(OBJECT_TYPES[type_name], instance)] = obj
        if self.network is not None:
            self.network.register(self._view)
        self._ticker = asyncio.create_task(self._tick(), name=f"sim-bacnet-ip:{self.instance}")
        logger.info("simulated BACnet/IP device %d on %s:%d", self.instance, *app.bound)

    async def stop(self) -> None:
        if self.network is not None:
            self.network.unregister(self._view)
        task, self._ticker = self._ticker, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        app, self.app = self.app, None
        if app is not None:
            app.close()

    def _build_objects(self) -> list[Any]:
        device = DeviceObject(
            objectIdentifier=("device", self.instance),
            objectName=self.name,
            vendorIdentifier=self.vendor_identifier,
            vendorName="Simulated Controls",
            modelName="SIM-AHU-200",
            firmwareRevision="2.4.1",
            applicationSoftwareVersion="ahu-seq 1.3",
            location="Plant room",
            description="Simulated air handling unit controller",
            maxApduLengthAccepted=self.max_apdu,
            segmentationSupported=(
                Segmentation.segmentedBoth if self.segmentation else Segmentation.noSegmentation
            ),
        )
        self.outside_air_temp = MeasuredAnalogInput(
            objectIdentifier=("analog-input", 1),
            objectName="Outside Air Temp",
            presentValue=8.5,
            units=EngineeringUnits.degreesCelsius,
            covIncrement=0.1,
            description="Outside air temperature sensor",
        )
        self.supply_air_temp = MeasuredAnalogValue(
            objectIdentifier=("analog-value", 3),
            objectName="Supply Air Temp",
            presentValue=18.0,
            units=EngineeringUnits.degreesCelsius,
            covIncrement=0.1,
            description="Supply air temperature after the heating coil",
        )
        self.supply_air_setpoint = AnalogValueObjectCmd(
            objectIdentifier=("analog-value", 4),
            objectName="Supply Air Temp Setpoint",
            presentValue=18.0,
            relinquishDefault=18.0,
            units=EngineeringUnits.degreesCelsius,
            covIncrement=0.1,
            description="Supply air temperature setpoint",
        )
        self.fan_command = BinaryOutputObject(
            objectIdentifier=("binary-output", 1),
            objectName="Supply Fan Command",
            presentValue=BinaryPV.active,
            relinquishDefault=BinaryPV.active,
            activeText="start",
            inactiveText="stop",
            outOfService=False,
            description="Supply fan start/stop",
        )
        self.fan_speed = AnalogOutputObject(
            objectIdentifier=("analog-output", 1),
            objectName="Supply Fan Speed",
            presentValue=0.0,
            relinquishDefault=0.0,
            units=EngineeringUnits.percent,
            covIncrement=1.0,
            description="Supply fan VFD speed command",
        )
        self.fan_status = MeasuredBinaryInput(
            objectIdentifier=("binary-input", 1),
            objectName="Supply Fan Status",
            presentValue=BinaryPV.inactive,
            activeText="running",
            inactiveText="stopped",
            outOfService=False,
        )
        self.smoke_damper = BinaryOutputObject(
            objectIdentifier=("binary-output", 9),
            objectName="Smoke Damper",
            presentValue=BinaryPV.inactive,
            relinquishDefault=BinaryPV.inactive,
            activeText="open",
            inactiveText="closed",
            outOfService=False,
            description="Smoke damper, life safety",
        )
        self.mode = ModeValue(
            objectIdentifier=("multi-state-value", 1),
            objectName="AHU Mode",
            presentValue=2,
            numberOfStates=len(MODES),
            stateText=list(MODES),
            outOfService=False,
        )
        notification = NotificationClassObject(
            objectIdentifier=("notification-class", 1),
            objectName="Alarms",
            notificationClass=1,
        )
        spares = [
            AnalogValueObject(
                objectIdentifier=("analog-value", 1000 + i),
                objectName=f"Spare AV {i}",
                presentValue=float(i),
                units=EngineeringUnits.noUnits,
                covIncrement=1.0,
            )
            for i in range(self.extra_objects)
        ]
        return [device, self.outside_air_temp, self.supply_air_temp, self.supply_air_setpoint, self.fan_speed,
                self.fan_command, self.fan_status, self.smoke_damper, self.mode, notification, *spares]

    def present_value(self, obj_type: int, instance: int) -> float:
        """The present value of a point object as a number (binary: 0/1)."""
        obj = self._objects.get((obj_type, instance))
        if obj is None:
            raise UcError(UC_ERR_NOT_FOUND, f"device {self.instance} has no object {obj_type}:{instance}")
        return float(int(obj.presentValue)) if obj_type in _BINARY else float(obj.presentValue)

    def cov_increment(self, obj_type: int, instance: int) -> float:
        increment = getattr(self._objects.get((obj_type, instance)), "covIncrement", None)
        return float(increment) if increment is not None else 0.0

    def set_supply_air_temp(self, value: float) -> None:
        self.supply_air_temp.presentValue = float(value)

    def set_mode(self, state: int) -> None:
        self.mode.presentValue = int(state)

    def cov_subscriptions(self) -> list[tuple[str, str]]:
        """(subscriber address, object) of the device's active COV
        subscriptions: the subscription table a real device has only so
        many entries in."""
        if self.app is None:
            return []
        return sorted(
            (str(cov.client_addr), f"{cov.obj_id[0]}:{cov.obj_id[1]}")
            for detection in self.app._cov_detections.values()
            for cov in detection.cov_subscriptions
        )

    async def _tick(self) -> None:
        """Drift the supply air temperature around its setpoint (a slow
        sine) and let the fan status follow the fan commands."""
        while True:
            await asyncio.sleep(self.tick_s)
            self._elapsed += self.tick_s
            if self.drift:
                setpoint = float(self.supply_air_setpoint.presentValue)
                sat = setpoint + 1.5 * math.sin(self._elapsed / 120.0 * 2 * math.pi)
                self.supply_air_temp.presentValue = round(sat, 2)
            running = (int(self.fan_command.presentValue) == BinaryPV.active
                       and float(self.fan_speed.presentValue) > 0.0)
            status = BinaryPV.active if running else BinaryPV.inactive
            if int(self.fan_status.presentValue) != status:
                self.fan_status.presentValue = status


_BINARY = frozenset({OBJECT_TYPES["binary-input"], OBJECT_TYPES["binary-output"], OBJECT_TYPES["binary-value"]})


class NetworkView:
    """A ``SimBacnetIpDevice`` as a ``sim.network.NetworkNode``: present
    values to read and subscribe to; everything else is a BACnet error."""

    def __init__(self, device: SimBacnetIpDevice) -> None:
        self.device = device
        self.name = device.name

    @property
    def address(self) -> str:
        return self.device.address

    @property
    def instance(self) -> int:
        return self.device.instance

    @property
    def reachable(self) -> bool:
        return self.device.app is not None

    def bacnet_read(self, obj_type: int, instance: int, prop: int, index: int = -1) -> Any:
        if prop != PROP_PRESENT_VALUE:
            raise UcError(UC_ERR_BACNET, f"device {self.instance}: only present values are simulated")
        return self.device.present_value(obj_type, instance)

    def bacnet_write(self, obj_type: int, instance: int, prop: int, value: Any, priority: int = 0,
                     index: int = -1) -> None:
        raise UcError(UC_ERR_BACNET, f"device {self.instance}: write access denied")

    def cov_state(self, obj_type: int, instance: int) -> tuple[float, float]:
        return self.device.present_value(obj_type, instance), self.device.cov_increment(obj_type, instance)

    def step(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=47809)
    parser.add_argument("--instance", type=int, default=100)
    parser.add_argument("--name", default="AHU-1 Controller")
    parser.add_argument("--no-cov", action="store_true", help="refuse SubscribeCOV")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    device = SimBacnetIpDevice(host=args.host, port=args.port, instance=args.instance, name=args.name,
                               cov=not args.no_cov)
    async with device:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
