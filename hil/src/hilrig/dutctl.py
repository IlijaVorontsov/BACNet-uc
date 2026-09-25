"""The DUT's way back onto the network, and the reset and power sequences of HIL.md 7.3.

:class:`DutLink` knows how to tell that the DUT is (back) online for the image under test:

- DHCP: dnsmasq logs a DHCPACK for the bench MAC (a DHCP lease);
- BACnet image: an I-Am from the bench instance answers a Who-Is;
- MQTT image: the DUT publishes its ``online`` status again, which it does only after
  CONNACK 0 and a granted SUBACK (apps/mqtt_tls). Only a live publish counts: the retained
  copy a new subscriber gets has the retain flag set (MQTT 3.1.1, 3.3.1.3);
- instrumented image: additionally ``HIL-READY ip=<ip> mac=<mac>`` on the console.

All of these are functional deadlines on the host clock (HIL.md 3.1, "Timing"); wire and
signal timing come from the capture, the logic analyzer or the stimulus.

:class:`DutControl` runs the sequences: ``reset()`` is ``stim reset`` (NRST pulse; the PHY
resets too) and ``cycle()`` is the E5V power cycle with the 3V3 rail watched while off. In
SIL both restart the native_sim process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hilrig.bench import Bench
from hilrig.flash import FlashResult, GuardResult
from hilrig.release import DutImage
from hilrig.sildut import BOOT_BANNER

if TYPE_CHECKING:
    from hilrig.bacnet import Bacnet
    from hilrig.console import Console
    from hilrig.mqtt import Device
    from hilrig.services import Dnsmasq
    from hilrig.sildut import SilDut
    from hilrig.stim import Stim

V3V3_OFF_MV = 300.0
HIL_READY = r"HIL-READY ip=(\S+) mac=(\S+)"


@dataclass
class SessionStart:
    """What the session start did: guard, flash (inside a capture), boot to network (SYS-01)."""

    guard: GuardResult | None = None
    flash: FlashResult | None = None
    flash_end: float | None = None  # time.time() when the flash returned: the reset release
    capture: Path | None = None  # pcapng of the flash and the boot
    console_mark: int = 0
    online_s: float | None = None  # seconds from flash end to I-Am / 'online' (functional)
    notes: list[str] = field(default_factory=list)


class DutTimeout(TimeoutError):
    """The DUT did not come back within the deadline (the message says which step)."""


@dataclass(frozen=True)
class Mark:
    """Positions in the logs the readiness checks read, and when they were taken."""

    t: float  # time.time(), the capture's clock
    mono: float
    dhcp: int
    console: int
    mqtt: int


@dataclass(frozen=True)
class Online:
    """Seconds from the mark to the DHCP lease (None: not checked) and to the app answering."""

    dhcp_s: float | None
    app_s: float


class DutLink:
    """Readiness checks for the image under test (every part is optional)."""

    def __init__(
        self,
        bench: Bench,
        image: DutImage | None,
        *,
        dnsmasq: Dnsmasq | None = None,
        bacnet: Bacnet | None = None,
        mqtt: Device | None = None,
        console: Console | None = None,
    ) -> None:
        self.bench, self.image = bench, image
        self.dnsmasq, self.bacnet, self.mqtt, self.console = dnsmasq, bacnet, mqtt, console

    @property
    def app(self) -> str | None:
        return self.image.app if self.image else None

    def mark(self) -> Mark:
        """Remember where every log is now."""
        return Mark(
            time.time(),
            time.monotonic(),
            self.dnsmasq.mark() if self.dnsmasq else 0,
            self.console.mark() if self.console else 0,
            self.mqtt.client.mark() if self.mqtt else 0,
        )

    def wait_dhcp(self, since: Mark, timeout: float) -> float:
        """Wait for a DHCPACK to the bench MAC after ``since``; seconds from the mark."""
        if self.dnsmasq is None or self.bench.dut.mac is None:
            raise DutTimeout("no dnsmasq or no dut.mac: the DHCP lease cannot be observed")
        try:
            self.dnsmasq.wait_for("DHCPACK", self.bench.dut.mac, since.dhcp, timeout)
        except TimeoutError as e:
            raise DutTimeout(f"DHCP: {e}") from None
        return time.monotonic() - since.mono

    def answers(self) -> bool:
        """One BACnet probe: does the DUT answer a Who-Is for its instance now?"""
        assert self.bacnet is not None
        instance = self.bench.dut.bacnet_instance
        return instance in [d.instance for d in self.bacnet.whois(instance, instance, wait_ms=500)]

    def wait_app(self, since: Mark, timeout: float) -> float:
        """Wait until the app answers (I-Am, or a new MQTT ``online``); seconds from the mark."""
        deadline = since.mono + timeout
        if self.app == "bacnet":
            if self.bacnet is None:
                raise DutTimeout("no bacnet tools: I-Am cannot be observed")
            while not self.answers():
                if time.monotonic() > deadline:
                    raise DutTimeout(
                        f"no I-Am from instance {self.bench.dut.bacnet_instance} within {timeout} s"
                    )
        elif self.app == "mqtt":
            if self.mqtt is None:
                raise DutTimeout("no MQTT observer: CONNACK cannot be observed")
            try:  # a live publish: the retained copy replayed at subscription has retain=1
                self.mqtt.client.wait_for(
                    self.mqtt.topic("status"),
                    lambda m: m.payload == b"online" and not m.retain,
                    max(0.1, deadline - time.monotonic()),
                    since.mqtt,
                )
            except TimeoutError:
                raise DutTimeout(f"no new 'online' status (CONNACK + SUBACK) within {timeout} s") from None
        else:
            raise DutTimeout(f"no readiness check for image {self.app!r}")
        if self.image is not None and self.image.instrumented and self.console is not None:
            try:
                self.console.wait_for(HIL_READY, max(0.1, deadline - time.monotonic()), since.console)
            except TimeoutError:
                raise DutTimeout(f"no HIL-READY line within {timeout} s") from None
        return time.monotonic() - since.mono

    def wait_online(self, since: Mark, timeout: float, *, dhcp: bool = True) -> Online:
        """Wait for the DHCP lease (if ``dhcp``) and the app, both within ``timeout`` of the mark."""
        dhcp_s = self.wait_dhcp(since, timeout) if dhcp else None
        return Online(dhcp_s, self.wait_app(since, timeout))


@dataclass
class ResetReport:
    """One reset: release time (``time.time()``), rstmon count before/after, back online."""

    released: float
    rstmon_before: int | None = None
    rstmon_after: int | None = None
    back: Online | None = None


@dataclass
class PowerCycleReport:
    """One power cycle: off/on times (``time.time()``), 3V3 samples (s after off, mV), back."""

    off: float
    on: float = 0.0
    v3v3: list[tuple[float, float]] = field(default_factory=list)
    back: Online | None = None
    back_s: float | None = None  # seconds from power-on until the app answered


class DutControl:
    """Reset and power sequences through the stimulus (hardware) or a process restart (SIL)."""

    def __init__(
        self,
        link: DutLink,
        *,
        stim: Stim | None = None,
        sil: SilDut | None = None,
        console: Console | None = None,
    ) -> None:
        if (stim is None) == (sil is None):
            raise ValueError("DutControl needs exactly one of stim (hardware) and sil (native_sim)")
        self.link, self.stim, self.sil, self.console = link, stim, sil, console

    def reset(self, hold_ms: int = 10, *, wait: bool = True, timeout: float = 20.0) -> ResetReport:
        """``reset()`` of HIL.md 7.3: NRST pulse, rstmon confirm, link + DHCP + I-Am/CONNACK."""
        mark = self.link.mark()
        if self.sil is not None:
            report = ResetReport(self.sil.restart())
        else:
            assert self.stim is not None
            before = self.stim.rstmon().n
            self.stim.reset(hold_ms)
            report = ResetReport(time.time(), before, self.stim.rstmon().n)
        if wait:
            report.back = self.link.wait_online(mark, timeout, dhcp=self.sil is None)
        return report

    def reboot_mark(self) -> tuple[int, int]:
        """Where the reboot evidence is now: rstmon count (hardware), or starts and console (SIL)."""
        if self.sil is not None:
            return self.sil.starts, self.sil.console.mark()
        assert self.stim is not None
        return self.stim.rstmon().n, 0

    def rebooted_since(self, mark: tuple[int, int]) -> bool:
        """Tell whether the DUT reset since :meth:`reboot_mark` without the rig causing it.

        Hardware: rstmon counts every NRST pulse, internal resets included. SIL: a restart of
        the process, or a Zephyr boot banner (a reboot that re-executes the image in place).
        """
        if self.sil is not None:
            banners = self.sil.console.lines(mark[1], BOOT_BANNER)
            return self.sil.starts != mark[0] or not self.sil.running() or bool(banners)
        assert self.stim is not None
        return self.stim.rstmon().n != mark[0]

    def ensure_online(self, timeout: float = 30.0) -> None:
        """Make sure the DUT is online now; reset it if it does not show it within a while.

        A test after a TLS or network fault test may find the DUT in its reconnect back-off
        (up to 60 s); a reset is quicker and gives every test the same start.
        """
        link = self.link
        if link.app == "bacnet" and link.bacnet is not None and link.answers():
            return
        if link.app == "mqtt" and link.mqtt is not None and link.image is not None:
            try:
                link.mqtt.telemetry(
                    timeout=link.image.publish_interval_s + 5.0, since=link.mqtt.client.mark()
                )
                return
            except TimeoutError:
                pass
        self.reset(timeout=timeout)

    def off(self) -> float:
        """DUT power off after ``stim safe`` (the console is closed first); returns the time."""
        if self.sil is not None:
            self.sil.stop()
            return time.time()
        assert self.stim is not None
        self.stim.safe()
        if self.console is not None:
            self.console.close()
        self.stim.power("off")
        return time.time()

    def on(self) -> float:
        """DUT power on and the console reopened; returns the time."""
        if self.sil is not None:
            return self.sil.restart(0.0)
        assert self.stim is not None
        self.stim.power("on")
        on = time.time()
        if self.console is not None:
            self.console.open(retry_s=10)
        return on

    def cycle(self, off_ms: int = 2000, *, wait: bool = True, timeout: float = 20.0) -> PowerCycleReport:
        """``power_cycle()`` of HIL.md 7.3, sampling v3v3 during the whole off time."""
        mark = self.link.mark()
        report = PowerCycleReport(self.off())
        start = time.monotonic()
        while (elapsed := time.monotonic() - start) < off_ms / 1000:
            if self.stim is not None:
                report.v3v3.append((elapsed, self.stim.adc("v3v3", 4).mv))
            else:
                time.sleep(0.05)
        report.on = self.on()
        on_offset = time.monotonic() - mark.mono
        if wait:
            report.back = self.link.wait_online(mark, on_offset + timeout, dhcp=self.sil is None)
            report.back_s = report.back.app_s - on_offset
        return report
