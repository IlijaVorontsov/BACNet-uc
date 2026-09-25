"""Minimal sd_notify(3) client (stdlib only).

``notify("READY=1")`` sends a datagram to ``$NOTIFY_SOCKET``. A socket name
starting with ``@`` is an abstract socket. Without NOTIFY_SOCKET (not started
by systemd) the call does nothing and returns False.
"""

from __future__ import annotations

import os
import socket


def notify(msg: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace
    elif not addr.startswith("/"):
        return False  # vsock addresses are not used here
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as sock:
        try:
            sock.connect(addr)
            sock.sendall(msg.encode())
        except OSError:
            return False
    return True


def ready() -> bool:
    return notify("READY=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def status(text: str) -> bool:
    return notify("STATUS=" + text.replace("\n", " "))
