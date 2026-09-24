"""bacpypes3 plumbing shared by the BACnet/IP driver and the simulated device.

bacpypes3 binds its UDP sockets with SO_REUSEPORT, so two processes can hold
the same port and the kernel then splits unicast traffic between them. Here
the socket is bound first, without SO_REUSEPORT, and handed to bacpypes3's
normal link layer: a port conflict fails loudly, port 0 gives a private
ephemeral port (tests), and the bound port is known before the application
starts. Broadcasts go to the address given to ``open_application``; to
receive broadcast I-Ams the socket must be bound to 0.0.0.0.

bacpypes3 answers a confirmed request that fails with the Error, Reject or
Abort PDU raised as an exception; those derive from ``BaseException``, so
``bacnet_error`` turns them into hub errors at the call site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from bacpypes3.apdu import (
    AbortPDU,
    AbortReason,
    APDU,
    ConfirmedCOVNotificationRequest,
    ErrorPDU,
    ErrorRejectAbortNack,
    IAmRequest,
    RejectPDU,
    SimpleAckPDU,
    UnconfirmedCOVNotificationRequest,
)
from bacpypes3.app import Application
from bacpypes3.basetypes import PropertyValue
from bacpypes3.comm import ApplicationServiceElement
from bacpypes3.errors import ServicesError
from bacpypes3.ipv4.link import NormalLinkLayer
from bacpypes3.object import Object
from bacpypes3.pdu import Address, IPv4Address
from bacpypes3.primitivedata import ObjectIdentifier

from ...core.errors import DeviceError, DeviceTimeout, InvalidRequest

logger = logging.getLogger(__name__)

DEFAULT_PORT = 47808
_LINK_ID = ObjectIdentifier(("network-port", 1))

A = TypeVar("A", bound="BoundApplication")

#: (source address, subscriber process id, monitored object, values) -> handled
CovHandler = Callable[[Address, int, ObjectIdentifier, list[PropertyValue]], bool]


class BacnetError(DeviceError):
    """The device answered a confirmed request with an Error, Reject or Abort.

    ``kind`` is "error", "reject" or "abort"; ``reason`` the kebab-case
    error code or reject/abort reason (``unknown-object``,
    ``unrecognized-service``, ``segmentation-not-supported``)."""

    def __init__(self, message: str, kind: str, reason: str, error_class: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.reason = reason
        self.error_class = error_class


def bacnet_error(device: str, exc: ErrorRejectAbortNack) -> DeviceError | DeviceTimeout:
    """The hub error for a bacpypes3 Error/Reject/Abort PDU. bacpypes3 reports
    a request that ran out of retries as an Abort with reason no-response."""
    if isinstance(exc, AbortPDU):
        reason = exc.apduAbortRejectReason
        if reason is not None and int(reason) == AbortReason.noResponse:
            return DeviceTimeout(f"{device} did not answer")
        text = str(reason)
        return BacnetError(f"{device} aborted the request: {text}", "abort", text)
    if isinstance(exc, RejectPDU):
        text = str(exc.apduAbortRejectReason)
        return BacnetError(f"{device} rejected the request: {text}", "reject", text)
    if isinstance(exc, ErrorPDU):
        error_class = str(getattr(exc, "errorClass", "") or "")
        code = str(getattr(exc, "errorCode", "") or "")
        return BacnetError(f"{device}: {error_class}: {code}", "error", code, error_class)
    return BacnetError(f"{device}: {exc}", "error", str(exc))


def parse_host_port(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """``"10.0.2.10"`` / ``"10.0.2.10:47809"`` -> (host, port). Only IPv4
    literals: BACnet/IP addresses are configured as such, and resolving names
    would block or hide a misconfiguration."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidRequest(f"{text!r} is not an IPv4 address")
    host, sep, port_text = text.strip().partition(":")
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        raise InvalidRequest(f"{text!r} is not an IPv4 address[:port]") from None
    port = default_port
    if sep:
        if not port_text.isdigit():
            raise InvalidRequest(f"{text!r} has an invalid port")
        port = int(port_text)
    if not 0 < port < 65536:
        raise InvalidRequest(f"{text!r}: port out of range")
    return host, port


def format_address(addr: Address) -> str:
    """``"host:port"`` for an IPv4 address (always with the port), else the
    bacpypes3 text form (remote stations behind routers: ``net:mac``)."""
    if isinstance(addr, IPv4Address):
        host, port = addr.addrTuple
        return f"{host}:{port}"
    return str(addr)


def to_address(host: str, port: int) -> IPv4Address:
    return IPv4Address(f"{host}/32:{port}")


def bind_udp(host: str, port: int) -> socket.socket:
    """A non-blocking UDP socket bound to (host, port), broadcasts allowed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((host, port))
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


class BoundApplication(Application):
    """An ``Application`` whose link layer runs on a socket bound by us."""

    _sock: socket.socket | None = None
    #: (host, port) the socket is bound to.
    bound: tuple[str, int] = ("0.0.0.0", 0)

    def close(self) -> None:
        super().close()
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    async def send_unconfirmed(self, apdu: APDU) -> None:
        """Send an unconfirmed request and wait until it is on the wire.
        ``request()`` sends from a detached task, which hides send errors."""
        await ApplicationServiceElement.request(self, apdu)


class HubApplication(BoundApplication):
    """The hub's own BACnet device. Every I-Am is passed to the registered
    listeners (discovery collects them without ``who_is()``'s one-device
    filter for directed requests), and COV notifications go to one handler
    that routes them by source and object, so the driver owns subscription
    renewal instead of bacpypes3's context managers (which it replaces)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.i_am_listeners: set[Callable[[IAmRequest], None]] = set()
        self.cov_handler: CovHandler | None = None

    async def do_IAmRequest(self, apdu: IAmRequest) -> None:
        await super().do_IAmRequest(apdu)
        await self.device_info_cache.set_device_info(apdu)
        for listener in tuple(self.i_am_listeners):
            try:
                listener(apdu)
            except Exception:  # one broken listener must not starve the others
                logger.exception("I-Am listener failed")

    async def do_UnconfirmedCOVNotificationRequest(  # type: ignore[override]
        self, apdu: UnconfirmedCOVNotificationRequest
    ) -> None:
        self._dispatch_cov(apdu)

    async def do_ConfirmedCOVNotificationRequest(  # type: ignore[override]
        self, apdu: ConfirmedCOVNotificationRequest
    ) -> None:
        if not self._dispatch_cov(apdu):
            raise ServicesError(errorCode="unknownSubscription")
        await self.response(SimpleAckPDU(context=apdu))

    def _dispatch_cov(self, apdu: Any) -> bool:
        handler = self.cov_handler
        if handler is None:
            return False
        try:
            return handler(
                apdu.pduSource,
                int(apdu.subscriberProcessIdentifier),
                apdu.monitoredObjectIdentifier,
                list(apdu.listOfValues or ()),
            )
        except Exception:  # malformed notifications are dropped, not fatal
            logger.exception("COV notification from %s failed", apdu.pduSource)
            return False


async def open_application(
    cls: type[A],
    objects: Sequence[Object],
    host: str,
    port: int,
    *,
    broadcast: tuple[str, int] | None = None,
    ready_timeout_s: float = 2.0,
) -> A:
    """Build ``cls`` from ``objects`` (one of them the device object) on a
    fresh socket bound to (host, port) and wait until it can send."""
    sock = bind_udp(host, port)
    app: A | None = None
    try:
        app = cls.from_object_list(list(objects))
        app._sock = sock
        app.bound = sock.getsockname()
        address = to_address(*app.bound)
        link = NormalLinkLayer(address, bind_socket=sock)
        link.server.broadcast_address = broadcast
        app.link_layers[_LINK_ID] = link
        app.nsap.bind(link, address=address)
        async with asyncio.timeout(ready_timeout_s):
            while link.server.local_transport is None:
                await asyncio.sleep(0.005)
    except BaseException:
        if app is not None:
            app.close()
        sock.close()
        raise
    return app
