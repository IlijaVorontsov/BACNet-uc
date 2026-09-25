# SPDX-License-Identifier: Apache-2.0
"""Run simulated BACnet-uc nodes (native_sim ``zephyr.exe``).

native_sim uses Native Simulator Offloaded Sockets (NSOS): every Zephyr
socket is a host socket. The SMP server listens on UDP 1337 and BACnet/IP on
the configured port of whatever network the process runs in. Three modes:

``host``
    One ``zephyr.exe`` directly on the host network. Only **one** node is
    possible (SMP port 1337 is fixed at build time). The node is reached at
    127.0.0.1; its BACnet/IP address is loopback (no ``network`` section).
    The start is refused while UDP 1337 or the node's BACnet/IP port is in
    use on the host: a second node would keep running without its SMP
    server, and the harness would manage whichever node owns the port.

``netns``
    One Linux network namespace per node (``bnuc-<node>``), each with a veth
    pair enslaved to the bridge ``bnuc0`` (host side address
    ``10.47.0.254/24``). Node *n* gets ``10.47.0.<n>/24``. The harness talks
    to the nodes from the host through the bridge. Needs root or
    CAP_NET_ADMIN + CAP_SYS_ADMIN; uses the ``ip`` command when installed,
    else ``pyroute2`` (``pip install pyroute2``). NSOS does not forward
    ``SO_BROADCAST``, so nodes find each other through the static bindings
    that :mod:`bacnet_uc_harness.render` writes into ``device.json``; the
    rendered ``network`` section (``dhcp: false``, the namespace address)
    lets the BACnet/IP port bind to the namespace address.

``compose``
    Writes ``docker-compose.yml`` running the executable in containers on a
    user-defined bridge network with the same addressing (gateway = host =
    ``.254``) and starts it with ``docker compose up -d``.

Each node runs with ``--flash=<workdir>/<node>.flash.bin`` (persistent
LittleFS image) and a unique ``--seed``. Process ids, addresses and commands
are kept in ``<workdir>/sim-state.json`` so another process can stop the
simulation. A process id from that file is only trusted (signalled, reported
alive) while the process still has the start time recorded at spawn: after a
host reboot or a crash the id may belong to an unrelated process.

Rebooting: the firmware's native_sim build sets ``CONFIG_NATIVE_SIM_REBOOT=y``,
so ``os reset`` / ``kernel reboot`` restart the process in place (same pid,
namespace, flash image and log). The harness reboots simulated nodes with
:meth:`SimManager.restart` (kill and start again; also works for builds
without that option) when it may (:meth:`SimManager.can_restart`: root for
``netns``), else over SMP.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from bacnet_uc_harness import paths
from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.manifest import System
from bacnet_uc_harness.render import SIM_HOST_ADDRESS, SIM_SUBNET, sim_address_plan

SimMode = Literal["host", "netns", "compose"]
MODES: tuple[SimMode, ...] = ("host", "netns", "compose")
BRIDGE = "bnuc0"
NETNS_PREFIX = "bnuc-"
NETNS_RUN_DIR = "/var/run/netns"
STATE_FILE = "sim-state.json"
DEFAULT_IMAGE = "ubuntu:24.04"
CAP_NET_ADMIN = 12
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CAP_SYS_ADMIN = 21
SMP_PORT = 1337  # CONFIG_MCUMGR_TRANSPORT_UDP_PORT of the firmware
#: log lines of a node whose SMP (smp_udp) or BACnet/IP (bip-init) socket could not be bound
_BIND_FAILED = re.compile(r"Could not bind|zsock_bind\(\) failure")


@dataclass
class SimNode:
    name: str
    index: int
    address: str
    flash: str
    log: str
    seed: int
    command: list[str]
    netns: str | None = None
    host_if: str | None = None
    pid: int | None = None
    device_instance: int | None = None
    board: str = "native_sim/native/64"
    bacnet_port: int = 47808
    started_at: float | None = None
    #: start time of process ``pid`` (epoch seconds, from /proc) at spawn
    proc_start: float | None = None


@dataclass
class SimState:
    system: str
    mode: SimMode
    workdir: str
    exe: str
    subnet: str = SIM_SUBNET
    bridge: str | None = None
    host_address: str | None = None
    backend: str | None = None
    compose_file: str | None = None
    nodes: dict[str, SimNode] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["nodes"] = {k: asdict(v) for k, v in self.nodes.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SimState:
        nodes = {k: SimNode(**v) for k, v in d.get("nodes", {}).items()}
        return cls(**{k: v for k, v in d.items() if k != "nodes"}, nodes=nodes)


def _has_caps(*caps: int) -> bool:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                eff = int(line.split()[1], 16)
                return all(eff & (1 << c) for c in caps)
    except OSError:
        pass
    return os.geteuid() == 0


def _udp_port_free(port: int, host: str = "0.0.0.0") -> bool:
    """Whether a UDP socket can bind ``host:port`` (like the node would)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def host_if_name(index: int) -> str:
    return f"vbnuc{index}"


def netns_name(node: str) -> str:
    return f"{NETNS_PREFIX}{node}"


# --- network backends ----------------------------------------------------------------------


class NetBackend:
    """Creates the bridge and one namespace + veth pair per node."""

    name = "none"

    def setup_bridge(self, bridge: str, host_cidr: str) -> None:
        raise NotImplementedError

    def add_node(self, ns: str, host_if: str, cidr: str, bridge: str, gateway: str) -> None:
        raise NotImplementedError

    def teardown(self, bridge: str | None, namespaces: Sequence[str]) -> list[str]:
        raise NotImplementedError

    def exec_prefix(self, ns: str) -> list[str]:
        raise NotImplementedError

    def preexec(self, ns: str) -> Callable[[], None] | None:
        return None

    def link_exists(self, name: str) -> bool:
        """Whether a network interface ``name`` exists in the network
        namespace of this process (an ioctl, unlike /sys/class/net)."""
        try:
            socket.if_nametoindex(name)
        except OSError:
            return False
        return True

    def netns_exists(self, ns: str) -> bool:
        """Whether the named network namespace ``ns`` exists."""
        return os.path.exists(f"{NETNS_RUN_DIR}/{ns}")


class IpCommandBackend(NetBackend):
    """iproute2 ``ip`` commands (pure command generation + execution)."""

    name = "ip"

    def __init__(self, ip: str = "ip", runner: Callable[[list[str]], None] | None = None) -> None:
        self.ip = ip
        self.runner = runner or self._run

    def _run(self, cmd: list[str]) -> None:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise HarnessError(f"{' '.join(cmd)}: {res.stderr.strip() or res.returncode}")

    def bridge_commands(self, bridge: str, host_cidr: str) -> list[list[str]]:
        ip = self.ip
        return [
            [ip, "link", "add", bridge, "type", "bridge"],
            [ip, "addr", "add", host_cidr, "dev", bridge],
            [ip, "link", "set", bridge, "up"],
        ]

    def node_commands(self, ns: str, host_if: str, cidr: str, bridge: str,
                      gateway: str) -> list[list[str]]:
        ip = self.ip
        peer = f"{host_if}p"
        return [
            [ip, "netns", "add", ns],
            [ip, "link", "add", host_if, "type", "veth", "peer", "name", peer],
            [ip, "link", "set", peer, "netns", ns],
            [ip, "-n", ns, "link", "set", peer, "name", "eth0"],
            [ip, "-n", ns, "addr", "add", cidr, "dev", "eth0"],
            [ip, "-n", ns, "link", "set", "lo", "up"],
            [ip, "-n", ns, "link", "set", "eth0", "up"],
            [ip, "-n", ns, "route", "add", "default", "via", gateway],
            [ip, "link", "set", host_if, "master", bridge],
            [ip, "link", "set", host_if, "up"],
        ]

    def teardown_commands(self, bridge: str | None, namespaces: Sequence[str]) -> list[list[str]]:
        cmds = [[self.ip, "netns", "del", ns] for ns in namespaces]
        if bridge:
            cmds.append([self.ip, "link", "del", bridge])
        return cmds

    def setup_bridge(self, bridge: str, host_cidr: str) -> None:
        for cmd in self.bridge_commands(bridge, host_cidr):
            self.runner(cmd)

    def add_node(self, ns: str, host_if: str, cidr: str, bridge: str, gateway: str) -> None:
        for cmd in self.node_commands(ns, host_if, cidr, bridge, gateway):
            self.runner(cmd)

    def teardown(self, bridge: str | None, namespaces: Sequence[str]) -> list[str]:
        errors = []
        for cmd in self.teardown_commands(bridge, namespaces):
            try:
                self.runner(cmd)
            except HarnessError as exc:
                errors.append(str(exc))
        return errors

    def exec_prefix(self, ns: str) -> list[str]:
        return [self.ip, "netns", "exec", ns]


class Pyroute2Backend(NetBackend):
    """The same topology through pyroute2 (no ``ip`` command needed)."""

    name = "pyroute2"

    def __init__(self) -> None:
        try:
            import pyroute2  # noqa: F401
        except ImportError as exc:
            raise HarnessError("pyroute2 is not installed (pip install pyroute2)") from exc

    def setup_bridge(self, bridge: str, host_cidr: str) -> None:
        from pyroute2 import IPRoute

        addr, prefix = host_cidr.split("/")
        with IPRoute() as ipr:
            ipr.link("add", ifname=bridge, kind="bridge")
            idx = ipr.link_lookup(ifname=bridge)[0]
            ipr.addr("add", index=idx, address=addr, prefixlen=int(prefix))
            ipr.link("set", index=idx, state="up")

    def add_node(self, ns: str, host_if: str, cidr: str, bridge: str, gateway: str) -> None:
        from pyroute2 import IPRoute, NetNS, netns

        addr, prefix = cidr.split("/")
        peer = f"{host_if}p"
        netns.create(ns)
        with IPRoute() as ipr:
            ipr.link("add", ifname=host_if, kind="veth", peer=peer)
            peer_idx = ipr.link_lookup(ifname=peer)[0]
            ipr.link("set", index=peer_idx, net_ns_fd=ns)
            host_idx = ipr.link_lookup(ifname=host_if)[0]
            br_idx = ipr.link_lookup(ifname=bridge)[0]
            ipr.link("set", index=host_idx, master=br_idx)
            ipr.link("set", index=host_idx, state="up")
        with NetNS(ns) as nsr:
            idx = nsr.link_lookup(ifname=peer)[0]
            nsr.link("set", index=idx, ifname="eth0")
            nsr.addr("add", index=idx, address=addr, prefixlen=int(prefix))
            nsr.link("set", index=idx, state="up")
            lo = nsr.link_lookup(ifname="lo")[0]
            nsr.link("set", index=lo, state="up")
            nsr.route("add", dst="default", gateway=gateway)

    def teardown(self, bridge: str | None, namespaces: Sequence[str]) -> list[str]:
        from pyroute2 import IPRoute, netns

        errors = []
        for ns in namespaces:
            try:
                netns.remove(ns)
            except OSError as exc:
                errors.append(f"remove netns {ns}: {exc}")
        if bridge:
            try:
                with IPRoute() as ipr:
                    found = ipr.link_lookup(ifname=bridge)
                    if found:
                        ipr.link("del", index=found[0])
            except Exception as exc:  # noqa: BLE001 - pyroute2 raises NetlinkError etc.
                errors.append(f"delete {bridge}: {exc}")
        return errors

    def exec_prefix(self, ns: str) -> list[str]:
        nsenter = shutil.which("nsenter")
        if nsenter:
            return [nsenter, f"--net={NETNS_RUN_DIR}/{ns}"]
        return []

    def preexec(self, ns: str) -> Callable[[], None] | None:
        if shutil.which("nsenter"):
            return None

        def enter() -> None:
            from pyroute2 import netns

            netns.setns(ns)

        return enter


def default_backend() -> NetBackend:
    ip = shutil.which("ip")
    if ip:
        return IpCommandBackend(ip)
    return Pyroute2Backend()


# --- manager -----------------------------------------------------------------------------------


class SimManager:
    """Start, inspect and stop simulated nodes of a system.

    Args:
        workdir: flash images, logs and state (default
            ``<home>/.bacnet-uc/sim/<system>``; required for :meth:`load`).
        mode: ``host``, ``netns`` or ``compose``.
        backend: network backend for ``netns`` (default: ``ip`` or pyroute2).
    """

    def __init__(self, workdir: Path | str | None = None, mode: SimMode = "netns", *,
                 subnet: str = SIM_SUBNET, bridge: str = BRIDGE,
                 backend: NetBackend | None = None, image: str = DEFAULT_IMAGE,
                 run_compose: bool = True) -> None:
        if mode not in MODES:
            raise HarnessError(f"unknown simulation mode {mode!r} (one of {', '.join(MODES)})")
        self.workdir = Path(workdir).expanduser().resolve() if workdir else None
        self.mode: SimMode = mode
        self.subnet = subnet
        self.bridge = bridge
        self._backend = backend
        self.image = image
        self.run_compose = run_compose
        self.state: SimState | None = None
        self._procs: dict[str, subprocess.Popen[bytes]] = {}

    # -- persistence
    @classmethod
    def load(cls, workdir: Path | str) -> SimManager:
        """Manager of a running simulation (from ``<workdir>/sim-state.json``)."""
        wd = Path(workdir).expanduser().resolve()
        try:
            state = SimState.from_dict(json.loads((wd / STATE_FILE).read_text()))
        except (OSError, ValueError, TypeError) as exc:
            raise HarnessError(f"no simulation state in {wd}: {exc}") from exc
        mgr = cls(wd, state.mode, subnet=state.subnet, bridge=state.bridge or BRIDGE)
        mgr.state = state
        return mgr

    @staticmethod
    def default_workdir(system: str) -> Path:
        return paths.state_dir() / "sim" / system

    def _save(self) -> None:
        if self.state is not None and self.workdir is not None:
            self.workdir.mkdir(parents=True, exist_ok=True)
            (self.workdir / STATE_FILE).write_text(json.dumps(self.state.to_dict(), indent=2))

    @property
    def backend(self) -> NetBackend:
        if self._backend is None:
            self._backend = default_backend()
        return self._backend

    # -- planning (pure)
    def host_cidr(self) -> str:
        return f"{self.subnet}.254/24"

    def addresses(self, system: System) -> dict[str, str]:
        if self.mode == "host":
            return {n.name: "127.0.0.1" for n in system.sim_nodes()}
        return sim_address_plan(system, self.subnet)

    def node_command(self, exe: Path | str, node: SimNode | None = None, *, flash: str = "",
                     seed: int = 0, prefix: Sequence[str] = ()) -> list[str]:
        f = node.flash if node else flash
        s = node.seed if node else seed
        return [*prefix, str(exe), f"--flash={f}", f"--seed={s}"]

    def plan_nodes(self, system: System, exe: Path | str, workdir: Path) -> list[SimNode]:
        sims = system.sim_nodes()
        if not sims:
            raise HarnessError(f"system {system.name!r} has no nodes with transport 'sim'")
        if self.mode == "host" and len(sims) > 1:
            raise HarnessError(
                f"mode 'host' runs one node only (SMP port 1337 is fixed), the system has "
                f"{len(sims)} simulated nodes: use mode 'netns' (root) or 'compose'")
        addrs = self.addresses(system)
        out = []
        for i, n in enumerate(sims, start=1):
            node = SimNode(name=n.name, index=i, address=addrs[n.name],
                           flash=str(workdir / f"{n.name}.flash.bin"),
                           log=str(workdir / f"{n.name}.log"), seed=0x5EED0000 + i, command=[],
                           device_instance=n.instance, board=n.board,
                           bacnet_port=n.bacnet_port)
            if self.mode == "netns":
                node.netns = netns_name(n.name)
                node.host_if = host_if_name(i)
            if self.mode == "compose":
                node.flash = f"/data/{n.name}.flash.bin"
                node.command = self.node_command("/opt/bacnet-uc/zephyr.exe", node)
            elif self.mode == "netns":
                prefix = self.backend.exec_prefix(node.netns or "")
                node.command = self.node_command(exe, node, prefix=prefix)
            else:
                node.command = self.node_command(exe, node)
            out.append(node)
        return out

    def compose_document(self, system: System, exe: Path | str, workdir: Path,
                         nodes: Sequence[SimNode]) -> dict[str, Any]:
        services = {}
        for n in nodes:
            services[n.name] = {
                "image": self.image,
                "command": n.command,
                "working_dir": "/data",
                "init": True,
                "volumes": [f"{Path(exe).resolve()}:/opt/bacnet-uc/zephyr.exe:ro",
                            f"{workdir}:/data"],
                "networks": {"bacnet_uc": {"ipv4_address": n.address}},
            }
        return {
            "name": f"bacnet-uc-{system.name}",
            "services": services,
            "networks": {"bacnet_uc": {
                "driver": "bridge",
                "driver_opts": {"com.docker.network.bridge.name": self.bridge},
                "ipam": {"config": [{"subnet": f"{self.subnet}.0/24",
                                     "gateway": f"{self.subnet}.254"}]},
            }},
        }

    # -- lifecycle
    def _netns_in_use(self, backend: NetBackend, nodes: Sequence[SimNode]) -> list[str]:
        """The bridge, namespaces and host veth names of this plan that exist."""
        out = []
        if backend.link_exists(self.bridge):
            out.append(f"bridge {self.bridge}")
        for n in nodes:
            if n.netns and backend.netns_exists(n.netns):
                out.append(f"namespace {n.netns}")
            if n.host_if and backend.link_exists(n.host_if):
                out.append(f"interface {n.host_if}")
        return out

    @staticmethod
    def _host_ports_in_use(nodes: Sequence[SimNode]) -> list[str]:
        """UDP ports a host-mode node needs that are taken on this host."""
        busy = []
        for n in nodes:
            for what, port in (("SMP", SMP_PORT), ("BACnet/IP", n.bacnet_port)):
                if not _udp_port_free(port):
                    busy.append(f"UDP {port} ({what})")
        return busy

    def _check_host_ports(self, nodes: Sequence[SimNode]) -> None:
        busy = self._host_ports_in_use(nodes)
        if busy:
            raise HarnessError(
                f"mode 'host': {', '.join(busy)} already in use on this host (another node or "
                "simulation?); the node would start without its SMP server and the harness "
                "would talk to whatever owns the port. Stop it, or use mode 'netns' or "
                "'compose'")

    def _bind_failures(self, node: SimNode, since: int) -> list[str]:
        """Log lines written since offset ``since`` that report a failed bind."""
        try:
            with open(node.log, "rb") as f:
                f.seek(since)
                text = f.read().decode(errors="replace")
        except OSError:
            return []
        return [_ANSI.sub("", ln) for ln in text.splitlines() if _BIND_FAILED.search(ln)]

    def _spawn(self, node: SimNode, cwd: Path) -> None:
        log = open(node.log, "ab")  # noqa: SIM115 - handed to the child
        try:
            preexec = self.backend.preexec(node.netns) if node.netns else None
            proc = subprocess.Popen(node.command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True,
                                    preexec_fn=preexec)
        except OSError as exc:
            raise HarnessError(f"cannot start {node.command[0]}: {exc}") from exc
        finally:
            log.close()
        node.pid = proc.pid
        node.started_at = time.time()
        node.proc_start = _proc_start_time(proc.pid)
        self._procs[node.name] = proc

    def start(self, system: System, firmware_exe: Path | str, workdir: Path | str | None = None,
              *, erase_flash: bool = False, startup_wait: float = 0.5) -> dict[str, str]:
        """Start the simulated nodes; returns their addresses by node name."""
        exe = Path(firmware_exe).expanduser().resolve()
        if not exe.is_file():
            raise HarnessError(f"firmware executable not found: {exe} (build native_sim/native/64 "
                               "with build_firmware)")
        wd = Path(workdir).expanduser().resolve() if workdir else (
            self.workdir or self.default_workdir(system.name))
        self.workdir = wd
        wd.mkdir(parents=True, exist_ok=True)
        if (wd / STATE_FILE).is_file():
            old = SimManager.load(wd)
            if any(_node_alive(n) for n in (old.state.nodes.values() if old.state else [])):
                raise HarnessError(f"a simulation is already running in {wd}; stop it first")
        nodes = self.plan_nodes(system, exe, wd)
        if self.mode == "host":
            self._check_host_ports(nodes)
        if erase_flash:
            for n in nodes:
                Path(wd / f"{n.name}.flash.bin").unlink(missing_ok=True)
        self.state = SimState(system=system.name, mode=self.mode, workdir=str(wd), exe=str(exe),
                              subnet=self.subnet, nodes={n.name: n for n in nodes})
        if self.mode == "compose":
            doc = self.compose_document(system, exe, wd, nodes)
            file = wd / "docker-compose.yml"
            file.write_text(yaml.safe_dump(doc, sort_keys=False))
            self.state.compose_file = str(file)
            self.state.bridge = self.bridge
            self.state.host_address = f"{self.subnet}.254"
            self._save()
            if self.run_compose:
                self._compose(["up", "-d"])
            return {n.name: n.address for n in nodes}
        if self.mode == "netns":
            if not _has_caps(CAP_NET_ADMIN, CAP_SYS_ADMIN):
                raise HarnessError("mode 'netns' needs root (CAP_NET_ADMIN and CAP_SYS_ADMIN); "
                                   "use mode 'compose' or run the harness as root")
            backend = self.backend
            taken = self._netns_in_use(backend, nodes)
            if taken:
                self.state = None
                raise HarnessError(
                    f"mode 'netns': {', '.join(taken)} already exist(s): another netns "
                    "simulation is running (sim_status / sim_stop), or they are left over from "
                    f"one that crashed (remove them: ip link del {self.bridge}; ip netns del "
                    "<name>)")
            self.state.backend = backend.name
            self.state.bridge = self.bridge
            self.state.host_address = f"{self.subnet}.254"
            self._save()
            # on failure tear down only what this call created (the bridge
            # did not exist before, even a half-made one is ours), never the
            # namespaces it did not get to
            created: list[str] = []
            try:
                backend.setup_bridge(self.bridge, self.host_cidr())
                for n in nodes:
                    created.append(n.netns or "")
                    backend.add_node(n.netns or "", n.host_if or "", f"{n.address}/24",
                                     self.bridge, SIM_HOST_ADDRESS if self.subnet == SIM_SUBNET
                                     else f"{self.subnet}.254")
            except Exception as exc:
                backend.teardown(self.bridge, [ns for ns in created if ns])
                self.state = None
                (wd / STATE_FILE).unlink(missing_ok=True)
                raise HarnessError(f"network setup failed: {exc}") from exc
        log_start = {}
        for n in nodes:
            log_start[n.name] = _file_size(n.log)
            self._spawn(n, wd)
        self._save()
        time.sleep(startup_wait)
        dead = [n.name for n in nodes if not _node_alive(n)]
        if dead:
            tails = {d: self.log_tail(d, 10) for d in dead}
            self.stop()
            raise HarnessError(f"simulated node(s) exited right after start: {tails}")
        unbound = {n.name: lines for n in nodes
                   if (lines := self._bind_failures(n, log_start[n.name]))}
        if unbound:
            self.stop()
            raise HarnessError(f"simulated node(s) could not bind their sockets (port in use?): "
                               f"{unbound}")
        return {n.name: n.address for n in nodes}

    def _compose(self, args: list[str]) -> str:
        if self.state is None or not self.state.compose_file:
            raise HarnessError("no compose file")
        docker = shutil.which("docker")
        if docker is None:
            raise HarnessError("docker not found; start the file manually: "
                               f"docker compose -f {self.state.compose_file} up -d")
        res = subprocess.run([docker, "compose", "-f", self.state.compose_file, *args],
                             capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise HarnessError(f"docker compose {' '.join(args)} failed: "
                               f"{(res.stderr or res.stdout).strip()}")
        return res.stdout

    def _kill(self, node: SimNode, timeout: float = 3.0) -> None:
        if not _node_alive(node):
            return  # gone, or the pid now belongs to another process
        pid = node.pid or 0
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while _node_alive(node) and time.monotonic() < deadline:
            proc = self._procs.get(node.name)
            if proc is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(0.1)
            else:
                time.sleep(0.1)
        if _node_alive(node):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        proc = self._procs.pop(node.name, None)
        if proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(1.0)

    def stop(self) -> dict[str, Any]:
        """Stop all nodes and remove the network (flash images are kept)."""
        if self.state is None:
            if self.workdir is not None and (self.workdir / STATE_FILE).is_file():
                self.state = SimManager.load(self.workdir).state
            else:
                return {"stopped": [], "errors": ["no simulation running"]}
        state = self.state
        errors: list[str] = []
        if state.mode == "compose":
            try:
                self._compose(["down"])
            except HarnessError as exc:
                errors.append(str(exc))
        else:
            for n in state.nodes.values():
                self._kill(n)
            if state.mode == "netns":
                backend: NetBackend
                if self._backend is not None:
                    backend = self._backend
                elif state.backend == "pyroute2":
                    backend = Pyroute2Backend()
                else:
                    backend = default_backend()
                errors += backend.teardown(state.bridge,
                                           [n.netns for n in state.nodes.values() if n.netns])
        stopped = list(state.nodes)
        if self.workdir is not None:
            (self.workdir / STATE_FILE).unlink(missing_ok=True)
        self.state = None
        return {"stopped": stopped, "errors": errors}

    def can_restart(self) -> bool:
        """Whether :meth:`restart` can work here: ``netns`` needs root to enter
        the namespace, ``compose`` the docker command."""
        if self.state is None:
            return False
        if self.state.mode == "netns":
            return _has_caps(CAP_NET_ADMIN, CAP_SYS_ADMIN)
        if self.state.mode == "compose":
            return shutil.which("docker") is not None
        return True

    def restart(self, node: str, *, wait: float = 0.3) -> SimNode:
        """Kill and restart one node (its flash image is kept): the reboot of a
        simulated node."""
        if self.state is None:
            raise HarnessError("no simulation running")
        try:
            n = self.state.nodes[node]
        except KeyError:
            raise HarnessError(f"node {node!r} is not simulated here") from None
        if self.state.mode == "compose":
            self._compose(["restart", node])
            return n
        self._kill(n)
        if self.state.mode == "host":
            self._check_host_ports([n])
        since = _file_size(n.log)
        self._spawn(n, Path(self.state.workdir))
        self._save()
        time.sleep(wait)
        if not _node_alive(n):
            raise HarnessError(f"node {node!r} exited after restart: {self.log_tail(node, 10)}")
        unbound = self._bind_failures(n, since)
        if unbound:
            self._kill(n)
            raise HarnessError(f"node {node!r} could not bind its sockets after the restart "
                               f"(port in use?): {unbound}")
        return n

    def status(self) -> dict[str, Any]:
        if self.state is None:
            return {"running": False, "nodes": {}}
        st = self.state
        nodes = {}
        for n in st.nodes.values():
            nodes[n.name] = {"address": n.address, "smp": f"udp:{n.address}:1337",
                             "bacnet": f"{n.address}:{n.bacnet_port}", "pid": n.pid,
                             "alive": _node_alive(n) if st.mode != "compose" else None,
                             "netns": n.netns, "flash": n.flash, "log": n.log,
                             "device_instance": n.device_instance}
        return {"running": True, "system": st.system, "mode": st.mode, "workdir": st.workdir,
                "exe": st.exe, "bridge": st.bridge, "host_address": st.host_address,
                "backend": st.backend, "compose_file": st.compose_file, "nodes": nodes}

    def log_tail(self, node: str, lines: int = 50) -> list[str]:
        if self.state is None or node not in self.state.nodes:
            return []
        try:
            text = Path(self.state.nodes[node].log).read_text(errors="replace")
        except OSError:
            return []
        return [_ANSI.sub("", ln) for ln in text.splitlines()[-lines:]]

    def inventory_nodes(self) -> list[dict[str, Any]]:
        """Inventory entries (``transport: sim``) of the running nodes."""
        if self.state is None:
            return []
        return [{"name": n.name, "transport": "sim", "host": n.address, "port": 1337,
                 "bacnet_address": f"{n.address}:{n.bacnet_port}", "board": n.board,
                 "device_instance": n.device_instance, "system": self.state.system}
                for n in self.state.nodes.values()]


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _proc_start_time(pid: int) -> float | None:
    """Start time of process ``pid`` in seconds since the epoch (Linux
    /proc), ``None`` when unknown."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # field 22, starttime (clock ticks after boot)
        btime = next(int(ln.split()[1]) for ln in Path("/proc/stat").read_text().splitlines()
                     if ln.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError, StopIteration):
        return None


def _same_process(node: SimNode) -> bool:
    """Whether ``node.pid`` is still the process the manager started: its
    start time matches the one recorded at spawn (state files without it:
    ``started_at``). Without /proc the pid is trusted."""
    if not node.pid:
        return False
    now = _proc_start_time(node.pid)
    if now is None:
        return not Path("/proc/self/stat").exists()
    if node.proc_start is not None:
        return abs(now - node.proc_start) < 1.0
    if node.started_at is not None:
        # recorded right after the spawn: the process started just before
        return -5.0 < node.started_at - now < 5.0
    return False


def _node_alive(node: SimNode) -> bool:
    """The node's process is running and is still the one that was started."""
    return _alive(node.pid) and _same_process(node)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie child is not alive
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True
