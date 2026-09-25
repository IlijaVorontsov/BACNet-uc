"""Rig services inside netns svc: dnsmasq, mosquitto, chrony and openssl s_server.

- dnsmasq gives the DUT 192.0.2.10 by a reservation on its real MAC (bench.yml), answers
  ``broker.hil.lan`` with 192.0.2.1 and hands out router, DNS and NTP servers.
- mosquitto listens with TLS on 192.0.2.1:8883 (test PKI, optional client certificates and
  CRL) and without TLS on 127.0.0.1:1883 for the rig's own observer. The broker writes the
  TLS key log (decision B10) when the binary supports ``--tls-keylog`` (mosquitto >= 2.1);
  Ubuntu 24.04 ships 2.0.18, so otherwise the eclipse-mosquitto 2.1 image runs, entering
  netns svc with nsenter. Without either, the broker runs without a key log and says so.
- chronyd serves NTP from 192.0.2.1 without touching the host clock (skipped if missing).
- :class:`TlsServer` is ``openssl s_server`` pinned to TLS 1.2 or 1.3 with a key log, for
  version-pinned handshake tests.

Each service logs into its run directory (inside the artifacts) and stops cleanly.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from hilrig import netns as nsmod

MOSQUITTO_IMAGE = "eclipse-mosquitto:2.1.2-alpine"
BROKER_NAME = "broker.hil.lan"
MIN_LEASE_S = 120  # dnsmasq refuses shorter leases


class ServiceError(RuntimeError):
    """A service did not start or became unavailable."""


class Service(ABC):
    """A server process inside a namespace (None: this one), output in ``<rundir>/<name>.log``."""

    name = "service"

    def __init__(self, netns: str | None, rundir: Path) -> None:
        self.netns = netns
        self.rundir = rundir
        self.log = rundir / f"{self.name}.log"
        self.proc: subprocess.Popen[bytes] | None = None

    @abstractmethod
    def argv(self) -> list[str]:
        """Return the command line (run inside ``netns`` unless :meth:`launch` differs)."""

    @abstractmethod
    def ready(self) -> bool:
        """Tell whether the service accepts requests."""

    def launch(self) -> subprocess.Popen[bytes]:
        """Start the process; subclasses change how (for example through docker)."""
        return nsmod.spawn(self.netns, self.argv(), log=self.log)

    def start(self, timeout: float = 15.0) -> Self:
        """Start the service and wait until :meth:`ready`; stop it again if it never is."""
        self.rundir.mkdir(parents=True, exist_ok=True)
        self.proc = self.launch()
        try:
            _wait(self.ready, timeout, self.proc, f"{self.name} (see {self.log})")
        except BaseException:
            self.stop()  # __exit__ does not run when __enter__ raises
            raise
        return self

    def stop(self) -> None:
        """Stop the service: SIGTERM, then SIGKILL after 5 s (idempotent)."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if proc.stdin:
            proc.stdin.close()

    def note(self, text: str) -> None:
        """Append a rig note to the service log (for example: no key log)."""
        with self.log.open("a") as f:
            f.write(f"# hilrig: {text}\n")

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def _wait(check: Callable[[], bool], timeout: float, proc: subprocess.Popen[bytes], what: str) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if proc.poll() is not None:
            raise ServiceError(f"{what} exited with status {proc.returncode}")
        if time.monotonic() > deadline:
            raise ServiceError(f"{what} not ready after {timeout} s")
        time.sleep(0.05)


@dataclass(frozen=True)
class Lease:
    """One line of the dnsmasq lease file."""

    expiry: int
    mac: str
    ip: str
    hostname: str


class Dnsmasq(Service):
    """DHCP (reservation for the DUT only) and DNS for subnet A."""

    name = "dnsmasq"

    def __init__(
        self,
        netns: str,
        rundir: Path,
        *,
        dut_mac: str | None,
        dut_ip: str = nsmod.DUT_IP,
        lease_s: int = 3600,
        iface: str = "svc0",
        server_ip: str = nsmod.HOSTS["svc"].ip,
        router_ip: str = nsmod.ROUTER_A_IP,
    ) -> None:
        super().__init__(netns, rundir)
        if lease_s < MIN_LEASE_S:
            raise ValueError(f"lease_s={lease_s}: dnsmasq needs at least {MIN_LEASE_S} s")
        self.dut_mac, self.dut_ip, self.lease_s = dut_mac, dut_ip, lease_s
        self.iface, self.server_ip, self.router_ip = iface, server_ip, router_ip
        self.leasefile = rundir / "dnsmasq.leases"

    def argv(self) -> list[str]:
        # "static": only reserved hosts get an address, so nothing else on br-a is served.
        argv = [
            "dnsmasq",
            "--keep-in-foreground",
            "--conf-file=/dev/null",
            "--no-resolv",
            "--no-hosts",
            "--bind-interfaces",
            f"--interface={self.iface}",
            "--user=root",
            "--pid-file=",
            "--dhcp-authoritative",
            "--log-dhcp",
            "--log-queries",
            "--log-facility=-",
            f"--dhcp-leasefile={self.leasefile}",
            f"--dhcp-range={self.server_ip},static,255.255.255.0,{self.lease_s}",
            f"--dhcp-option=option:router,{self.router_ip}",
            f"--dhcp-option=option:dns-server,{self.server_ip}",
            f"--dhcp-option=option:ntp-server,{self.server_ip}",
            f"--address=/{BROKER_NAME}/{self.server_ip}",
        ]
        if self.dut_mac:
            argv.append(f"--dhcp-host={self.dut_mac},{self.dut_ip},{self.lease_s}")
        return argv

    def test_config(self) -> None:
        """Check the command line with ``dnsmasq --test``; raise ServiceError if rejected."""
        proc = nsmod.run(self.netns, [*self.argv(), "--test"], check=False)
        if proc.returncode != 0:
            raise ServiceError(f"dnsmasq --test: {proc.stderr.strip()}")

    def ready(self) -> bool:
        return nsmod.listening(self.netns, "udp", 67) and nsmod.listening(self.netns, "udp", 53)

    def leases(self) -> list[Lease]:
        """Return the current leases."""
        if not self.leasefile.exists():
            return []
        leases = []
        for line in self.leasefile.read_text().splitlines():
            expiry, mac, ip, hostname, *_ = line.split()
            leases.append(Lease(int(expiry), mac, ip, hostname))
        return leases


@dataclass(frozen=True)
class BrokerTls:
    """TLS settings of the broker's 8883 listener.

    ``min_version`` is mosquitto's ``tls_version``, a minimum, not a pin: pin versions with
    :class:`TlsServer`.
    """

    ca: Path
    cert: Path
    key: Path
    crl: Path | None = None
    require_certificate: bool = False
    min_version: Literal["tlsv1.2", "tlsv1.3"] = "tlsv1.2"


def mosquitto_has_keylog(binary: str = "mosquitto") -> bool:
    """Tell whether a mosquitto binary supports ``--tls-keylog`` (added in 2.1)."""
    if not shutil.which(binary):
        return False
    proc = subprocess.run([binary, "-h"], capture_output=True, text=True, timeout=10, check=False)
    return "--tls-keylog" in proc.stdout + proc.stderr


def docker_image_available(image: str = MOSQUITTO_IMAGE) -> bool:
    """Tell whether docker runs and has ``image`` locally (bootstrap pulls it)."""
    if not shutil.which("docker"):
        return False
    proc = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30, check=False)
    return proc.returncode == 0


class Mosquitto(Service):
    """MQTT broker: TLS on ``bind_ip:port`` and a plain observer listener on 127.0.0.1.

    ``runtime`` is ``local`` (the host's mosquitto), ``docker`` (``image``) or ``auto``:
    local if it can write a key log, else docker if the image is there, else local without
    a key log. :attr:`keylog` is None and :attr:`keylog_status` says why when there is none.
    """

    name = "mosquitto"

    def __init__(
        self,
        netns: str,
        rundir: Path,
        tls: BrokerTls,
        *,
        bind_ip: str = nsmod.HOSTS["svc"].ip,
        port: int = 8883,
        observer_port: int = 1883,
        keylog: bool = True,
        runtime: Literal["auto", "local", "docker"] = "auto",
        image: str = MOSQUITTO_IMAGE,
        name: str = "mosquitto",
    ) -> None:
        self.name = name
        super().__init__(netns, rundir)
        self.tls, self.bind_ip, self.port, self.observer_port = tls, bind_ip, port, observer_port
        self.image = image
        self.conf = rundir / f"{name}.conf"
        local_keylog = mosquitto_has_keylog()
        why = f"runtime {runtime} was requested"
        if runtime == "auto":
            use_docker = keylog and not local_keylog and docker_image_available(image)
            runtime = "docker" if use_docker else "local"
            why = f"docker image {image} is unavailable"
        self.runtime = runtime
        self.keylog: Path | None = None
        if not keylog:
            self.keylog_status = "no keylog: not requested"
        elif runtime == "docker" or local_keylog:
            self.keylog = rundir / f"{name}-keys.log"
            self.keylog_status = f"broker key log {self.keylog}"
        else:
            self.keylog_status = f"no keylog: the local mosquitto lacks --tls-keylog (needs 2.1); {why}"
        self.container = f"hil-{name}-{netns}"

    def config(self) -> str:
        """Return the broker configuration."""
        t = self.tls
        lines = [
            "# generated by hilrig.services.Mosquitto",
            "per_listener_settings false",
            "allow_anonymous true",
            "persistence false",
            "user root",
            "log_dest stdout",
            "log_type all",
            "connection_messages true",
            "log_timestamp_format %Y-%m-%dT%H:%M:%S",
            f"listener {self.port} {self.bind_ip}",
            f"cafile {t.ca}",
            f"certfile {t.cert}",
            f"keyfile {t.key}",
            f"tls_version {t.min_version}",
            f"require_certificate {'true' if t.require_certificate else 'false'}",
        ]
        if t.crl:
            lines.append(f"crlfile {t.crl}")
        lines.append(f"listener {self.observer_port} 127.0.0.1")
        return "\n".join(lines) + "\n"

    def argv(self) -> list[str]:
        argv = ["mosquitto", "-c", str(self.conf)]
        return argv + (["--tls-keylog", str(self.keylog)] if self.keylog else [])

    def launch(self) -> subprocess.Popen[bytes]:
        self.conf.write_text(self.config())
        self.note(f"runtime {self.runtime}; {self.keylog_status}")
        if self.runtime == "local":
            return super().launch()
        self._docker_rm()
        mounts: list[str] = ["-v", "/run/netns:/run/netns:ro", "-v", f"{self.rundir}:{self.rundir}"]
        for d in sorted({p.parent for p in (self.tls.ca, self.tls.cert, self.tls.key, self.tls.crl) if p}):
            if d != self.rundir:
                mounts += ["-v", f"{d}:{d}:ro"]
        # No network of its own: nsenter (privileged) moves the broker into netns svc.
        docker = ["docker", "run", "--rm", "--name", self.container, "--privileged", "--network", "none"]
        enter = ["--entrypoint", "nsenter", self.image, f"--net=/run/netns/{self.netns}"]
        broker = ["/usr/sbin/mosquitto", *self.argv()[1:]]
        return nsmod.spawn(None, [*docker, *mounts, *enter, *broker], log=self.log)

    def ready(self) -> bool:
        return all(nsmod.listening(self.netns, "tcp", port) for port in (self.port, self.observer_port))

    def _docker_rm(self) -> None:
        """Remove this broker's container, whatever state a previous run left it in."""
        subprocess.run(["docker", "rm", "-f", self.container], capture_output=True, timeout=30, check=False)

    def stop(self) -> None:
        super().stop()
        if self.runtime == "docker":
            self._docker_rm()


class TlsServer(Service):
    """``openssl s_server`` pinned to one TLS version, writing an NSS key log.

    With ``ca`` it requests a client certificate (``-Verify 1``: required).
    """

    def __init__(
        self,
        netns: str,
        rundir: Path,
        *,
        cert: Path,
        key: Path,
        version: Literal["1.2", "1.3"],
        port: int = 8884,
        bind_ip: str = nsmod.HOSTS["svc"].ip,
        ca: Path | None = None,
        name: str | None = None,
    ) -> None:
        self.name = name or f"s_server-tls{version.replace('.', '')}-{port}"
        super().__init__(netns, rundir)
        self.cert, self.key, self.version, self.ca = cert, key, version, ca
        self.port, self.bind_ip = port, bind_ip
        self.keylog = rundir / f"{self.name}-keys.log"

    def argv(self) -> list[str]:
        argv = ["openssl", "s_server", "-accept", f"{self.bind_ip}:{self.port}", "-cert", str(self.cert)]
        argv += ["-key", str(self.key), f"-tls{self.version.replace('.', '_')}"]
        argv += ["-keylogfile", str(self.keylog)]
        return argv + (["-CAfile", str(self.ca), "-Verify", "1"] if self.ca else [])

    def ready(self) -> bool:
        return nsmod.listening(self.netns, "tcp", self.port)


class Chrony(Service):
    """chronyd serving NTP on ``bind_ip`` from the host clock, which it never adjusts (-x)."""

    name = "chrony"

    def __init__(
        self, netns: str, rundir: Path, *, bind_ip: str = nsmod.HOSTS["svc"].ip, allow: str = nsmod.SUBNET_A
    ) -> None:
        super().__init__(netns, rundir)
        self.conf = rundir / "chrony.conf"
        self.bind_ip, self.allow = bind_ip, allow

    @staticmethod
    def available() -> bool:
        """Tell whether chronyd is installed."""
        return shutil.which("chronyd") is not None

    def argv(self) -> list[str]:
        return ["chronyd", "-d", "-x", "-u", "root", "-f", str(self.conf)]

    def launch(self) -> subprocess.Popen[bytes]:
        pidfile = self.rundir / "chronyd.pid"
        # cmdport 0 and bindcmdaddress / turn both command sockets off, so chronyd creates
        # nothing outside the run directory (the Unix socket would live in /run/chrony).
        config = [f"bindaddress {self.bind_ip}", f"allow {self.allow}", "local stratum 8"]
        config += ["cmdport 0", "bindcmdaddress /"]
        self.conf.write_text("\n".join([*config, f"pidfile {pidfile}", ""]))
        return super().launch()

    def ready(self) -> bool:
        return nsmod.listening(self.netns, "udp", 123)


class RigServices:
    """The session's services for one topology: dnsmasq, mosquitto and (if installed) chrony."""

    def __init__(self, dnsmasq: Dnsmasq, broker: Mosquitto, chrony: Chrony | None) -> None:
        self.dnsmasq, self.broker, self.chrony = dnsmasq, broker, chrony

    @property
    def all(self) -> Sequence[Service]:
        """Return the services in start order."""
        return [s for s in (self.dnsmasq, self.chrony, self.broker) if s is not None]

    def start(self) -> RigServices:
        """Start every service (stopping those already started if one fails)."""
        try:
            for service in self.all:
                service.start()
        except BaseException:
            self.stop()
            raise
        return self

    def stop(self) -> None:
        """Stop every service, last started first."""
        for service in reversed(self.all):
            service.stop()

    def __enter__(self) -> RigServices:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
