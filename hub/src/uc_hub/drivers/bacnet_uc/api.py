"""Contract for talking to one BACnet-uc node over SMP.

``NodeApi`` mirrors ``docs/management-protocol.md`` (BACnet branch): OS group
(0), FS group (8) and the custom groups ``uc_app`` (64), ``uc_io`` (65) and
``uc_node`` (66). ``client.SmpNodeClient`` implements it over UDP;
``sim.smp_node`` serves the same protocol for tests and demos; the manifest
engine and the driver only depend on this interface.

Every method raises ``core.errors.DeviceError`` (with ``rc`` and ``group``) when
the node answers with an error, ``DeviceTimeout`` when it does not answer after
the configured retries, and ``Unsupported`` for rc 11 (UNSUPPORTED) or an
SMP "not supported" (legacy rc 8).
"""

from __future__ import annotations

import abc
from typing import Any

GROUP_OS = 0
GROUP_IMAGE = 1
GROUP_STAT = 2
GROUP_SETTINGS = 3
GROUP_FS = 8
GROUP_SHELL = 9
GROUP_UC_APP = 64
GROUP_UC_IO = 65
GROUP_UC_NODE = 66

# uc_app
APP_LIST, APP_INSTALL, APP_START, APP_STOP, APP_REMOVE, APP_STATUS = 0, 1, 2, 3, 4, 5
# uc_io
IO_CATALOG, IO_READ, IO_WRITE, IO_FORCE = 0, 1, 2, 3
# uc_node
NODE_INFO, NODE_RELOAD, NODE_OBJECTS, NODE_PROP_READ, NODE_PROP_WRITE = 0, 1, 2, 3, 4
#: Requested from the firmware session (docs/SESSION_NOTES.md, B1); nodes
#: without it answer UNSUPPORTED or "not supported".
NODE_IDENTIFY = 5

# Group rc values shared by the custom groups.
RC_NAMES = {
    0: "OK",
    1: "UNKNOWN",
    2: "INVALID",
    3: "NOT_FOUND",
    4: "EXISTS",
    5: "BUSY",
    6: "NO_MEM",
    7: "IO",
    8: "STATE",
    9: "VERIFY",
    10: "PERM",
    11: "UNSUPPORTED",
    12: "LIMIT",
}

CFG_DIR = "/lfs/cfg"
APPS_DIR = "/lfs/apps"


class NodeApi(abc.ABC):
    """One BACnet-uc node. Implementations are safe to call concurrently
    (requests are serialised or matched by sequence number)."""

    address: str

    # -- OS / FS --------------------------------------------------------------
    @abc.abstractmethod
    async def echo(self, text: str) -> str: ...

    @abc.abstractmethod
    async def reset(self) -> None: ...

    @abc.abstractmethod
    async def file_upload(self, path: str, data: bytes) -> None:
        """FS group upload in chunks sized to the node's MTU (``off``/``data``/
        ``len``/``name``), then verify with the FS ``hash`` command (sha256)
        when the node supports it."""

    @abc.abstractmethod
    async def file_download(self, path: str) -> bytes:
        """Raises ``NotFound`` when the file does not exist (rc ENOENT)."""

    @abc.abstractmethod
    async def file_sha256(self, path: str) -> bytes | None:
        """sha256 of a file on the node, None when the file does not exist."""

    # -- uc_node ----------------------------------------------------------------
    @abc.abstractmethod
    async def info(self) -> dict[str, Any]:
        """``<node info>`` map."""

    @abc.abstractmethod
    async def reload(self, doc: str) -> bool:
        """``doc`` in device|io|apps|all. Returns ``reboot_required``."""

    @abc.abstractmethod
    async def objects(self) -> list[dict[str, Any]]:
        """All ``<object>`` maps, paging through ``offset``/``count``."""

    @abc.abstractmethod
    async def prop_read(
        self, obj_type: str | int, instance: int, prop: str | int = "present-value",
        index: int | None = None,
    ) -> Any: ...

    @abc.abstractmethod
    async def prop_write(
        self, obj_type: str | int, instance: int, value: Any,
        prop: str | int = "present-value", priority: int | None = None,
        index: int | None = None, lease_ms: int | None = None,
    ) -> None:
        """``value`` None writes NULL (relinquish when ``priority`` is set).
        ``lease_ms`` is sent only when not None (firmware request B2)."""

    @abc.abstractmethod
    async def identify(self, seconds: int = 30) -> None: ...

    # -- uc_io ------------------------------------------------------------------
    @abc.abstractmethod
    async def io_catalog(self) -> dict[str, Any]:
        """``{"board": ..., "channels": [<channel>...]}``"""

    @abc.abstractmethod
    async def io_read(self, name: str | None = None) -> dict[str, float]: ...

    @abc.abstractmethod
    async def io_write(self, name: str, value: float) -> None: ...

    @abc.abstractmethod
    async def io_force(
        self, name: str, value: float | None, lease_ms: int | None = None
    ) -> None:
        """``value`` None releases the force."""

    # -- uc_app -----------------------------------------------------------------
    @abc.abstractmethod
    async def app_list(self) -> list[dict[str, Any]]: ...

    @abc.abstractmethod
    async def app_install(self, manifest: dict[str, Any]) -> None:
        """``<app manifest>``; ``sha256`` is bytes (32) on the wire."""

    @abc.abstractmethod
    async def app_start(self, name: str) -> None: ...

    @abc.abstractmethod
    async def app_stop(self, name: str) -> None: ...

    @abc.abstractmethod
    async def app_remove(self, name: str, delete_file: bool = False) -> None: ...

    @abc.abstractmethod
    async def app_status(self, name: str) -> dict[str, Any]: ...

    async def close(self) -> None:
        return None
