"""Simulated BACnet-uc site: SMP nodes, a room model, the BACnet network
between nodes and Python emulations of the uc-link and thermostat WASM apps.

``smp_node`` is imported on first use so ``python -m uc_hub.sim.smp_node``
does not load the module twice.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from .network import LinkSpec, ManualClock, SimNetwork, ThermostatApp, UcLinkApp
from .room import RoomModel

if TYPE_CHECKING:
    from .smp_node import EXAMPLE_IO, SimFeatures, SimNode, start_sim_nodes

_LAZY = frozenset({"EXAMPLE_IO", "SimFeatures", "SimNode", "start_sim_nodes"})

__all__ = [
    "EXAMPLE_IO",
    "LinkSpec",
    "ManualClock",
    "RoomModel",
    "SimFeatures",
    "SimNetwork",
    "SimNode",
    "ThermostatApp",
    "UcLinkApp",
    "start_sim_nodes",
]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        return getattr(importlib.import_module(".smp_node", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
