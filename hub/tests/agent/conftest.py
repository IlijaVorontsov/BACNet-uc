"""Agent runs over a hub without devices: a scripted model and fake tools
of every tier, so each path of the loop can be driven exactly."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from support.site import Hub, start_hub

from uc_hub.core.errors import Conflict
from uc_hub.llm.base import LlmEvent, LlmProvider, ToolDef
from uc_hub.llm.scripted import ScriptedProvider
from uc_hub.tools import ApprovalInfo, Tool, ToolCallContext, ToolResult

EMPTY = {"type": "object", "additionalProperties": False, "properties": {}}
TEXT = {"type": "object", "additionalProperties": False, "required": ["text"],
        "properties": {"text": {"type": "string"}}}


def bare_site(ports: dict[str, int]) -> dict[str, Any]:
    return {
        "apiVersion": "bacnet-uc/v1",
        "kind": "Site",
        "metadata": {"name": "hq", "description": "Agent test site"},
        "spaces": [{"id": "hq", "name": "HQ"}],
        "policy": {"approval_ttl_s": 600},
    }


class Controls:
    """What the fake tools do; tests change it while a run is going."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.refuse = False
        self.calls: list[str] = []


async def _describe(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    if ctx.services.fake.refuse:
        raise Conflict("the switch moved in the meantime")
    return ApprovalInfo(title=f"Use {args.get('text', 'it')}", summary=[f"dev: use {args.get('text', 'it')}"],
                        targets=1)


def fake_tools(controls: Controls) -> list[Tool]:
    async def echo(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        controls.calls.append(f"echo:{args['text']}")
        return ToolResult(True, f"echo {args['text']}", {"text": args["text"]})

    async def fail(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        controls.calls.append("fail")
        return ToolResult(False, "it broke", error_code="device_error")

    async def big(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        return ToolResult(True, "many rows", {"rows": [f"row {i:04d} " + "x" * 40 for i in range(400)]})

    async def block(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        controls.calls.append("block")
        await controls.release.wait()
        return ToolResult(True, "unblocked")

    async def switch(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        controls.calls.append(f"switch:{args['text']}")
        return ToolResult(True, f"switched {args['text']}")

    async def commit(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
        controls.calls.append(f"commit:{args['text']}")
        return ToolResult(True, f"committed {args['text']}")

    return [
        Tool("echo", "Echo text.", "R", TEXT, echo),
        Tool("fail", "Always fails.", "R", EMPTY, fail),
        Tool("big", "A large result.", "R", EMPTY, big),
        Tool("block", "Waits until released.", "R", EMPTY, block, timeout_s=math.inf),
        Tool("switch", "A live change.", "L", TEXT, switch, describe_approval=_describe),
        Tool("commit", "A committed change.", "C", TEXT, commit, describe_approval=_describe),
    ]


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args}


def rules(*items: tuple[dict[str, Any], dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": f"r{i}", "when": when, "respond": respond} for i, (when, respond) in enumerate(items)]


class Recording(ScriptedProvider):
    """A scripted provider that keeps what each call was given."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[list[dict[str, Any]], list[ToolDef]]] = []

    def stream(self, messages: list[dict[str, Any]], tools: list[ToolDef], *, reasoning_effort: str | None = None,
               max_tokens: int | None = None) -> AsyncIterator[LlmEvent]:
        self.calls.append((messages, tools))
        return super().stream(messages, tools)


def scripted(items: list[dict[str, Any]]) -> Recording:
    base = ScriptedProvider.from_dict({"rules": items, "fallback": {"text": "no rule"}}, delay_s=0)
    return Recording(base.rules, base.fallback, delay_s=0, chunk_size=8)


AgentHub = Callable[..., Awaitable[Hub]]


@pytest.fixture
async def agent_hub(tmp_path: Path) -> AsyncIterator[AgentHub]:
    """``await agent_hub(rules, hub={...})``: a hub with the fake tools and a
    scripted model; ``hub.services.fake`` controls the tools."""
    hubs: list[Hub] = []

    async def make(items: list[dict[str, Any]] | LlmProvider, **options: Any) -> Hub:
        llm = items if isinstance(items, LlmProvider) else scripted(items)
        options.setdefault("sweep_interval_s", 0.05)
        hub = await start_hub(tmp_path / f"hub{len(hubs)}", nodes=(), doc=options.pop("doc", bare_site), llm=llm,
                              **options)
        hubs.append(hub)
        install_fakes(hub)
        return hub

    try:
        yield make
    finally:
        for hub in hubs:
            await hub.close()


def install_fakes(hub: Hub, controls: Controls | None = None) -> Controls:
    controls = controls or getattr(hub.services, "fake", None) or Controls()
    hub.services.fake = controls  # type: ignore[attr-defined]
    for tool in fake_tools(controls):
        if hub.services.registry.get(tool.name) is None:
            hub.services.registry.register(tool)
    return controls


async def until(check: Callable[[], Awaitable[Any]], timeout_s: float = 5.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        result = await check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what or check}")
        await asyncio.sleep(0.01)


async def state(hub: Hub, run_id: str, *states: str, timeout_s: float = 5.0) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        run = await hub.services.store.get_run(run_id)
        return run if run is not None and run["state"] in states else None

    return await until(check, timeout_s, f"run {run_id} in {states}")  # type: ignore[no-any-return]


async def events(hub: Hub, run_id: str, *types: str) -> list[dict[str, Any]]:
    found = await hub.services.store.list_events(run_id, limit=10_000)
    return [e for e in found if not types or e["type"] in types]


async def pending(hub: Hub, run_id: str) -> dict[str, Any]:
    async def check() -> dict[str, Any] | None:
        rows = await hub.services.store.list_approvals(state="pending", run_id=run_id)
        return rows[0] if rows else None

    return await until(check, what="a pending approval")  # type: ignore[no-any-return]
