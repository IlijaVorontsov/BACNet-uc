"""A running hub over simulated BACnet-uc nodes: ``site.yaml`` and
``hub.yaml`` in a temporary directory, ``Services`` started on them."""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from uc_hub.core.ids import PointRef
from uc_hub.llm.base import LlmProvider
from uc_hub.runtime import Services, load_config
from uc_hub.sim import SimNetwork, SimNode
from uc_hub.tools import Prepared, ToolCallContext, ToolResult

SITE = "hq"

R204_IO: list[dict[str, Any]] = [
    {"channel": "di0", "type": "binary-input", "instance": 1, "name": "R204 Window"},
    {"channel": "do0", "type": "binary-output", "instance": 1, "name": "R204 Heater"},
    {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "R204 Temp", "units": "degrees-celsius",
     "scale": 0.1, "offset": -50.0},
    {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "R204 Valve", "units": "percent"},
]
R205_IO: list[dict[str, Any]] = [
    {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "R205 Temp", "units": "degrees-celsius",
     "scale": 0.1, "offset": -50.0},
    {"channel": "do0", "type": "binary-output", "instance": 1, "name": "R205 Smoke Damper"},
    {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "R205 Valve", "units": "percent"},
]
NODES: dict[str, dict[str, Any]] = {
    "r204-ctl": {"instance": 2041, "io": R204_IO, "location": "Room 204"},
    "r205-ctl": {"instance": 2051, "io": R205_IO, "location": "Room 205"},
}

DRIVERS: dict[str, Any] = {
    "bacnet_uc": {"timeout_s": 0.3, "retries": 1, "poll_interval_s": 0.05, "heartbeat_s": 0.2, "refresh_s": 0.3,
                  "discover_broadcast": ""},
}
SITE_OPTIONS = {"retry_min_s": 0.05, "retry_max_s": 0.5, "describe_timeout_s": 10.0}


def node_entry(name: str, port: int) -> dict[str, Any]:
    spec = NODES[name]
    return {
        "name": name,
        "board": "native_sim",
        "transport": {"kind": "udp", "host": "127.0.0.1", "port": port},
        "device": {"instance": spec["instance"], "name": name, "location": spec["location"]},
        "io": copy.deepcopy(spec["io"]),
    }


def site_doc(ports: dict[str, int]) -> dict[str, Any]:
    """The test building: two room controllers on floor 2, tests, tags, a
    life-safety damper and a deny pattern."""
    return {
        "apiVersion": "bacnet-uc/v1",
        "kind": "Site",
        "metadata": {"name": SITE, "description": "Test building"},
        "spaces": [
            {"id": "hq", "name": "HQ"},
            {"id": "f2", "name": "Floor 2", "parent": "hq"},
            {"id": "r204", "name": "Room 204", "parent": "f2"},
            {"id": "r205", "name": "Room 205", "parent": "f2"},
            {"id": "plant", "name": "Plant room", "parent": "hq"},
        ],
        "placement": {"r204-ctl": "r204", "r205-ctl": "r205"},
        "system": {
            "apiVersion": "bacnet-uc/v1",
            "kind": "System",
            "metadata": {"name": "hq-uc"},
            "nodes": [node_entry(name, port) for name, port in ports.items()],
            "tests": [
                {"name": "heater relay checkout", "steps": [
                    {"write": {"point": "r204-ctl/binary-output:1", "value": 1, "priority": 12}},
                    {"expect": {"point": "r204-ctl/binary-output:1", "op": "eq", "value": 1, "within_ms": 1000}},
                    {"write": {"point": "r204-ctl/binary-output:1", "value": None, "priority": 12}},
                ]},
                {"name": "cold room input", "steps": [
                    {"force": {"node": "r204-ctl", "channel": "ai0", "value": 650}},
                    {"expect": {"point": "r204-ctl/analog-input:1", "op": "approx", "value": 15.0,
                                "tolerance": 0.2, "within_ms": 2000}},
                    {"release": {"node": "r204-ctl", "channel": "ai0"}},
                ]},
            ],
        },
        "tags": {
            "r204-ctl/analog-input:1": ["Zone_Air_Temperature_Sensor"],
            "r205-ctl/analog-input:1": ["Zone_Air_Temperature_Sensor"],
            "r204-ctl/analog-output:1": ["Reheat_Valve_Command"],
        },
        "safety": {"r205-ctl/binary-output:1": "life-safety"},
        "policy": {"deny": ["r205-ctl/analog-output:*"], "max_writes_per_minute": 30},
    }


async def eventually(check: Callable[[], Any], timeout_s: float = 5.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or check}")
        await asyncio.sleep(0.02)


@dataclass
class Hub:
    path: Path
    net: SimNetwork
    nodes: dict[str, SimNode]
    services: Services
    hub_doc: dict[str, Any] = field(default_factory=dict)
    #: Services keyword arguments, kept for ``restart``.
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def site(self) -> Any:
        return self.services.site

    def ref(self, text: str) -> PointRef:
        return PointRef.parse(text, default_site=SITE)

    def ctx(self, *roles: str, user: str = "tester", run_id: str | None = None,
            call_id: str | None = None) -> ToolCallContext:
        return self.services.context(user, roles or ("admin",), run_id=run_id, call_id=call_id)

    async def prepare(self, tool: str, args: dict[str, Any] | str, *roles: str,
                      run_id: str | None = None) -> Prepared:
        return await self.services.runner.prepare(self.ctx(*roles, run_id=run_id), tool, args)

    async def call(self, tool: str, args: dict[str, Any] | str, *roles: str, run_id: str | None = None,
                   approver: str = "approver") -> ToolResult:
        """Prepare and run a call; approval-tier calls are approved by ``approver``, an admin."""
        prepared = await self.prepare(tool, args, *roles, run_id=run_id)
        if prepared.action != "approval":
            return await self.services.runner.execute(prepared)
        return await self.services.runner.execute(prepared, approved_by=approver, approver_roles={"admin"})

    async def described(self, *names: str, timeout_s: float = 5.0) -> None:
        names = names or tuple(self.nodes)
        await eventually(lambda: all(self.site.description(n) is not None for n in names), timeout_s,
                         f"{', '.join(names)} described")

    async def restart(self) -> Services:
        """Stop the services and start new ones on the same files and database."""
        await self.services.stop()
        self.services = Services(load_config(self.path / "hub.yaml"), site_options=SITE_OPTIONS, **self.options)
        await self.services.start()
        return self.services

    async def close(self) -> None:
        await self.services.stop()
        await self.net.aclose()


async def start_hub(
    tmp_path: Path,
    *,
    nodes: Iterable[str] = ("r204-ctl", "r205-ctl"),
    doc: Callable[[dict[str, int]], dict[str, Any]] = site_doc,
    edit: Callable[[dict[str, Any]], None] | None = None,
    hub: dict[str, Any] | None = None,
    node_options: dict[str, dict[str, Any]] | None = None,
    offline: Iterable[str] = (),
    wait: bool = True,
    llm: LlmProvider | None = None,
    sweep_interval_s: float = 5.0,
) -> Hub:
    """Simulated nodes (with their io.json already on them), ``site.yaml``
    from ``doc(ports)`` (then ``edit``), ``hub.yaml`` with fast driver
    settings (merged with ``hub``), and started services (with ``llm`` and
    ``sweep_interval_s`` when given)."""
    net = SimNetwork()
    sims: dict[str, SimNode] = {}
    for name in nodes:
        spec = NODES[name]
        sim = SimNode(name=name, instance=spec["instance"], io=spec["io"], network=net,
                      **(node_options or {}).get(name, {}))
        await sim.start()
        sim.online = name not in offline
        sims[name] = sim
    site = doc({name: sim.port for name, sim in sims.items()})
    if edit is not None:
        edit(site)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "site.yaml").write_text(yaml.safe_dump(site, sort_keys=False))
    hub_doc: dict[str, Any] = {"site_file": "site.yaml", "data_dir": "data", "drivers": copy.deepcopy(DRIVERS)}
    for key, value in (hub or {}).items():
        if isinstance(value, dict) and isinstance(hub_doc.get(key), dict):
            hub_doc[key] = {**hub_doc[key], **value}
        else:
            hub_doc[key] = value
    (tmp_path / "hub.yaml").write_text(yaml.safe_dump(hub_doc, sort_keys=False))
    options: dict[str, Any] = {"sweep_interval_s": sweep_interval_s}
    if llm is not None:
        options["llm"] = llm
    services = Services(load_config(tmp_path / "hub.yaml"), site_options=SITE_OPTIONS, **options)
    try:
        await services.start()
    except BaseException:
        await net.aclose()
        raise
    result = Hub(tmp_path, net, sims, services, hub_doc, options)
    if wait and sims:
        try:
            await result.described(*(n for n in sims if n not in offline))
        except BaseException:
            await result.close()
            raise
    return result
