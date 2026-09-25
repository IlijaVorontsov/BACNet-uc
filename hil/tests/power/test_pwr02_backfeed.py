"""PWR-02 back-feed and fail-safe (rig; hardware).

Catalogue PWR-02: automated: with the DUT off after stim safe, v3v3 <= 300 mV and v5 <= 300 mV;
stim dac ai0 1000 while off returns ERR -1. With the stimulus held at its reset vector
(OpenOCD 'reset halt' on its probe) for 60 s, then with its hub port off for 60 s: the DUT
keeps answering (ping 0 % loss plus BACnet RP or MQTT telemetry continuity) and its uptime
(SMP uc_node info or telemetry) never goes back. v5 >= 4.75 V before and after. Commissioning
only (DMM, recorded in bench.yml): V_ON > 1.2 V released and < 0.3 V cut; NRST >= 3.0 V with
the stimulus idle.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable

import pytest

from hilrig import flash as flash_mod
from hilrig import netns as nsmod
from hilrig.bacnet import Bacnet
from hilrig.bench import Bench
from hilrig.mqtt import Device
from hilrig.netns import Topology
from hilrig.release import DutImage
from hilrig.services import RigServices
from hilrig.smp import Smp
from hilrig.stim import EPERM, Stim, StimError

pytestmark = [pytest.mark.rig, pytest.mark.hil_only]

OFF_MV = 300.0
V5_MIN_MV = 4750.0
HOLD_S = 60.0


def test_pwr02_dut_off_rails_and_analog_refusal(stim: Stim, services: RigServices, bench: Bench) -> None:
    stim.safe()
    stim.power("off")
    try:
        time.sleep(0.5)
        v3v3, v5 = stim.adc("v3v3", 16).mv, stim.adc("v5", 16).mv
        assert v3v3 <= OFF_MV and v5 <= OFF_MV, f"DUT off: v3v3 {v3v3:.0f} mV, v5 {v5:.0f} mV (back-feed?)"
        with pytest.raises(StimError) as err:
            stim.dac("ai0", 1000)
        assert err.value.errno == EPERM
    finally:
        mark = services.dnsmasq.mark()
        stim.power("on")
    if bench.dut.mac:
        services.dnsmasq.wait_for("DHCPACK", bench.dut.mac, mark, timeout=60)  # leave the DUT online


class Continuity:
    """Pings the DUT and probes its app every few seconds; records uptimes and failures."""

    def __init__(self, check: Callable[[], float], ns: str, ip: str) -> None:
        self.check, self.ns, self.ip = check, ns, ip
        self.failures: list[str] = []
        self.uptimes: list[float] = []
        self.pings = nsmod.PingResult(0, 0, ())
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._ping), threading.Thread(target=self._probe)]

    def _ping(self) -> None:
        sent = received = 0
        while not self._stop.is_set():
            result = nsmod.ping(self.ns, self.ip, count=1, timeout=1.0)
            sent, received = sent + result.sent, received + result.received
            time.sleep(0.5)
        self.pings = nsmod.PingResult(sent, received, ())

    def _probe(self) -> None:
        while not self._stop.wait(5.0):
            try:
                self.uptimes.append(self.check())
            except Exception as e:  # any failed probe is a continuity failure
                self.failures.append(f"{time.strftime('%H:%M:%S')} {e}")

    def __enter__(self) -> Continuity:
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=30)

    def assert_ok(self, phase: str) -> None:
        assert self.pings.sent and self.pings.loss == 0.0, (
            f"{phase}: ping {self.pings.received}/{self.pings.sent}"
        )
        assert not self.failures, f"{phase}: probes failed: {self.failures}"
        assert self.uptimes == sorted(self.uptimes), f"{phase}: uptime went back (reboot): {self.uptimes}"


def uhubctl(location: str, port: int, action: str) -> None:
    subprocess.run(
        ["uhubctl", "-l", location, "-p", str(port), "-a", action],
        capture_output=True,
        check=True,
        timeout=30,
    )


@pytest.mark.slow
@pytest.mark.timeout(HOLD_S * 2 + 240)
@pytest.mark.parametrize("app_image", ["bacnet", "mqtt"], indirect=True)
def test_pwr02_dut_survives_a_halted_and_an_unpowered_stimulus(
    app_image: DutImage, request: pytest.FixtureRequest, stim: Stim, bench: Bench, netns: Topology
) -> None:
    assert bench.stim is not None
    if not bench.stim.probe or flash_mod.find_openocd() is None:
        pytest.skip("needs bench.yml stim.probe and OpenOCD to hold the stimulus in reset")
    if bench.hub is None or bench.hub.stim_port is None or shutil.which("uhubctl") is None:
        pytest.skip("needs bench.yml hub.location/stim_port and uhubctl to cut the stimulus's USB power")
    assert stim.adc("v5", 16).mv >= V5_MIN_MV
    instance = bench.dut.bacnet_instance
    if app_image.app == "bacnet":
        bacnet: Bacnet = request.getfixturevalue("bacnet")
        smp: Smp = request.getfixturevalue("smp")

        def check() -> float:
            bacnet.read(instance, "device", instance, "object-name")
            return float(smp.node_info()["uptime_s"])
    else:
        mqtt: Device = request.getfixturevalue("mqtt")
        interval = app_image.publish_interval_s

        def check() -> float:
            return float(mqtt.telemetry(timeout=2 * interval + 5, since=mqtt.client.mark())["uptime_s"])

    svc = netns.ns("svc")
    # every hold ends in a finally: a stimulus left halted or unpowered would fail the rest
    # of the session as rig faults
    with Continuity(check, svc, bench.dut.ip) as halted:
        try:
            flash_mod.reset_target(bench.stim.probe, halt=True)
            time.sleep(HOLD_S)
        finally:
            flash_mod.reset_target(bench.stim.probe)  # run again: the stimulus boots into safe
    stim.reopen(connect_timeout=15)  # before the verdict, so the session keeps its link
    halted.assert_ok("stimulus halted")
    with Continuity(check, svc, bench.dut.ip) as unpowered:
        try:
            uhubctl(bench.hub.location, bench.hub.stim_port, "off")
            time.sleep(HOLD_S)
        finally:
            uhubctl(bench.hub.location, bench.hub.stim_port, "on")
    stim.reopen(connect_timeout=30)
    unpowered.assert_ok("stimulus unpowered")
    assert stim.adc("v5", 16).mv >= V5_MIN_MV


def test_pwr02_commissioning_record(bench: Bench) -> None:
    rec = bench.commissioning
    if rec is None or None in (rec.v_on_released_v, rec.v_on_cut_v, rec.nrst_idle_v):
        pytest.skip("bench.yml commissioning (v_on_released_v, v_on_cut_v, nrst_idle_v) is not recorded")
    assert rec.v_on_released_v is not None and rec.v_on_released_v > 1.2
    assert rec.v_on_cut_v is not None and rec.v_on_cut_v < 0.3
    assert rec.nrst_idle_v is not None and rec.nrst_idle_v >= 3.0
