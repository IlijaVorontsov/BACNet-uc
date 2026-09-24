"""Simulated BACnet network and emulated WebAssembly applications.

``SimNetwork`` maps BACnet device instances to simulated nodes. It stands in
for BACnet/IP between boards: applications on one node read, write and
subscribe to points of another through it, the way ``uc_remote_read`` and
``uc_cov_subscribe`` do on real hardware.

The applications below are NATIVE PYTHON EMULATIONS of WebAssembly modules,
not a WASM runtime. A ``SimNode`` chooses one by the module's file name:

- ``*uc-link*`` / ``*uc_link*``: the stock uc-link app (wasm/examples/uc-link),
  driven by the same ``count``/``l<i>`` parameters.
- ``*thermostat*``: a PI thermostat (analog-output:1 from analog-input:1).
- anything else: an idle app that only counts ticks.

They call the node through ``AppHost``, which mirrors the ``bacnet_uc.h``
imports: permissions are checked, failures raise ``UcError`` with the
``UC_ERR_*`` code and count as host-call errors in ``uc_app status``.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import TracebackType
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# bacnet_uc.h error codes.
UC_OK = 0
UC_ERR_INVALID = -1
UC_ERR_NOT_FOUND = -2
UC_ERR_PERM = -3
UC_ERR_TIMEOUT = -4
UC_ERR_BUSY = -5
UC_ERR_NO_MEM = -6
UC_ERR_BACNET = -7
UC_ERR_UNSUPPORTED = -8
UC_ERR_IO = -9
UC_ERR_TYPE = -10
UC_ERR_EXISTS = -11
UC_ERR_NO_ROUTE = -12

UC_DEVICE_LOCAL = 0xFFFFFFFF
UC_PRIORITY_NONE = 0
UC_ARRAY_ALL = -1

OBJ_ANALOG_INPUT, OBJ_ANALOG_OUTPUT, OBJ_ANALOG_VALUE = 0, 1, 2
OBJ_BINARY_VALUE, OBJ_MULTI_STATE_VALUE = 5, 19
PROP_PRESENT_VALUE = 85
PROP_RELINQUISH_DEFAULT = 104

LOG_ERR, LOG_WRN, LOG_INF, LOG_DBG = 1, 2, 3, 4
_LOG_LEVELS = {
    LOG_ERR: logging.ERROR, LOG_WRN: logging.WARNING, LOG_INF: logging.INFO, LOG_DBG: logging.DEBUG,
}


class UcError(Exception):
    """A host call failed with a ``UC_ERR_*`` code."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or f"UC error {code}")
        self.code = code


class ManualClock:
    """Monotonic seconds that only move when ``advance`` is called."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("time only moves forward")
        self.now += seconds
        return self.now


class NetworkNode(Protocol):
    """What the network needs from a simulated node."""

    name: str

    @property
    def address(self) -> str: ...

    @property
    def instance(self) -> int: ...

    @property
    def reachable(self) -> bool: ...

    def bacnet_read(self, obj_type: int, instance: int, prop: int, index: int = UC_ARRAY_ALL) -> Any:
        """ReadProperty; raises ``UcError`` (UC_ERR_BACNET for an Error PDU)."""

    def bacnet_write(
        self, obj_type: int, instance: int, prop: int, value: Any, priority: int = 0,
        index: int = UC_ARRAY_ALL,
    ) -> None: ...

    def cov_state(self, obj_type: int, instance: int) -> tuple[float, float]:
        """``(present value, COV increment)`` for change detection."""

    def step(self) -> None: ...

    async def stop(self) -> None: ...


class SimNetwork:
    """Registry of simulated nodes by BACnet device instance."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock: Callable[[], float] = clock or time.monotonic
        self._nodes: list[NetworkNode] = []

    @property
    def nodes(self) -> list[NetworkNode]:
        return list(self._nodes)

    def register(self, node: NetworkNode) -> None:
        if node not in self._nodes:
            self._nodes.append(node)

    def unregister(self, node: NetworkNode) -> None:
        if node in self._nodes:
            self._nodes.remove(node)

    def find(self, instance: int) -> NetworkNode | None:
        for node in self._nodes:
            if node.instance == instance:
                return node
        return None

    def addresses(self) -> dict[str, str]:
        """Node name -> ``host:port``, the driver's ``sim_addresses`` setting."""
        return {node.name: node.address for node in self._nodes}

    def _route(self, device: int) -> NetworkNode:
        node = self.find(device)
        if node is None:
            raise UcError(UC_ERR_NO_ROUTE, f"device {device} is not on the network")
        if not node.reachable:
            raise UcError(UC_ERR_TIMEOUT, f"device {device} does not answer")
        return node

    def read(self, device: int, obj_type: int, instance: int, prop: int, index: int = UC_ARRAY_ALL) -> Any:
        return self._route(device).bacnet_read(obj_type, instance, prop, index)

    def write(
        self, device: int, obj_type: int, instance: int, prop: int, value: Any,
        priority: int = 0, index: int = UC_ARRAY_ALL,
    ) -> None:
        self._route(device).bacnet_write(obj_type, instance, prop, value, priority, index)

    def cov_state(self, device: int, obj_type: int, instance: int) -> tuple[float, float]:
        return self._route(device).cov_state(obj_type, instance)

    def step(self) -> None:
        for node in list(self._nodes):
            node.step()

    def advance(self, seconds: float, step_s: float = 1.0) -> None:
        """Move a ``ManualClock`` forward in ``step_s`` increments, stepping
        every node after each one."""
        if not isinstance(self.clock, ManualClock):
            raise TypeError("advance() needs a SimNetwork built with a ManualClock")
        if step_s <= 0:
            raise ValueError("step_s must be positive")
        remaining = seconds
        while remaining > 1e-9:
            dt = min(step_s, remaining)
            self.clock.advance(dt)
            self.step()
            remaining -= dt

    async def aclose(self) -> None:
        for node in list(self._nodes):
            await node.stop()
        self._nodes.clear()

    async def __aenter__(self) -> SimNetwork:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()


class AppHost(Protocol):
    """Host functions of ``bacnet_uc.h`` as seen by an emulated app. Every
    call raises ``UcError`` on failure."""

    @property
    def local_instance(self) -> int: ...

    def log(self, level: int, message: str) -> None: ...

    def param(self, key: str) -> str | None: ...

    def set_tick_period(self, period_ms: int) -> None: ...

    def obj_create(self, obj_type: int, instance: int, name: str) -> None: ...

    def obj_delete(self, obj_type: int, instance: int) -> None: ...

    def prop_read(self, obj_type: int, instance: int, prop: int, index: int = UC_ARRAY_ALL) -> float: ...

    def prop_write(
        self, obj_type: int, instance: int, prop: int, value: float,
        priority: int = UC_PRIORITY_NONE, index: int = UC_ARRAY_ALL,
    ) -> None: ...

    def prop_write_null(self, obj_type: int, instance: int, prop: int, priority: int) -> None: ...

    def remote_read(
        self, device: int, obj_type: int, instance: int, prop: int,
        index: int = UC_ARRAY_ALL, timeout_ms: int = 1000,
    ) -> float: ...

    def remote_write(
        self, device: int, obj_type: int, instance: int, prop: int, value: float,
        priority: int = UC_PRIORITY_NONE, index: int = UC_ARRAY_ALL, timeout_ms: int = 1000,
    ) -> None: ...

    def cov_subscribe(self, device: int, obj_type: int, instance: int, lifetime_s: int) -> int: ...

    def cov_unsubscribe(self, sub_id: int) -> None: ...

    def io_find(self, name: str) -> int: ...

    def io_read(self, channel: int) -> float: ...

    def io_write(self, channel: int, value: float) -> None: ...


class EmulatedApp:
    """Python stand-in for a WASM module; methods are the ``uc_app_*`` exports."""

    #: What ``uc_app status`` and logs call this emulation.
    kind = "idle"

    def __init__(self, host: AppHost) -> None:
        self.host = host

    def init(self) -> int:
        return UC_OK

    def tick(self, now_ms: int) -> None:
        return None

    def on_cov(
        self, sub_id: int, device: int, obj_type: int, instance: int, prop: int, value: float,
    ) -> None:
        return None

    def on_write(self, obj_type: int, instance: int, prop: int, priority: int, value: float) -> None:
        return None

    def deinit(self) -> None:
        return None

    def param_number(self, key: str, default: float) -> float:
        """``uc_param_get_number`` with a default for absent or bad values."""
        text = self.host.param(key)
        if text is None:
            return default
        try:
            value = float(text)
        except ValueError:
            self.host.log(LOG_WRN, f"param {key}={text!r} is not a number, using {default}")
            return default
        return value if math.isfinite(value) else default


@dataclass(slots=True)
class LinkSpec:
    """One ``l<i>`` parameter of uc-link."""

    src_device: int
    src_type: int
    src_instance: int
    dst_type: int
    dst_instance: int
    mode: str
    period_ms: int
    priority: int
    scale: float
    offset: float

    @classmethod
    def parse(cls, text: str) -> LinkSpec:
        """``"<src_device> <src_type> <src_instance> <dst_type> <dst_instance>
        <mode> <period_ms> <priority> <scale> <offset>"``."""
        fields = text.split()
        if len(fields) != 10:
            raise ValueError(f"expected 10 fields, got {len(fields)}")
        try:
            spec = cls(
                int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3]), int(fields[4]),
                fields[5], int(fields[6]), int(fields[7]), float(fields[8]), float(fields[9]),
            )
        except ValueError as e:
            raise ValueError(f"bad number: {e}") from e
        if spec.mode not in ("cov", "poll"):
            raise ValueError(f"mode must be cov or poll, not {spec.mode!r}")
        if not 0 <= spec.priority <= 16:
            raise ValueError(f"priority {spec.priority} not in 0..16")
        if spec.period_ms < 0 or min(spec.src_device, spec.src_type, spec.src_instance,
                                     spec.dst_type, spec.dst_instance) < 0:
            raise ValueError("negative field")
        if not (math.isfinite(spec.scale) and math.isfinite(spec.offset)):
            raise ValueError("scale and offset must be finite")
        return spec

    def format(self) -> str:
        return (
            f"{self.src_device} {self.src_type} {self.src_instance} {self.dst_type} "
            f"{self.dst_instance} {self.mode} {self.period_ms} {self.priority} "
            f"{self.scale:g} {self.offset:g}"
        )


@dataclass(slots=True)
class _Link:
    index: int
    spec: LinkSpec
    sub_id: int | None = None
    last: float | None = None
    next_poll_ms: int = 0


class UcLinkApp(EmulatedApp):
    """Emulation of the stock uc-link app: copies source present values to
    destination objects on this node, per ``count`` and ``l0``..``l<N-1>``."""

    kind = "uc-link"
    MAX_LINKS = 8
    COV_LIFETIME_S = 300
    MIN_PERIOD_MS = 100
    _CREATABLE = (OBJ_ANALOG_VALUE, OBJ_BINARY_VALUE, OBJ_MULTI_STATE_VALUE)

    def __init__(self, host: AppHost) -> None:
        super().__init__(host)
        self.links: list[_Link] = []

    def init(self) -> int:
        count_text = self.host.param("count")
        try:
            count = int(count_text) if count_text is not None else -1
        except ValueError:
            count = -1
        if not 1 <= count <= self.MAX_LINKS:
            self.host.log(LOG_ERR, f"count must be 1..{self.MAX_LINKS}, got {count_text!r}")
            return UC_ERR_INVALID
        for i in range(count):
            text = self.host.param(f"l{i}")
            try:
                spec = LinkSpec.parse(text or "")
            except ValueError as e:
                self.host.log(LOG_ERR, f"link l{i} skipped: {e}")
                continue
            if self._prepare_destination(i, spec):
                self.links.append(_Link(i, spec))
        for link in self.links:
            if link.spec.mode == "cov":
                self._subscribe(link)
        polls = [link.spec.period_ms for link in self.links if link.spec.mode == "poll"]
        if polls:
            self.host.set_tick_period(max(self.MIN_PERIOD_MS, min(polls)))
        return UC_OK

    def _prepare_destination(self, i: int, spec: LinkSpec) -> bool:
        try:
            if spec.dst_type in self._CREATABLE:
                try:
                    self.host.obj_create(spec.dst_type, spec.dst_instance, f"link-{i}")
                except UcError as e:
                    if e.code != UC_ERR_EXISTS:
                        raise
            else:
                self.host.prop_read(spec.dst_type, spec.dst_instance, PROP_PRESENT_VALUE)
        except UcError as e:
            self.host.log(
                LOG_ERR, f"link l{i} skipped: destination {spec.dst_type}:{spec.dst_instance}: {e}"
            )
            return False
        return True

    def _source_device(self, spec: LinkSpec) -> int:
        return UC_DEVICE_LOCAL if spec.src_device == self.host.local_instance else spec.src_device

    def _subscribe(self, link: _Link) -> None:
        spec = link.spec
        try:
            link.sub_id = self.host.cov_subscribe(
                self._source_device(spec), spec.src_type, spec.src_instance, self.COV_LIFETIME_S
            )
        except UcError as e:
            self.host.log(LOG_WRN, f"link l{link.index}: subscribe failed ({e}), retrying on tick")

    def _read_source(self, spec: LinkSpec) -> float:
        if self._source_device(spec) == UC_DEVICE_LOCAL:
            return self.host.prop_read(spec.src_type, spec.src_instance, PROP_PRESENT_VALUE)
        return self.host.remote_read(
            spec.src_device, spec.src_type, spec.src_instance, PROP_PRESENT_VALUE
        )

    def _write_destination(self, link: _Link, value: float) -> None:
        spec = link.spec
        try:
            self.host.prop_write(
                spec.dst_type, spec.dst_instance, PROP_PRESENT_VALUE,
                value * spec.scale + spec.offset, spec.priority,
            )
        except UcError as e:
            self.host.log(LOG_WRN, f"link l{link.index}: write failed: {e}")
            return
        link.last = value

    def tick(self, now_ms: int) -> None:
        for link in self.links:
            if link.spec.mode == "cov":
                if link.sub_id is None:
                    self._subscribe(link)
                continue
            if now_ms < link.next_poll_ms:
                continue
            link.next_poll_ms = now_ms + link.spec.period_ms
            try:
                value = self._read_source(link.spec)
            except UcError:
                continue
            if value != link.last:
                self._write_destination(link, value)

    def on_cov(
        self, sub_id: int, device: int, obj_type: int, instance: int, prop: int, value: float,
    ) -> None:
        for link in self.links:
            if link.sub_id == sub_id:
                self._write_destination(link, value)

    def deinit(self) -> None:
        for link in self.links:
            if link.sub_id is not None:
                with contextlib.suppress(UcError):
                    self.host.cov_unsubscribe(link.sub_id)
                link.sub_id = None


class ThermostatApp(EmulatedApp):
    """Emulation of a PI thermostat app: drives analog-output:1 (valve, %)
    from analog-input:1 (room temperature) at priority 12, towards the
    setpoint in analog-value:1, which it creates with the ``setpoint``
    parameter as relinquish default. Parameters: ``setpoint`` (21.0),
    ``kp`` (%/K, 10), ``ki`` (%/(K*s), 0.011); optional ``sensor_device``
    and ``sensor_instance`` read the temperature from another device."""

    kind = "thermostat"
    PRIORITY = 12
    SENSOR = (OBJ_ANALOG_INPUT, 1)
    OUTPUT = (OBJ_ANALOG_OUTPUT, 1)
    SETPOINT = (OBJ_ANALOG_VALUE, 1)

    def __init__(self, host: AppHost) -> None:
        super().__init__(host)
        self.kp = 10.0
        self.ki = 0.011
        self.sensor_device = UC_DEVICE_LOCAL
        self.sensor_instance = 1
        self.integral = 0.0
        self.output = 0.0
        self._last_ms: int | None = None

    def init(self) -> int:
        setpoint = self.param_number("setpoint", 21.0)
        self.kp = self.param_number("kp", self.kp)
        self.ki = self.param_number("ki", self.ki)
        device = int(self.param_number("sensor_device", -1))
        if device >= 0 and device != self.host.local_instance:
            self.sensor_device = device
        self.sensor_instance = int(self.param_number("sensor_instance", 1))
        try:
            self.host.obj_create(*self.SETPOINT, "Setpoint")
        except UcError as e:
            if e.code != UC_ERR_EXISTS:
                self.host.log(LOG_ERR, f"cannot create the setpoint object: {e}")
                return e.code
        try:
            self.host.prop_write(*self.SETPOINT, PROP_RELINQUISH_DEFAULT, setpoint)
        except UcError as e:
            self.host.log(LOG_WRN, f"cannot set the default setpoint: {e}")
        return UC_OK

    def _temperature(self) -> float:
        if self.sensor_device == UC_DEVICE_LOCAL:
            return self.host.prop_read(OBJ_ANALOG_INPUT, self.sensor_instance, PROP_PRESENT_VALUE)
        return self.host.remote_read(
            self.sensor_device, OBJ_ANALOG_INPUT, self.sensor_instance, PROP_PRESENT_VALUE
        )

    def tick(self, now_ms: int) -> None:
        dt = 0.0 if self._last_ms is None else min(max((now_ms - self._last_ms) / 1000.0, 0.0), 60.0)
        self._last_ms = now_ms
        try:
            temp = self._temperature()
            setpoint = self.host.prop_read(*self.SETPOINT, PROP_PRESENT_VALUE)
        except UcError:
            return
        error = setpoint - temp
        self.integral = min(max(self.integral + self.ki * error * dt, 0.0), 100.0)
        self.output = min(max(self.kp * error + self.integral, 0.0), 100.0)
        try:
            self.host.prop_write(*self.OUTPUT, PROP_PRESENT_VALUE, self.output, self.PRIORITY)
        except UcError as e:
            self.host.log(LOG_WRN, f"valve write failed: {e}")

    def deinit(self) -> None:
        with contextlib.suppress(UcError):
            self.host.prop_write_null(*self.OUTPUT, PROP_PRESENT_VALUE, self.PRIORITY)


def emulation_for(file: str) -> type[EmulatedApp]:
    """The emulated app class for a module path, chosen by file name."""
    stem = PurePosixPath(file).stem.lower()
    if "uc-link" in stem or "uc_link" in stem:
        return UcLinkApp
    if "thermostat" in stem:
        return ThermostatApp
    return EmulatedApp


def log_level(level: int) -> int:
    return _LOG_LEVELS.get(level, logging.INFO)
