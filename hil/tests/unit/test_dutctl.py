"""Readiness waits and the HIL.md 7.3 sequences (hilrig.dutctl), and the SIL DUT launcher
(hilrig.sildut), against the pty fake stimulus and a stand-in ``zephyr.exe``."""

from __future__ import annotations

import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from fake_stim import FakeStim
from hilrig.bench import Bench
from hilrig.console import Console
from hilrig.dutctl import DutControl, DutLink, DutTimeout
from hilrig.mqtt import Message
from hilrig.release import DutImage
from hilrig.services import DhcpEvent, Dnsmasq
from hilrig.sildut import SilDut, supported_options
from hilrig.stim import Stim

BENCH = Bench.from_dict(
    {
        "name": "unit",
        "mode": "sil",
        "dut": {
            "profile": "sil",
            "board": "native_sim/native/64",
            "bacnet_instance": 7,
            "mac": "02:00:00:00:00:01",
        },
    }
)
FAKE_EXE = """#!{python}
import sys, time
if "--help" in sys.argv:
    print("[-h] [--help] [-seed=<r_seed>] [--mac-addr=<mac>] [--device_id=<id>] [-no-rt]")
    sys.exit(0)
print("args", " ".join(sys.argv[1:]), flush=True)
print("HIL-READY ip=192.0.2.10 mac=02:48:49:4c:00:0a", flush=True)
time.sleep(60)
"""


class FakeDnsmasq:
    """mark()/wait_for() of Dnsmasq: an ACK arrives ``delay`` s after each mark."""

    def __init__(self, delay: float = 0.05) -> None:
        self.delay, self.marks = delay, 0

    def mark(self) -> int:
        self.marks += 1
        return self.marks

    def wait_for(self, kind: str, mac: str, since: int, timeout: float) -> DhcpEvent:
        if self.delay > timeout:
            raise TimeoutError(f"no {kind} for {mac} within {timeout} s")
        return DhcpEvent(0.0, kind, mac, "192.0.2.10")


class FakeDevice:
    """topic()/client.mark()/client.wait_for() of hilrig.mqtt.Device and its MqttClient."""

    def __init__(self, online: bool = True, retained_only: bool = False) -> None:
        self.online, self.retained_only, self.client = online, retained_only, self
        self.waited: list[int] = []

    def mark(self) -> int:
        return 42

    def topic(self, kind: str) -> str:
        return f"bacnet-uc/z0/{kind}"

    def wait_for(self, topic: str, predicate: Any, timeout: float, since: int) -> Message:
        """Offers the retained 'online' first, then (unless retained_only) a live one."""
        self.waited.append(since)
        for retain in (True, False)[: 1 if self.retained_only else 2]:
            message = Message(topic, b"online", retain, 1, 0.0)
            if self.online and predicate(message):
                return message
        raise TimeoutError("no status")


def link(image: DutImage, **parts: Any) -> DutLink:
    return DutLink(
        BENCH,
        image,
        dnsmasq=cast(Dnsmasq, parts.get("dnsmasq", FakeDnsmasq())),
        mqtt=cast(Any, parts.get("mqtt")),
        console=parts.get("console"),
    )


@pytest.fixture
def stim(tmp_path: Path) -> Iterator[tuple[Stim, FakeStim]]:
    with FakeStim(board="nucleo_f767zi") as fake, Stim(fake.port, timeout=0.5) as driver:
        yield driver, fake


def test_mqtt_readiness_is_a_new_online_status() -> None:
    device = FakeDevice()
    dut = link(DutImage("mqtt"), mqtt=device)
    mark = dut.mark()
    back = dut.wait_online(mark, 5)
    assert back.dhcp_s is not None and back.app_s >= 0 and device.waited == [42]
    with pytest.raises(DutTimeout, match="'online' status"):
        link(DutImage("mqtt"), mqtt=FakeDevice(online=False)).wait_app(mark, 0.1)
    with pytest.raises(DutTimeout, match="'online' status"):  # Regression: the retained copy counted
        link(DutImage("mqtt"), mqtt=FakeDevice(retained_only=True)).wait_app(mark, 0.1)
    with pytest.raises(DutTimeout, match="DHCP"):
        link(DutImage("mqtt"), dnsmasq=FakeDnsmasq(delay=10), mqtt=device).wait_online(dut.mark(), 0.1)
    with pytest.raises(DutTimeout, match="no bacnet tools"):
        link(DutImage("bacnet")).wait_app(mark, 0.1)


def test_instrumented_readiness_also_waits_for_hil_ready(tmp_path: Path) -> None:
    console = Console(tmp_path / "c.log")
    image = DutImage("mqtt", config={"CONFIG_HIL": "y"})
    dut = link(image, mqtt=FakeDevice(), console=console)
    mark = dut.mark()
    threading.Timer(0.1, console.feed, ["HIL-READY ip=192.0.2.10 mac=02:00:00:00:00:01\n"]).start()
    assert dut.wait_app(mark, 2) >= 0.1
    with pytest.raises(DutTimeout, match="HIL-READY"):
        dut.wait_app(dut.mark(), 0.2)


def test_reset_counts_the_nrst_pulse(stim: tuple[Stim, FakeStim]) -> None:
    driver, fake = stim
    control = DutControl(link(DutImage("mqtt"), mqtt=FakeDevice()), stim=driver)
    report = control.reset()
    assert (report.rstmon_before, report.rstmon_after) == (0, 1) and report.back is not None
    assert "stim reset 10" in fake.commands


def test_power_cycle_samples_the_rail_while_off(stim: tuple[Stim, FakeStim], tmp_path: Path) -> None:
    driver, fake = stim
    control = DutControl(link(DutImage("mqtt"), mqtt=FakeDevice()), stim=driver)
    report = control.cycle(300)
    assert len(report.v3v3) >= 3 and all(mv == 0.0 for _, mv in report.v3v3)  # the fake rail is off
    assert report.on > report.off and report.back is not None
    order = [c for c in fake.commands if c.startswith(("stim safe", "stim power"))]
    assert order[:3] == ["stim safe", "stim power off", "stim power on"]
    with pytest.raises(ValueError, match="exactly one"):
        DutControl(control.link)


def fake_exe(tmp_path: Path) -> Path:
    exe = tmp_path / "zephyr" / "zephyr.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text(FAKE_EXE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_sil_dut_passes_only_supported_options_and_restarts(tmp_path: Path) -> None:
    exe = fake_exe(tmp_path)
    assert {"mac-addr", "device_id", "seed", "no-rt", "help"} <= supported_options(exe)
    with SilDut(exe, tmp_path / "run", netns=None) as dut:
        line, _ = dut.console.wait_for(r"^args ", 5)
        assert "--mac-addr=02:48:49:4c:00:0a" in line.text and "--device_id=1212763185" in line.text
        assert "--flash=" not in line.text  # this build has no flash simulator
        dut.console.wait_for("HIL-READY", 5)
        first = dut.started_at
        control = DutControl(link(DutImage("mqtt"), mqtt=FakeDevice()), sil=dut)
        report = control.reset()
        assert report.released >= first and dut.starts == 2 and dut.running()
        assert report.back is not None and report.back.dhcp_s is None  # SIL: no DHCP wait
        cycle = control.cycle(100)
        assert dut.starts == 3 and cycle.v3v3 == []
    assert not dut.running()


FAKE_BOOTING_EXE = """#!{python}
import os, sys
if "--help" in sys.argv:
    print("[--help]")
    sys.exit(0)
print("*** Booting Zephyr OS build fake ***", flush=True)
while data := os.read(0, 64):
    if b"reboot" in data:  # CONFIG_NATIVE_SIM_REBOOT: the image re-executes in place
        print("*** Booting Zephyr OS build fake ***", flush=True)
    if b"exit" in data:  # without it native_sim exits on sys_reboot
        sys.exit(0)
"""


def test_sil_dut_generation_counts_every_boot(tmp_path: Path) -> None:
    """Regression: a BACnet test after a SIL DUT (re)start sent its Who-Is before the DUT had
    its lease (BIP-01 found no I-Am). app_image now waits again whenever the generation (boots
    seen) changed, including in-place reboots and the restart after an exit of its own."""
    exe = tmp_path / "zephyr.exe"
    exe.write_text(FAKE_BOOTING_EXE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    with SilDut(exe, tmp_path / "run", netns=None) as dut:
        assert dut.generation == 1
        dut.console.send("reboot")
        deadline = time.monotonic() + 5
        while dut.generation != 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert dut.generation == 2 and dut.starts == 1
        dut.console.send("exit")
        while dut.generation != 3 and time.monotonic() < deadline + 5:
            time.sleep(0.05)
        assert dut.generation == 3 and dut.starts == 2 and dut.exits == 1
        dut.restart()
        assert dut.generation == 4
