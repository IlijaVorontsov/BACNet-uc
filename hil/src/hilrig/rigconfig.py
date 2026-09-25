"""Known state of the BACnet DUT: golden configuration documents pushed over SMP (D25, D30).

Flashing never erases the F767's external NOR, so ``/lfs/cfg`` survives between runs. At
session start the ``rig_config`` fixture therefore uploads golden ``device.json``,
```io.json`` and ``apps.json``, checks each file's SHA-256 on the node, runs ``uc_node reload
all``, reboots when the node reports ``reboot_required`` (instance or network changes), and
checks the instance (``uc_node info``) and the Object_Name (``uc_node prop_read``).

The documents are derived from bench.yml and the BACnet branch's ``schemas/examples``
(docs/configuration.md): the examples give the document layout and the BACnet options, the
bench gives the device instance and name of this slot (D30), DHCP on, and one IO point per
base catalog channel with the channel's number as instance (``do3`` -> BO:3, ``di1`` -> BI:1,
debounce 20 ms), which IO-01, IO-04 and PERS-01 use. They are validated against the
schemas when jsonschema is installed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hilrig.bench import Bench, CatalogChannel
from hilrig.smp import Smp

CFG_DIR = "/lfs/cfg"
DOCS = ("device", "io", "apps")
OBJECT_TYPES = {"di": "binary-input", "do": "binary-output", "ai": "analog-input", "ao": "analog-output"}
DEBOUNCE_MS = 20  # docs/io.md default, stated explicitly: IO-04 relies on it
SAMPLE_MS = 100

# Fallback templates when the branch's examples are not at hand (same layout).
_DEVICE_TEMPLATE: dict[str, Any] = {
    "schema": 1,
    "device": {"instance": 260001, "name": "bacnet-uc"},
    "bacnet": {"udp_port": 47808, "apdu_timeout_ms": 3000, "apdu_retries": 3},
    "log": {"level": "inf"},
}


class RigConfigError(RuntimeError):
    """A document did not arrive intact, or the node did not take it."""


def _load(examples: Path | None, name: str) -> dict[str, Any] | None:
    path = examples / f"{name}.json" if examples else None
    if path is None or not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def io_point(chan: CatalogChannel) -> dict[str, Any]:
    """The io.json point that binds ``chan`` to the object of the same number."""
    point: dict[str, Any] = {
        "channel": chan.name,
        "type": OBJECT_TYPES[chan.kind],
        "instance": int(chan.name[2:]),
        "name": f"HIL {chan.name}",
        "sample_ms": SAMPLE_MS,
    }
    if chan.kind == "di":
        point["debounce_ms"] = DEBOUNCE_MS
    return point


def golden(bench: Bench, examples: Path | None = None, **device: str) -> dict[str, dict[str, Any]]:
    """The golden documents for ``bench`` (``device`` overrides name, description, location).

    ``examples`` is the BACnet branch's ``schemas/examples`` directory.
    """
    template = _load(examples, "device") or _DEVICE_TEMPLATE
    dev = copy.deepcopy(template)
    dev["device"] = {
        "instance": bench.dut.bacnet_instance,
        "name": device.get("name", f"hil-{bench.name}"),
        "description": device.get("description", f"HIL bench {bench.name} ({bench.dut.profile})"),
        "location": device.get("location", "HIL rig"),
    }
    dev["network"] = {"dhcp": True}  # the example's static address is not the rig's (dnsmasq)
    dev["bacnet"] = {k: v for k, v in dev.get("bacnet", {}).items() if k != "static_bindings"}
    dev["bacnet"]["udp_port"] = 47808
    points = [io_point(c) for c in bench.catalog if not c.hil_io]
    io = {"schema": (_load(examples, "io") or {"schema": 1})["schema"], "points": points}
    apps = {"schema": (_load(examples, "apps") or {"schema": 1})["schema"], "apps": []}
    return {"device": dev, "io": io, "apps": apps}


def encode(doc: Mapping[str, Any]) -> bytes:
    """The exact bytes uploaded (and hashed): compact, key order kept, newline-terminated."""
    return (json.dumps(doc, separators=(",", ":")) + "\n").encode()


def validate(docs: Mapping[str, Mapping[str, Any]], schemas: Path) -> list[str]:
    """Validate each document against ``schemas/<name>.schema.json``; return what was skipped.

    Raises jsonschema's ValidationError for an invalid document.
    """
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed: documents not validated"]
    skipped = []
    for name, doc in docs.items():
        schema = schemas / f"{name}.schema.json"
        if not schema.exists():
            skipped.append(f"{schema} not found")
            continue
        jsonschema.validate(doc, json.loads(schema.read_text()))
    return skipped


@dataclass
class ApplyReport:
    """What :meth:`RigConfig.apply` did."""

    uploaded: list[str] = field(default_factory=list)
    sha256: dict[str, str] = field(default_factory=dict)
    reboot_required: bool = False
    rebooted_in_s: float | None = None
    node: dict[str, Any] = field(default_factory=dict)
    object_name: str | None = None


class RigConfig:
    """Pushes documents to the DUT and verifies them."""

    def __init__(
        self, smp: Smp, docs: Mapping[str, Mapping[str, Any]], *, reboot_timeout: float = 90.0
    ) -> None:
        self.smp = smp
        self.docs = {k: dict(v) for k, v in docs.items()}
        self.reboot_timeout = reboot_timeout

    def apply(self, **override: Mapping[str, Any]) -> ApplyReport:
        """Upload changed documents, verify SHA-256, reload all, reboot if required, check.

        ``override`` replaces documents for this call only (PERS-01 pushes its own
        device.json, then re-applies the golden one).
        """
        docs = {**self.docs, **override}
        report = ApplyReport()
        for name in DOCS:
            data = encode(docs[name])
            path = f"{CFG_DIR}/{name}.json"
            digest = hashlib.sha256(data).digest()
            if self.smp.sha256(path) != digest:
                self.smp.upload(path, data)
                report.uploaded.append(name)
            if self.smp.sha256(path) != digest:
                raise RigConfigError(f"{path}: SHA-256 on the node differs from the uploaded document")
            report.sha256[name] = digest.hex()
        report.reboot_required = self.smp.reload("all")
        if report.reboot_required:
            start = time.monotonic()
            self.smp.reset()
            time.sleep(2.0)  # let the node go down before polling it
            self.smp.wait_up(self.reboot_timeout)
            report.rebooted_in_s = time.monotonic() - start
        report.node = self.smp.node_info()
        device = docs["device"]["device"]
        instance = report.node.get("device", {}).get("instance")
        if instance != device["instance"]:
            raise RigConfigError(
                f"node reports instance {instance} after the push, expected {device['instance']}"
            )
        # uc_node info reports the name the BACnet thread published at start, which a reload
        # does not refresh (firmware 2161be7); the Object_Name itself is the applied value.
        name = self.smp.prop_read("device", instance, "object-name")
        if name != device["name"]:
            raise RigConfigError(f"Object_Name is {name!r} after the push, expected {device['name']!r}")
        report.object_name = name
        return report
