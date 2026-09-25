# SPDX-License-Identifier: Apache-2.0
"""Minimal asynchronous BACnet/IP client: Who-Is, ReadProperty, WriteProperty.

The client sends unsegmented confirmed requests with
segmented-response-accepted = 0 and max-APDU 1476. A property that does not
fit into one APDU makes the server abort with segmentation-not-supported;
:meth:`BacnetClient.read_property` then reads arrays element by element.

Discovery note: BACnet devices (including the BACnet-uc firmware, which uses
the bacnet-stack Who-Is handler) answer Who-Is with a *broadcast* I-Am to
UDP port 47808. A client bound to an ephemeral port only sees unicast
I-Ams. Pass ``local_port=47808`` or ``listen_broadcast=True`` (an additional
``SO_REUSEADDR`` receive socket on port 47808) to see broadcast I-Ams; do not
use either on a host that runs native_sim nodes on port 47808.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Any

from bacnet_uc_harness.bacnet import codec, enums
from bacnet_uc_harness.errors import BacnetError, HarnessError, HarnessTimeout

log = logging.getLogger(__name__)

Address = tuple[str, int]


@dataclass
class IAm:
    """A received I-Am."""

    device: int
    address: Address
    max_apdu: int
    segmentation: int
    vendor_id: int
    network: int | None = None  # SNET when the device is behind a router
    mac: bytes = b""  # SADR when the device is behind a router


def parse_address(address: Address | str | list[Any], default_port: int = 47808) -> Address:
    """``("1.2.3.4", 47808)``, ``"1.2.3.4:47808"`` or ``"1.2.3.4"``."""
    if isinstance(address, tuple | list):
        if len(address) != 2:
            raise HarnessError(f"invalid BACnet address {address!r}")
        return str(address[0]), int(address[1])
    text = str(address).strip()
    if text.count(":") == 1:
        host, _, port = text.partition(":")
        return host, int(port)
    return text, default_port


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, client: BacnetClient) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: Any) -> None:
        self._client._on_datagram(data, (addr[0], addr[1]))

    def error_received(self, exc: Exception) -> None:
        log.debug("BACnet socket error: %s", exc)


class BacnetClient:
    """BACnet/IP client on one UDP socket."""

    def __init__(
        self,
        local_host: str = "0.0.0.0",
        local_port: int = 0,
        broadcast: str = "255.255.255.255",
        port: int = 47808,
        apdu_timeout: float = 3.0,
        retries: int = 2,
        *,
        max_apdu: int = codec.MAX_APDU_BIP,
        listen_broadcast: bool = False,
    ) -> None:
        self.local_host = local_host
        self.local_port = local_port
        self.broadcast = broadcast
        self.port = port
        self.apdu_timeout = apdu_timeout
        self.retries = retries
        self.max_apdu = max_apdu
        self.listen_broadcast = listen_broadcast
        #: every I-Am seen, by device instance
        self.iams: dict[int, IAm] = {}
        self._transport: asyncio.DatagramTransport | None = None
        self._bcast_transport: asyncio.DatagramTransport | None = None
        self._pending: dict[int, tuple[Address, int, asyncio.Future[codec.Apdu]]] = {}
        self._next_invoke = 0
        self._iam_queues: set[asyncio.Queue[IAm]] = set()
        self._resolved: dict[str, str] = {}

    async def __aenter__(self) -> BacnetClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def local_address(self) -> Address:
        """Bound ``(host, port)`` of the socket."""
        if self._transport is None:
            raise HarnessError("BACnet client not started")
        name = self._transport.get_extra_info("sockname")
        return name[0], name[1]

    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self),
                local_addr=(self.local_host, self.local_port),
                family=socket.AF_INET,
                allow_broadcast=True,
            )
        except OSError as exc:
            raise HarnessError(
                f"cannot bind BACnet/IP socket {self.local_host}:{self.local_port}: {exc}"
            ) from exc
        if self.listen_broadcast and self.local_address[1] != self.port:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("", self.port))
                sock.setblocking(False)
                self._bcast_transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self), sock=sock
                )
            except OSError as exc:
                sock.close()
                log.warning("no broadcast listener on port %d: %s", self.port, exc)

    async def close(self) -> None:
        for _, _, fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HarnessError("BACnet client closed"))
        self._pending.clear()
        for tr in (self._transport, self._bcast_transport):
            if tr is not None:
                tr.close()
        self._transport = None
        self._bcast_transport = None

    # --- I/O ------------------------------------------------------------------------------

    async def _resolve(self, address: Address | str) -> Address:
        host, port = parse_address(address, self.port)
        if host in self._resolved:
            return self._resolved[host], port
        try:
            ip = str(ipaddress.IPv4Address(host))
        except ValueError:
            loop = asyncio.get_running_loop()
            try:
                infos = await loop.getaddrinfo(
                    host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM
                )
            except OSError as exc:
                raise HarnessError(f"cannot resolve {host}: {exc}") from exc
            ip = infos[0][4][0]
        self._resolved[host] = ip
        return ip, port

    def _send(self, data: bytes, address: Address) -> None:
        if self._transport is None:
            raise HarnessError("BACnet client not started")
        self._transport.sendto(data, address)

    def _on_datagram(self, data: bytes, addr: Address) -> None:
        try:
            bvlc = codec.decode_bvlc(data)
            if bvlc.function not in (
                codec.BVLC_ORIGINAL_UNICAST_NPDU,
                codec.BVLC_ORIGINAL_BROADCAST_NPDU,
                codec.BVLC_FORWARDED_NPDU,
            ):
                return
            src = bvlc.origin or addr
            npdu = codec.decode_npdu(bvlc.npdu)
            if npdu.is_network_message or not npdu.apdu:
                return
            apdu = codec.decode_apdu(npdu.apdu)
        except codec.CodecError as exc:
            log.debug("dropping malformed BACnet packet from %s: %s", addr, exc)
            return
        if apdu.pdu_type == codec.PDU_UNCONFIRMED_REQUEST:
            if apdu.service == enums.SERVICE_UNCONFIRMED_I_AM:
                self._on_i_am(apdu, src, npdu)
            return
        if apdu.pdu_type == codec.PDU_CONFIRMED_REQUEST:
            # the client serves no objects
            if apdu.invoke_id is not None and npdu.dnet is None:
                reject = codec.encode_reject(
                    apdu.invoke_id, enums.REJECT_REASONS["unrecognized-service"]
                )
                with contextlib.suppress(HarnessError):
                    self._send(codec.encode_bip(reject), addr)
            return
        if apdu.invoke_id is None:
            return
        entry = self._pending.get(apdu.invoke_id)
        if entry is None:
            return
        dest, service, fut = entry
        if src[0] != dest[0] and addr[0] != dest[0]:
            log.debug("invoke id %d answered by %s, expected %s", apdu.invoke_id, src, dest)
            return
        if fut.done():
            return
        if apdu.pdu_type in (codec.PDU_COMPLEX_ACK,) and apdu.segmented:
            # we never accept segmented responses: abort the transaction
            abort = codec.encode_abort(
                apdu.invoke_id, enums.ABORT_REASONS["segmentation-not-supported"], server=False
            )
            with contextlib.suppress(HarnessError):
                self._send(codec.encode_bip(abort), addr)
            fut.set_exception(
                BacnetError("abort", reason=enums.ABORT_REASONS["segmentation-not-supported"])
            )
            return
        if apdu.pdu_type == codec.PDU_SEGMENT_ACK:
            return
        fut.set_result(apdu)

    def _on_i_am(self, apdu: codec.Apdu, src: Address, npdu: codec.Npdu) -> None:
        try:
            data = codec.decode_i_am(apdu.data)
        except codec.CodecError as exc:
            log.debug("malformed I-Am from %s: %s", src, exc)
            return
        iam = IAm(
            device=data.device,
            address=src,
            max_apdu=data.max_apdu,
            segmentation=data.segmentation,
            vendor_id=data.vendor_id,
            network=npdu.snet,
            mac=npdu.sadr,
        )
        self.iams[iam.device] = iam
        for q in list(self._iam_queues):
            q.put_nowait(iam)

    def _alloc_invoke_id(self) -> int:
        for _ in range(256):
            invoke = self._next_invoke
            self._next_invoke = (self._next_invoke + 1) & 0xFF
            if invoke not in self._pending:
                return invoke
        raise HarnessError("no free BACnet invoke id (256 requests outstanding)")

    async def confirmed_request(
        self,
        address: Address | str,
        service: int,
        service_data: bytes,
        timeout: float | None = None,
    ) -> codec.Apdu:
        """Send a confirmed request; returns the SimpleACK/ComplexACK APDU.

        Raises:
            BacnetError: Error, Reject or Abort response.
            HarnessTimeout: no response after ``retries`` retransmissions.
        """
        if self._transport is None:
            await self.start()
        dest = await self._resolve(address)
        invoke = self._alloc_invoke_id()
        apdu = codec.encode_confirmed_request(invoke, service, service_data, max_apdu=self.max_apdu)
        packet = codec.encode_bip(apdu, expecting_reply=True)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[codec.Apdu] = loop.create_future()
        self._pending[invoke] = (dest, service, fut)
        t = self.apdu_timeout if timeout is None else timeout
        try:
            for _ in range(self.retries + 1):
                self._send(packet, dest)
                done, _ = await asyncio.wait({fut}, timeout=t)
                if done:
                    break
            else:
                raise HarnessTimeout(
                    f"no BACnet response from {dest[0]}:{dest[1]} "
                    f"({enums.CONFIRMED_SERVICE_NAMES.get(service, service)}) after "
                    f"{self.retries + 1} attempts of {t:.1f} s"
                )
            rsp = fut.result()
        finally:
            self._pending.pop(invoke, None)
        if rsp.pdu_type == codec.PDU_ERROR:
            raise BacnetError("error", error_class=rsp.error_class, error_code=rsp.error_code)
        if rsp.pdu_type == codec.PDU_REJECT:
            raise BacnetError("reject", reason=rsp.reason)
        if rsp.pdu_type == codec.PDU_ABORT:
            raise BacnetError("abort", reason=rsp.reason)
        if rsp.service != service:
            raise HarnessError(f"BACnet ack for service {rsp.service}, expected {service}")
        return rsp

    # --- services -----------------------------------------------------------------------------

    async def who_is(
        self,
        low: int | None = None,
        high: int | None = None,
        timeout: float = 2.0,
        target: Address | str | None = None,
    ) -> list[IAm]:
        """Send Who-Is (broadcast, or unicast to ``target``) and collect I-Ams
        for ``timeout`` seconds (returns early when ``low == high`` answered).
        """
        if self._transport is None:
            await self.start()
        if high is None and low is not None:
            high = low
        if low is None and high is not None:
            low = 0
        queue: asyncio.Queue[IAm] = asyncio.Queue()
        self._iam_queues.add(queue)
        found: dict[int, IAm] = {}
        try:
            if target is None:
                dest: Address = (self.broadcast, self.port)
                broadcast = True
            else:
                dest = await self._resolve(target)
                broadcast = False
            self._send(codec.encode_bip(codec.who_is(low, high), broadcast=broadcast), dest)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    iam = await asyncio.wait_for(queue.get(), remaining)
                except TimeoutError:
                    break
                if low is not None and high is not None and not low <= iam.device <= high:
                    continue
                found[iam.device] = iam
                if low is not None and low == high:
                    break
        finally:
            self._iam_queues.discard(queue)
        return sorted(found.values(), key=lambda i: i.device)

    async def read_property_ack(
        self,
        address: Address | str,
        obj_type: str | int,
        instance: int,
        prop: str | int = "present-value",
        index: int | None = None,
        timeout: float | None = None,
    ) -> codec.ReadPropertyAck:
        """ReadProperty returning the decoded ACK (all values as a list)."""
        try:
            data = codec.read_property_request_data(obj_type, instance, prop, index)
        except ValueError as exc:
            raise HarnessError(str(exc)) from None
        rsp = await self.confirmed_request(
            address, enums.SERVICE_CONFIRMED_READ_PROPERTY, data, timeout
        )
        try:
            return codec.decode_read_property_ack(rsp.data)
        except codec.CodecError as exc:
            raise HarnessError(f"malformed ReadProperty-ACK: {exc}") from exc

    async def read_property(
        self,
        address: Address | str,
        obj_type: str | int,
        instance: int,
        prop: str | int = "present-value",
        index: int | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Read one property.

        Returns the Python value (see :mod:`bacnet_uc_harness.bacnet.codec`);
        arrays and lists read without ``index`` return a list, object
        identifiers a ``(type, instance)`` tuple (:class:`codec.ObjectId`).
        """
        try:
            prop_num = enums.property_number(prop)
            enums.object_type_number(obj_type)
        except ValueError as exc:
            raise HarnessError(str(exc)) from None
        is_array = prop_num in codec.ARRAY_OR_LIST_PROPERTIES
        try:
            ack = await self.read_property_ack(
                address, obj_type, instance, prop_num, index, timeout
            )
        except BacnetError as exc:
            too_big = (
                exc.kind == "abort"
                and exc.reason
                in (
                    enums.ABORT_REASONS["segmentation-not-supported"],
                    enums.ABORT_REASONS["buffer-overflow"],
                    enums.ABORT_REASONS["apdu-too-long"],
                )
            ) or (exc.kind == "reject" and exc.reason == enums.REJECT_REASONS["buffer-overflow"])
            if not (too_big and is_array and index is None):
                raise
            return await self._read_array_by_index(address, obj_type, instance, prop_num, timeout)
        values = ack.values
        if index is None and is_array:
            return list(values)
        if len(values) == 1:
            return values[0]
        return list(values)

    async def _read_array_by_index(
        self,
        address: Address | str,
        obj_type: str | int,
        instance: int,
        prop: int,
        timeout: float | None,
    ) -> list[Any]:
        count = await self.read_property(address, obj_type, instance, prop, 0, timeout)
        if not isinstance(count, int):
            raise HarnessError(f"array length of property {prop} is not an Unsigned: {count!r}")
        out: list[Any] = []
        for i in range(1, count + 1):
            out.append(await self.read_property(address, obj_type, instance, prop, i, timeout))
        return out

    async def write_property(
        self,
        address: Address | str,
        obj_type: str | int,
        instance: int,
        prop: str | int,
        value: Any,
        priority: int | None = None,
        index: int | None = None,
        tag: str | int | None = None,
    ) -> None:
        """Write one property.

        The datatype is ``tag`` if given, else the natural datatype of the
        property (:func:`codec.property_value_tag`, e.g. REAL for an analog
        Present_Value, ENUMERATED for a binary one), else it follows the Python
        type. ``None`` writes NULL (relinquishes the ``priority`` slot). Text
        values of enumerated properties are accepted (``"active"``,
        ``"degrees-celsius"``, ...). A list writes all elements of an array.
        """
        try:
            value_bytes = encode_property_value(obj_type, prop, value, tag)
            data = codec.write_property_request_data(
                obj_type, instance, prop, value_bytes, priority, index
            )
        except ValueError as exc:
            raise HarnessError(str(exc)) from None
        await self.confirmed_request(address, enums.SERVICE_CONFIRMED_WRITE_PROPERTY, data)

    async def device_communication_control(
        self,
        address: Address | str,
        enable: str | int = "enable",
        duration_min: int | None = None,
        password: str | None = None,
    ) -> None:
        """DeviceCommunicationControl (``enable``, ``disable``,
        ``disable-initiation``). BACnet-uc nodes require the ``bacnet.password``
        of their device.json (security/password-failure otherwise)."""
        state = _choice(enable, enums.DCC_ENABLE_DISABLE, "enable-disable")
        data = codec.device_communication_control_request_data(state, duration_min, password)
        await self.confirmed_request(
            address, enums.SERVICE_CONFIRMED_DEVICE_COMMUNICATION_CONTROL, data
        )

    async def reinitialize_device(
        self, address: Address | str, state: str | int = "warmstart", password: str | None = None
    ) -> None:
        """ReinitializeDevice (``coldstart``, ``warmstart``, ...); needs the
        node's ``bacnet.password``. The node restarts after the SimpleACK."""
        st = _choice(state, enums.REINITIALIZED_STATES, "reinitialized state")
        data = codec.reinitialize_device_request_data(st, password)
        await self.confirmed_request(address, enums.SERVICE_CONFIRMED_REINITIALIZE_DEVICE, data)


def _choice(value: str | int, table: dict[str, int], what: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    try:
        return table[str(value).lower()]
    except KeyError:
        raise HarnessError(f"unknown {what} {value!r} (one of {', '.join(table)})") from None


_ENUM_TEXT_TABLES: dict[str, dict[str, int]] = {
    "units": enums.UNITS,
    "polarity": enums.POLARITY,
    "event-state": enums.EVENT_STATES,
    "reliability": enums.RELIABILITY,
    "system-status": enums.DEVICE_STATUS,
    "segmentation-supported": enums.SEGMENTATION,
    "object-type": enums.OBJECT_TYPES,
}


def _enum_from_text(obj_type: str | int, prop: str | int, text: str) -> int:
    stripped = text.strip()
    if stripped.isdigit():
        return int(stripped)
    key = stripped.lower().replace("_", "-").replace(" ", "-")
    prop_name = enums.property_name(enums.property_number(prop)).lower()
    if prop_name in (
        "present-value",
        "relinquish-default",
        "priority-array",
        "alarm-value",
        "feedback-value",
    ):
        if key in ("active", "on", "true", "1"):
            return 1
        if key in ("inactive", "off", "false", "0"):
            return 0
    table = _ENUM_TEXT_TABLES.get(prop_name)
    if table is not None:
        for name, number in table.items():
            if name.lower() == key:
                return number
    raise codec.CodecError(f"unknown enumeration value {text!r} for {prop_name}")


def _encode_scalar(
    obj_type: str | int, prop: str | int, value: Any, tag: str | int | None
) -> bytes:
    if value is None:
        return codec.encode_application_null()
    if tag is None:
        return codec.encode_value(value)
    number = codec.normalize_tag_name(tag)
    if number == codec.TAG_ENUMERATED and isinstance(value, str):
        value = _enum_from_text(obj_type, prop, value)
    elif number in (codec.TAG_REAL, codec.TAG_DOUBLE) and isinstance(value, bool):
        value = float(value)
    return codec.encode_value(value, number)


def encode_property_value(
    obj_type: str | int, prop: str | int, value: Any, tag: str | int | None = None
) -> bytes:
    """Application-tagged encoding of a value written to a property (see
    :meth:`BacnetClient.write_property`)."""
    if tag is None and value is not None:
        tag = codec.property_value_tag(obj_type, prop)
    if _is_element_list(value, tag):
        return b"".join(_encode_scalar(obj_type, prop, v, tag) for v in value)
    return _encode_scalar(obj_type, prop, value, tag)


def _is_element_list(value: Any, tag: str | int | None) -> bool:
    """True if ``value`` is a list of array elements rather than one value
    that is itself a sequence (object identifier, date, time, bit string)."""
    special = (codec.BitString, codec.ObjectId, codec.Date, codec.Time)
    if not isinstance(value, list | tuple) or isinstance(value, special):
        return False
    number = None if tag is None else codec.normalize_tag_name(tag)
    if number in (codec.TAG_OBJECT_ID, codec.TAG_DATE, codec.TAG_TIME, codec.TAG_BIT_STRING):
        # a sequence of scalars is one value; a sequence of sequences/refs is a list
        return all(isinstance(v, list | tuple | str) for v in value) and len(value) > 0
    return True
