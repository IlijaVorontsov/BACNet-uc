from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from support.site import Hub, start_hub


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = await start_hub(tmp_path)
    try:
        yield h
    finally:
        await h.close()
