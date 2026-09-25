"""Bearer tokens, dev mode and the role checks of the API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from support.site import Hub, start_hub

from uc_hub.api import create_app
from uc_hub.core.errors import ValidationFailed
from uc_hub.llm.scripted import ScriptedProvider

from .conftest import TOKENS, auth, run_state, serve, stream

ADMIN, OPS, VIEW = auth("t-admin"), auth("t-ops"), auth("t-view")


@pytest.fixture
async def secured(tmp_path: Path) -> AsyncIterator[Hub]:
    h = await start_hub(tmp_path, hub=TOKENS, llm=ScriptedProvider.demo(delay_s=0))
    try:
        yield h
    finally:
        await h.close()


@pytest.fixture
async def api(secured: Hub) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(secured.services)),
                                 base_url="http://hub") as client:
        yield client


async def test_requests_need_a_valid_token(api: httpx.AsyncClient) -> None:
    for headers in ({}, auth("wrong"), {"Authorization": "Basic dC1hZG1pbg=="}, {"Authorization": "Bearer "}):
        response = await api.get("/api/site", headers=headers)
        assert response.status_code == 401 and response.json()["error"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"
    assert (await api.get("/api/site", params={"access_token": "t-admin"})).status_code == 401
    health = await api.get("/api/health")
    assert health.status_code == 200 and health.json()["dev_mode"] is False
    assert (await api.get("/api/me", headers=OPS)).json() == {"user": "tech1", "roles": ["operator"]}
    assert (await api.get("/api/me", headers={"Authorization": "bearer t-view"})).json()["user"] == "guest"


async def test_roles(api: httpx.AsyncClient, secured: Hub) -> None:
    assert (await api.post("/api/devices/r204-ctl/identify", json={}, headers=VIEW)).status_code == 403
    assert (await api.post("/api/devices/r204-ctl/identify", json={}, headers=OPS)).status_code == 200
    assert (await api.get("/api/audit", headers=OPS)).status_code == 403
    entries = (await api.get("/api/audit", headers=ADMIN)).json()["entries"]
    assert entries[0]["user"] == "tech1" and entries[0]["tool"] == "device_identify"

    run = (await api.post("/api/runs", json={"message": "Run IO checkout for r204-ctl."}, headers=OPS)).json()
    assert run["created_by"] == "tech1"
    await run_state(api, run["id"], "waiting_approval", headers=OPS)
    (approval,) = (await api.get("/api/approvals", params={"state": "pending"}, headers=VIEW)).json()["approvals"]
    denied = await api.post(f"/api/approvals/{approval['id']}", json={"decision": "approve"}, headers=VIEW)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "denied"
    assert (await api.post(f"/api/runs/{run['id']}/cancel", headers=VIEW)).status_code == 403
    decided = await api.post(f"/api/approvals/{approval['id']}", json={"decision": "approve"}, headers=OPS)
    assert decided.json()["decided_by"] == "tech1"
    viewer_run = (await api.post("/api/runs", json={"message": "help"}, headers=VIEW)).json()
    assert viewer_run["created_by"] == "guest"
    assert (await api.post(f"/api/runs/{run['id']}/cancel", headers=OPS)).json() == {}


async def test_streams_take_the_token_from_the_query(secured: Hub) -> None:
    app = create_app(secured.services, keepalive_s=0.2)
    async with serve(app) as url, httpx.AsyncClient(base_url=url) as client:
        async with stream(client, "/api/live", params={"ids": "r204-ctl/analog-input:1"}) as refused:
            assert refused.response.status_code == 401
        async with stream(client, "/api/live", params={"ids": "r204-ctl/analog-input:1",
                                                        "access_token": "t-view"}) as live:
            assert live.response.status_code == 200
            assert (await live.next()).event == "reading"


async def test_a_token_from_a_missing_environment_variable_fails_the_start(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, hub={"auth": {"tokens": [{"user": "x", "roles": ["admin"],
                                                              "token_env": "UC_HUB_TEST_NO_SUCH_TOKEN"}]}})
    try:
        with pytest.raises(ValidationFailed, match="invalid auth tokens"):
            create_app(hub.services)
    finally:
        await hub.close()
