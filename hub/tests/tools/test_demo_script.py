"""The offline demo script (llm/scripts/demo.yaml) against the real tools:
its tool names and arguments pass validation, and the results carry what
its rules look for (plan ids, ok flags, answers)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from support.site import Hub, start_hub

from uc_hub.llm.base import Completed
from uc_hub.llm.scripted import ScriptedProvider


def uncommissioned(doc: dict[str, Any]) -> None:
    del doc["placement"]["r204-ctl"]
    del doc["tags"]["r204-ctl/analog-input:1"]


async def converse(hub: Hub, text: str, run_id: str) -> tuple[list[str], str]:
    """A minimal agent loop: the model's tool calls go through the runner,
    approvals are granted by an admin."""
    provider = ScriptedProvider.demo(delay_s=0)
    tools = hub.services.registry.tool_defs()
    messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
    called: list[str] = []
    for _ in range(20):
        final = [e async for e in provider.stream(messages, tools)][-1]
        assert isinstance(final, Completed)
        messages.append({"role": "assistant", "content": final.text, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in final.tool_calls]})
        if not final.tool_calls:
            return called, final.text
        for call in final.tool_calls:
            ctx = hub.services.context("tech", {"admin"}, run_id=run_id, call_id=call.id)
            prepared = await hub.services.runner.prepare(ctx, call.name, call.arguments)
            assert prepared.action != "refused", (call.name, prepared.reason)
            if prepared.action == "approval":
                assert hub.services.runner.can_approve(prepared, {"admin"})
            result = await hub.services.runner.execute(
                prepared, approved_by="admin" if prepared.action == "approval" else None, approver_roles={"admin"})
            called.append(f"{call.name}:{'ok' if result.ok else 'failed'}")
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result.to_model_text()})
    raise AssertionError("the conversation did not end")


async def test_commission_room_204(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, edit=uncommissioned)
    try:
        run = await hub.services.store.create_run(title="demo", created_by="tech")
        called, text = await converse(hub, "Commission room 204", run["id"])
        assert called == ["site_search:ok", "device_describe:ok", "point_read:ok", "manifest_get:ok",
                          "manifest_edit:ok", "plan:ok", "apply:ok", "test_run:ok"]
        assert text.startswith("Room 204 is commissioned: 2 of 2 tests passed")
        manifests = hub.services.manifests
        assert manifests.live_revision == 2 and manifests.live.placement["r204-ctl"] == "r204"
        temp = await hub.site.point(hub.ref("r204-ctl/analog-input:1"))
        assert temp.tags == ["Zone_Air_Temperature_Sensor", "space:r204"] and temp.space == "r204"
        events = [e["type"] for e in await hub.services.store.list_events(run["id"])]
        assert events == ["plan.updated", "plan.updated", "plan.updated", "tests.updated"]
    finally:
        await hub.close()


async def test_a_failed_test_leads_to_a_checkout_question(tmp_path: Path) -> None:
    def failing(doc: dict[str, Any]) -> None:
        uncommissioned(doc)
        doc["system"]["tests"][1]["steps"][1]["expect"].update(value=30.0, within_ms=100)

    hub = await start_hub(tmp_path, edit=failing)
    try:
        run = await hub.services.store.create_run(title="demo", created_by="tech")

        async def answer() -> None:
            while True:
                pending = await hub.services.store.list_questions(run["id"], pending_only=True)
                if pending:
                    await hub.services.questions.answer(pending[0]["id"], "Yes", user="tech", run_id=run["id"])
                    return
                await asyncio.sleep(0.02)

        answering = asyncio.create_task(answer())
        called, text = await converse(hub, "Commission room 204", run["id"])
        await answering
        assert called[-4:] == ["apply:ok", "test_run:failed", "io_force:ok", "ask_user:ok"]
        assert "the wiring and the valve work" in text
        (lease,) = hub.services.leases.active()
        assert lease.target == {"node": "r204-ctl", "channel": "ai0"}
        assert round(lease.expires_at - lease.created_at) == 120
        (question,) = await hub.services.store.list_questions(run["id"])
        assert (question["options"], question["answer"]) == (["Yes", "No"], "Yes")
    finally:
        await hub.close()
