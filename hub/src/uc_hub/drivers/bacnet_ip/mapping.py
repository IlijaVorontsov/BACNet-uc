"""BACnet object types as hub points, and present-value conversion both ways.

Only object types with a scalar present value become points. Datatype mapping
(present value on the wire -> ``Point.datatype`` -> ``core.types.Value``):

=====================================================  ================  =======  ==========================
Object types                                           BACnet datatype   Point    Value
=====================================================  ================  =======  ==========================
analog-input/-output/-value, pulse-converter           REAL              real     float
large-analog-value                                     Double            real     float
binary-input/-output/-value                            BACnetBinaryPV    enum     0 = inactive, 1 = active
multi-state-input/-output/-value                       Unsigned          int      state number, 1..n
integer-value                                          INTEGER           int      int
positive-integer-value, accumulator                    Unsigned          int      int
characterstring-value                                  CharacterString   string   str
=====================================================  ================  =======  ==========================

Writes accept the hub forms above plus the obvious spellings (``"active"``,
``True``, numeric strings); ``None`` becomes BACnet NULL, which relinquishes
a priority slot. Units are the BACnet engineering unit names bacpypes3 prints
(``degrees-celsius``); ``no-units`` maps to ``None``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal

from bacpypes3.basetypes import BinaryPV, EngineeringUnits, PriorityValue
from bacpypes3.primitivedata import (
    Atomic,
    CharacterString,
    Double,
    Integer,
    Null,
    ObjectIdentifier,
    ObjectType,
    Real,
    Unsigned,
)
from bacpypes3.vendor import get_vendor_info

from ...core.errors import InvalidRequest
from ...core.ids import bacnet_obj
from ...core.types import Datatype, PointKind, Value

#: Wildcard device instance: a device answers a request for it as its own.
WILDCARD_DEVICE = 4194303
MAX_INSTANCE = 4194302

_OBJ_RE = re.compile(r"^(?P<type>[a-z][a-z-]*):(?P<instance>[0-9]{1,7})$")
_REAL_MAX = 3.4028234663852886e38
_TRUE_WORDS = frozenset({"active", "on", "true", "1"})
_FALSE_WORDS = frozenset({"inactive", "off", "false", "0"})
_TEXT_LIMIT = 256

Commandability = Literal["always", "maybe", "never"]


@dataclass(frozen=True, slots=True)
class TypeInfo:
    kind: PointKind
    datatype: Datatype
    #: The object has a ``units`` property.
    units: bool
    #: "always": the standard requires a priority array; "maybe": only when
    #: the vendor made the present value commandable; "never": no priority array.
    priority: Commandability


POINT_TYPES: dict[str, TypeInfo] = {
    "analog-input": TypeInfo(PointKind.INPUT, "real", True, "never"),
    "analog-output": TypeInfo(PointKind.OUTPUT, "real", True, "always"),
    "analog-value": TypeInfo(PointKind.VALUE, "real", True, "maybe"),
    "binary-input": TypeInfo(PointKind.INPUT, "enum", False, "never"),
    "binary-output": TypeInfo(PointKind.OUTPUT, "enum", False, "always"),
    "binary-value": TypeInfo(PointKind.VALUE, "enum", False, "maybe"),
    "multi-state-input": TypeInfo(PointKind.INPUT, "int", False, "never"),
    "multi-state-output": TypeInfo(PointKind.OUTPUT, "int", False, "always"),
    "multi-state-value": TypeInfo(PointKind.VALUE, "int", False, "maybe"),
    "integer-value": TypeInfo(PointKind.VALUE, "int", True, "maybe"),
    "positive-integer-value": TypeInfo(PointKind.VALUE, "int", True, "maybe"),
    "large-analog-value": TypeInfo(PointKind.VALUE, "real", True, "maybe"),
    "characterstring-value": TypeInfo(PointKind.VALUE, "string", False, "maybe"),
    "accumulator": TypeInfo(PointKind.INPUT, "int", True, "never"),
    "pulse-converter": TypeInfo(PointKind.INPUT, "real", True, "never"),
}

_REAL_TYPES = frozenset({"analog-input", "analog-output", "analog-value", "pulse-converter"})
_UNSIGNED_TYPES = frozenset({"positive-integer-value", "accumulator"})


def parse_point_obj(obj: str) -> tuple[str, int]:
    """``("analog-value", 3)`` for a point object id; ``InvalidRequest`` for
    ids that are not a supported BACnet point type."""
    m = _OBJ_RE.match(obj)
    if m is None:
        raise InvalidRequest(f"{obj!r} is not a BACnet object id (expected <type>:<instance>)")
    type_name, instance = m["type"], int(m["instance"])
    if type_name not in POINT_TYPES:
        raise InvalidRequest(f"{type_name} objects are not points (no scalar present value)")
    if instance > MAX_INSTANCE:
        raise InvalidRequest(f"object instance {instance} out of range")
    return type_name, instance


def object_identifier(type_name: str, instance: int) -> ObjectIdentifier:
    return ObjectIdentifier((ObjectType(type_name), instance))


@cache
def present_value_type(type_name: str) -> type:
    """The bacpypes3 class of the standard present value of ``type_name``."""
    object_class = get_vendor_info(0).get_object_class(ObjectType(type_name))
    value_type: type = object_class.get_property_type("present-value")
    return value_type


def obj_text(objid: Any) -> str | None:
    """``"analog-value:3"`` for a bacpypes3 object identifier; None for
    proprietary types, which have no name."""
    try:
        type_name = str(objid[0])
        instance = int(objid[1])
    except (TypeError, ValueError, IndexError):
        return None
    if type_name.isdigit():
        return None
    return bacnet_obj(type_name, instance)


def clean_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    """Device-supplied text without control characters, capped: names and
    descriptions end up in the UI and in model prompts."""
    if value is None:
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value)).strip()
    return text[:limit]


def units_name(value: Any) -> str | None:
    """The engineering unit name; None for no-units and for proprietary
    unit numbers, which have no name."""
    if value is None:
        return None
    try:
        text = str(EngineeringUnits(value))
    except (TypeError, ValueError):
        return None
    if text == "no-units" or text.isdigit():
        return None
    return text


def to_value(type_name: str, raw: Any) -> Value:
    """A present value read from a device as the hub's ``Value``. Raises
    ``ValueError`` for values the hub cannot represent (NaN, infinity)."""
    if raw is None or isinstance(raw, Null):
        return None
    datatype = POINT_TYPES[type_name].datatype
    if datatype == "real":
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"present value is {number}")
        return number
    if datatype in ("enum", "int"):
        return int(raw)
    return str(raw)


def priority_slot(type_name: str, slot: PriorityValue) -> Value:
    """One priority-array entry as a ``Value`` (None for an empty slot)."""
    choice = getattr(slot, "_choice", None)
    if choice is None or choice == "null":
        return None
    raw = getattr(slot, choice)
    if choice in ("real", "double"):
        number = float(raw)
        return number if math.isfinite(number) else None
    if choice in ("enumerated", "unsigned", "integer"):
        return int(raw)
    if choice == "boolean":
        return bool(raw)
    return clean_text(raw)


def _number(type_name: str, value: Value) -> float:
    if isinstance(value, (bool, int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            raise InvalidRequest(f"{value!r} is not a number ({type_name})") from None
    else:
        raise InvalidRequest(f"{value!r} is not a valid {type_name} value")
    if not math.isfinite(number):
        raise InvalidRequest(f"{value!r} is not a finite number")
    return number


def _whole(type_name: str, value: Value, low: int, high: int) -> int:
    number = _number(type_name, value)
    if not number.is_integer() or not low <= number <= high:
        raise InvalidRequest(f"{type_name} takes a whole number in {low}..{high}, not {value!r}")
    return int(number)


def to_bacnet(type_name: str, value: Value) -> Atomic:
    """A hub value as the present-value datatype of ``type_name``;
    ``InvalidRequest`` when it cannot be one. ``None`` is NULL (relinquish)."""
    if value is None:
        return Null(())
    info = POINT_TYPES.get(type_name)
    if info is None:
        raise InvalidRequest(f"{type_name} objects are not points")
    if info.datatype == "real":
        number = _number(type_name, value)
        if type_name in _REAL_TYPES:
            if abs(number) > _REAL_MAX:
                raise InvalidRequest(f"{value!r} does not fit a BACnet REAL")
            return Real(number)
        return Double(number)
    if info.datatype == "enum":
        if isinstance(value, str) and value.strip().lower() in _TRUE_WORDS | _FALSE_WORDS:
            active = value.strip().lower() in _TRUE_WORDS
        else:
            active = bool(_whole(type_name, value, 0, 1))
        return BinaryPV("active" if active else "inactive")
    if info.datatype == "string":
        if not isinstance(value, str):
            raise InvalidRequest(f"{type_name} takes a string, not {value!r}")
        return CharacterString(value)
    if type_name.startswith("multi-state-"):
        return Unsigned(_whole(type_name, value, 1, 2**32 - 1))
    if type_name in _UNSIGNED_TYPES:
        return Unsigned(_whole(type_name, value, 0, 2**32 - 1))
    return Integer(_whole(type_name, value, -(2**31), 2**31 - 1))
