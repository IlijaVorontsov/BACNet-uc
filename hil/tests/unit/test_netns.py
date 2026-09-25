"""Rig topology scripts and namespace helpers (hil/net, hilrig.netns, install-net-wrappers)."""

from __future__ import annotations

import socket
import subprocess
import time
from pathlib import Path

import pytest

from hilrig import netns
from hilrig.netns import HOSTS, NetnsError, Topology
from rig_skips import needs_root, needs_tools

HIL = Path(__file__).resolve().parents[2]
UP, DOWN = HIL / "net" / "up.sh", HIL / "net" / "down.sh"
PREFIX = "hn-"


def udp_round_trip(src_ns: str, dst_ns: str, dst_ip: str) -> bytes:
    """Send a datagram from ``src_ns`` to ``dst_ip`` in ``dst_ns`` and return the echoed reply."""
    with netns.enter(dst_ns):
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with netns.enter(src_ns):
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with server, client:
        server.bind((dst_ip, 40000))
        server.settimeout(3)
        client.settimeout(3)
        client.sendto(b"ping", (dst_ip, 40000))
        data, peer = server.recvfrom(64)
        server.sendto(data + b"-pong", peer)
        return client.recvfrom(64)[0]


def leftovers() -> list[str]:
    """Return namespaces with the test prefix."""
    names = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True, check=True).stdout
    return [line.split()[0] for line in names.splitlines() if line.startswith(PREFIX)]


def root_links() -> set[str]:
    """Return the interface names of the root namespace."""
    out = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True, check=True).stdout
    return {line.split(": ")[1].split("@")[0] for line in out.splitlines()}


def test_exec_argv() -> None:
    assert netns.exec_argv(None, ["true"]) == ["true"]
    assert netns.exec_argv("svc", ["dnsmasq", "-k"]) == ["ip", "netns", "exec", "svc", "dnsmasq", "-k"]


def test_topology_arguments_and_capture_interface() -> None:
    assert Topology().up_args() == []
    assert Topology(prefix="t1-", sil=True).up_args() == ["--prefix", "t1-", "--sil"]
    assert Topology(dut_iface="eth1").capture_iface == "eth1"
    assert Topology(sil=True).capture_iface == "zeth"
    assert Topology(standin=True).capture_iface == "p-dut0"
    assert Topology(prefix="t1-", standin=True).namespaces()[-1] == "t1-dut"
    with pytest.raises(NetnsError, match="no DUT attachment"):
        _ = Topology().capture_iface


def test_session_takes_down_what_a_failed_up_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the netns fixtures leaked the namespaces of an up.sh that failed part-way."""
    calls: list[str] = []

    def script(name: str, wrapper: Path, args: list[str]) -> None:
        calls.append(name)
        if name == "up.sh":
            raise NetnsError("up.sh failed half-way")

    monkeypatch.setattr(netns, "_net_script", script)
    monkeypatch.setattr(Topology, "is_up", lambda self: False)
    with pytest.raises(NetnsError, match="half-way"), Topology(prefix="t1-").session():
        pytest.fail("the block must not run")
    assert calls == ["up.sh", "down.sh"]

    calls.clear()
    monkeypatch.setattr(netns, "_net_script", lambda name, wrapper, args: calls.append(name))
    with Topology(prefix="t1-").session() as topology:
        assert topology.prefix == "t1-"
    assert calls == ["up.sh", "down.sh"]

    calls.clear()
    monkeypatch.setattr(Topology, "is_up", lambda self: True)  # someone else's topology
    with pytest.raises(RuntimeError, match="test failed"), Topology().session():
        raise RuntimeError("test failed")
    assert calls == ["up.sh"]  # left up for its owner


@needs_root
def test_enter_unknown_namespace_is_an_error() -> None:
    with pytest.raises(NetnsError, match="no network namespace 'hn-nosuch'"), netns.enter("hn-nosuch"):
        pass


@needs_root
@needs_tools("ip")
def test_sil_topology_up_route_down_without_leftovers() -> None:
    topo = Topology(prefix=PREFIX, sil=True)
    topo.down()
    links_before = root_links()
    topo.up()
    topo.up()  # idempotent
    try:
        assert topo.is_up()
        links = netns.run(topo.ns("lan-a"), ["ip", "-o", "link", "show", "master", "br-a"]).stdout
        assert {"p-svc0", "p-sim10", "p-sim20", "p-rtra", "zeth"} <= {
            line.split(": ")[1].split("@")[0] for line in links.splitlines()
        }
        assert netns.run(topo.ns("rtr"), ["sysctl", "-n", "net.ipv4.ip_forward"]).stdout.strip() == "1"
        # subnet B -> subnet A through rtr, both ways
        assert udp_round_trip(topo.ns("fd"), topo.ns("svc"), HOSTS["svc"].ip) == b"ping-pong"
        assert udp_round_trip(topo.ns("sim1"), topo.ns("bbmdb"), HOSTS["bbmdb"].ip) == b"ping-pong"
        # a process left running is stopped by down.sh
        sleeper = subprocess.Popen(netns.exec_argv(topo.ns("sim2"), ["sleep", "600"]))
    finally:
        topo.down()
        topo.down()  # idempotent
    assert sleeper.wait(timeout=5) != 0
    assert leftovers() == []
    assert not topo.is_up()
    assert root_links() == links_before


@needs_root
@needs_tools("ip")
@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--prefix", "Bad"], "invalid --prefix"),
        (["--prefix", "hn-", "--sil", "--standin"], "are exclusive"),
        (["--prefix", "hn-", "--dut-iface", "lo"], "'lo' is not a physical NIC"),
        (["--prefix", "hn-", "--dut-iface", "../x"], "invalid interface name"),
        (["--prefix", "hn-", "--dut-iface", "hn-nosuch0"], "no interface 'hn-nosuch0'"),
        (["--frobnicate"], "unknown argument"),
    ],
)
def test_up_refuses_bad_arguments_without_side_effects(args: list[str], message: str) -> None:
    proc = subprocess.run([str(UP), *args], capture_output=True, text=True, timeout=30, check=False)
    assert proc.returncode == 2 and message in proc.stderr
    assert leftovers() == []


@needs_tools("visudo")
def test_wrapper_installer_stages_root_owned_copies_and_a_narrow_sudoers_rule(tmp_path: Path) -> None:
    proc = subprocess.run(
        [str(HIL / "host" / "install-net-wrappers.sh")],
        env={"DESTDIR": str(tmp_path)},
        timeout=30,
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    up, down = tmp_path / "usr/local/sbin/hil-net-up", tmp_path / "usr/local/sbin/hil-net-down"
    assert up.read_bytes() == UP.read_bytes() and down.read_bytes() == DOWN.read_bytes()
    assert up.stat().st_mode & 0o7777 == 0o755
    rule = tmp_path / "etc/sudoers.d/hil-net"
    assert rule.stat().st_mode & 0o7777 == 0o440
    text = rule.read_text()
    commands = text.split("NOPASSWD:", 1)[1]
    assert "ip netns" not in commands and str(HIL) not in commands
    assert "/usr/local/sbin/hil-net-up" in commands and "/usr/local/sbin/hil-net-down" in commands
    assert subprocess.run(["visudo", "-cq", "-f", str(rule)], timeout=30, check=False).returncode == 0


@needs_root
@needs_tools("ip")
def test_ping_carrier_and_link_control() -> None:
    """In-process ICMP ping (no iputils) and the link control of R-06/NET-03."""
    topo = Topology(prefix=PREFIX, sil=True)
    topo.down()
    topo.up()
    try:
        svc, sim1 = topo.ns("svc"), topo.ns("sim1")
        result = netns.ping(svc, HOSTS["sim1"].ip, count=3, interval=0.05)
        assert (result.sent, result.received, result.loss) == (3, 3, 0.0) and max(result.rtts) < 1.0
        assert netns.carrier(sim1, "sim10")
        t_down = netns.set_link(sim1, "sim10", up=False)
        assert not netns.carrier(sim1, "sim10") and abs(t_down - time.time()) < 5
        assert netns.ping(svc, HOSTS["sim1"].ip, count=2, interval=0.05, timeout=0.2).loss == 1.0
        netns.set_link(sim1, "sim10", up=True)
        assert netns.ping(svc, HOSTS["sim1"].ip, count=2, interval=0.05).received == 2
    finally:
        topo.down()
