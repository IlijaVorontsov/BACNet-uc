"""Fixtures for the MQTT tests: throwaway mosquitto brokers (plain and mutual
TLS) on ephemeral ports, a recording driver context and simulated devices."""

from __future__ import annotations

import asyncio
import getpass
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from uc_hub.core.driver import DriverContext
from uc_hub.core.types import DeviceRecord, ProtocolName, Reading
from uc_hub.drivers.mqtt import MqttDriver
from uc_hub.sim.mqtt_device import SimJsonSensor, SimMqttTlsDevice

MOSQUITTO = os.environ.get("MOSQUITTO") or shutil.which("mosquitto") or "/usr/sbin/mosquitto"
OPENSSL = shutil.which("openssl")
SITE = "hq"


@dataclass(frozen=True)
class Pki:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    other_ca: Path


class Broker:
    """One mosquitto process. ``stop()`` then ``start()`` reuses the port, so
    clients can reconnect to the "same" broker."""

    host = "127.0.0.1"

    def __init__(self, workdir: Path, pki: Pki | None = None) -> None:
        self.workdir = workdir
        self.pki = pki
        self.port = 0
        self.log = workdir / "mosquitto.log"
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        fixed = self.port != 0
        for _ in range(1 if fixed else 5):
            if not fixed:
                self.port = _free_port()
            offset = self.log.stat().st_size if self.log.exists() else 0
            conf = self._write_conf()
            with open(self.log, "ab") as log:
                self._proc = subprocess.Popen(
                    [MOSQUITTO, "-c", str(conf)], stdout=log, stderr=subprocess.STDOUT)
            if self._wait_ready(offset):
                return
            self.stop()
        raise RuntimeError(f"mosquitto did not start:\n{self.log.read_text()[-2000:]}")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)

    def settings(self, **overrides: Any) -> dict[str, Any]:
        """Driver settings for this broker, with short timeouts for tests."""
        out: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "client_id": f"hub-{secrets.token_hex(3)}",
            "timeout_s": 3.0,
            "keepalive_s": 10,
            "reconnect_min_s": 0.05,
            "reconnect_max_s": 0.3,
            "command_timeout_s": 1.0,
        }
        if self.pki is not None:
            out["host"] = "localhost"
            out["tls"] = {"ca": str(self.pki.ca), "cert": str(self.pki.client_cert),
                          "key": str(self.pki.client_key)}
        out.update(overrides)
        return out

    def _write_conf(self) -> Path:
        lines = [
            f"listener {self.port} {self.host}",
            "allow_anonymous true",
            "persistence false",
            "log_dest stderr",
            "log_type error",
            "log_type warning",
            "log_type notice",
            "log_type information",
            # Keep the current user: as root, mosquitto would otherwise switch
            # to "mosquitto" before reading the (private) key files.
            f"user {getpass.getuser()}",
        ]
        if self.pki is not None:
            lines += [
                f"cafile {self.pki.ca}",
                f"certfile {self.pki.server_cert}",
                f"keyfile {self.pki.server_key}",
                "require_certificate true",
            ]
        conf = self.workdir / "mosquitto.conf"
        conf.write_text("\n".join(lines) + "\n")
        return conf

    def _wait_ready(self, log_offset: int) -> bool:
        assert self._proc is not None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                return False
            with open(self.log, "rb") as f:
                f.seek(log_offset)
                running = b" running" in f.read()
            if running:
                try:
                    with socket.create_connection((self.host, self.port), timeout=0.5):
                        return True
                except OSError:
                    pass
            time.sleep(0.01)
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _openssl(*args: str, cwd: Path) -> None:
    assert OPENSSL is not None
    subprocess.run([OPENSSL, *args], cwd=cwd, check=True, capture_output=True, timeout=30)


def _make_ca(d: Path, name: str) -> tuple[Path, Path]:
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{name}.key", cwd=d)
    _openssl("req", "-x509", "-new", "-key", f"{name}.key", "-sha256", "-days", "2",
             "-subj", f"/CN={name}", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-out", f"{name}.crt", cwd=d)
    return d / f"{name}.crt", d / f"{name}.key"


def _make_cert(d: Path, name: str, ca: str, cn: str, ext: list[str]) -> tuple[Path, Path]:
    _openssl("ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{name}.key", cwd=d)
    _openssl("req", "-new", "-key", f"{name}.key", "-subj", f"/CN={cn}", "-out", f"{name}.csr",
             cwd=d)
    (d / f"{name}.ext").write_text("\n".join(ext) + "\n")
    _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt", "-CAkey", f"{ca}.key",
             "-CAcreateserial", "-days", "2", "-sha256", "-out", f"{name}.crt",
             "-extfile", f"{name}.ext", cwd=d)
    return d / f"{name}.crt", d / f"{name}.key"


@pytest.fixture(scope="session")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    """A throwaway CA with a broker certificate for ``localhost`` only (no IP
    address, so connecting to 127.0.0.1 fails the host name check), a client
    certificate, and an unrelated second CA."""
    if OPENSSL is None:
        pytest.skip("openssl not found")
    d = tmp_path_factory.mktemp("pki")
    ca, _ = _make_ca(d, "ca")
    other_ca, _ = _make_ca(d, "other-ca")
    server_cert, server_key = _make_cert(d, "server", "ca", "localhost", [
        "basicConstraints=CA:FALSE", "keyUsage=critical,digitalSignature",
        "extendedKeyUsage=serverAuth", "subjectAltName=DNS:localhost"])
    client_cert, client_key = _make_cert(d, "client", "ca", "uc-hub-test", [
        "basicConstraints=CA:FALSE", "keyUsage=critical,digitalSignature",
        "extendedKeyUsage=clientAuth"])
    return Pki(ca, server_cert, server_key, client_cert, client_key, other_ca)


def _need_mosquitto() -> None:
    if not (os.path.isfile(MOSQUITTO) and os.access(MOSQUITTO, os.X_OK)):
        pytest.skip(f"mosquitto binary not found ({MOSQUITTO})")


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    _need_mosquitto()
    b = Broker(tmp_path)
    b.start()
    yield b
    b.stop()


@pytest.fixture
def tls_broker(tmp_path: Path, pki: Pki) -> Iterator[Broker]:
    _need_mosquitto()
    b = Broker(tmp_path, pki)
    b.start()
    yield b
    b.stop()


@dataclass
class Recorder:
    """The driver context callbacks, recorded."""

    readings: list[Reading] = field(default_factory=list)
    online: list[tuple[str, bool]] = field(default_factory=list)

    def publish(self, reading: Reading) -> None:
        self.readings.append(reading)

    def set_online(self, device: str, online: bool) -> None:
        self.online.append((device, online))

    def last(self, point: str) -> Reading | None:
        for r in reversed(self.readings):
            if str(r.ref) == point:
                return r
        return None

    def is_online(self, device: str) -> bool | None:
        for name, state in reversed(self.online):
            if name == device:
                return state
        return None


async def eventually(check: Callable[[], Any], timeout_s: float = 5.0, what: str = "") -> Any:
    """Poll ``check`` until it returns something truthy."""
    deadline = time.monotonic() + timeout_s
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or check}")
        await asyncio.sleep(0.02)


def record(name: str, address: str = "") -> DeviceRecord:
    return DeviceRecord(site=SITE, name=name, protocol=ProtocolName.MQTT, address=address)


DriverFactory = Callable[..., Awaitable[tuple[MqttDriver, Recorder]]]


@pytest.fixture
async def make_driver() -> AsyncIterator[DriverFactory]:
    """``await make_driver(settings, [(record, spec), ...], start=True)``."""
    drivers: list[MqttDriver] = []

    async def make(
        settings: dict[str, Any],
        devices: Iterable[tuple[DeviceRecord, dict[str, Any]]] = (),
        start: bool = True,
    ) -> tuple[MqttDriver, Recorder]:
        rec = Recorder()
        driver = MqttDriver(DriverContext(
            site=SITE, publish=rec.publish, set_online=rec.set_online, settings=settings))
        drivers.append(driver)
        for dev_record, spec in devices:
            await driver.add_device(dev_record, spec)
        if start:
            await driver.start()
            assert await driver.wait_connected(5.0), "driver did not connect"
        return driver, rec

    yield make
    for driver in drivers:
        await driver.stop()


@pytest.fixture
async def sims() -> AsyncIterator[list[SimMqttTlsDevice | SimJsonSensor]]:
    """Append simulated devices here; they are stopped after the test."""
    devices: list[SimMqttTlsDevice | SimJsonSensor] = []
    yield devices
    await asyncio.gather(*(d.stop() for d in devices), return_exceptions=True)
