"""Core data types shared by every uc-hub module.

This module is a contract: drivers, the manifest engine, the tool layer, the
agent loop and the HTTP API all exchange these types. The JSON forms produced
by ``to_json`` are the shapes documented in ``docs/ai-harness/API.md``; keep the
two in sync.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from .ids import PointRef

#: A point value as it crosses module boundaries. BACnet REAL/DOUBLE -> float,
#: UNSIGNED/SIGNED/ENUMERATED -> int, binary present values -> int 0/1,
#: BOOLEAN -> bool, CharacterString -> str, NULL -> None.
Value = float | int | bool | str | None

#: ``Point.datatype``, one canonical meaning whatever the protocol:
#: ``real`` float; ``int`` whole number (multi-state state numbers 1..n,
#: counters); ``enum`` two-state value 0 (inactive, off) or 1 (active, on),
#: the BACnet binary present value; ``bool`` true/false; ``string`` text.
Datatype = Literal["real", "int", "bool", "enum", "string"]
Tier = Literal["R", "S", "L", "C"]


class ProtocolName(StrEnum):
    BACNET_UC = "bacnet-uc"
    BACNET_IP = "bacnet-ip"
    MQTT = "mqtt"


class PointKind(StrEnum):
    INPUT = "input"
    OUTPUT = "output"
    VALUE = "value"


class SafetyClass(StrEnum):
    NORMAL = "normal"
    CRITICAL = "critical"
    LIFE_SAFETY = "life-safety"


class Quality(StrEnum):
    GOOD = "good"
    STALE = "stale"
    FAULT = "fault"
    OFFLINE = "offline"


@dataclass(slots=True)
class Point:
    """One readable (and maybe writable) value of a device."""

    ref: PointRef
    name: str
    kind: PointKind
    datatype: Datatype = "real"
    units: str | None = None
    writable: bool = False
    #: True when the point has a BACnet priority array (writes take a priority).
    commandable: bool = False
    tags: list[str] = field(default_factory=list)
    safety: SafetyClass = SafetyClass.NORMAL
    description: str = ""
    #: Human-readable origin, e.g. "io:ai0", "app:thermostat", "system",
    #: "mqtt:sensors/r204/co2 $.ppm", "bridge:hq/r204-co2/co2".
    source: str = ""
    space: str | None = None
    #: Driver-private addressing (object type number, topic, JSONPath, ...).
    #: Never shown to the model; not part of the JSON form.
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.ref),
            "device": self.ref.device,
            "obj": self.ref.obj,
            "name": self.name,
            "kind": self.kind.value,
            "datatype": self.datatype,
            "units": self.units,
            "writable": self.writable,
            "commandable": self.commandable,
            "tags": list(self.tags),
            "safety": self.safety.value,
            "description": self.description,
            "source": self.source,
            "space": self.space,
        }


@dataclass(slots=True)
class Reading:
    ref: PointRef
    value: Value
    ts: float = field(default_factory=time.time)
    quality: Quality = Quality.GOOD
    #: Active priority of a commandable point (1..16), when the driver knows it.
    priority: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": str(self.ref),
            "value": self.value,
            "ts": self.ts,
            "quality": self.quality.value,
        }
        if self.priority is not None:
            out["priority"] = self.priority
        if self.error:
            out["error"] = self.error
        return out


@dataclass(slots=True)
class DeviceRecord:
    site: str
    name: str
    protocol: ProtocolName
    #: "10.0.2.51:1337" (bacnet-uc SMP), "10.0.2.10:47808" (bacnet-ip),
    #: "bacnet-uc/zephyr-0a1b" (mqtt topic prefix).
    address: str
    online: bool = False
    #: True for devices whose configuration the hub owns (BACnet-uc nodes).
    managed: bool = False
    model: str = ""
    firmware: str = ""
    hwid: str = ""
    #: BACnet device instance, when the device has one.
    instance: int | None = None
    space: str | None = None
    last_seen: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_json(self, point_count: int | None = None) -> dict[str, Any]:
        out = {
            "name": self.name,
            "protocol": self.protocol.value,
            "address": self.address,
            "online": self.online,
            "managed": self.managed,
            "model": self.model,
            "firmware": self.firmware,
            "hwid": self.hwid,
            "instance": self.instance,
            "space": self.space,
            "last_seen": self.last_seen,
        }
        if point_count is not None:
            out["points"] = point_count
        return out


@dataclass(slots=True)
class DeviceDescription:
    device: DeviceRecord
    points: list[Point] = field(default_factory=list)
    #: Installed applications (BACnet-uc ``<app status>`` maps), empty otherwise.
    apps: list[dict[str, Any]] = field(default_factory=list)
    #: Anything else a driver wants to show (node info, MQTT info payload, ...).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "device": self.device.to_json(len(self.points)),
            "points": [p.to_json() for p in self.points],
            "apps": self.apps,
            "extra": self.extra,
        }


@dataclass(slots=True)
class WriteResult:
    ref: PointRef
    ok: bool
    value: Value = None
    priority: int | None = None
    error: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": str(self.ref),
            "ok": self.ok,
            "value": self.value,
            "priority": self.priority,
            "error": self.error,
        }


@dataclass(slots=True)
class DiscoveredDevice:
    """A device seen on the network, before it is known to the site manifest."""

    protocol: ProtocolName
    address: str
    instance: int | None = None
    name: str = ""
    model: str = ""
    hwid: str = ""
    #: True when the device answered as a BACnet-uc node (SMP ``uc_node info``).
    bacnet_uc: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol.value,
            "address": self.address,
            "instance": self.instance,
            "name": self.name,
            "model": self.model,
            "hwid": self.hwid,
            "bacnet_uc": self.bacnet_uc,
            "extra": self.extra,
        }


ChangeKind = Literal[
    "upload-doc",      # write /lfs/cfg/<doc>.json on a node (payload: doc, content)
    "upload-file",     # write another file, e.g. a .wasm (payload: path, sha256, size)
    "install-app",     # uc_app install (payload: manifest)
    "remove-app",      # uc_app remove (payload: name)
    "reload",          # uc_node reload (payload: doc)
    "bridge-add",      # gateway bridge (payload: bridge)
    "bridge-remove",
    "bridge-update",
    "tags",            # tag changes in the building model (payload: point -> tags)
    "device-config",   # third-party device property writes (payload: writes)
]


@dataclass(slots=True)
class Change:
    """One step of a plan. Changes of one target are applied in list order."""

    id: str
    target: str  # device name, or "gateway"
    kind: ChangeKind
    summary: str
    #: Unified diff (or a short before/after) shown to the approver.
    diff: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    tier: Tier = "C"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "kind": self.kind,
            "summary": self.summary,
            "diff": self.diff,
            "tier": self.tier,
        }


@dataclass(slots=True)
class ChangeResult:
    change_id: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"change_id": self.change_id, "ok": self.ok, "detail": self.detail}


@dataclass(slots=True)
class Plan:
    id: str
    #: Draft manifest revision the plan was computed from.
    revision: int
    #: Revision that is live now; applying it again is the rollback.
    base_revision: int
    changes: list[Change] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    #: Warnings the approver must see (e.g. an app asks for "io" permission).
    warnings: list[str] = field(default_factory=list)
    #: Targets that could not be planned (target -> reason, e.g. the node did
    #: not answer). A plan with blocked targets is incomplete and is never
    #: applied, also when it is rebuilt from storage.
    blocked: dict[str, str] = field(default_factory=dict)

    @property
    def targets(self) -> list[str]:
        seen: list[str] = []
        for c in self.changes:
            if c.target not in seen:
                seen.append(c.target)
        return seen

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "base_revision": self.base_revision,
            "created_at": self.created_at,
            "targets": self.targets,
            "changes": [c.to_json() for c in self.changes],
            "warnings": list(self.warnings),
            "blocked": dict(self.blocked),
        }


TestStatus = Literal["pass", "fail", "error", "skipped", "running", "not-run"]


@dataclass(slots=True)
class TestResult:
    __test__ = False  # not a pytest test class

    name: str
    status: TestStatus
    duration_ms: int = 0
    #: Index of the step that failed, if any.
    failed_step: int | None = None
    detail: str = ""
    target: Literal["live", "sim"] = "live"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "failed_step": self.failed_step,
            "detail": self.detail,
            "target": self.target,
        }
