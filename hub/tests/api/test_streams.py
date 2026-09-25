"""The event streams over a real server: run events with replay, resume
and clamping, keep-alive comments, and live values with their watches."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from support.site import Hub

from uc_hub.api import create_app

from .conftest import run_state, serve, stream, wait_for


@pytest.fixture
async def http(hub: Hub) -> AsyncIterator[httpx.AsyncClient]:
    async with serve(create_app(hub.services, keepalive_s=0.2)) as url, httpx.AsyncClient(base_url=url) as client:
        yield client


async def finished_run(http: httpx.AsyncClient) -> dict[str, object]:
    run = (await http.post("/api/runs", json={"message": "What is the temperature in room 204?"})).json()
    return await run_state(http, run["id"], "idle")


async def test_run_events_replay_then_stream_live(http: httpx.AsyncClient, hub: Hub) -> None:
    run = await finished_run(http)
    last = int(run["last_seq"])  # type: ignore[call-overload]
    async with stream(http, f"/api/runs/{run['id']}/events") as events:
        assert events.response.headers["content-type"].startswith("text/event-stream")
        assert events.response.headers["cache-control"] == "no-store"
        replay = await events.take(last)
        assert [int(e.id or 0) for e in replay] == list(range(1, last + 1))
        for event in replay:
            data = json.loads(event.data)
            assert data["type"] == event.event and data["seq"] == int(event.id or 0) and data["run_id"] == run["id"]
        assert replay[0].event == "message.user" and replay[-1].event == "run.state"
        await http.post(f"/api/runs/{run['id']}/messages", json={"message": "help"})
        live = await events.take(2)
        assert [(e.event, int(e.id or 0)) for e in live] == [("message.user", last + 1), ("run.state", last + 2)]
        assert (await events.comment(timeout_s=2)).startswith(":")


async def test_resume_points(http: httpx.AsyncClient) -> None:
    run = await finished_run(http)
    run_id, last = run["id"], int(run["last_seq"])  # type: ignore[call-overload]
    async with stream(http, f"/api/runs/{run_id}/events", params={"after": 3}) as events:
        assert int((await events.next()).id or 0) == 4
    async with stream(http, f"/api/runs/{run_id}/events", params={"after": 2},
                      headers={"Last-Event-ID": "5"}) as events:
        assert int((await events.next()).id or 0) == 6
    async with stream(http, f"/api/runs/{run_id}/events", params={"after": 10_000}) as events:
        await http.post(f"/api/runs/{run_id}/messages", json={"message": "help"})
        assert int((await events.next()).id or 0) == last + 1
    for bad in ({"after": "x"}, {"after": -1}):
        async with stream(http, f"/api/runs/{run_id}/events", params=bad) as refused:
            assert refused.response.status_code == 400
    async with stream(http, "/api/runs/r_nope/events") as missing:
        assert missing.response.status_code == 404
        assert json.loads(await missing.response.aread())["error"]["code"] == "not_found"


async def test_live_values_start_with_the_cache_and_hold_a_watch(http: httpx.AsyncClient, hub: Hub) -> None:
    site = hub.site
    valve = hub.ref("r204-ctl/analog-output:1")
    await wait_for(lambda: _cached(hub, "r204-ctl/analog-input:1"))
    before = site.watch_count(valve)
    async with stream(http, "/api/live", params={"ids": "r204-ctl/analog-input:1,hq/r204-ctl/analog-output:1"}) as live:
        first = json.loads((await live.next()).data)
        assert first["id"] == "hq/r204-ctl/analog-input:1" and first["quality"] == "good"
        assert site.watch_count(valve) == before + 1
        hub.nodes["r204-ctl"].force("ai0", 650.0)
        async with asyncio.timeout(5):
            while True:
                reading = json.loads((await live.next()).data)
                if reading["id"] == "hq/r204-ctl/analog-input:1" and reading["value"] == pytest.approx(15.0):
                    break
    await wait_for(lambda: _released(site, valve, before))

    async with stream(http, "/api/live", params={"device": "r205-ctl"}) as device:
        seen = {json.loads((await device.next()).data)["id"] for _ in range(2)}
        assert all(i.startswith("hq/r205-ctl/") for i in seen)
    for params, status in (({}, 400), ({"device": "nope"}, 404), ({"ids": "hq/nope/x:1"}, 404),
                           ({"ids": "bad id"}, 400), ({"ids": ","}, 400)):
        async with stream(http, "/api/live", params=params) as refused:
            assert refused.response.status_code == status


async def _cached(hub: Hub, point: str) -> bool:
    return hub.site.latest(hub.ref(point)) is not None


async def _released(site: object, ref: object, count: int) -> bool:
    return site.watch_count(ref) == count  # type: ignore[attr-defined, no-any-return]
