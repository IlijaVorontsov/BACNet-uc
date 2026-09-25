from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from uc_hub.core.errors import NotFound

from .fakes import wasm

THERMOSTAT = wasm("thermostat")
UC_LINK = wasm("uc-link")

_SITE: dict[str, Any] = {
    "apiVersion": "bacnet-uc/v1",
    "kind": "Site",
    "metadata": {"name": "hq", "description": "test site"},
    "spaces": [
        {"id": "f2", "name": "Floor 2"},
        {"id": "r204", "name": "Room 204", "parent": "f2"},
        {"id": "r205", "name": "Room 205", "parent": "f2"},
        {"id": "plant", "name": "Plant"},
    ],
    "placement": {"r204-ctl": "r204", "r205-ctl": "r205", "ahu1-ctl": "plant", "r204-co2": "r204"},
    "system": {
        "apiVersion": "bacnet-uc/v1",
        "kind": "System",
        "metadata": {"name": "hq-uc"},
        "nodes": [
            {
                "name": "r204-ctl",
                "board": "native_sim",
                "transport": {"kind": "udp", "host": "127.0.0.1", "port": 13204},
                "device": {"instance": 2041, "name": "R204 Controller", "location": "Room 204"},
                "io": [
                    {"channel": "ai0", "type": "analog-input", "instance": 1, "name": "R204 Temp",
                     "units": "degrees-celsius", "scale": 0.1, "offset": -50},
                    {"channel": "ao0", "type": "analog-output", "instance": 1, "name": "R204 Valve",
                     "units": "percent"},
                ],
            },
            {
                "name": "r205-ctl",
                "board": "native_sim",
                "transport": {"kind": "udp", "host": "127.0.0.1", "port": 13205},
                "device": {"instance": 2051, "name": "R205 Controller"},
                "io": [{"channel": "di0", "type": "binary-input", "instance": 1, "name": "R205 Window"}],
            },
        ],
        "apps": [
            {"name": "thermostat", "node": "r204-ctl", "wasm": "apps/thermostat.wasm",
             "perms": ["bacnet.local"], "params": {"setpoint": 21.5}},
        ],
        "links": [{"from": "ahu1-ctl/analog-value:3", "to": "r204-ctl/analog-value:10"}],
        "tests": [
            {"name": "valve opens when cold", "steps": [
                {"force": {"node": "r204-ctl", "channel": "ai0", "value": 650}},
                {"expect": {"point": "r204-ctl/analog-output:1", "op": "gt", "value": 50, "within_ms": 5000}},
                {"release": {"node": "r204-ctl", "channel": "ai0"}},
            ]},
        ],
    },
    "external_devices": [
        {"name": "ahu1-ctl", "protocol": "bacnet-ip", "address": "10.0.2.10", "device_instance": 100},
        {"name": "r204-co2", "protocol": "mqtt", "profile": "generic-json", "topic": "sensors/r204/co2",
         "points": [{"id": "co2", "path": "$.ppm", "units": "parts-per-million"}]},
    ],
    "bridges": [{"from": "r204-co2/co2", "to": "r204-ctl/analog-value:20", "max_age_s": 120}],
    "tags": {"r204-ctl/analog-input:1": ["Zone_Air_Temperature_Sensor"]},
    "safety": {"ahu1-ctl/binary-output:9": "life-safety"},
}


def site_doc() -> dict[str, Any]:
    """A small valid site document (fresh copy)."""
    return copy.deepcopy(_SITE)


@pytest.fixture
def doc() -> dict[str, Any]:
    return site_doc()


def resolver(files: dict[str, bytes]) -> Callable[[str], bytes]:
    def read(path: str) -> bytes:
        if path not in files:
            raise NotFound(f"{path} does not exist")
        return files[path]

    return read


@pytest.fixture
def app_files() -> dict[str, bytes]:
    return {"apps/thermostat.wasm": THERMOSTAT}
