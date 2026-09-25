"""Hold mode (DESIGN.md §6.3, §14.3).

While ``/etc/uc-controller/hold`` exists the BMS zone of the firewall only
drops, and uc-hub does not start (ConditionPathExists=! in its unit). Restore
and drills set it; ``uc-ctl bms up`` clears it.
"""

from __future__ import annotations

import os
import time

from . import paths as _paths


def _path(p: _paths.Paths | None) -> str:
    return (p or _paths.current()).hold


def is_held(p: _paths.Paths | None = None) -> bool:
    return os.path.exists(_path(p))


def set(reason: str = "", p: _paths.Paths | None = None) -> None:  # noqa: A001 (interface name)
    path = _path(p)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(path, "w") as fh:
        fh.write(f"{stamp} {reason}".rstrip() + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def clear(p: _paths.Paths | None = None) -> bool:
    """Remove the hold marker. Returns True if it existed."""
    try:
        os.unlink(_path(p))
        return True
    except FileNotFoundError:
        return False


def reason(p: _paths.Paths | None = None) -> str | None:
    try:
        with open(_path(p)) as fh:
            return fh.read().strip()
    except FileNotFoundError:
        return None
