"""The HTTP server of ``uc-hub serve``, ``mcp`` and ``demo``: uvicorn, with
stop signals that let the hub shut down in order."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import Iterator

import uvicorn

_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class HubServer(uvicorn.Server):
    """A uvicorn server whose SIGINT and SIGTERM only ask it to exit.

    uvicorn raises a stop signal again once it has shut down, and SIGTERM
    (how systemd stops a service) then ends the process at once: the hub
    would never release the agent's leases or stop its drivers, and the
    demo would leave its broker and working directory behind. Here
    ``serve()`` just returns and the caller shuts down. A second Ctrl-C
    while it does forces the exit, as with plain uvicorn.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        loop = asyncio.get_running_loop()
        for sig in _STOP_SIGNALS:
            loop.add_signal_handler(sig, self.handle_exit, sig, None)
        try:
            yield
        finally:
            for sig in _STOP_SIGNALS:
                loop.remove_signal_handler(sig)
