"""BIP-03 ReadProperty matrix of the Device object (release; hardware and SIL).

Catalogue BIP-03: all required Device properties read OK. The tshark-decoded value equals the
tool value. Firmware_Revision equals the artifact's CONFIG_UC_FW_VERSION. An unknown property
returns error unknown-property (32). An unknown object returns unknown-object (31). No
malformed frames.

Required properties: the Device object's R properties of ASHRAE 135 (Property_List required
since protocol revision 14; the DUT is revision 28). Wireshark decodes most values only as
text items (``vendor-identifier: (Unsigned) 260``), so the comparison reads those.
"""

from __future__ import annotations

import pytest

from hilrig.bacnet import Bacnet, BacnetError
from hilrig.bench import Bench
from hilrig.capture import Capture
from hilrig.release import DutImage

pytestmark = pytest.mark.release

REQUIRED = (
    "object-identifier",
    "object-name",
    "object-type",
    "system-status",
    "vendor-name",
    "vendor-identifier",
    "model-name",
    "firmware-revision",
    "application-software-version",
    "protocol-version",
    "protocol-revision",
    "protocol-services-supported",
    "protocol-object-types-supported",
    "object-list",
    "max-apdu-length-accepted",
    "segmentation-supported",
    "apdu-timeout",
    "number-of-apdu-retries",
    "device-address-binding",
    "database-revision",
    "property-list",
)
# scalar values whose decoded text must contain the tool's value
COMPARED = (
    "vendor-name",
    "vendor-identifier",
    "model-name",
    "firmware-revision",
    "application-software-version",
    "protocol-version",
    "protocol-revision",
    "max-apdu-length-accepted",
    "apdu-timeout",
    "number-of-apdu-retries",
    "database-revision",
)
UNKNOWN_PROPERTY, UNKNOWN_OBJECT = "32", "31"
NO_SUCH_PROPERTY = 4194  # a property identifier the Device object does not have


@pytest.mark.capture("udp port 47808")
@pytest.mark.parametrize("app_image", ["bacnet"], indirect=True)
def test_bip03_device_read_property_matrix(
    app_image: DutImage, bacnet: Bacnet, capture: Capture, bench: Bench
) -> None:
    inst, dut = bench.dut.bacnet_instance, bench.dut.ip
    values = {prop: bacnet.read(inst, "device", inst, prop) for prop in REQUIRED}  # raises on any error
    assert values["object-identifier"] == f"(device, {inst})"
    assert values["firmware-revision"].strip('"') == app_image.fw_version, (
        "Firmware_Revision != CONFIG_UC_FW_VERSION"
    )
    with pytest.raises(BacnetError) as prop_err:
        bacnet.read(inst, "device", inst, str(NO_SUCH_PROPERTY))
    assert (prop_err.value.error_class, prop_err.value.error_code) == ("property", "unknown-property")
    with pytest.raises(BacnetError) as obj_err:
        bacnet.read(inst, "analog-input", 4194302, "present-value")
    assert (obj_err.value.error_class, obj_err.value.error_code) == ("object", "unknown-object")
    capture.stop()

    acks = capture.texts(f"bacapp.type == 3 && ip.src == {dut}")
    decoded: dict[str, str] = {}  # Wireshark spells some names in its own case: number-of-APDU-retries
    for row in acks:
        prop = next(
            (t.split(": ", 1)[1].split(" (")[0] for t in row.texts if t.startswith("Property Identifier:")),
            "",
        ).lower()
        for text in row.texts:
            if text.lower().startswith(f"{prop}: "):
                decoded[prop] = text
    for prop in COMPARED:
        tool = values[prop].strip('"')
        assert prop in decoded and tool in decoded[prop], (
            f"{prop}: tool {tool!r}, tshark {decoded.get(prop)!r}"
        )
    names = [
        r["bacapp.object_name"]
        for r in capture.rows(f"bacapp.type == 3 && ip.src == {dut}", "bacapp.object_name")
    ]
    assert values["object-name"].strip('"') in names, (
        f"object-name: tool {values['object-name']}, tshark {names}"
    )
    errors = capture.rows(f"bacapp.type == 5 && ip.src == {dut}", "bacapp.error_code")
    assert [e["bacapp.error_code"] for e in errors] == [UNKNOWN_PROPERTY, UNKNOWN_OBJECT]
