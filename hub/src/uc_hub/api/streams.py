"""The event streams (SSE): a run's events and live point values.

Run events are replayed from the store after ``?after=<seq>`` or the
``Last-Event-ID`` header (the header wins when both are given, as a
reconnecting ``EventSource`` sends it), then streamed live, each with
``id: <seq>``. A resume point past the run's last event is clamped to it,
so a client that remembers a larger number (another database, a typo)
still gets every new event. Live values start with the cached readings;
the stream holds a watch on its points (reference counted, released when
the client goes away). Both send keep-alive comments.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import APIRouter, Depends, Request
from sse_starlette import EventSourceResponse, ServerSentEvent

from ..core.errors import InvalidRequest, NotFound
from ..core.ids import PointRef
from ..runtime.config import Identity
from ..runtime.services import Services
from ..store.db import dumps
from .auth import Auth

MAX_LIVE_POINTS = 1000
#: How long a closing live stream may take to release its watches.
RELEASE_TIMEOUT_S = 10.0


def stream_routes(services: Services, auth: Auth, *, keepalive_s: float) -> APIRouter:
    router = APIRouter(prefix="/api")
    caller = Depends(auth.stream_caller)

    @router.get("/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, after: str | None = None,
                         who: Identity = caller) -> EventSourceResponse:
        run = await services.store.get_run(run_id)
        if run is None:
            raise NotFound(f"no run {run_id}")
        header = request.headers.get("last-event-id", "").strip()
        start = min(_seq(header, "Last-Event-ID") if header else _seq(after, "after"), run["last_seq"])

        async def events() -> AsyncIterator[ServerSentEvent]:
            async with services.events.subscribe(run_id, start) as subscription:
                async for event in subscription:
                    yield ServerSentEvent(data=dumps(event), event=event["type"], id=str(event["seq"]))

        return EventSourceResponse(events(), ping=keepalive_s)

    @router.get("/live")
    async def live(ids: str | None = None, device: str | None = None,
                   who: Identity = caller) -> EventSourceResponse:
        site = services.site
        devices: list[str] | None = None
        if ids:
            refs = list(dict.fromkeys(site.point_ref(text) for text in ids.split(",") if text.strip()))
            for ref in refs:
                site.device(ref.device)
        elif device:
            site.device(device)
            devices = [device]
            refs = [p.ref for p in site.points(device)]
        else:
            refs = []
        if not refs and devices is None:
            raise InvalidRequest("give ids=<point ids> or device=<name>")
        if len(refs) > MAX_LIVE_POINTS:
            raise InvalidRequest(f"at most {MAX_LIVE_POINTS} points per stream")

        async def readings() -> AsyncIterator[ServerSentEvent]:
            subscription = services.live.subscribe(points=None if devices else refs, devices=devices)
            watched: list[PointRef] = []
            try:
                await site.ensure_watched(refs)
                watched = refs
                async for reading in subscription:
                    yield ServerSentEvent(data=dumps(reading.to_json()), event="reading")
            finally:
                await subscription.aclose()
                if watched:
                    # A closed stream is cancelled; the release must still happen.
                    with anyio.move_on_after(RELEASE_TIMEOUT_S, shield=True):
                        await site.release_watched(watched)

        return EventSourceResponse(readings(), ping=keepalive_s)

    return router


def _seq(value: Any, what: str) -> int:
    if value is None or value == "":
        return 0
    try:
        seq = int(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{what} must be an event number") from None
    if seq < 0:
        raise InvalidRequest(f"{what} must not be negative")
    return seq
