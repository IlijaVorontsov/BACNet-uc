"""SMP (MCUmgr, UDP 1337) to the BACnet DUT through the product team's own client.

The rig does not write a third SMP client: it reuses ``bacnet_uc_harness.smp`` from the
BACnet branch's ``harness/`` (HIL.md 3.2; FW-06 covers its API). The package is found

1. installed (``pip install --no-deps -e $FW/harness`` plus ``cbor2``, its only runtime need
   for SMP), or
2. from a checkout: ``$HIL_BACNET_HARNESS`` names ``harness/`` (or ``harness/src``) of a
   firmware worktree and is put on ``sys.path``.

:func:`harness_problem` says why neither works, so fixtures skip with that reason.

The client is asyncio-based; :class:`Smp` runs its event loop in a thread created inside the
rig namespace, so its UDP socket lives in netns svc like every other rig client.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import sys
import threading
import time
from collections.abc import Coroutine
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

from hilrig import netns as nsmod

HARNESS_ENV = "HIL_BACNET_HARNESS"
SMP_PORT = 1337

T = TypeVar("T")


class SmpUnavailable(RuntimeError):
    """The BACnet harness SMP client cannot be imported (the message says why)."""


def _candidates() -> list[Path]:
    root = os.environ.get(HARNESS_ENV)
    if not root:
        return []
    path = Path(root)
    return [path / "src", path] if (path / "src" / "bacnet_uc_harness").is_dir() else [path]


def load_client() -> ModuleType:
    """Import ``bacnet_uc_harness.smp`` (installed, or from ``$HIL_BACNET_HARNESS``)."""
    try:
        return importlib.import_module("bacnet_uc_harness.smp")
    except ImportError as first:
        missing = first.name
        for path in _candidates():
            if (path / "bacnet_uc_harness").is_dir() and str(path) not in sys.path:
                sys.path.insert(0, str(path))
                try:
                    return importlib.import_module("bacnet_uc_harness.smp")
                except ImportError as e:
                    missing = e.name
        where = os.environ.get(HARNESS_ENV)
        hint = f"${HARNESS_ENV}={where}" if where else f"${HARNESS_ENV} is not set"
        raise SmpUnavailable(
            f"the BACnet harness SMP client is unavailable (missing module {missing!r}; {hint}): "
            "pip install cbor2 and point HIL_BACNET_HARNESS at the firmware checkout's harness/"
        ) from None


def harness_problem() -> str | None:
    """None when the SMP client imports, else the reason (for pytest.skip)."""
    try:
        load_client()
    except SmpUnavailable as e:
        return str(e)
    return None


class Smp:
    """A synchronous SMP client for ``host`` (the DUT) with its socket in ``netns``.

    Methods wrap the harness ``SmpClient`` coroutines of the same name; an SMP error rc
    raises the harness's ``SmpError`` and a lost exchange its ``HarnessTimeout``.
    """

    def __init__(
        self,
        host: str,
        port: int = SMP_PORT,
        *,
        netns: str | None = None,
        timeout: float = 3.0,
        retries: int = 2,
    ) -> None:
        mod = load_client()
        self.host, self.port, self.netns = host, port, netns
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.call_soon(ready.set)
            self._loop.run_forever()

        with nsmod.enter(netns) if netns else contextlib.nullcontext():
            self._thread = threading.Thread(target=run, name=f"smp-{host}", daemon=True)
            self._thread.start()  # created inside the netns: the UDP socket is opened there
        ready.wait(5)
        self.client: Any = self._run(mod.connect_udp(host, port, timeout=timeout))
        self.client.retries = retries

    def _run(self, coro: Coroutine[Any, Any, T], timeout: float = 120.0) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Run ``SmpClient.<name>(*args, **kwargs)`` and return its result."""
        return self._run(getattr(self.client, name)(*args, **kwargs))

    def close(self) -> None:
        """Close the socket and stop the event loop thread (idempotent)."""
        if self._loop.is_closed():
            return
        with contextlib.suppress(Exception):
            self._run(self.client.close(), timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> Smp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- the calls the rig uses -----------------------------------------------------------
    def echo(self, text: str = "hil") -> str:
        """OS echo: the cheapest liveness probe."""
        result: str = self.call("os_echo", text)
        return result

    def node_info(self) -> dict[str, Any]:
        """``uc_node info``: fw, board, device, net, uptime_s, fs, ..."""
        info: dict[str, Any] = self.call("node_info")
        return info

    def io_read(self, name: str | None = None) -> dict[str, float]:
        """``uc_io read``: logical channel values (di/do 0 or 1, ai mV, ao %)."""
        values: dict[str, float] = self.call("io_read", name)
        return values

    def io_force(self, name: str, value: float) -> None:
        """``uc_io force``: override a channel until :meth:`io_release`."""
        self.call("io_force", name, value)

    def io_release(self, name: str) -> None:
        """``uc_io force`` with ``release``."""
        self.call("io_release", name)

    def upload(self, path: str, data: bytes) -> None:
        """FS upload (chunked)."""
        self.call("fs_upload", path, data)

    def sha256(self, path: str) -> bytes | None:
        """FS hash of ``path`` (None if the node has no such file)."""
        try:
            digest: bytes = self.call("fs_hash", path, "sha256")
        except Exception as e:  # SmpError(FILE_NOT_FOUND) from the harness
            if getattr(e, "rc_name", "") in ("NOT_FOUND", "FILE_NOT_FOUND", "ENOENT"):
                return None
            raise
        return digest

    def reload(self, doc: str = "all", timeout: float = 15.0) -> bool:
        """``uc_node reload``; returns ``reboot_required``.

        Applying documents takes the node a while (io re-creates objects), so the reply gets
        ``timeout`` before a retry, which would apply the documents a second time.
        """
        saved = self.client.timeout
        self.client.timeout = max(saved, timeout)
        try:
            required: bool = self.call("node_reload", doc)
        finally:
            self.client.timeout = saved
        return required

    def prop_read(self, obj_type: str, instance: int, prop: str) -> Any:
        """``uc_node prop_read``: a property of a local object, read through SMP."""
        return self.call("prop_read", obj_type, instance, prop)

    def reset(self) -> None:
        """OS reset (the node reboots; the reply may be lost)."""
        with contextlib.suppress(TimeoutError):
            self.call("os_reset")

    def wait_up(self, timeout: float = 60.0) -> float:
        """Poll OS echo until the node answers; return the seconds it took."""
        start = time.monotonic()
        while True:
            try:
                self.echo()
                return time.monotonic() - start
            except Exception:  # timeouts while the node reboots
                if time.monotonic() - start > timeout:
                    raise
                time.sleep(0.5)
