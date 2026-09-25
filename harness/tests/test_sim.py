# SPDX-License-Identifier: Apache-2.0
"""Simulation manager: command generation, compose file, process lifecycle."""

from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from bacnet_uc_harness import manifest as m
from bacnet_uc_harness import sim
from bacnet_uc_harness.errors import HarnessError

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "systems"


def one_node_system() -> m.System:
    doc = {"apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "one"},
           "nodes": [{"name": "solo", "board": "native_sim/native/64",
                      "transport": {"kind": "sim"}, "device": {"instance": 5, "name": "solo"}}]}
    return m.load_system_dict(doc)


@pytest.fixture
def fake_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "zephyr.exe"
    exe.write_text("#!/bin/sh\necho \"fake zephyr $@\"\nexec sleep 30\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


class FakeBackend(sim.NetBackend):
    name = "fake"

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_on = fail_on

    def setup_bridge(self, bridge: str, host_cidr: str) -> None:
        self.calls.append(("bridge", bridge, host_cidr))

    def add_node(self, ns: str, host_if: str, cidr: str, bridge: str, gateway: str) -> None:
        if ns == self.fail_on:
            raise HarnessError("RTNETLINK answers: Operation not permitted")
        self.calls.append(("node", ns, host_if, cidr, bridge, gateway))

    def teardown(self, bridge: str | None, namespaces: Sequence[str]) -> list[str]:
        self.calls.append(("teardown", bridge, tuple(namespaces)))
        return []

    def exec_prefix(self, ns: str) -> list[str]:
        return []


def test_ip_backend_commands() -> None:
    b = sim.IpCommandBackend("ip", runner=lambda cmd: None)
    assert b.bridge_commands("bnuc0", "10.47.0.254/24") == [
        ["ip", "link", "add", "bnuc0", "type", "bridge"],
        ["ip", "addr", "add", "10.47.0.254/24", "dev", "bnuc0"],
        ["ip", "link", "set", "bnuc0", "up"],
    ]
    cmds = b.node_commands("bnuc-a", "vbnuc1", "10.47.0.1/24", "bnuc0", "10.47.0.254")
    assert cmds[0] == ["ip", "netns", "add", "bnuc-a"]
    assert ["ip", "link", "add", "vbnuc1", "type", "veth", "peer", "name", "vbnuc1p"] in cmds
    assert ["ip", "link", "set", "vbnuc1p", "netns", "bnuc-a"] in cmds
    assert ["ip", "-n", "bnuc-a", "link", "set", "vbnuc1p", "name", "eth0"] in cmds
    assert ["ip", "-n", "bnuc-a", "addr", "add", "10.47.0.1/24", "dev", "eth0"] in cmds
    assert ["ip", "-n", "bnuc-a", "route", "add", "default", "via", "10.47.0.254"] in cmds
    assert cmds[-2:] == [["ip", "link", "set", "vbnuc1", "master", "bnuc0"],
                         ["ip", "link", "set", "vbnuc1", "up"]]
    assert b.teardown_commands("bnuc0", ["bnuc-a", "bnuc-b"]) == [
        ["ip", "netns", "del", "bnuc-a"], ["ip", "netns", "del", "bnuc-b"],
        ["ip", "link", "del", "bnuc0"]]
    assert b.exec_prefix("bnuc-a") == ["ip", "netns", "exec", "bnuc-a"]


def test_ip_backend_executes_and_collects_teardown_errors() -> None:
    ran: list[list[str]] = []

    def runner(cmd: list[str]) -> None:
        ran.append(cmd)
        if cmd[2] == "del" and cmd[3] == "bnuc-x":
            raise HarnessError("Cannot remove namespace")

    b = sim.IpCommandBackend("ip", runner=runner)
    b.setup_bridge("br", "10.1.0.254/24")
    b.add_node("ns", "v1", "10.1.0.1/24", "br", "10.1.0.254")
    assert len(ran) == 3 + 10
    errs = b.teardown("br", ["bnuc-x", "bnuc-y"])
    assert errs == ["Cannot remove namespace"] and ran[-1] == ["ip", "link", "del", "br"]


def test_plan_nodes_netns(tmp_path: Path) -> None:
    s = m.load_system(EXAMPLES / "sim-demo.yaml")
    mgr = sim.SimManager(tmp_path, "netns", backend=sim.IpCommandBackend("/sbin/ip"))
    nodes = mgr.plan_nodes(s, "/opt/fw/zephyr.exe", tmp_path)
    assert [(n.name, n.address, n.netns, n.host_if) for n in nodes] == [
        ("sim-a", "10.47.0.1", "bnuc-sim-a", "vbnuc1"),
        ("sim-b", "10.47.0.2", "bnuc-sim-b", "vbnuc2")]
    assert nodes[0].command == ["/sbin/ip", "netns", "exec", "bnuc-sim-a", "/opt/fw/zephyr.exe",
                                f"--flash={tmp_path}/sim-a.flash.bin",
                                f"--seed={0x5EED0001}"]
    assert nodes[0].seed != nodes[1].seed
    assert nodes[1].device_instance == 2002


def test_host_mode_single_node(tmp_path: Path) -> None:
    s = m.load_system(EXAMPLES / "sim-demo.yaml")
    mgr = sim.SimManager(tmp_path, "host")
    with pytest.raises(HarnessError, match="one node only"):
        mgr.plan_nodes(s, "zephyr.exe", tmp_path)
    nodes = mgr.plan_nodes(one_node_system(), "zephyr.exe", tmp_path)
    assert nodes[0].address == "127.0.0.1" and nodes[0].netns is None
    assert nodes[0].command[0] == "zephyr.exe"


def test_no_sim_nodes(tmp_path: Path) -> None:
    s = m.load_system(EXAMPLES / "hvac-demo.yaml")
    with pytest.raises(HarnessError, match="no nodes with transport 'sim'"):
        sim.SimManager(tmp_path, "host").plan_nodes(s, "x", tmp_path)
    with pytest.raises(HarnessError, match="unknown simulation mode"):
        sim.SimManager(tmp_path, "vm")  # type: ignore[arg-type]


def test_compose_file(tmp_path: Path, fake_exe: Path) -> None:
    s = m.load_system(EXAMPLES / "sim-demo.yaml")
    mgr = sim.SimManager(tmp_path / "wd", "compose", run_compose=False)
    addrs = mgr.start(s, fake_exe)
    assert addrs == {"sim-a": "10.47.0.1", "sim-b": "10.47.0.2"}
    doc = yaml.safe_load((tmp_path / "wd" / "docker-compose.yml").read_text())
    assert doc["name"] == "bacnet-uc-sim-demo"
    svc = doc["services"]["sim-b"]
    assert svc["command"] == ["/opt/bacnet-uc/zephyr.exe", "--flash=/data/sim-b.flash.bin",
                              f"--seed={0x5EED0002}"]
    assert svc["networks"]["bacnet_uc"]["ipv4_address"] == "10.47.0.2"
    assert f"{fake_exe}:/opt/bacnet-uc/zephyr.exe:ro" in svc["volumes"]
    net = doc["networks"]["bacnet_uc"]
    assert net["ipam"]["config"] == [{"subnet": "10.47.0.0/24", "gateway": "10.47.0.254"}]
    assert net["driver_opts"]["com.docker.network.bridge.name"] == "bnuc0"
    st = sim.SimManager.load(tmp_path / "wd").status()
    assert st["mode"] == "compose" and st["compose_file"].endswith("docker-compose.yml")


def test_host_mode_lifecycle(tmp_path: Path, fake_exe: Path) -> None:
    mgr = sim.SimManager(tmp_path / "wd", "host")
    addrs = mgr.start(one_node_system(), fake_exe, startup_wait=0.2)
    assert addrs == {"solo": "127.0.0.1"}
    try:
        st = mgr.status()
        assert st["running"] and st["nodes"]["solo"]["alive"]
        pid = st["nodes"]["solo"]["pid"]
        assert "fake zephyr --flash=" in "\n".join(mgr.log_tail("solo"))
        # a second manager finds the running simulation
        other = sim.SimManager.load(tmp_path / "wd")
        assert other.status()["nodes"]["solo"]["pid"] == pid
        with pytest.raises(HarnessError, match="already running"):
            sim.SimManager(tmp_path / "wd", "host").start(one_node_system(), fake_exe)
        n = mgr.restart("solo", wait=0.1)
        assert n.pid != pid and sim._alive(n.pid)
        assert not sim._alive(pid)
        assert mgr.inventory_nodes() == [{
            "name": "solo", "transport": "sim", "host": "127.0.0.1", "port": 1337,
            "bacnet_address": "127.0.0.1:47808", "board": "native_sim/native/64",
            "device_instance": 5, "system": "one"}]
    finally:
        res = mgr.stop()
    assert res == {"stopped": ["solo"], "errors": []}
    assert not (tmp_path / "wd" / sim.STATE_FILE).exists()
    assert not mgr.status()["running"]


def test_netns_lifecycle_with_fake_backend(tmp_path: Path, fake_exe: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim, "_has_caps", lambda *caps: True)
    backend = FakeBackend()
    s = m.load_system(EXAMPLES / "sim-demo.yaml")
    mgr = sim.SimManager(tmp_path / "wd", "netns", backend=backend)
    mgr.start(s, fake_exe, startup_wait=0.2)
    try:
        assert backend.calls == [
            ("bridge", "bnuc0", "10.47.0.254/24"),
            ("node", "bnuc-sim-a", "vbnuc1", "10.47.0.1/24", "bnuc0", "10.47.0.254"),
            ("node", "bnuc-sim-b", "vbnuc2", "10.47.0.2/24", "bnuc0", "10.47.0.254"),
        ]
        st = mgr.status()
        assert all(n["alive"] for n in st["nodes"].values())
        assert st["nodes"]["sim-b"]["smp"] == "udp:10.47.0.2:1337"
    finally:
        mgr.stop()
    assert backend.calls[-1] == ("teardown", "bnuc0", ("bnuc-sim-a", "bnuc-sim-b"))


def test_netns_requires_privileges(tmp_path: Path, fake_exe: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim, "_has_caps", lambda *caps: False)
    mgr = sim.SimManager(tmp_path, "netns", backend=FakeBackend())
    with pytest.raises(HarnessError, match="needs root"):
        mgr.start(m.load_system(EXAMPLES / "sim-demo.yaml"), fake_exe)


def test_netns_setup_failure_rolls_back(tmp_path: Path, fake_exe: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim, "_has_caps", lambda *caps: True)
    backend = FakeBackend(fail_on="bnuc-sim-b")
    mgr = sim.SimManager(tmp_path, "netns", backend=backend)
    with pytest.raises(HarnessError, match="network setup failed"):
        mgr.start(m.load_system(EXAMPLES / "sim-demo.yaml"), fake_exe)
    assert backend.calls[-1][0] == "teardown"
    assert not (tmp_path / sim.STATE_FILE).exists()


def test_process_exiting_at_start(tmp_path: Path) -> None:
    exe = tmp_path / "bad.exe"
    exe.write_text("#!/bin/sh\necho 'cannot open flash' >&2\nexit 3\n")
    exe.chmod(0o755)
    mgr = sim.SimManager(tmp_path / "wd", "host")
    with pytest.raises(HarnessError, match="cannot open flash"):
        mgr.start(one_node_system(), exe, startup_wait=0.3)
    assert not (tmp_path / "wd" / sim.STATE_FILE).exists()


def test_missing_executable(tmp_path: Path) -> None:
    with pytest.raises(HarnessError, match="not found"):
        sim.SimManager(tmp_path, "host").start(one_node_system(), tmp_path / "nope.exe")


def test_erase_flash(tmp_path: Path, fake_exe: Path) -> None:
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "solo.flash.bin").write_bytes(b"x")
    mgr = sim.SimManager(wd, "host")
    mgr.start(one_node_system(), fake_exe, erase_flash=True, startup_wait=0.1)
    try:
        assert not (wd / "solo.flash.bin").exists()
    finally:
        mgr.stop()


def test_state_roundtrip() -> None:
    st = sim.SimState(system="s", mode="netns", workdir="/w", exe="/e", bridge="bnuc0",
                      nodes={"a": sim.SimNode(name="a", index=1, address="10.47.0.1",
                                              flash="/w/a.flash.bin", log="/w/a.log", seed=1,
                                              command=["x"], netns="bnuc-a", pid=7)})
    again = sim.SimState.from_dict(st.to_dict())
    assert again == st


def test_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sim.shutil, "which", lambda name: "/usr/sbin/ip" if name == "ip" else None)
    b = sim.default_backend()
    assert isinstance(b, sim.IpCommandBackend) and b.ip == "/usr/sbin/ip"
    which: Callable[[str], str | None] = lambda name: None  # noqa: E731
    monkeypatch.setattr(sim.shutil, "which", which)
    try:
        import pyroute2  # noqa: F401
    except ImportError:
        with pytest.raises(HarnessError, match="pyroute2"):
            sim.default_backend()
    else:
        assert isinstance(sim.default_backend(), sim.Pyroute2Backend)


async def test_rebooter_falls_back_to_smp(tmp_path: Path, fake_exe: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulated nodes are restarted by the manager when it may (root for
    netns), otherwise rebooted over SMP (the firmware restarts in place)."""
    from bacnet_uc_harness.mcp_server import HarnessContext

    caps = {"ok": True}
    monkeypatch.setattr(sim, "_has_caps", lambda *c: caps["ok"])
    s = m.load_system(EXAMPLES / "sim-demo.yaml")
    ctx = HarnessContext(tmp_path)
    mgr = sim.SimManager(ctx.state_dir() / "sim" / s.name, "netns", backend=FakeBackend())
    mgr.start(s, fake_exe, startup_wait=0.1)
    ctx.sims[s.name] = mgr
    calls: list[str] = []

    class FakeNodeConn:
        async def reboot(self) -> None:
            calls.append("smp reset")

    async def node(name: str) -> Any:
        return FakeNodeConn()

    ctx.node = node  # type: ignore[method-assign]
    try:
        assert mgr.can_restart()
        pid = mgr.state.nodes["sim-a"].pid if mgr.state else None
        await ctx.rebooter(s)("sim-a")
        assert mgr.state is not None and mgr.state.nodes["sim-a"].pid != pid and not calls
        caps["ok"] = False
        assert not mgr.can_restart()
        await ctx.rebooter(s)("sim-a")
        assert calls == ["smp reset"]
    finally:
        caps["ok"] = True
        mgr.stop()
        await ctx.close()
