# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests against the real native_sim firmware.

Marker ``e2e``; every test is skipped unless ``BACNET_UC_FIRMWARE`` points to
the ``zephyr.exe`` of a ``native_sim/native/64`` build of ``firmware/``::

    west build -b native_sim/native/64 BACNet-uc/firmware -d /tmp/build-native
    BACNET_UC_FIRMWARE=/tmp/build-native/zephyr/zephyr.exe python -m pytest -m e2e tests/e2e

- ``test_single_node.py`` runs one node in the simulation's ``host`` mode:
  SMP on 127.0.0.1:1337 and BACnet/IP on port 47808 of the test's network
  namespace. The ports must be free (skipped otherwise); run the tests in a
  private network namespace to be independent of other processes
  (``run-isolated.sh``).
- ``test_sim_demo.py`` runs ``examples/systems/sim-demo.yaml`` (two nodes,
  ``netns`` mode) through the CLI: needs root (CAP_NET_ADMIN +
  CAP_SYS_ADMIN) and the ``ip`` command or pyroute2; skipped otherwise, and
  when a simulation network (bridge ``bnuc0``) already exists.

``run-isolated.sh`` (root) runs pytest in new network *and* mount namespaces
with a private ``/run/netns``, so neither the ports, the bridge nor the
namespace names can collide with anything else on the machine.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from bacnet_uc_harness.manifest import load_system_dict
from bacnet_uc_harness.sim import CAP_NET_ADMIN, CAP_SYS_ADMIN, SimManager, _has_caps

FIRMWARE_ENV = "BACNET_UC_FIRMWARE"
REPO = Path(__file__).resolve().parents[3]
EXAMPLES = REPO / "harness" / "examples" / "systems"
SINGLE_INSTANCE = 3100


def firmware_path() -> Path | None:
    value = os.environ.get(FIRMWARE_ENV)
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_file() else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    exe = firmware_path()
    if exe is not None:
        return
    reason = (f"{FIRMWARE_ENV} does not name a native_sim zephyr.exe"
              if os.environ.get(FIRMWARE_ENV) else f"{FIRMWARE_ENV} not set")
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def firmware_exe() -> Path:
    exe = firmware_path()
    if exe is None:
        pytest.skip(f"{FIRMWARE_ENV} not set")
    return exe


def _udp_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def _netns_problem() -> str | None:
    """``None`` when the netns simulation can run here, else the reason."""
    if not sys.platform.startswith("linux"):
        return "network namespaces need Linux"
    if not _has_caps(CAP_NET_ADMIN, CAP_SYS_ADMIN):
        return "netns mode needs root (CAP_NET_ADMIN + CAP_SYS_ADMIN)"
    if shutil.which("ip") is None:
        try:
            import pyroute2  # noqa: F401
        except ImportError:
            return "neither the ip command nor pyroute2 is installed"
    if Path("/sys/class/net/bnuc0").exists():
        return "bridge bnuc0 exists: another simulation is running (use run-isolated.sh)"
    return None


@pytest.fixture
def netns_required() -> None:
    """Skip unless the netns simulation can run here."""
    problem = _netns_problem()
    if problem:
        pytest.skip(problem)


@pytest.fixture(scope="module")
def e2e_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Harness home (``.bacnet-uc`` state, build cache) of a test module."""
    return tmp_path_factory.mktemp("bacnet-uc-home")


@pytest.fixture(scope="module")
def single_node(firmware_exe: Path, e2e_home: Path) -> Iterator[SimManager]:
    """One native_sim node (host mode, empty flash) for a test module."""
    for port in (1337, 47808):
        if not _udp_port_free("0.0.0.0", port):
            pytest.skip(f"UDP port {port} is in use (another node?): run the e2e tests in a "
                        "private network namespace, e.g. tests/e2e/run-isolated.sh")
    doc = {
        "apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "e2e-single"},
        "nodes": [{"name": "e2e", "board": "native_sim/native/64", "transport": {"kind": "sim"},
                   "device": {"instance": SINGLE_INSTANCE, "name": "e2e"}}],
    }
    system = load_system_dict(doc)
    mgr = SimManager(e2e_home / "sim" / "e2e-single", "host")
    mgr.start(system, firmware_exe, erase_flash=True)
    try:
        yield mgr
    finally:
        mgr.stop()
