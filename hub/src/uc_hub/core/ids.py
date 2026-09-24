"""Point identifiers: ``<site>/<device>/<object>``.

``object`` is ``<bacnet-type-name>:<instance>`` for BACnet points
(``analog-input:1``) and a dotted path for MQTT points
(``telemetry.uptime_s``, ``co2``). Site and device names follow the manifest
patterns (``[a-z0-9][a-z0-9-]*``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NAME = r"[a-z0-9][a-z0-9_-]*"
_OBJ = r"[A-Za-z0-9_.:-]+"
_POINT_RE = re.compile(rf"^(?P<site>{_NAME})/(?P<device>{_NAME})/(?P<obj>{_OBJ})$")
_BACNET_OBJ_RE = re.compile(r"^(?P<type>[a-z][a-z-]*):(?P<instance>[0-9]+)$")


@dataclass(frozen=True, slots=True, order=True)
class PointRef:
    site: str
    device: str
    obj: str

    def __str__(self) -> str:
        return f"{self.site}/{self.device}/{self.obj}"

    @classmethod
    def parse(cls, text: str, default_site: str | None = None) -> "PointRef":
        """Parse ``site/device/obj``; ``device/obj`` is accepted when
        ``default_site`` is given (the manifest's ``links`` use that form)."""
        text = text.strip()
        m = _POINT_RE.match(text)
        if m:
            return cls(m["site"], m["device"], m["obj"])
        if default_site is not None:
            m = _POINT_RE.match(f"{default_site}/{text}")
            if m:
                return cls(m["site"], m["device"], m["obj"])
        raise ValueError(f"not a point id: {text!r} (expected site/device/object)")

    @property
    def bacnet(self) -> tuple[str, int] | None:
        """``("analog-input", 1)`` for BACnet object ids, else None."""
        m = _BACNET_OBJ_RE.match(self.obj)
        if not m:
            return None
        return m["type"], int(m["instance"])


def bacnet_obj(type_name: str, instance: int) -> str:
    return f"{type_name}:{instance}"


# ASHRAE 135 object type numbers for the types BACnet-uc and the hub use.
OBJECT_TYPES: dict[str, int] = {
    "analog-input": 0,
    "analog-output": 1,
    "analog-value": 2,
    "binary-input": 3,
    "binary-output": 4,
    "binary-value": 5,
    "device": 8,
    "multi-state-input": 13,
    "multi-state-output": 14,
    "multi-state-value": 19,
}
OBJECT_TYPE_NAMES: dict[int, str] = {v: k for k, v in OBJECT_TYPES.items()}

#: Object types whose present value has a priority array.
COMMANDABLE_TYPES = {"analog-output", "binary-output", "multi-state-output"}
#: Value objects are commandable when they expose a priority array; BACnet-uc
#: value objects do (the firmware uses the bacnet-stack defaults).
OPTIONALLY_COMMANDABLE_TYPES = {"analog-value", "binary-value", "multi-state-value"}
