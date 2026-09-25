"""Offline checks of the MS/TP bench-test helpers in hil/tests/mstp/mstp_bus.py.

The host MS/TP node runs against a fake serial port that echoes what is written, like the
FTDI USB-RS485 adapter; BusView runs on a hand-made trace.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from hilrig import la, mstp
from hilrig.mstp import FrameType

BAUD = 38400


@pytest.fixture(scope="module")
def bus() -> Iterator[ModuleType]:
    path = Path(__file__).resolve().parents[1] / "mstp" / "mstp_bus.py"
    spec = importlib.util.spec_from_file_location("mstp_bus_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules[spec.name]


class EchoPort:
    """pyserial stand-in: ``incoming`` is what the bus delivers; writes are echoed back."""

    def __init__(self) -> None:
        self.incoming = bytearray()
        self.written = bytearray()

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def read(self, size: int) -> bytes:
        if not self.incoming:
            time.sleep(0.001)  # the real port's 1 ms timeout
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data: bytes) -> None:
        self.written += data
        self.incoming += data

    def flush(self) -> None:
        pass


def sent_frames(port: EchoPort) -> list[mstp.Frame]:
    return mstp.parse_frames(mstp.to_octets(bytes(port.written), 0.0, BAUD), BAUD)


def test_host_node_answers_polls_and_returns_the_token(bus: ModuleType) -> None:
    port = EchoPort()
    node = bus.HostNode(port, BAUD)
    held: list[Any] = []

    def use_token(n: Any) -> None:  # a HostNode of the module under test
        held.append(n)
        n.send(mstp.build_frame(FrameType.TEST_REQUEST, bus.DUT_MAC, n.mac, b"x"))

    port.incoming += mstp.build_frame(FrameType.POLL_FOR_MASTER, bus.HOST_MAC, bus.DUT_MAC)
    port.incoming += mstp.build_frame(FrameType.POLL_FOR_MASTER, 9, bus.DUT_MAC)  # not for us
    port.incoming += mstp.build_frame(FrameType.TOKEN, bus.HOST_MAC, bus.DUT_MAC)
    node.serve(0.05, on_token=use_token)
    frames = sent_frames(port)
    assert [(f.ftype, f.dst, f.src) for f in frames] == [
        (FrameType.REPLY_TO_POLL_FOR_MASTER, bus.DUT_MAC, bus.HOST_MAC),
        (FrameType.TEST_REQUEST, bus.DUT_MAC, bus.HOST_MAC),  # sent while holding the token
        (FrameType.TOKEN, bus.DUT_MAC, bus.HOST_MAC),
    ]
    assert len(held) == 1  # the echoes of its own frames did not trigger anything


def test_host_node_listen_and_keep_bus_busy(bus: ModuleType) -> None:
    port = EchoPort()
    node = bus.HostNode(port, BAUD)
    port.incoming += mstp.build_frame(FrameType.POLL_FOR_MASTER, bus.HOST_MAC, bus.DUT_MAC)
    heard = node.listen(0.02)
    assert [f.ftype for f in heard] == [
        FrameType.POLL_FOR_MASTER
    ] and not port.written  # listen never answers
    node.keep_bus_busy(0.25, period_s=0.1)
    tokens = sent_frames(port)
    assert len(tokens) == 3 and {(f.ftype, f.dst) for f in tokens} == {(FrameType.TOKEN, bus.ABSENT_MAC)}


def uart_toggles(raw: bytes, t0: float) -> list[float]:
    bits = [b for octet in raw for b in [0] + [(octet >> j) & 1 for j in range(8)] + [1]] + [1]
    toggles, level = [], 1
    for i, b in enumerate(bits):
        if b != level:
            toggles.append(t0 + i / BAUD)
            level = b
    return toggles


def test_bus_view_splits_dut_and_other_frames(bus: ModuleType) -> None:
    token = mstp.build_frame(FrameType.TOKEN, bus.DUT_MAC, bus.HOST_MAC)
    reply = mstp.build_frame(FrameType.POLL_FOR_MASTER, 6, bus.DUT_MAC)
    t_reply = 0.005
    de_rise, de_fall = t_reply - 10e-6, t_reply + len(reply) * 10 / BAUD + 20e-6
    trace = la.Trace(1_000_000_000, 0.02, {
        "de": la.Signal(0, np.array([de_rise, de_fall])),
        "tx": la.Signal(1, np.array(uart_toggles(reply, t_reply))),
        "bus": la.Signal(1, np.array(uart_toggles(token, 0.001) + uart_toggles(reply, t_reply))),
    })  # fmt: skip
    view = bus.BusView(trace, BAUD)
    assert [f.raw for f in view.tx_frames] == [reply] and [f.raw for f in view.rx_frames] == [token]
    assert view.de == [(de_rise, de_fall)] and view.limits.baud == BAUD
    assert mstp.usage_delay(view.rx_frames, view.tx_frames, bus.DUT_MAC, view.limits).ok
