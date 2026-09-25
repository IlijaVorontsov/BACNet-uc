"""Capture on a veth pair between two throwaway namespaces (hilrig.capture)."""

from __future__ import annotations

import os
import signal
import socket
import ssl
import struct
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hilrig import capture as capture_mod
from hilrig import netns
from hilrig.capture import Capture, CaptureError, sentinel_count
from hilrig.pki import Pki
from rig_skips import needs_root, needs_tools

pytestmark = needs_tools("tshark", "editcap")
live = pytest.mark.usefixtures("veth")  # tests on the veth pair: root, ip and openssl

CAP_NS, PEER_NS = "hct-cap", "hct-peer"
CAP_IP, PEER_IP, BROADCAST = "10.99.0.1", "10.99.0.2", "10.99.0.255"
WHO_IS = bytes.fromhex("810b000801001008")  # BVLC broadcast, NPDU, Who-Is (no limits)


def ip(*args: str) -> None:
    subprocess.run(["ip", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def veth() -> Iterator[None]:
    """ca0 (10.99.0.1) in hct-cap <-> cb0 (10.99.0.2) in hct-peer."""
    for mark in (needs_root, needs_tools("ip", "openssl")):
        if mark.args[0]:
            pytest.skip(mark.kwargs["reason"])
    for ns in (CAP_NS, PEER_NS):
        subprocess.run(["ip", "netns", "del", ns], capture_output=True, check=False)
        ip("netns", "add", ns)
        ip("-n", ns, "link", "set", "lo", "up")
    ip("link", "add", "ca0", "netns", CAP_NS, "type", "veth", "peer", "name", "cb0", "netns", PEER_NS)
    for ns, dev, addr in ((CAP_NS, "ca0", CAP_IP), (PEER_NS, "cb0", PEER_IP)):
        ip("-n", ns, "addr", "add", f"{addr}/24", "dev", dev)
        ip("-n", ns, "link", "set", dev, "up")
    yield
    for ns in (CAP_NS, PEER_NS):
        ip("netns", "del", ns)
    assert not any(netns.exists(ns) for ns in (CAP_NS, PEER_NS))


def capture(tmp_path: Path, bpf: str, keylog: Path | None = None, name: str = "t") -> Capture:
    return Capture(
        "ca0",
        CAP_NS,
        bpf,
        tmp_path / f"{name}.pcapng",
        sentinel_netns=PEER_NS,
        sentinel_dst=BROADCAST,
        keylog=keylog,
    )


def peer_socket() -> socket.socket:
    with netns.enter(PEER_NS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


@live
def test_barrier_then_known_packet_decoded_and_sentinels_excluded(tmp_path: Path) -> None:
    cap = capture(tmp_path, "udp port 47808")
    with cap, peer_socket() as sock:
        assert cap.running
        with pytest.raises(CaptureError, match="stopped capture"):
            cap.rows("udp")
        sock.sendto(WHO_IS, (BROADCAST, 47808))
        sock.sendto(b"not captured", (BROADCAST, 5000))  # outside the BPF
    assert not cap.running
    assert sentinel_count(cap.path) >= 1
    rows = cap.rows("udp", "ip.src", "udp.dstport", "bvlc.function", "bacapp.unconfirmed_service")
    assert [
        (r["ip.src"], r["udp.dstport"], r["bvlc.function"], r["bacapp.unconfirmed_service"]) for r in rows
    ] == [(PEER_IP, "47808", "0x0b", "8")]
    assert rows[0].t > 1.7e9  # frame.time_epoch
    assert cap.stop() == cap.path  # idempotent


@live
def test_capture_that_never_sees_a_sentinel_fails(tmp_path: Path) -> None:
    cap = Capture(
        "nosuch0",
        CAP_NS,
        "udp",
        tmp_path / "x.pcapng",
        sentinel_netns=PEER_NS,
        sentinel_dst=BROADCAST,
        ready_timeout=3,
    )
    with pytest.raises(CaptureError, match="never saw a sentinel"):
        cap.start()
    assert not cap.running


def mqtt_connect(client_id: str) -> bytes:
    """An MQTT 3.1.1 CONNECT packet (clean session, keepalive 60)."""
    payload = len(client_id).to_bytes(2, "big") + client_id.encode()
    body = b"\x00\x04MQTT\x04\x02\x00\x3c" + payload
    return bytes([0x10, len(body)]) + body


def tls_session(pki: Pki, keylog: Path, client_id: str) -> None:
    """Send an MQTT CONNECT over TLS 1.3 from the peer to a TLS server in the capture netns."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(pki.servers["good"].cert, pki.servers["good"].key)
    with netns.enter(CAP_NS):
        listener = socket.create_server((CAP_IP, 8883))
    received: list[bytes] = []

    def serve() -> None:
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as tls:
            received.append(tls.recv(1024))

    thread = threading.Thread(target=serve)
    thread.start()
    client_ctx = ssl.create_default_context(cafile=pki.ca)
    client_ctx.keylog_filename = str(keylog)
    with netns.enter(PEER_NS):
        raw = socket.create_connection((CAP_IP, 8883), timeout=5)
    with client_ctx.wrap_socket(raw, server_hostname="broker.hil.lan") as tls:
        tls.sendall(mqtt_connect(client_id))
        thread.join(timeout=5)
    listener.close()
    assert received == [mqtt_connect(client_id)]


@live
def test_keylog_is_injected_so_the_pcap_decrypts_on_its_own(tmp_path: Path) -> None:
    pki = Pki.generate(tmp_path / "pki")
    keylog = tmp_path / "keys.log"
    with capture(tmp_path, "tcp port 8883", keylog=keylog, name="with-keys") as cap:
        tls_session(pki, keylog, "hil-decrypted")
    assert "CLIENT_HANDSHAKE_TRAFFIC_SECRET" in keylog.read_text()
    # no keylog option at analysis time: the secrets travel inside the pcapng
    assert [r["mqtt.clientid"] for r in cap.rows("mqtt.msgtype == 1", "mqtt.clientid")] == ["hil-decrypted"]

    with capture(tmp_path, "tcp port 8883", name="without-keys") as plain:
        tls_session(pki, tmp_path / "other-keys.log", "hil-hidden")
    assert plain.rows("mqtt") == []
    assert len(plain.rows("tls.app_data")) >= 1  # the records are there, but opaque


def netns_pids(ns: str) -> list[int]:
    out = subprocess.run(["ip", "netns", "pids", ns], capture_output=True, text=True, check=True).stdout
    return [int(pid) for pid in out.split()]


@live
def test_wedged_tshark_is_killed_with_its_dumpcap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: only tshark got the signals, so killing a hung tshark orphaned dumpcap."""
    monkeypatch.setattr(capture_mod, "STOP_TIMEOUT_S", 1.0)
    cap = capture(tmp_path, "udp port 47808").start()
    try:
        (tshark,) = [p for p in netns_pids(CAP_NS) if Path(f"/proc/{p}/comm").read_text().strip() == "tshark"]
        os.kill(tshark, signal.SIGSTOP)  # hung: it can no longer pass SIGINT on to dumpcap
    finally:
        cap.stop()
    deadline = time.monotonic() + 5
    while netns_pids(CAP_NS) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert netns_pids(CAP_NS) == []


def syslog_pcap(path: Path, message: bytes) -> Path:
    """Write a pcap holding one syslog datagram (UDP 514) that carries ``message``."""
    payload = b"<13>" + message
    udp = struct.pack("!HHHH", 5000, 514, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, bytes(4), bytes(4))
    frame = bytes(6) + bytes(6) + b"\x08\x00" + ip + udp
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)  # LINKTYPE_ETHERNET
    path.write_bytes(header + struct.pack("<IIII", 1, 0, len(frame), len(frame)) + frame)
    return path


def test_rows_keep_values_that_contain_line_separator_characters(tmp_path: Path) -> None:
    """Regression: str.splitlines() cut rows at characters tshark prints raw (\x1c, U+2028)."""
    text = "a\x1cb\u2028c\x85d"
    cap = Capture("unused0", None, "", syslog_pcap(tmp_path / "syslog.pcap", text.encode()))
    rows = cap.rows("syslog", "syslog.msg", "udp.dstport")
    assert [(r["syslog.msg"], r["udp.dstport"]) for r in rows] == [(text, "514")]
