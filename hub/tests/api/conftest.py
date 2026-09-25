"""The HTTP API over a hub with simulated nodes and the demo script: an
in-process client for JSON endpoints and a real server for the streams."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus
from support.site import Hub, start_hub

from uc_hub.api import create_app
from uc_hub.llm.scripted import ScriptedProvider

TOKENS = {"auth": {"tokens": [
    {"user": "ilija", "roles": ["admin"], "token": "t-admin"},
    {"user": "tech1", "roles": ["operator"], "token": "t-ops"},
    {"user": "guest", "roles": ["viewer"], "token": "t-view"},
]}}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = await start_hub(tmp_path, llm=ScriptedProvider.demo(delay_s=0), sweep_interval_s=0.1)
    try:
        yield h
    finally:
        await h.close()


@pytest.fixture
async def api(hub: Hub) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(hub.services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
        yield client


@asynccontextmanager
async def serve(app: Any) -> AsyncIterator[str]:
    """A real uvicorn server on a free port, for the event streams."""
    AppStatus.should_exit = False
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, lifespan="off",
                                           timeout_graceful_shutdown=1))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        AppStatus.should_exit = False


@dataclass
class SseEvent:
    event: str = "message"
    data: str = ""
    id: str | None = None


@dataclass
class SseReader:
    """Events and comments of one open stream."""

    response: httpx.Response
    comments: list[str] = field(default_factory=list)
    _lines: Any = None

    async def next(self, timeout_s: float = 5.0) -> SseEvent:
        if self._lines is None:
            self._lines = self.response.aiter_lines()
        event = SseEvent()
        async with asyncio.timeout(timeout_s):
            async for line in self._lines:
                if line == "":
                    if event.data or event.event != "message":
                        return event
                    continue
                if line.startswith(":"):
                    self.comments.append(line)
                    continue
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "event":
                    event.event = value
                elif name == "data":
                    event.data += value
                elif name == "id":
                    event.id = value
        raise AssertionError("the stream ended")

    async def comment(self, timeout_s: float = 5.0) -> str:
        """The next comment line (a keep-alive), skipping events."""
        if self._lines is None:
            self._lines = self.response.aiter_lines()
        async with asyncio.timeout(timeout_s):
            async for line in self._lines:
                if line.startswith(":"):
                    self.comments.append(line)
                    return line
        raise AssertionError("the stream ended")

    async def take(self, count: int, timeout_s: float = 5.0) -> list[SseEvent]:
        return [await self.next(timeout_s) for _ in range(count)]


@asynccontextmanager
async def stream(client: httpx.AsyncClient, path: str, **kwargs: Any) -> AsyncIterator[SseReader]:
    async with client.stream("GET", path, **kwargs) as response:
        yield SseReader(response)


async def wait_for(check: Callable[[], Any], timeout_s: float = 10.0) -> Any:
    async with asyncio.timeout(timeout_s):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.02)


async def run_state(api: httpx.AsyncClient, run_id: str, *states: str, headers: dict[str, str] | None = None,
                    timeout_s: float = 10.0) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        run = (await api.get(f"/api/runs/{run_id}", headers=headers)).json()
        return run if run["state"] in states else None

    return await wait_for(check, timeout_s)  # type: ignore[no-any-return]
