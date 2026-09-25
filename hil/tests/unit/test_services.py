"""Rig services in netns svc, used from sim1 (hilrig.services), on the private topology."""

from __future__ import annotations

import os
import socket
import ssl
import struct
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from hilrig import netns
from hilrig.mqtt import MqttClient, MqttError, TlsOptions
from hilrig.netns import HOSTS, Topology
from hilrig.pki import Pki
from hilrig.services import (
    BrokerTls,
    Chrony,
    Dnsmasq,
    Mosquitto,
    RigServices,
    Service,
    ServiceError,
    TlsServer,
    docker_image_available,
    mosquitto_has_keylog,
)
from rig_skips import needs_tools

pytestmark = needs_tools("dnsmasq", "mosquitto", "openssl")

DUT_MAC = "02:48:49:4c:00:99"
SVC_IP = HOSTS["svc"].ip
LEASE_S = 600


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    return Pki.generate(tmp_path_factory.mktemp("pki"))


@pytest.fixture(scope="module")
def rundir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("services")


@pytest.fixture(scope="module")
def rig(unit_net: Topology, pki: Pki, rundir: Path) -> Iterator[RigServices]:
    svc = unit_net.ns("svc")
    good = pki.servers["good"]
    services = RigServices(
        Dnsmasq(svc, rundir, dut_mac=DUT_MAC, lease_s=LEASE_S),
        Mosquitto(svc, rundir, BrokerTls(pki.ca, good.cert, good.key, crl=pki.crl), runtime="local"),
        Chrony(svc, rundir) if Chrony.available() else None,
    )
    with services:
        yield services
    assert netns.run(None, ["ip", "netns", "pids", svc]).stdout.strip() == "", "a service outlived stop()"


# ---- dnsmasq --------------------------------------------------------------------------------
def dhcp(
    ns: str, mac: str, msg_type: int, xid: bytes, extra: bytes = b"", timeout: float = 5.0
) -> tuple[str, dict[int, bytes]]:
    """Send one DHCP message with chaddr ``mac`` from ``ns``; return (yiaddr, options) of the reply."""
    options = bytes([53, 1, msg_type, 55, 5, 1, 3, 6, 42, 51]) + extra + b"\xff"
    packet = (
        struct.pack("!BBBB4sHH", 1, 1, 6, 0, xid, 0, 0x8000)
        + bytes(16)
        + bytes.fromhex(mac.replace(":", ""))
        + bytes(10 + 64 + 128)
        + b"\x63\x82\x53\x63"
        + options
    )
    with netns.enter(ns):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 68))
        sock.settimeout(timeout)
        sock.sendto(packet, ("255.255.255.255", 67))
        while True:
            reply = sock.recv(1500)
            if reply[0] == 2 and reply[4:8] == xid:
                break
    found: dict[int, bytes] = {}
    i = 240
    while i < len(reply) and reply[i] != 255:
        if reply[i] == 0:
            i += 1
            continue
        found[reply[i]] = reply[i + 2 : i + 2 + reply[i + 1]]
        i += 2 + reply[i + 1]
    return socket.inet_ntoa(reply[16:20]), found


def dns_a(ns: str, server: str, name: str) -> list[str]:
    """Resolve an A record with one hand-made query."""
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    query += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00\x00\x01\x00\x01"
    with netns.enter(ns):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock:
        sock.settimeout(5)
        sock.sendto(query, (server, 53))
        reply = sock.recv(512)
    answers, offset = struct.unpack("!H", reply[6:8])[0], len(query)
    addresses = []
    for _ in range(answers):
        rtype, _, _, rdlen = struct.unpack("!HHIH", reply[offset + 2 : offset + 12])
        if rtype == 1:
            addresses.append(socket.inet_ntoa(reply[offset + 12 : offset + 12 + rdlen]))
        offset += 12 + rdlen
    return addresses


def test_dnsmasq_command_line_passes_its_own_check(rig: RigServices) -> None:
    rig.dnsmasq.test_config()
    with pytest.raises(ValueError, match="at least 120 s"):
        Dnsmasq("x", Path("/tmp"), dut_mac=None, lease_s=60)


def test_dnsmasq_reserves_the_dut_address_with_lease_router_dns_and_ntp(
    rig: RigServices, unit_net: Topology
) -> None:
    sim1 = unit_net.ns("sim1")
    ip, offer = dhcp(sim1, DUT_MAC, 1, os.urandom(4))
    assert ip == netns.DUT_IP and offer[53] == b"\x02"  # DHCPOFFER
    assert struct.unpack("!I", offer[51])[0] == LEASE_S
    assert socket.inet_ntoa(offer[3]) == netns.ROUTER_A_IP
    assert socket.inet_ntoa(offer[6]) == SVC_IP and socket.inet_ntoa(offer[42]) == SVC_IP
    xid = os.urandom(4)
    requested = bytes([50, 4]) + socket.inet_aton(ip) + bytes([54, 4]) + offer[54]
    ip, ack = dhcp(sim1, DUT_MAC, 3, xid, requested)
    assert (ip, ack[53]) == (netns.DUT_IP, b"\x05")  # DHCPACK
    leases = rig.dnsmasq.leases()
    assert [(lease.mac, lease.ip) for lease in leases] == [(DUT_MAC, netns.DUT_IP)]
    assert abs(leases[0].expiry - time.time() - LEASE_S) < 30


def test_dnsmasq_offers_nothing_to_other_macs(rig: RigServices, unit_net: Topology) -> None:
    with pytest.raises(TimeoutError):
        dhcp(unit_net.ns("sim1"), "02:00:00:00:00:77", 1, os.urandom(4), timeout=2)


def test_dnsmasq_answers_the_broker_name(rig: RigServices, unit_net: Topology) -> None:
    assert dns_a(unit_net.ns("sim1"), SVC_IP, "broker.hil.lan") == [SVC_IP]


# ---- mosquitto --------------------------------------------------------------------------------
def connect(unit_net: Topology, port: int, tls: TlsOptions) -> None:
    """Connect an MQTT client from sim1 over TLS; raise if the broker refuses it."""
    client = MqttClient(SVC_IP, port, netns=unit_net.ns("sim1"), tls=tls, client_id="hil-probe")
    client.connect(timeout=3)
    client.publish("hil/probe", "x")
    client.close()


def refused(unit_net: Topology, port: int, tls: TlsOptions) -> bool:
    """Tell whether the broker rejects this client, in either TLS 1.2 or 1.3 fashion.

    TLS 1.2 fails the handshake; under TLS 1.3 the client finishes first and the broker's
    alert arrives on the first read, so CONNACK never comes.
    """
    try:
        connect(unit_net, port, tls)
    except (ssl.SSLError, OSError, TimeoutError, MqttError):
        return True
    return False


def test_broker_serves_tls_with_the_test_pki_and_logs(rig: RigServices, unit_net: Topology, pki: Pki) -> None:
    broker = rig.broker
    connect(unit_net, broker.port, TlsOptions(pki.ca))
    connect(unit_net, broker.port, TlsOptions(pki.ca, version="1.2"))
    assert refused(unit_net, broker.port, TlsOptions(pki.rogue_ca))  # client does not trust it
    log = broker.log.read_text()
    assert "Opening ipv4 listen socket on port 8883" in log and "hil-probe" in log
    assert f"# hilrig: runtime local; {broker.keylog_status}" in log


def test_local_broker_without_keylog_support_records_it(rig: RigServices) -> None:
    if mosquitto_has_keylog():
        pytest.skip("the local mosquitto supports --tls-keylog")
    assert rig.broker.keylog is None and rig.broker.keylog_status.startswith("no keylog:")


def test_mutual_tls_broker_with_crl(unit_net: Topology, pki: Pki, rundir: Path) -> None:
    good = pki.servers["good"]
    tls = BrokerTls(pki.ca, good.cert, good.key, crl=pki.crl, require_certificate=True)
    with Mosquitto(
        unit_net.ns("svc"), rundir, tls, port=8885, observer_port=1885, runtime="local", name="mosquitto-mtls"
    ):
        dut = TlsOptions(pki.ca, pki.dut.cert, pki.dut.key)
        connect(unit_net, 8885, dut)
        connect(unit_net, 8885, TlsOptions(pki.ca, pki.dut.cert, pki.dut.key, version="1.2"))
        for version in ("1.2", "1.3"):
            assert refused(unit_net, 8885, TlsOptions(pki.ca, version=version))  # no certificate
            revoked = pki.clients["revoked"]
            assert refused(unit_net, 8885, TlsOptions(pki.ca, revoked.cert, revoked.key, version=version))
            rogue = pki.clients["rogue"]
            assert refused(unit_net, 8885, TlsOptions(pki.ca, rogue.cert, rogue.key, version=version))


@pytest.mark.skipif(
    not docker_image_available(), reason="docker or the eclipse-mosquitto 2.1 image is unavailable"
)
def test_docker_broker_writes_a_key_log(unit_net: Topology, pki: Pki, rundir: Path) -> None:
    good = pki.servers["good"]
    broker = Mosquitto(
        unit_net.ns("svc"),
        rundir,
        BrokerTls(pki.ca, good.cert, good.key),
        port=8886,
        observer_port=1886,
        runtime="docker",
        name="mosquitto-docker",
    )
    with broker:
        assert broker.keylog is not None
        connect(unit_net, 8886, TlsOptions(pki.ca, version="1.3"))
        connect(unit_net, 8886, TlsOptions(pki.ca, version="1.2"))
        keys = broker.keylog.read_text()
    assert "CLIENT_HANDSHAKE_TRAFFIC_SECRET" in keys and "CLIENT_RANDOM" in keys
    listed = subprocess.run(
        ["docker", "ps", "-aq", "-f", f"name={broker.container}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    assert listed.strip() == ""


def test_broker_that_cannot_start_reports_its_log(unit_net: Topology, pki: Pki, rundir: Path) -> None:
    good = pki.servers["good"]
    broken = BrokerTls(pki.ca, good.cert, rundir / "missing.key")
    with pytest.raises(ServiceError, match="exited"):
        Mosquitto(
            unit_net.ns("svc"),
            rundir,
            broken,
            port=8887,
            observer_port=1887,
            runtime="local",
            name="mosquitto-broken",
        ).start()


class NeverReady(Service):
    """A process that starts but never becomes ready."""

    name = "never-ready"

    def argv(self) -> list[str]:
        return ["sleep", "60"]

    def ready(self) -> bool:
        return False

    def launch(self) -> subprocess.Popen[bytes]:
        self.launched = super().launch()
        return self.launched


def test_service_that_never_gets_ready_is_stopped(tmp_path: Path) -> None:
    """Regression: a failed readiness wait left the process running (``with`` never exits)."""
    service = NeverReady(None, tmp_path)
    with pytest.raises(ServiceError, match=r"not ready after 0\.3 s"):
        service.start(timeout=0.3)  # what ``with service:`` runs in __enter__
    assert service.launched.poll() is not None and service.proc is None
    assert service.launched.stdin is not None and service.launched.stdin.closed


# ---- openssl s_server --------------------------------------------------------------------------
TLS12, TLS13 = ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3


def handshake(
    unit_net: Topology,
    port: int,
    ca: Path,
    *,
    versions: tuple[ssl.TLSVersion, ssl.TLSVersion] = (TLS12, TLS13),
    cert: tuple[Path, Path] | None = None,
) -> str:
    """Complete a TLS handshake from sim1 with the given (minimum, maximum) versions."""
    ctx = ssl.create_default_context(cafile=str(ca))
    ctx.minimum_version, ctx.maximum_version = versions
    if cert:
        ctx.load_cert_chain(*cert)
    with netns.enter(unit_net.ns("sim1")):
        raw = socket.create_connection((SVC_IP, port), timeout=5)
    with ctx.wrap_socket(raw, server_hostname="broker.hil.lan") as tls:
        return str(tls.version())


@pytest.mark.parametrize(
    ("version", "negotiated", "label", "other"),
    [
        ("1.2", "TLSv1.2", "CLIENT_RANDOM", TLS13),
        ("1.3", "TLSv1.3", "CLIENT_HANDSHAKE_TRAFFIC_SECRET", TLS12),
    ],
)
def test_s_server_pins_the_version_and_logs_keys(
    unit_net: Topology,
    pki: Pki,
    rundir: Path,
    version: Literal["1.2", "1.3"],
    negotiated: str,
    label: str,
    other: ssl.TLSVersion,
) -> None:
    good = pki.servers["good"]
    with TlsServer(unit_net.ns("svc"), rundir, cert=good.cert, key=good.key, version=version) as server:
        assert handshake(unit_net, server.port, pki.ca) == negotiated
        with pytest.raises(ssl.SSLError):
            handshake(unit_net, server.port, pki.ca, versions=(other, other))  # a client pinned elsewhere
    assert label in server.keylog.read_text()


def test_s_server_can_require_a_client_certificate(unit_net: Topology, pki: Pki, rundir: Path) -> None:
    good = pki.servers["good"]
    with TlsServer(
        unit_net.ns("svc"), rundir, cert=good.cert, key=good.key, version="1.2", ca=pki.ca, port=8888
    ):
        assert (
            handshake(unit_net, 8888, pki.ca, versions=(TLS12, TLS12), cert=(pki.dut.cert, pki.dut.key))
            == "TLSv1.2"
        )
        with pytest.raises((ssl.SSLError, ConnectionResetError)):
            handshake(unit_net, 8888, pki.ca, versions=(TLS12, TLS12))


# ---- chrony ------------------------------------------------------------------------------------
@pytest.mark.skipif(not Chrony.available(), reason="chronyd is not installed")
def test_chrony_answers_ntp_from_subnet_a(rig: RigServices, unit_net: Topology) -> None:
    request = b"\x23" + bytes(47)  # SNTP v4 client request
    with netns.enter(unit_net.ns("sim1")):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with sock:
        sock.settimeout(5)
        sock.sendto(request, (SVC_IP, 123))
        reply = sock.recv(128)
    mode, stratum = reply[0] & 0x07, reply[1]
    assert mode == 4 and stratum == 8  # server reply from 'local stratum 8'
    seconds = struct.unpack("!I", reply[40:44])[0] - 2208988800
    assert abs(seconds - time.time()) < 5
    # Regression: chronyd's Unix command socket created /run/chrony outside the run directory.
    with netns.enter(unit_net.ns("svc")):
        unix_sockets = Path("/proc/thread-self/net/unix").read_text()
    assert "chronyd.sock" not in unix_sockets
