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
import os
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SUBNET_A = "192.0.2.0/24"
BROADCAST_A = "192.0.2.255"
DUT_IP = "192.0.2.10"
ROUTER_A_IP = "192.0.2.254"

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


def listening(netns: str | None, proto: str, port: int) -> bool:
    """Tell whether an IPv4 TCP socket listens, or a UDP socket is bound, on ``port``.

    Reads the namespace's socket table, so readiness checks need no probe connection
    (which servers would log as a failed client).
    """
    with enter(netns) if netns else contextlib.nullcontext():
        table = Path(f"/proc/thread-self/net/{proto}").read_text()
    for line in table.splitlines()[1:]:
        cols = line.split()
        local_port = int(cols[1].rsplit(":", 1)[1], 16)
        if local_port == port and (proto == "udp" or cols[3] == "0A"):
            return True
    return False


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
