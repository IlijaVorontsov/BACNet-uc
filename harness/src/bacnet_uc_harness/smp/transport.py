# SPDX-License-Identifier: Apache-2.0
"""SMP transports: UDP (port 1337) and the shell UART console.

A transport sends one encoded SMP frame and returns the first response frame
that carries the same sequence number. Retries and error handling are done
by :class:`bacnet_uc_harness.smp.client.SmpClient`.

Console framing (Zephyr ``smp_shell`` / ``subsys/mgmt/mcumgr/transport/src/
serial_util.c``)::

    packet = be16(len(frame) + 2) || frame || be16(crc16_xmodem(frame))
    line   = marker || base64(chunk of packet) || "\\n"   (<= 127 bytes)

The marker is ``0x06 0x09`` for the first line of a packet and ``0x04 0x14``
for continuation lines. Each line carries a multiple of 3 packet bytes
(4 base64 characters), except the last, so every line decodes on its own.
"""

from __future__ import annotations

import abc
import asyncio
import base64
import binascii
import collections
import contextlib
import logging
import socket
import struct
import threading
from collections.abc import Callable
from typing import Any

from bacnet_uc_harness.errors import HarnessError, HarnessTimeout
from bacnet_uc_harness.smp.codec import SMP_HEADER_SIZE

log = logging.getLogger(__name__)

SERIAL_HDR_PKT = b"\x06\x09"
SERIAL_HDR_FRAG = b"\x04\x14"
SERIAL_MAX_FRAME = 127  # MCUMGR_SERIAL_MAX_FRAME, including marker and newline


class SmpTransport(abc.ABC):
    """One SMP request/response exchange at a time."""

    @abc.abstractmethod
    async def request(self, frame: bytes, seq: int, timeout: float) -> bytes:
        """Send ``frame`` and return the response frame with sequence ``seq``.

        Raises:
            HarnessTimeout: no matching response within ``timeout`` seconds.
            HarnessError: the transport failed.
        """

    @abc.abstractmethod
    async def close(self) -> None:
        """Release the transport."""

    @property
    @abc.abstractmethod
    def mtu(self) -> int:
        """Largest SMP frame (header + CBOR) the transport carries."""

    async def open(self) -> None:  # noqa: B027 - optional hook
        """Open the transport (``request`` opens it on first use)."""


def _is_response(frame: bytes) -> bool:
    # op 1 (read-rsp) or 3 (write-rsp)
    return len(frame) >= SMP_HEADER_SIZE and (frame[0] & 0x07) in (1, 3)


# --- UDP ----------------------------------------------------------------------------


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: UdpTransport) -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._owner._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        self._owner._last_error = exc
        log.debug("SMP UDP socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self._owner._transport = None


class UdpTransport(SmpTransport):
    """SMP over UDP (``CONFIG_MCUMGR_TRANSPORT_UDP``), one request in flight."""

    def __init__(self, host: str, port: int = 1337, mtu: int = 1024) -> None:
        self.host = host
        self.port = port
        self._mtu = mtu
        self._transport: asyncio.DatagramTransport | None = None
        self._lock = asyncio.Lock()
        self._pending: tuple[int, asyncio.Future[bytes]] | None = None
        self._last_error: Exception | None = None

    @property
    def mtu(self) -> int:
        return self._mtu

    def __repr__(self) -> str:
        return f"UdpTransport({self.host!r}, {self.port})"

    async def open(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _UdpProtocol(self), remote_addr=(self.host, self.port), family=family
            )
        except OSError as exc:
            raise HarnessError(f"SMP UDP {self.host}:{self.port}: {exc}") from exc

    def _on_datagram(self, data: bytes) -> None:
        if self._pending is None or len(data) < SMP_HEADER_SIZE or not _is_response(data):
            return
        seq, fut = self._pending
        if data[6] == seq and not fut.done():
            fut.set_result(bytes(data))
        else:
            log.debug("SMP UDP: dropping frame with seq %d (waiting for %d)", data[6], seq)

    async def request(self, frame: bytes, seq: int, timeout: float) -> bytes:
        if len(frame) > self._mtu:
            raise HarnessError(f"SMP frame of {len(frame)} bytes exceeds the MTU {self._mtu}")
        async with self._lock:
            await self.open()
            assert self._transport is not None
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bytes] = loop.create_future()
            self._pending = (seq, fut)
            self._last_error = None
            try:
                self._transport.sendto(frame)
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                detail = f" (socket error: {self._last_error})" if self._last_error else ""
                raise HarnessTimeout(
                    f"no SMP response from {self.host}:{self.port} within {timeout:.1f} s{detail}"
                ) from None
            finally:
                self._pending = None

    async def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None


# --- console framing ------------------------------------------------------------------


def crc16_xmodem(data: bytes, crc: int = 0) -> int:
    """CRC16-CCITT, polynomial 0x1021, initial value 0, no reflection
    (Zephyr ``crc16_itu_t(0, ...)``; check value 0x31C3 for "123456789")."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def encode_serial_packet(frame: bytes, max_frame: int = SERIAL_MAX_FRAME) -> bytes:
    """Frame one SMP packet for the console: returns the lines, each ending
    with ``\\n``, concatenated."""
    packet = struct.pack(">H", len(frame) + 2) + frame + struct.pack(">H", crc16_xmodem(frame))
    encoded = base64.b64encode(packet)
    chunk = ((max_frame - 3) // 4) * 4  # marker (2) + newline (1) are not encoded
    if chunk <= 0:
        raise ValueError("max_frame too small")
    out = bytearray()
    for i, off in enumerate(range(0, len(encoded), chunk)):
        out += SERIAL_HDR_PKT if i == 0 else SERIAL_HDR_FRAG
        out += encoded[off : off + chunk]
        out += b"\n"
    return bytes(out)


class SerialFrameDecoder:
    """Reassembles SMP packets from console lines.

    Lines that do not contain a frame marker are console output (log messages,
    shell prompt) and are ignored by :meth:`feed_line`.
    """

    def __init__(self) -> None:
        self._buf: bytearray | None = None
        self._expected = 0

    def reset(self) -> None:
        self._buf = None
        self._expected = 0

    @staticmethod
    def find_marker(line: bytes) -> int:
        """Index of the first frame marker in ``line``, -1 if none."""
        idx = [i for i in (line.find(SERIAL_HDR_PKT), line.find(SERIAL_HDR_FRAG)) if i >= 0]
        return min(idx) if idx else -1

    def feed_line(self, line: bytes) -> bytes | None:
        """Process one line (with or without the trailing newline).

        Returns a complete SMP frame (CRC checked and stripped) or ``None``.
        """
        idx = self.find_marker(line)
        if idx < 0:
            return None
        marker = line[idx : idx + 2]
        body = line[idx + 2 :].strip(b"\r\n \t")
        try:
            data = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError):
            log.debug("SMP console: bad base64, dropping packet")
            self.reset()
            return None
        if marker == SERIAL_HDR_PKT:
            if len(data) < 2:
                self.reset()
                return None
            self._expected = struct.unpack_from(">H", data, 0)[0]
            if self._expected <= 2:
                self.reset()
                return None
            self._buf = bytearray(data[2:])
        else:
            if self._buf is None:
                return None
            self._buf += data
        if len(self._buf) < self._expected:
            return None
        packet = bytes(self._buf)
        expected = self._expected
        self.reset()
        if len(packet) > expected:
            log.debug("SMP console: packet longer than its length field, dropping")
            return None
        if crc16_xmodem(packet) != 0:
            log.debug("SMP console: CRC mismatch, dropping packet")
            return None
        return packet[:-2]


class LineSplitter:
    """Splits a byte stream into ``\\n``-terminated lines."""

    def __init__(self, max_line: int = 4096) -> None:
        self._buf = bytearray()
        self._max = max_line

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        lines: list[bytes] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            lines.append(bytes(self._buf[: nl + 1]))
            del self._buf[: nl + 1]
        if len(self._buf) > self._max:
            lines.append(bytes(self._buf))
            self._buf.clear()
        return lines


# --- serial --------------------------------------------------------------------------------


class SerialTransport(SmpTransport):
    """SMP over the shell console UART (``CONFIG_MCUMGR_TRANSPORT_SHELL``).

    Requires the optional dependency ``pyserial`` (``pip install
    bacnet-uc-harness[serial]``). Console lines that are not SMP frames are
    kept in :attr:`console` (last 1000 lines) and passed to ``on_console``.
    ``device`` may also be a pyserial URL (``socket://host:port``,
    ``rfc2217://...``).

    ``line_delay`` paces the lines of a multi-line packet: the node holds
    every received line in one of ``CONFIG_MCUMGR_TRANSPORT_SHELL_RX_BUF_COUNT``
    (default 2) buffers until the shell thread has processed it, and drops
    lines ("smp_shell: Failed to alloc SMP buf") when they arrive faster.
    Measured on native_sim: 0 s loses the third line of a 3-line packet,
    10 ms and more work; the default is 20 ms.
    """

    def __init__(
        self,
        device: str,
        baud: int = 115200,
        mtu: int = 256,
        line_delay: float = 0.02,
        on_console: Callable[[str], None] | None = None,
    ) -> None:
        self.device = device
        self.baud = baud
        self._mtu = mtu
        self.line_delay = line_delay
        self.on_console = on_console
        self.console: collections.deque[str] = collections.deque(maxlen=1000)
        self._serial: Any = None
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: tuple[int, asyncio.Future[bytes]] | None = None
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._splitter = LineSplitter()
        self._decoder = SerialFrameDecoder()

    @property
    def mtu(self) -> int:
        return self._mtu

    def __repr__(self) -> str:
        return f"SerialTransport({self.device!r}, {self.baud})"

    async def open(self) -> None:
        if self._serial is not None:
            return
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:
            raise HarnessError(
                "the serial transport needs pyserial: pip install 'bacnet-uc-harness[serial]'"
            ) from exc
        self._loop = asyncio.get_running_loop()

        def _open() -> Any:
            return serial.serial_for_url(self.device, baudrate=self.baud, timeout=0.05)

        try:
            self._serial = await self._loop.run_in_executor(None, _open)
        except Exception as exc:  # serial.SerialException and OSError
            raise HarnessError(f"cannot open {self.device}: {exc}") from exc
        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name=f"smp-serial-{self.device}", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        ser = self._serial
        loop = self._loop
        assert loop is not None
        while not self._stop.is_set():
            try:
                data = ser.read(256)
            except Exception as exc:  # port closed or device gone
                if not self._stop.is_set():
                    log.warning("SMP serial read failed: %s", exc)
                break
            if data:
                with contextlib.suppress(RuntimeError):  # loop closed
                    loop.call_soon_threadsafe(self._feed, bytes(data))

    def _feed(self, data: bytes) -> None:
        for line in self._splitter.feed(data):
            frame = self._decoder.feed_line(line)
            if frame is not None:
                self._on_frame(frame)
                continue
            if SerialFrameDecoder.find_marker(line) < 0:
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                self.console.append(text)
                if self.on_console is not None:
                    try:
                        self.on_console(text)
                    except Exception:  # never let a callback kill the reader
                        log.exception("console callback failed")

    def _on_frame(self, frame: bytes) -> None:
        if self._pending is None or not _is_response(frame):
            return
        seq, fut = self._pending
        if frame[6] == seq and not fut.done():
            fut.set_result(frame)

    async def _write(self, data: bytes) -> None:
        assert self._loop is not None
        lines = data.split(b"\n")
        if data.endswith(b"\n"):
            lines = lines[:-1]
        for i, line in enumerate(lines):
            if i > 0 and self.line_delay > 0:
                await asyncio.sleep(self.line_delay)
            await self._loop.run_in_executor(None, self._serial.write, line + b"\n")
        await self._loop.run_in_executor(None, self._serial.flush)

    async def request(self, frame: bytes, seq: int, timeout: float) -> bytes:
        if len(frame) > self._mtu:
            raise HarnessError(f"SMP frame of {len(frame)} bytes exceeds the MTU {self._mtu}")
        async with self._lock:
            await self.open()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bytes] = loop.create_future()
            self._pending = (seq, fut)
            try:
                await self._write(encode_serial_packet(frame))
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:
                raise HarnessTimeout(
                    f"no SMP response on {self.device} within {timeout:.1f} s"
                ) from None
            except OSError as exc:
                raise HarnessError(f"SMP serial write failed: {exc}") from exc
            finally:
                self._pending = None

    async def close(self) -> None:
        self._stop.set()
        ser, self._serial = self._serial, None
        if self._reader is not None:
            await asyncio.get_running_loop().run_in_executor(None, self._reader.join, 1.0)
            self._reader = None
        if ser is not None:
            with contextlib.suppress(Exception):
                ser.close()
