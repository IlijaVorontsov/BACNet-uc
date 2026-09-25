# SPDX-License-Identifier: Apache-2.0
"""BACnet enumeration tables and name helpers."""

from __future__ import annotations

import re

import pytest

from bacnet_uc_harness.bacnet import enums

BACTEXT = "modules/lib/bacnet/stack/src/bacnet/bactext.c"


def test_object_types_complete() -> None:
    # all standard object types 0..64 of the stack are present
    assert set(enums.OBJECT_TYPES.values()) >= set(range(0, 65))
    assert enums.OBJECT_TYPES["analog-input"] == 0
    assert enums.OBJECT_TYPES["multi-state-value"] == 19
    assert enums.OBJECT_TYPES["network-port"] == 56
    assert enums.OBJECT_TYPES["device"] == 8


def test_common_properties() -> None:
    expected = {
        "object-identifier": 75,
        "object-name": 77,
        "object-type": 79,
        "present-value": 85,
        "description": 28,
        "status-flags": 111,
        "event-state": 36,
        "out-of-service": 81,
        "units": 117,
        "priority-array": 87,
        "relinquish-default": 104,
        "cov-increment": 22,
        "object-list": 76,
        "number-of-states": 74,
        "state-text": 110,
        "vendor-name": 121,
        "vendor-identifier": 120,
        "model-name": 70,
        "firmware-revision": 44,
        "application-software-version": 12,
        "protocol-version": 98,
        "protocol-revision": 139,
        "max-apdu-length-accepted": 62,
        "segmentation-supported": 107,
        "location": 58,
        "system-status": 112,
        "polarity": 84,
        "active-text": 4,
        "inactive-text": 46,
        "property-list": 371,
    }
    for name, number in expected.items():
        assert enums.PROPERTIES[name] == number, name
    assert len(enums.PROPERTIES) > 500


def test_units() -> None:
    assert enums.UNITS["degrees-celsius"] == 62
    assert enums.UNITS["percent"] == 98
    assert enums.UNITS["no-units"] == 95
    assert enums.UNITS["millivolts"] == 124
    assert len(enums.UNITS) > 400


def test_tables_match_bactext(repo_root) -> None:  # type: ignore[no-untyped-def]
    """Every name in the generated tables occurs in the stack's bactext.c."""
    path = repo_root.parent / BACTEXT
    if not path.exists():
        pytest.skip("bacnet-stack not in the west workspace")
    text = path.read_text()
    names = set(re.findall(r'"([^"]+)"', text))
    for table in (enums.OBJECT_TYPES, enums.PROPERTIES, enums.UNITS):
        missing = [n for n in table if n not in names]
        assert not missing, missing[:5]


def test_lookup_helpers() -> None:
    assert enums.object_type_number("analog-input") == 0
    assert enums.object_type_number("Analog_Input") == 0
    assert enums.object_type_number("AI") == 0
    assert enums.object_type_number("msv") == 19
    assert enums.object_type_number("19") == 19
    assert enums.object_type_number(130) == 130  # proprietary
    assert enums.property_number("present-value") == 85
    assert enums.property_number("present_value") == 85
    assert enums.property_number("Number-Of-APDU-Retries") == 73
    assert enums.property_number(512) == 512
    assert enums.unit_number("degrees-celsius") == 62
    assert enums.object_type_name(2) == "analog-value"
    assert enums.object_type_name(600) == "600"
    assert enums.property_name(85) == "present-value"
    assert enums.error_code_name(31) == "unknown-object"
    assert enums.error_class_name(2) == "property"
    assert enums.abort_reason_name(4) == "segmentation-not-supported"
    assert enums.reject_reason_name(9) == "unrecognized-service"
    assert enums.reject_reason_name(70) == "proprietary-70"
    for bad in ("no-such-type", -1, 1024, True, 1.5):
        with pytest.raises(ValueError):
            enums.object_type_number(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        enums.property_number("no-such-property")
    with pytest.raises(ValueError):
        enums.unit_number("furlongs-per-fortnight")


def test_object_refs() -> None:
    assert enums.parse_object_ref("analog-input:1") == (0, 1)
    assert enums.parse_object_ref("analog-value,10") == (2, 10)
    assert enums.parse_object_ref("binary-output 3") == (4, 3)
    assert enums.parse_object_ref("AV:4194302") == (2, 4194302)
    assert enums.parse_object_ref("8:1001") == (8, 1001)
    assert enums.parse_object_ref(("device", 5)) == (8, 5)
    assert enums.format_object_ref(0, 1) == "analog-input:1"
    assert enums.format_object_ref("msv", 2) == "multi-state-value:2"
    for bad in ("analog-input", "analog-input:x", "analog-input:4194304", "nope:1"):
        with pytest.raises(ValueError):
            enums.parse_object_ref(bad)


def test_services_and_errors() -> None:
    assert enums.CONFIRMED_SERVICES["read-property"] == 12
    assert enums.CONFIRMED_SERVICES["write-property"] == 15
    assert enums.UNCONFIRMED_SERVICES["who-is"] == 8
    assert enums.UNCONFIRMED_SERVICES["i-am"] == 0
    assert enums.ERROR_CODES["unknown-property"] == 32
    assert enums.ERROR_CODES["write-access-denied"] == 40
    assert enums.SEGMENTATION["no-segmentation"] == 3
