"""Network namespaces of the HIL rig (design v2, decision B7) and running code inside them.

hil/net/up.sh and down.sh build and remove the topology. As root, the scripts in this
checkout run directly. Otherwise the root-owned copies that hil/host/install-net-wrappers.sh
installs as /usr/local/sbin/hil-net-{up,down} run through ``sudo -n``: sudoers allows only
those two, because a rule for ``ip netns exec`` or for work-tree scripts is a root shell.

Running something *inside* a namespace (``ip netns exec`` or setns) needs CAP_SYS_ADMIN, so
the parts of a session that use :func:`run`, :func:`spawn` or :func:`enter` run as root (in
CI: the privileged job container).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import socket
import struct
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SUBNET_A = "192.0.2.0/24"
BROADCAST_A = "192.0.2.255"
DUT_IP = "192.0.2.10"
ROUTER_A_IP = "192.0.2.254"
SVC2_IP = "192.0.2.2"  # second svc0 address: TLS endpoints that take over broker.hil.lan (D33)

WRAPPER_UP = Path("/usr/local/sbin/hil-net-up")
WRAPPER_DOWN = Path("/usr/local/sbin/hil-net-down")
SCRIPTS = Path(__file__).resolve().parents[2] / "net"

_CLONE_NEWNET = 0x40000000
_libc = ctypes.CDLL(None, use_errno=True)


@dataclass(frozen=True)
class Host:
    """A rig host: the namespace it lives in (without prefix), its interface and address."""

    netns: str
    iface: str
    ip: str


HOSTS: Mapping[str, Host] = {
    h.netns: h
    for h in (
        Host("svc", "svc0", "192.0.2.1"),
        Host("sim1", "sim10", "192.0.2.11"),
        Host("sim2", "sim20", "192.0.2.12"),
        Host("rtr", "rtra", ROUTER_A_IP),
        Host("fd", "fd0", "198.51.100.10"),
        Host("bbmdb", "bb0", "198.51.100.2"),
        Host("dut", "dut0", DUT_IP),
    )
}
NAMESPACES = ("lan-a", "svc", "sim1", "sim2", "rtr", "lan-b", "fd", "bbmdb")


class NetnsError(RuntimeError):
    """The topology could not be changed, or a namespace could not be entered."""


def exec_argv(netns: str | None, argv: Sequence[str]) -> list[str]:
    """Return the command line that runs ``argv`` inside ``netns`` (as is for None)."""
    return list(argv) if netns is None else ["ip", "netns", "exec", netns, *argv]


def run(
    netns: str | None,
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 30.0,
    check: bool = True,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` inside ``netns`` and return its captured text output.

    ``env`` is the complete environment (None: this process's). With ``check`` a non-zero
    exit raises :class:`subprocess.CalledProcessError`, whose ``stderr`` says what went wrong.
    """
    return subprocess.run(
        exec_argv(netns, argv),
        env=env,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def spawn(
    netns: str | None,
    argv: Sequence[str],
    *,
    log: Path,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Start ``argv`` inside ``netns`` with stdout and stderr appended to ``log``.

    ``env`` is the complete environment (None: this process's). stdin is a pipe that stays
    open, because some servers (openssl s_server) exit on EOF.
    """
    with log.open("ab") as out:
        return subprocess.Popen(
            exec_argv(netns, argv),
            env=env,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def _setns(fd: int) -> None:
    if _libc.setns(fd, _CLONE_NEWNET) != 0:
        err = ctypes.get_errno()
        raise NetnsError(f"setns: {os.strerror(err)} (entering a netns needs CAP_SYS_ADMIN)")


@contextlib.contextmanager
def enter(netns: str) -> Iterator[None]:
    """Move the calling thread into ``netns`` for the duration of the block.

    Sockets created inside the block keep that namespace after it ends, and so do threads
    started inside it (a new thread inherits its creator's namespace). That is how in-process
    clients (capture sentinels, paho, the DHCP probe) talk to the rig without a subprocess.
    Only the network namespace changes; files and mounts are those of the host.
    """
    try:
        target = os.open(f"/run/netns/{netns}", os.O_RDONLY)
    except FileNotFoundError:
        raise NetnsError(f"no network namespace '{netns}' (is the topology up?)") from None
    home = os.open("/proc/thread-self/ns/net", os.O_RDONLY)
    try:
        _setns(target)
        try:
            yield
        finally:
            _setns(home)
    finally:
        os.close(target)
        os.close(home)


def exists(netns: str) -> bool:
    """Tell whether a named network namespace exists."""
    return Path("/run/netns", netns).exists()


def listening(netns: str | None, proto: str, port: int, ip: str | None = None) -> bool:
    """Tell whether an IPv4 TCP socket listens, or a UDP socket is bound, on ``port``.

    With ``ip`` only a socket bound to that address (or to 0.0.0.0) counts, so a server on
    192.0.2.2:8883 is not taken for ready while mosquitto holds 192.0.2.1:8883. Reads the
    namespace's socket table, so readiness checks need no probe connection (which servers
    would log as a failed client).
    """
    with enter(netns) if netns else contextlib.nullcontext():
        table = Path(f"/proc/thread-self/net/{proto}").read_text()
    for line in table.splitlines()[1:]:
        cols = line.split()
        local_ip, _, local_port = cols[1].rpartition(":")
        bound = socket.inet_ntoa(struct.pack("<I", int(local_ip, 16)))
        if int(local_port, 16) != port or (proto != "udp" and cols[3] != "0A"):
            continue
        if ip is None or bound in (ip, "0.0.0.0"):
            return True
    return False


def carrier(netns: str | None, iface: str) -> bool:
    """Tell whether ``iface`` has carrier (LOWER_UP), for example the DUT NIC in lan-a."""
    proc = run(netns, ["ip", "-j", "link", "show", "dev", iface])
    flags: list[str] = json.loads(proc.stdout)[0].get("flags", [])
    return "LOWER_UP" in flags


def set_link(netns: str | None, iface: str, up: bool) -> float:
    """Set ``iface`` administratively up or down and return the wall-clock time it happened.

    Taking the host side of the DUT's cable down powers down the NIC's PHY on the usual
    drivers, so the DUT loses carrier (R-06, NET-03). The time is ``time.time()``, the clock
    of the capture timestamps.
    """
    run(netns, ["ip", "link", "set", "dev", iface, "up" if up else "down"])
    return time.time()


@dataclass(frozen=True)
class PingResult:
    """ICMP echo statistics: requests sent, replies received and their round-trip times (s)."""

    sent: int
    received: int
    rtts: tuple[float, ...]

    @property
    def loss(self) -> float:
        """Fraction of requests without a reply."""
        return 1.0 - self.received / self.sent if self.sent else 1.0


def _icmp_checksum(data: bytes) -> int:
    data += b"\0" * (len(data) % 2)
    total: int = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def ping(
    netns: str | None, dst: str, *, count: int = 10, interval: float = 0.2, timeout: float = 1.0
) -> PingResult:
    """Send ``count`` ICMP echo requests to ``dst`` from ``netns`` (raw socket: needs root).

    In-process, so the rig does not depend on iputils being installed on the host.
    """
    with enter(netns) if netns else contextlib.nullcontext():
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
    ident, rtts = os.getpid() & 0xFFFF, []
    with sock:
        sock.settimeout(timeout)
        for seq in range(count):
            header = struct.pack("!BBHHH", 8, 0, 0, ident, seq)
            payload = b"hil-ping" + struct.pack("!d", time.monotonic())
            packet = struct.pack("!BBHHH", 8, 0, _icmp_checksum(header + payload), ident, seq) + payload
            start = time.monotonic()
            sock.sendto(packet, (dst, 0))
            while (left := timeout - (time.monotonic() - start)) > 0:
                sock.settimeout(left)
                try:
                    reply, (src, _) = sock.recvfrom(1500)
                except TimeoutError:
                    break
                icmp = reply[(reply[0] & 0x0F) * 4 :]
                kind, _, _, rid, rseq = struct.unpack("!BBHHH", icmp[:8])
                if src == dst and kind == 0 and (rid, rseq) == (ident, seq):
                    rtts.append(time.monotonic() - start)
                    break
            time.sleep(max(0.0, interval - (time.monotonic() - start)))
    return PingResult(count, len(rtts), tuple(rtts))


@dataclass(frozen=True)
class Topology:
    """One instance of the rig topology.

    ``prefix`` is prepended to every namespace name, so unit tests can build private copies
    next to a running rig. At most one of ``dut_iface`` (real NIC, HIL), ``sil`` (TAP zeth
    for native_sim) and ``standin`` (netns dut for a bacserv stand-in) is set.
    """

    prefix: str = ""
    dut_iface: str | None = None
    sil: bool = False
    standin: bool = False

    def ns(self, name: str) -> str:
        """Return the full namespace name of a rig namespace such as ``svc``."""
        return self.prefix + name

    def namespaces(self) -> tuple[str, ...]:
        """Return every namespace this topology creates."""
        names = NAMESPACES + (("dut",) if self.standin else ())
        return tuple(self.ns(n) for n in names)

    @property
    def capture_iface(self) -> str:
        """Return the interface in lan-a that faces the DUT (the wire view for captures)."""
        if self.dut_iface:
            return self.dut_iface
        if self.sil:
            return "zeth"
        if self.standin:
            return "p-dut0"
        raise NetnsError("this topology has no DUT attachment to capture on")

    def is_up(self) -> bool:
        """Tell whether all namespaces of this topology exist."""
        return all(exists(n) for n in self.namespaces())

    def up_args(self) -> list[str]:
        """Return the arguments for up.sh / hil-net-up."""
        args = ["--prefix", self.prefix] if self.prefix else []
        if self.dut_iface:
            args += ["--dut-iface", self.dut_iface]
        if self.sil:
            args.append("--sil")
        if self.standin:
            args.append("--standin")
        return args

    def up(self) -> None:
        """Create the topology (idempotent)."""
        _net_script("up.sh", WRAPPER_UP, self.up_args())

    def down(self) -> None:
        """Remove the topology and stop every process still running in it (idempotent)."""
        _net_script("down.sh", WRAPPER_DOWN, ["--prefix", self.prefix] if self.prefix else [])

    @contextlib.contextmanager
    def session(self) -> Iterator[Topology]:
        """Bring the topology up for the block; take it down after, unless it was up before.

        A partial topology (left by an aborted run) counts as not up, so it is completed and
        then removed. When :meth:`up` fails part-way, what it created is removed too.
        """
        owner = not self.is_up()
        try:
            self.up()
            yield self
        finally:
            if owner:
                self.down()


def _net_script(script: str, wrapper: Path, args: list[str]) -> None:
    local = SCRIPTS / script
    if os.geteuid() == 0:
        argv = [str(local if local.exists() else wrapper), *args]
    elif wrapper.exists():
        argv = ["sudo", "-n", str(wrapper), *args]
    else:
        raise NetnsError(
            f"not root and {wrapper} is not installed: run "
            "'sudo hil/host/install-net-wrappers.sh' once (see hil/README.md)"
        )
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise NetnsError(f"{' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}")
