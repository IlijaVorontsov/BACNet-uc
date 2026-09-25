from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from uc_hub.store import Store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[Store]:
    s = Store(tmp_path / "hub.db")
    await s.open()
    try:
        yield s
    finally:
        await s.close()
