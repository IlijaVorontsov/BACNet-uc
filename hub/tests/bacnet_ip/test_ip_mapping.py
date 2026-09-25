"""Object and value mapping, address parsing, settings and error mapping:
no network."""

from __future__ import annotations

import math

import pytest
from bacpypes3.apdu import AbortPDU, Error, RejectPDU
from bacpypes3.basetypes import BinaryPV, EngineeringUnits, PriorityValue
from bacpypes3.pdu import IPv4Address
from bacpypes3.primitivedata import CharacterString, Double, Integer, Null, ObjectIdentifier, Real, Unsigned

from uc_hub.core.driver import DriverContext
from uc_hub.core.errors import DeviceTimeout, InvalidRequest
from uc_hub.core.types import PointKind
from uc_hub.drivers.bacnet_ip import BacnetIpDriver
from uc_hub.drivers.bacnet_ip.mapping import (
    POINT_TYPES,
    clean_text,
    obj_text,
    parse_point_obj,
    present_value_type,
    priority_slot,
    to_bacnet,
    to_value,
    units_name,
)
from uc_hub.drivers.bacnet_ip.stack import BacnetError, bacnet_error, format_address, parse_host_port


def test_point_types_kinds_and_datatypes() -> None:
    assert POINT_TYPES["analog-input"].kind is PointKind.INPUT
    assert POINT_TYPES["binary-output"].kind is PointKind.OUTPUT
    assert POINT_TYPES["multi-state-value"].kind is PointKind.VALUE
    assert POINT_TYPES["binary-value"].datatype == "enum"
    assert POINT_TYPES["multi-state-input"].datatype == "int"
    assert POINT_TYPES["large-analog-value"].datatype == "real"
    assert POINT_TYPES["characterstring-value"].datatype == "string"
    assert POINT_TYPES["analog-output"].priority == "always"
    assert POINT_TYPES["analog-value"].priority == "maybe"
    assert POINT_TYPES["analog-input"].priority == "never"


@pytest.mark.parametrize("obj", ["analog-value:3", "binary-output:0", "multi-state-value:4194302"])
def test_parse_point_obj_accepts_points(obj: str) -> None:
    type_name, instance = parse_point_obj(obj)
    assert f"{type_name}:{instance}" == obj


@pytest.mark.parametrize("obj", ["co2", "device:100", "notification-class:1", "analog-value:4194303",
                                 "Analog-Value:1", "analog-value:-1", "130:1"])
def test_parse_point_obj_rejects_the_rest(obj: str) -> None:
    with pytest.raises(InvalidRequest):
        parse_point_obj(obj)


def test_obj_text() -> None:
    assert obj_text(ObjectIdentifier(("analog-value", 3))) == "analog-value:3"
    assert obj_text(ObjectIdentifier((200, 1))) is None
    assert obj_text(None) is None


def test_to_value() -> None:
    assert to_value("analog-value", Real(21.5)) == 21.5
    assert isinstance(to_value("analog-input", Real(1)), float)
    assert to_value("binary-output", BinaryPV("active")) == 1
    assert to_value("binary-input", BinaryPV("inactive")) == 0
    assert to_value("multi-state-value", Unsigned(3)) == 3
    assert to_value("integer-value", Integer(-4)) == -4
    assert to_value("large-analog-value", Double(1e300)) == 1e300
    assert to_value("characterstring-value", CharacterString("auto")) == "auto"
    assert to_value("analog-value", Null(())) is None
    with pytest.raises(ValueError):
        to_value("analog-value", Real(math.nan))


def test_to_bacnet_analog() -> None:
    assert to_bacnet("analog-output", 55) == Real(55.0)
    assert isinstance(to_bacnet("analog-output", "12.5"), Real)
    assert isinstance(to_bacnet("large-analog-value", 1e300), Double)
    for bad in ("hot", math.inf, math.nan, 1e39, [1]):
        with pytest.raises(InvalidRequest):
            to_bacnet("analog-value", bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("value, active", [
    (1, True), (0, False), (True, True), (False, False), (1.0, True),
    ("active", True), ("Inactive", False), ("on", True), ("off", False), ("1", True),
])
def test_to_bacnet_binary(value: object, active: bool) -> None:
    assert to_bacnet("binary-output", value) == BinaryPV("active" if active else "inactive")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [2, -1, 0.5, "open", [1]])
def test_to_bacnet_binary_rejects(value: object) -> None:
    with pytest.raises(InvalidRequest):
        to_bacnet("binary-value", value)  # type: ignore[arg-type]


def test_to_bacnet_integers_and_strings() -> None:
    assert to_bacnet("multi-state-value", 3) == Unsigned(3)
    assert to_bacnet("multi-state-output", "2") == Unsigned(2)
    assert to_bacnet("integer-value", -7) == Integer(-7)
    assert to_bacnet("positive-integer-value", 0) == Unsigned(0)
    assert to_bacnet("characterstring-value", "eco") == CharacterString("eco")
    for type_name, bad in (("multi-state-value", 0), ("multi-state-value", 2.5),
                           ("positive-integer-value", -1), ("integer-value", 2**31),
                           ("characterstring-value", 5)):
        with pytest.raises(InvalidRequest):
            to_bacnet(type_name, bad)


def test_none_is_null() -> None:
    assert isinstance(to_bacnet("analog-output", None), Null)


def test_priority_slot() -> None:
    assert priority_slot("analog-output", PriorityValue(null=())) is None
    assert priority_slot("analog-output", PriorityValue(real=55.5)) == 55.5
    assert priority_slot("binary-output", PriorityValue(enumerated=1)) == 1
    assert priority_slot("multi-state-output", PriorityValue(unsigned=3)) == 3
    assert priority_slot("integer-value", PriorityValue(integer=-2)) == -2
    assert priority_slot("binary-value", PriorityValue(boolean=True)) is True


def test_units_name() -> None:
    assert units_name(EngineeringUnits("degrees-celsius")) == "degrees-celsius"
    assert units_name(98) == "percent"
    assert units_name(EngineeringUnits("no-units")) is None
    assert units_name(300) is None
    assert units_name(None) is None


def test_present_value_type() -> None:
    assert present_value_type("analog-value") is Real
    assert present_value_type("binary-output") is BinaryPV
    assert present_value_type("multi-state-input") is Unsigned


def test_clean_text() -> None:
    assert clean_text("Supply\x00Air\nTemp ") == "Supply Air Temp"
    assert len(clean_text("x" * 1000)) == 256
    assert clean_text(None) == ""


def test_parse_host_port() -> None:
    assert parse_host_port("10.0.2.10") == ("10.0.2.10", 47808)
    assert parse_host_port("10.0.2.10:47809") == ("10.0.2.10", 47809)
    assert parse_host_port("10.0.2.10", 1234) == ("10.0.2.10", 1234)
    for bad in ("", "ahu.local", "10.0.2.10:", "10.0.2.10:x", "10.0.2.10:70000", "10.0.2:1"):
        with pytest.raises(InvalidRequest):
            parse_host_port(bad)


def test_format_address() -> None:
    assert format_address(IPv4Address("10.0.2.10")) == "10.0.2.10:47808"
    assert format_address(IPv4Address("127.0.0.1:40000")) == "127.0.0.1:40000"


def test_bacnet_errors_become_hub_errors() -> None:
    assert isinstance(bacnet_error("ahu", AbortPDU(reason=65)), DeviceTimeout)
    abort = bacnet_error("ahu", AbortPDU(reason=4))
    assert isinstance(abort, BacnetError) and abort.kind == "abort"
    assert abort.reason == "segmentation-not-supported"
    reject = bacnet_error("ahu", RejectPDU(reason=9))
    assert isinstance(reject, BacnetError) and (reject.kind, reject.reason) == ("reject", "unrecognized-service")
    error = bacnet_error("ahu", Error(errorClass="property", errorCode="write-access-denied", service_choice=15))
    assert isinstance(error, BacnetError)
    assert (error.kind, error.error_class, error.reason) == ("error", "property", "write-access-denied")
    assert "write-access-denied" in str(error)


def _settings(**values: object) -> object:
    return BacnetIpDriver(DriverContext("hq", lambda r: None, settings=dict(values))).settings


def test_settings_defaults() -> None:
    s = _settings()
    assert (s.interface, s.port, s.broadcast) == ("0.0.0.0", 47808, ("255.255.255.255", 47808))  # type: ignore[attr-defined]
    assert (s.device_instance, s.device_name, s.max_objects) == (4194000, "uc-hub", 256)  # type: ignore[attr-defined]
    assert s.poll_interval_s == 5.0 and s.cov  # type: ignore[attr-defined]


def test_settings_broadcast() -> None:
    assert _settings(interface="10.0.2.5/24").broadcast == ("10.0.2.255", 47808)  # type: ignore[attr-defined]
    assert _settings(interface="10.0.2.5").broadcast is None  # type: ignore[attr-defined]
    assert _settings(broadcast=None).broadcast is None  # type: ignore[attr-defined]
    assert _settings(broadcast="10.0.9.255:47809").broadcast == ("10.0.9.255", 47809)  # type: ignore[attr-defined]
    assert _settings(port=0).broadcast is None  # type: ignore[attr-defined]
    targets = _settings(discover_targets=["10.0.3.255", "10.0.4.7:47809"]).discover_targets  # type: ignore[attr-defined]
    assert targets == (("10.0.3.255", 47808), ("10.0.4.7", 47809))


@pytest.mark.parametrize("values", [
    {"interface": "eth0"}, {"port": 70000}, {"port": "47808"}, {"timeout_s": 0}, {"retries": -1},
    {"device_instance": 4194303}, {"discover_targets": "10.0.0.255"}, {"broadcast": "nowhere"},
    {"poll_interval_s": True}, {"timeout_s": math.inf}, {"timeout_s": 1e9}, {"refresh_s": math.nan},
])
def test_settings_rejected(values: dict[str, object]) -> None:
    with pytest.raises(InvalidRequest):
        _settings(**values)
