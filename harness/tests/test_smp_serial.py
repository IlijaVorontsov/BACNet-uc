# SPDX-License-Identifier: Apache-2.0
"""SMP over the shell console: framing, CRC, multi-line packets, and the
SerialTransport end to end over a pseudo terminal."""

from __future__ import annotations

import asyncio
import base64
import os
import struct
import sys

import pytest

from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.client import SmpClient
from bacnet_uc_harness.smp.codec import encode_frame
from bacnet_uc_harness.smp.transport import (
    SERIAL_HDR_FRAG,
    SERIAL_HDR_PKT,
    SERIAL_MAX_FRAME,
    LineSplitter,
    SerialFrameDecoder,
    SerialTransport,
    crc16_xmodem,
    encode_serial_packet,
)
from bacnet_uc_harness.testing import FakeNode

# --- reference implementation: port of Zephyr serial_util.c ------------------------------


def zephyr_tx_pkt(data: bytes) -> bytes:
    """Python port of mcumgr_serial_tx_pkt() (Zephyr 4.4)."""
    out = bytearray()
    crc = crc16_xmodem(data)
    # as in the C code, max_input is reduced once (first frame) and never reset
    max_input = ((SERIAL_MAX_FRAME - 3) >> 2) * 3
    src_off = 0
    first = True
    marker = SERIAL_HDR_PKT
    length = len(data)
    while src_off < length:
        out += marker
        if first:
            out += base64.b64encode(struct.pack(">H", length + 2) + data[:1])
            src_off += 1
            max_input -= 3
        to_process = min(max_input, length - src_off)
        reminder = max_input - (length - src_off)
        last = False
        if reminder in (0, 1):
            to_process -= 1
        elif reminder >= 2:
            last = True
        while to_process >= 3:
            out += base64.b64encode(data[src_off : src_off + 3])
            src_off += 3
            to_process -= 3
        if last:
            rest = data[src_off:]
            src_off = length
            tail = rest + struct.pack(">H", crc)
            if len(tail) == 4:  # two data bytes + CRC: 3 + 1
                out += base64.b64encode(tail[:3]) + base64.b64encode(tail[3:])
            else:
                out += base64.b64encode(tail)
        out += b"\n"
        marker = SERIAL_HDR_FRAG
        first = False
    return bytes(out)


def zephyr_rx(lines: list[bytes]) -> bytes | None:
    """Python port of mcumgr_serial_process_frag() for a sequence of lines."""
    buf = bytearray()
    pkt_len = 0
    for line in lines:
        op = line[:2]
        body = line[2:].rstrip(b"\n")
        if op == SERIAL_HDR_PKT:
            buf = bytearray()
        elif op != SERIAL_HDR_FRAG or not buf:
            return None
        buf += base64.b64decode(body)
        if op == SERIAL_HDR_PKT:
            pkt_len = struct.unpack(">H", buf[:2])[0]
            del buf[:2]
        if len(buf) < pkt_len:
            continue
        if len(buf) > pkt_len or crc16_xmodem(bytes(buf)) != 0:
            return None
        return bytes(buf[:-2])
    return None


def split_lines(data: bytes) -> list[bytes]:
    return [line + b"\n" for line in data.split(b"\n") if line]


# --- unit tests ------------------------------------------------------------------------------


def test_crc16_check_value() -> None:
    assert crc16_xmodem(b"123456789") == 0x31C3
    assert crc16_xmodem(b"") == 0
    data = b"hello world"
    assert crc16_xmodem(data + struct.pack(">H", crc16_xmodem(data))) == 0


def test_single_line_packet() -> None:
    frame = encode_frame(g.OP_WRITE, g.GROUP_OS, g.OS_ECHO, 1, {"d": "hi"})
    out = encode_serial_packet(frame)
    assert out.startswith(SERIAL_HDR_PKT) and out.endswith(b"\n") and out.count(b"\n") == 1
    raw = base64.b64decode(out[2:-1])
    assert raw[:2] == struct.pack(">H", len(frame) + 2)
    assert raw[2:-2] == frame
    assert raw[-2:] == struct.pack(">H", crc16_xmodem(frame))
    assert SerialFrameDecoder().feed_line(out) == frame


@pytest.mark.parametrize("size", [1, 2, 3, 88, 89, 90, 91, 92, 93, 94, 180, 181, 182, 255, 1000])
def test_multi_line_packets(size: int) -> None:
    frame = bytes((i * 7) & 0xFF for i in range(size))
    out = encode_serial_packet(frame)
    lines = split_lines(out)
    assert all(len(line) <= SERIAL_MAX_FRAME for line in lines)
    assert lines[0].startswith(SERIAL_HDR_PKT)
    assert all(line.startswith(SERIAL_HDR_FRAG) for line in lines[1:])
    # every line decodes on its own (whole base64 quanta)
    assert all((len(line) - 3) % 4 == 0 for line in lines)
    # our decoder and the Zephyr receive algorithm both reassemble it
    dec = SerialFrameDecoder()
    results = [dec.feed_line(line) for line in lines]
    assert results[:-1] == [None] * (len(lines) - 1)
    assert results[-1] == frame
    assert zephyr_rx(lines) == frame


@pytest.mark.parametrize("size", list(range(1, 200)) + [255, 256, 300, 511, 1024])
def test_decoder_accepts_zephyr_framing(size: int) -> None:
    frame = bytes((i * 13 + size) & 0xFF for i in range(size))
    lines = split_lines(zephyr_tx_pkt(frame))
    assert all(len(line) <= SERIAL_MAX_FRAME for line in lines)
    assert zephyr_rx(lines) == frame  # sanity of the port
    dec = SerialFrameDecoder()
    out = [dec.feed_line(line) for line in lines]
    assert out[-1] == frame and all(o is None for o in out[:-1])


def test_decoder_ignores_console_and_resyncs() -> None:
    frame = encode_frame(g.OP_READ_RSP, g.GROUP_UC_NODE, 0, 9, {"x": "y" * 300})
    lines = split_lines(encode_serial_packet(frame))
    dec = SerialFrameDecoder()
    assert dec.feed_line(b"[00:00:01.000,000] <inf> uc_main: booted\r\n") is None
    # a frame after a shell prompt on the same line
    assert dec.feed_line(b"uart:~$ " + lines[0]) is None
    assert dec.feed_line(b"<wrn> some log line\n") is None  # interleaved log
    for line in lines[1:-1]:
        assert dec.feed_line(line) is None
    assert dec.feed_line(lines[-1]) == frame
    # continuation without a first line is ignored
    assert dec.feed_line(lines[1]) is None
    # corrupted CRC is dropped
    bad = bytearray(encode_serial_packet(b"\x01\x00\x00\x00\x00\x00\x01\x00"))
    body = bytearray(base64.b64decode(bytes(bad[2:-1])))
    body[-1] ^= 0xFF
    assert dec.feed_line(SERIAL_HDR_PKT + base64.b64encode(bytes(body)) + b"\n") is None
    # invalid base64 resets
    assert dec.feed_line(SERIAL_HDR_PKT + b"!!!!\n") is None


def test_line_splitter() -> None:
    sp = LineSplitter()
    assert sp.feed(b"abc") == []
    assert sp.feed(b"\ndef\ngh") == [b"abc\n", b"def\n"]
    assert sp.feed(b"\n") == [b"gh\n"]


# --- SerialTransport over a pty ----------------------------------------------------------------


class PtyDevice:
    """The device side of a pty: decodes SMP lines, answers through a
    FakeNode and interleaves console output."""

    def __init__(self, node: FakeNode) -> None:
        import tty

        self.node = node
        self.master, self.slave = os.openpty()
        tty.setraw(self.slave)
        self.path = os.ttyname(self.slave)
        self.splitter = LineSplitter()
        self.decoder = SerialFrameDecoder()
        self.frames = 0

    def start(self) -> None:
        asyncio.get_running_loop().add_reader(self.master, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self.master, 4096)
        except OSError:
            return
        for line in self.splitter.feed(data):
            frame = self.decoder.feed_line(line)
            if frame is None:
                continue
            self.frames += 1
            rsp = self.node.handle_smp_frame(frame)
            if rsp is None:
                continue
            out = b"[00:00:02.000] <inf> uc_mgmt: request\r\n" + encode_serial_packet(rsp)
            out += b"uart:~$ \r\n"
            os.write(self.master, out)

    def close(self) -> None:
        asyncio.get_running_loop().remove_reader(self.master)
        os.close(self.master)
        os.close(self.slave)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a Linux pty")
async def test_serial_transport_end_to_end() -> None:
    pytest.importorskip("serial")
    node = FakeNode()
    dev = PtyDevice(node)
    dev.start()
    console: list[str] = []
    transport = SerialTransport(dev.path, 115200, on_console=console.append)
    client = SmpClient(transport, timeout=2.0)
    try:
        assert await client.os_echo("over serial") == "over serial"
        data = bytes(range(256)) * 3
        await client.fs_upload("/lfs/data/blob.bin", data)
        assert node.files["/lfs/data/blob.bin"] == data
        # the download response (512 byte chunks) spans several console lines
        assert await client.fs_download("/lfs/data/blob.bin") == data
        catalog = await client.io_catalog()
        assert catalog["board"] == "native_sim"
        # frame size is limited by the 256 byte shell MTU
        assert transport.mtu == 256
        assert dev.frames >= 8
        assert any("uc_mgmt: request" in line for line in console)
        assert "uart:~$ " in transport.console
    finally:
        await client.close()
        dev.close()


async def test_serial_transport_without_device() -> None:
    pytest.importorskip("serial")
    from bacnet_uc_harness.errors import HarnessError

    transport = SerialTransport("/dev/does-not-exist-bacnet-uc")
    with pytest.raises(HarnessError):
        await transport.open()
