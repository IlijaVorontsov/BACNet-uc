"""BACnet text names <-> numbers for object types, properties and units.

The management protocol accepts object types and properties "as the BACnet
text names used by the BACnet stack or as numbers" and returns units as the
ENUMERATED number. bacnet-stack's names are the ASHRAE 135 identifiers in
kebab case (``analog-input``, ``present-value``, ``degrees-celsius``), the same
spelling bacpypes3 uses for ``str()`` of its enumerations, so its tables serve
both directions and cover every standard value.
"""

from __future__ import annotations

from functools import lru_cache

from bacpypes3.basetypes import EngineeringUnits, PropertyIdentifier
from bacpypes3.primitivedata import ObjectType

from ...core.errors import InvalidRequest

PROP_PRESENT_VALUE = 85
PROP_PRIORITY_ARRAY = 87
PROP_RELINQUISH_DEFAULT = 104
PROP_UNITS = 117
UNITS_NO_UNITS = 95

_MAX_ENUM = 0x3FFFFF


def _number(cls: type, value: str | int, what: str) -> int:
    if isinstance(value, bool):
        raise InvalidRequest(f"{what} must be a name or a number, not {value!r}")
    if isinstance(value, int):
        if not 0 <= value <= _MAX_ENUM:
            raise InvalidRequest(f"{what} {value} out of range")
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _number(cls, int(text), what)
        return _lookup(cls, text, what)
    raise InvalidRequest(f"{what} must be a name or a number, not {type(value).__name__}")


@lru_cache(maxsize=1024)
def _lookup(cls: type, text: str, what: str) -> int:
    try:
        return int(cls(text))
    except (ValueError, TypeError, KeyError) as e:
        raise InvalidRequest(f"unknown {what} {text!r}") from e


@lru_cache(maxsize=1024)
def _name(cls: type, number: int) -> str | None:
    """Kebab-case name, None when bacpypes3 does not know the number
    (its ``str()`` then returns the digits)."""
    text = str(cls(number))
    return None if text.isdigit() else text


def object_type_id(value: str | int) -> int:
    return _number(ObjectType, value, "object type")


def object_type_name(value: str | int) -> str:
    """Name for display and point ids; the number as text when unknown."""
    number = object_type_id(value)
    return _name(ObjectType, number) or str(number)


def property_id(value: str | int) -> int:
    return _number(PropertyIdentifier, value, "property")


def property_name(value: str | int) -> str:
    number = property_id(value)
    return _name(PropertyIdentifier, number) or str(number)


def units_id(value: str | int) -> int:
    return _number(EngineeringUnits, value, "units")


def units_name(value: str | int | None) -> str | None:
    """Text name of an engineering unit; None for no-units or unknown input."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = units_id(value)
    except InvalidRequest:
        return None
    if number == UNITS_NO_UNITS:
        return None
    return _name(EngineeringUnits, number) or str(number)


def wire_object_type(value: str | int) -> str | int:
    """What to send for ``type``: the stack's text name when there is one."""
    number = object_type_id(value)
    return _name(ObjectType, number) or number


def wire_property(value: str | int) -> str | int:
    number = property_id(value)
    return _name(PropertyIdentifier, number) or number
