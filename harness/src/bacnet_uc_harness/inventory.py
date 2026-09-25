# SPDX-License-Identifier: Apache-2.0
"""Node inventory: ``<home>/.bacnet-uc/inventory.yaml``.

``<home>`` is ``BACNET_UC_HOME`` or the current directory. The inventory maps
node names to how the harness reaches them:

.. code-block:: yaml

    nodes:
      sensor:
        transport: udp          # udp | serial | sim
        host: 192.168.10.51     # udp/sim: management (SMP) address
        port: 1337
        bacnet_address: 192.168.10.51:47808
        board: nucleo_f767zi
      bench:
        transport: serial
        device: /dev/ttyACM0
        baud: 115200
        board: frdm_mcxn947/mcxn947/cpu0

Simulated nodes started by the harness are added with ``transport: sim``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from bacnet_uc_harness import paths
from bacnet_uc_harness.errors import HarnessError

TransportKind = Literal["udp", "serial", "sim"]
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


@dataclass
class InventoryNode:
    name: str
    transport: TransportKind = "udp"
    host: str | None = None
    port: int = 1337
    device: str | None = None
    baud: int = 115200
    bacnet_address: str | None = None
    board: str | None = None
    device_instance: int | None = None
    system: str | None = None
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise HarnessError(f"invalid node name {self.name!r} (lowercase letters, digits, '-')")
        if self.transport not in ("udp", "serial", "sim"):
            raise HarnessError(f"invalid transport {self.transport!r} (udp, serial or sim)")
        if self.transport in ("udp", "sim") and not self.host:
            raise HarnessError(f"node {self.name!r}: transport {self.transport} needs a host")
        if self.transport == "serial" and not self.device:
            raise HarnessError(f"node {self.name!r}: transport serial needs a device")
        self.port = int(self.port)
        self.baud = int(self.baud)

    @property
    def smp_target(self) -> str:
        """``udp:<host>:<port>`` or ``serial:<device>:<baud>``."""
        if self.transport == "serial":
            return f"serial:{self.device}:{self.baud}"
        return f"udp:{self.host}:{self.port}"

    @property
    def bacnet_target(self) -> str | None:
        """BACnet/IP ``host:port`` (the SMP host with port 47808 by default)."""
        if self.bacnet_address:
            return self.bacnet_address if ":" in self.bacnet_address else \
                f"{self.bacnet_address}:47808"
        if self.transport in ("udp", "sim") and self.host:
            return f"{self.host}:47808"
        return None

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in asdict(self).items() if v not in (None, "", {})}
        d.pop("name", None)
        if self.transport == "serial":
            d.pop("port", None)
        else:
            d.pop("baud", None)
        return d

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, **self.to_dict(), "smp": self.smp_target,
                "bacnet": self.bacnet_target}

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> InventoryNode:
        known = {f for f in cls.__dataclass_fields__ if f not in ("name", "extra")}
        kwargs = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known and k != "extra"}
        extra.update(d.get("extra", {}) or {})
        return cls(name=name, extra=extra, **kwargs)


def inventory_path(home: Path | str | None = None) -> Path:
    base = Path(home).expanduser().resolve() if home else paths.home_dir()
    return base / paths.STATE_DIR_NAME / "inventory.yaml"


class Inventory:
    """Load, edit and save the inventory file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or inventory_path()
        self.nodes: dict[str, InventoryNode] = {}

    @classmethod
    def load(cls, home: Path | str | None = None, path: Path | str | None = None) -> Inventory:
        inv = cls(Path(path) if path else inventory_path(home))
        if inv.path.is_file():
            try:
                doc = yaml.safe_load(inv.path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                raise HarnessError(f"{inv.path}: {exc}") from exc
            nodes = doc.get("nodes", {}) if isinstance(doc, dict) else {}
            if not isinstance(nodes, dict):
                raise HarnessError(f"{inv.path}: 'nodes' must be a mapping")
            for name, entry in nodes.items():
                inv.nodes[str(name)] = InventoryNode.from_dict(str(name), dict(entry or {}))
        return inv

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"nodes": {n.name: n.to_dict() for n in self.nodes.values()}}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# BACnet-uc harness inventory\n" + yaml.safe_dump(doc, sort_keys=False),
                       encoding="utf-8")
        tmp.replace(self.path)
        return self.path

    def add(self, node: InventoryNode, replace: bool = False) -> InventoryNode:
        if node.name in self.nodes and not replace:
            raise HarnessError(f"node {node.name!r} already in the inventory (use replace)")
        self.nodes[node.name] = node
        return node

    def remove(self, name: str) -> InventoryNode:
        try:
            return self.nodes.pop(name)
        except KeyError:
            raise HarnessError(f"node {name!r} not in the inventory {self.path}") from None

    def get(self, name: str) -> InventoryNode:
        try:
            return self.nodes[name]
        except KeyError:
            known = ", ".join(sorted(self.nodes)) or "none"
            raise HarnessError(f"node {name!r} not in the inventory (known: {known}); add it "
                               "with add_node / 'bacnet-uc node add'") from None

    def list(self) -> list[InventoryNode]:
        return [self.nodes[k] for k in sorted(self.nodes)]

    def __contains__(self, name: object) -> bool:
        return name in self.nodes
