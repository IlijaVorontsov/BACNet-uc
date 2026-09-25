"""The MCP server: the tool registry over MCP, calls as the configured
identity in a session run, approvals decided by a person in the meantime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from support.site import Hub, start_hub

from uc_hub.mcp_server import HubMcpServer


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = await start_hub(tmp_path, hub={"mcp": {"user": "claude", "roles": ["operator"], "approval_wait_s": 30}})
    try:
        yield h
    finally:
        await h.close()


def body(result: Any) -> dict[str, Any]:
    return json.loads(result.content[0].text)  # type: ignore[no-any-return]


async def approve_next(hub: Hub, user: str = "boss") -> dict[str, Any]:
    while True:
        rows = await hub.services.store.list_approvals(state="pending")
        if rows:
            return await hub.services.approvals.decide(rows[0]["id"], "approve", user=user, roles={"admin"})
        await asyncio.sleep(0.02)


async def test_tools_are_listed_for_the_configured_roles(hub: Hub) -> None:
    async with Client(HubMcpServer(hub.services)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert {"site_search", "point_read", "manifest_edit", "plan", "apply", "point_write", "io_force"} <= set(tools)
    assert "ask_user" not in tools
    assert tools["site_search"].annotations.read_only_hint is True  # type: ignore[union-attr]
    assert tools["apply"].annotations.destructive_hint is True  # type: ignore[union-attr]
    assert tools["point_write"].description.startswith("[tier L] ")  # type: ignore[union-attr]
    assert "approval in the hub's web app" in tools["apply"].description  # type: ignore[operator]
    assert tools["point_read"].input_schema["required"] == ["points"]

    async with Client(HubMcpServer(hub.services, roles=["viewer"])) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names and all(hub.services.registry.get(n).tier == "R" for n in names)  # type: ignore[union-attr]


async def test_calls_run_in_a_session_and_wait_for_approvals(hub: Hub) -> None:
    server = HubMcpServer(hub.services)
    async with Client(server) as client:
        read = await client.call_tool("point_read", {"points": ["r204-ctl/analog-input:1"]})
        assert not read.is_error and body(read)["ok"] is True
        deciding = asyncio.create_task(approve_next(hub))
        write = await client.call_tool("point_write", {"point": "r204-ctl/binary-output:1", "value": 1,
                                                       "lease_s": 30})
        decided = await deciding
        assert not write.is_error and body(write)["summary"].startswith("wrote 1 to r204-ctl/binary-output:1")
        wrong = await client.call_tool("point_read", {"points": "not a list"})
        assert wrong.is_error and body(wrong)["error"] == "invalid"

    session = await server.session()
    run = await hub.services.store.get_run(session)
    assert run is not None and (run["title"], run["created_by"], run["state"]) == ("MCP session (claude)", "claude",
                                                                                   "idle")
    assert decided["run_id"] == session and decided["requested_by"] == "claude"
    types = [e["type"] for e in await hub.services.store.list_events(session)]
    assert types.count("tool.call") == 3 and types.count("tool.result") == 3
    assert "approval.request" in types and "approval.decided" in types
    audit = await hub.services.store.list_audit(run_id=session)
    assert {a["user"] for a in audit if a["action"] == "tool"} == {"claude"}
    (lease,) = hub.services.leases.active(session)
    assert lease.created_by == "claude"


async def test_an_undecided_approval_expires_when_the_wait_ends(hub: Hub) -> None:
    async with Client(HubMcpServer(hub.services, approval_wait_s=0.2)) as client:
        result = await client.call_tool("io_force", {"node": "r204-ctl", "channel": "ai0", "value": 650})
    assert result.is_error
    assert body(result)["error"] == "expired" and "while the MCP client waited" in body(result)["summary"]
    (approval,) = await hub.services.store.list_approvals()
    assert approval["state"] == "expired"
    assert hub.nodes["r204-ctl"].channels["ai0"].forced is None


async def test_a_cancelled_session_takes_no_more_calls(hub: Hub) -> None:
    server = HubMcpServer(hub.services)
    async with Client(server) as client:
        assert not (await client.call_tool("site_tree", {})).is_error
        await hub.services.runs.cancel(await server.session(), user="boss", roles={"admin"})
        refused = await client.call_tool("site_tree", {})
    assert refused.is_error and refused.content[0].text.startswith("conflict: a user cancelled this MCP session")


async def test_policy_refusals_come_back_as_errors(hub: Hub) -> None:
    async with Client(HubMcpServer(hub.services, roles=["viewer"])) as client:
        denied = await client.call_tool("manifest_edit", {"json_patch": [{"op": "remove", "path": "/tags"}]})
        asked = await client.call_tool("ask_user", {"question": "Open?"})
    assert denied.is_error and body(denied)["error"] == "denied"
    assert asked.is_error and "not available over MCP" in body(asked)["summary"]
