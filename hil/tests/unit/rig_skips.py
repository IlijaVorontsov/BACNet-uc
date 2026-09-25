"""Skip marks shared by the rig's module tests (tests/unit is on pytest's pythonpath)."""

from __future__ import annotations

import os
import shutil

import pytest

needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root: network namespaces (CAP_NET_ADMIN, CAP_SYS_ADMIN)"
)


def needs_tools(*tools: str) -> pytest.MarkDecorator:
    """Skip unless every program in ``tools`` is on PATH."""
    missing = [t for t in tools if shutil.which(t) is None]
    return pytest.mark.skipif(bool(missing), reason=f"not installed: {', '.join(missing)}")
