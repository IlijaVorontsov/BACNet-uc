"""Shared pieces of the MS/TP bench-bus tests (catalogue MSTP-*): node plan, the host's MS/TP
node and the decoded view of one logic-analyzer capture.

Node plan (v1 design 4.1): DUT MAC 5, host MAC 7, Max_Master 10. LA channels (bench.yml
``la.channels``): ``de`` = DUT RS-485 DE, ``tx`` = DUT UART TX (transceiver DI), ``bus`` = RO
of the listen-only sniffer, which sees every node including the DUT.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import serial

from hilrig import mstp
from hilrig.la import Trace
from hilrig.mstp import FrameType

DUT_MAC = 5
HOST_MAC = 7
ABSENT_MAC = 8  # no node: frames to it keep the bus busy without giving anyone the token
LA_CHANNELS = ("de", "tx", "bus")


@dataclass
class BusView:
    """One LA capture of the MS/TP bus, decoded for the Clause 9 checks in hilrig.mstp."""

    trace: Trace
    baud: int
    limits: mstp.Limits = field(init=False)
    de: list[tuple[float, float]] = field(init=False)
    tx_octets: list[mstp.Octet] = field(init=False)  # DUT UART TX
    bus_octets: list[mstp.Octet] = field(init=False)  # everything on the wire
    tx_frames: list[mstp.Frame] = field(init=False)  # frames the DUT sent (from its TX line)
    rx_frames: list[mstp.Frame] = field(init=False)  # frames other nodes sent (from the sniffer)

    def __post_init__(self) -> None:
        end = self.trace.duration_s
        self.limits = mstp.Limits(self.baud)
        self.de = self.trace["de"].intervals()
        self.tx_octets = self.trace.uart("tx", self.baud)
        self.bus_octets = self.trace.uart("bus", self.baud)
        self.tx_frames = mstp.parse_frames(self.tx_octets, self.baud, until=end)
        bus = mstp.parse_frames(self.bus_octets, self.baud, until=end)
        self.rx_frames = mstp.split_by_de(bus, self.de)[1]


class HostNode:
    """The host's RS-485 adapter as MS/TP master ``mac``.

    It answers Poll-For-Master and hands a received token straight back, which is enough for
    the DUT to form a two-master ring with it. Host timing is millisecond-grade and only has to
    beat the DUT's Tusage_timeout; every timing assertion comes from the logic analyzer. Frames
    the adapter echoes back (its receiver stays on while sending) are recognised by source.
    """

    def __init__(self, port: serial.Serial, baud: int, mac: int = HOST_MAC) -> None:
        self.port, self.mac = port, mac
        self.octet_s = mstp.OCTET_BITS / baud
        # USB delivers octets in bursts; only the longest legal abort time avoids false timeouts
        self.parser = mstp.FrameParser(baud, frame_abort_s=mstp.FRAME_ABORT_MAX_S)

    def send(self, frame: bytes) -> None:
        self.port.write(frame)
        self.port.flush()

    def listen(self, duration_s: float) -> list[mstp.Frame]:
        """Frames from other nodes received during ``duration_s``, without answering them."""
        frames: list[mstp.Frame] = []
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            frames += self._receive()
        return frames

    def serve(self, duration_s: float, on_token: Callable[[HostNode], None] | None = None) -> None:
        """Take part in the ring for ``duration_s``; ``on_token`` runs while the host holds it."""
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            for frame in self._receive():
                src = frame.src  # never None in a valid frame
                if not frame.ok or frame.dst != self.mac or src is None:
                    continue
                if frame.ftype == FrameType.POLL_FOR_MASTER:
                    self.send(mstp.build_frame(FrameType.REPLY_TO_POLL_FOR_MASTER, src, self.mac))
                elif frame.ftype == FrameType.TOKEN:
                    if on_token is not None:
                        on_token(self)
                    self.send(mstp.build_frame(FrameType.TOKEN, src, self.mac))

    def keep_bus_busy(self, duration_s: float, period_s: float = 0.1) -> None:
        """Send a Token to an absent station every ``period_s``: other masters stay IDLE."""
        end = time.monotonic() + duration_s
        while time.monotonic() < end:
            self.send(mstp.build_frame(FrameType.TOKEN, ABSENT_MAC, self.mac))
            self.listen(period_s)

    def _receive(self) -> list[mstp.Frame]:
        data = self.port.read(self.port.in_waiting or 1)
        now = time.monotonic()
        octets = [mstp.Octet(now - (len(data) - i) * self.octet_s, b) for i, b in enumerate(data)]
        frames = [f for f in map(self.parser.feed, octets) if f is not None]
        return [f for f in frames if f.src != self.mac]


CaptureBus = Callable[[float, Callable[[], None]], BusView]
"""The ``capture_bus`` fixture: record while an action runs, return the decoded capture."""
