from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from uc_hub.store import Store


class FakeClock:
    def __init__(self, now: float = 1_790_290_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Store]:
    s = Store(tmp_path / "hub.db", clock=clock)
    await s.open()
    try:
        yield s
    finally:
        await s.close()
