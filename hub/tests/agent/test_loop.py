"""The agent loop: streaming into run events, tool calls, budgets, stops
and errors of the model, cancel, and runs that outlive a hub restart."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from uc_hub.agent.loop import ACTIVE_STATES
from uc_hub.agent.prompts import SYSTEM_PROMPT
from uc_hub.core.errors import Conflict, InvalidRequest, NotFound, PolicyDenied
from uc_hub.llm.base import (
    Completed,
    LlmError,
    LlmProvider,
    TextDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallStart,
    ToolDef,
)

from .conftest import AgentHub, call, events, install_fakes, pending, rules, scripted, state, until

ADMIN = {"admin"}


async def start(hub: Any, message: str, *, user: str = "dev", roles: set[str] = ADMIN, **kw: Any) -> str:
    run = await hub.services.runs.create_run(user=user, roles=roles, message=message, **kw)
    return str(run["id"])


async def test_a_turn_streams_into_events_and_history(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"thinking": "I should echo the text first.", "text": "Echoing now.",
                       "tool_calls": [call("echo", text="hello")]}),
        ({"tool": "echo", "ok": True}, {"text": "Echoed: ${summary}"}),
    ))
    run_id = await start(hub, "Echo hello. Then stop.")
    await state(hub, run_id, "idle")
    types = [e["type"] for e in await events(hub, run_id)]
    assert types[:2] == ["message.user", "run.state"]
    assert types[-1] == "run.state"
    assert "thinking.delta" in types and "message.delta" in types
    call_events = [e for e in await events(hub, run_id, "tool.call", "tool.args.delta", "tool.result")]
    assert [e["type"] for e in call_events] == ["tool.call", "tool.args.delta", "tool.call", "tool.result"]
    first, delta, full, result = call_events
    assert first["args"] == {} and first["tier"] == "R" and first["tool"] == "echo"
    assert json.loads(delta["delta"]) == {"text": "hello"}
    assert full["args"] == {"text": "hello"} and full["call_id"] == first["call_id"] == result["call_id"]
    assert result["ok"] is True and result["summary"] == "echo hello" and result["data"] == {"text": "hello"}
    assert "handle" not in result and result["duration_ms"] >= 0
    done = [e["text"] for e in await events(hub, run_id, "message.done")]
    assert done == ["Echoing now.", "Echoed: echo hello"]
    thinking = "".join(e["text"] for e in await events(hub, run_id, "thinking.delta"))
    assert thinking == "I should echo the text first."

    run = await hub.services.store.get_run(run_id)
    assert run is not None and run["title"] == "Echo hello" and run["model"] == "scripted"
    history = await hub.services.store.load_messages(run_id)
    assert [m["role"] for m in history] == ["system", "user", "assistant", "tool", "assistant"]
    assert history[0]["content"].startswith("Site status at ")
    assert history[2]["tool_calls"][0]["function"] == {"name": "echo", "arguments": '{"text": "hello"}'}
    assert history[2]["reasoning_content"] == "I should echo the text first."
    assert json.loads(history[3]["content"])["summary"] == "echo hello"


async def test_the_model_sees_the_prompt_the_playbook_and_its_tools(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(({"always": True}, {"text": "ok"})))
    llm = hub.services.llm
    run_id = await start(hub, "Check the IO of r204-ctl", playbook="io-checkout")
    await state(hub, run_id, "idle")
    run = await hub.services.store.get_run(run_id)
    assert run is not None and run["title"] == "IO checkout"
    messages, tools = llm.calls[-1]  # type: ignore[attr-defined]
    assert messages[0]["role"] == "system" and messages[0]["content"].startswith(SYSTEM_PROMPT)
    assert "Playbook for this run (IO checkout)" in messages[0]["content"]
    names = {t.name for t in tools}
    assert {"echo", "switch", "commit", "apply", "point_write"} <= names

    viewer = await start(hub, "What is there?", roles={"viewer"})
    await state(hub, viewer, "idle")
    _, tools = llm.calls[-1]  # type: ignore[attr-defined]
    tiers = {hub.services.registry.get(t.name).tier for t in tools}  # type: ignore[union-attr]
    assert tiers == {"R"}
    with pytest.raises(InvalidRequest, match="unknown playbook"):
        await start(hub, "hi", playbook="golf")
    with pytest.raises(InvalidRequest):
        await start(hub, "   ")


async def test_large_results_are_handles(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(({"turn": 0}, {"tool_calls": [call("big")]}), ({"always": True}, {"text": "done"})))
    run_id = await start(hub, "Big")
    await state(hub, run_id, "idle")
    (result,) = await events(hub, run_id, "tool.result")
    assert result["handle"].startswith("result://r") and "data" not in result
    tool_message = (await hub.services.store.load_messages(run_id))[3]
    assert json.loads(tool_message["content"])["handle"] == result["handle"]


async def test_approvals_approve_reject_and_expire(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("switch", text="on")]}),
        ({"tool": "switch"}, {"text": "${summary}"}),
    ), hub={})
    approvals = hub.services.approvals

    approved = await start(hub, "Switch it", user="tech", roles={"operator"})
    await state(hub, approved, "waiting_approval")
    request = await pending(hub, approved)
    assert (request["tool"], request["tier"], request["title"], request["requested_by"]) == (
        "switch", "L", "Use on", "tech")
    await asyncio.sleep(0.3)  # the approver takes a while; the call's duration must not count it
    decided = await approvals.decide(request["id"], "approve", user="boss", roles={"operator"}, comment="go")
    assert (decided["state"], decided["decided_by"], decided["scope"]) == ("approved", "boss", "call")
    await state(hub, approved, "idle")
    assert [e["approval"]["id"] for e in await events(hub, approved, "approval.request", "approval.decided")] == [
        request["id"], request["id"]]
    (result,) = await events(hub, approved, "tool.result")
    assert result["ok"] and result["summary"] == "switched on" and result["duration_ms"] < 250
    assert hub.services.fake.calls == ["switch:on"]  # type: ignore[attr-defined]
    states = [e["state"] for e in await events(hub, approved, "run.state")]
    assert states == ["running", "waiting_approval", "running", "idle"]
    audit = await hub.services.store.list_audit(run_id=approved)
    assert [(a["action"], a["outcome"]) for a in reversed(audit)] == [("approval", "approved"), ("tool", "ok")]

    rejected = await start(hub, "Switch it")
    request = await pending(hub, rejected)
    await approvals.decide(request["id"], "reject", user="boss", roles={"operator"}, comment="not now")
    await state(hub, rejected, "idle")
    (result,) = await events(hub, rejected, "tool.result")
    assert not result["ok"] and result["summary"] == "boss rejected the call: not now; nothing was changed"
    with pytest.raises(Conflict, match="already rejected"):
        await approvals.decide(request["id"], "approve", user="boss", roles=ADMIN)


async def test_the_sweep_expires_approvals(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("switch", text="on")]}),
        ({"tool": "switch"}, {"text": "${summary}"}),
    ))
    policy = hub.services.policy
    policy.settings = dataclasses.replace(policy.settings, approval_ttl_s=1)  # the manifest allows 60 s at least
    run_id = await start(hub, "Switch it")
    request = await pending(hub, run_id)
    await state(hub, run_id, "idle", timeout_s=5)
    (decided,) = await events(hub, run_id, "approval.decided")
    assert decided["approval"]["id"] == request["id"] and decided["approval"]["state"] == "expired"
    (result,) = await events(hub, run_id, "tool.result")
    assert not result["ok"] and result["summary"].startswith("the approval expired")
    assert hub.services.fake.calls == []  # type: ignore[attr-defined]


async def test_who_may_approve(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("commit", text="plan")]}),
        ({"tool": "commit"}, {"text": "${summary}"}),
    ))
    approvals = hub.services.approvals
    run_id = await start(hub, "Commit", user="eng", roles={"commissioner"})
    request = await pending(hub, run_id)
    with pytest.raises(PolicyDenied, match="commissioner or admin"):
        await approvals.decide(request["id"], "approve", user="ops", roles={"operator"})
    with pytest.raises(InvalidRequest, match="tier C"):
        await approvals.decide(request["id"], "approve", user="boss", roles=ADMIN, scope="run")
    with pytest.raises(PolicyDenied, match="requester"):
        await approvals.decide(request["id"], "reject", user="guest", roles={"viewer"})
    hub.services.fake.refuse = True  # type: ignore[attr-defined]
    with pytest.raises(Conflict, match="refuses this call now: the switch moved"):
        await approvals.decide(request["id"], "approve", user="boss", roles=ADMIN)
    assert (await hub.services.store.get_approval(request["id"]))["state"] == "pending"  # type: ignore[index]
    rejected = await approvals.decide(request["id"], "reject", user="eng", roles={"commissioner"})
    assert rejected["state"] == "rejected"
    await state(hub, run_id, "idle")
    with pytest.raises(NotFound):
        await approvals.decide("a_missing", "approve", user="boss", roles=ADMIN)


async def test_an_approval_decided_as_its_call_starts_waiting_runs_the_call(agent_hub: AgentHub) -> None:
    """The decision is stored while the call looks the approval up: the call
    still gets the approved (re-checked) call from the decision."""
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("switch", text="on")]}),
        ({"tool": "switch"}, {"text": "${summary}"}),
    ))
    store, approvals = hub.services.store, hub.services.approvals
    read, audit = store.get_approval, approvals._audit
    stored, go_on = asyncio.Event(), asyncio.Event()
    deciding: list[asyncio.Task[Any]] = []

    async def slow_audit(row: dict[str, Any], args: Any, user: str, detail: str) -> None:
        if row["state"] == "approved":
            stored.set()
            await go_on.wait()
        await audit(row, args, user, detail)

    async def racing_read(approval_id: str, *, with_args: bool = False) -> dict[str, Any] | None:
        if with_args or deciding:
            return await read(approval_id, with_args=with_args)
        deciding.append(asyncio.create_task(approvals.decide(approval_id, "approve", user="boss",
                                                             roles={"operator"})))
        await stored.wait()
        row = await read(approval_id)
        go_on.set()
        return row

    approvals._audit = slow_audit  # type: ignore[method-assign]
    store.get_approval = racing_read  # type: ignore[method-assign]
    run_id = await start(hub, "Switch it", user="tech", roles={"operator"})
    await state(hub, run_id, "idle")
    assert (await deciding[0])["state"] == "approved"
    (result,) = await events(hub, run_id, "tool.result")
    assert result["ok"] and result["summary"] == "switched on"
    assert hub.services.fake.calls == ["switch:on"]  # type: ignore[attr-defined]


async def test_a_run_wide_approval_covers_later_tier_l_calls(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("switch", text="a")]}),
        ({"turn": 1}, {"tool_calls": [call("switch", text="b"), call("commit", text="c")]}),
        ({"always": True}, {"text": "done"}),
    ))
    run_id = await start(hub, "Switch twice, then commit", user="tech", roles={"operator", "commissioner"})
    first = await pending(hub, run_id)
    await hub.services.approvals.decide(first["id"], "approve", user="boss", roles={"operator"}, scope="run")
    commit = await until(lambda: _pending_tool(hub, run_id, "commit"), what="the commit approval")
    assert hub.services.fake.calls == ["switch:a", "switch:b"]  # type: ignore[attr-defined]
    meta = await hub.services.store.run_meta(run_id)
    assert meta["grant"] == {"approval_id": first["id"], "user": "boss", "roles": ["operator"]}
    audit = await hub.services.store.list_audit(run_id=run_id)
    assert any(a["action"] == "approval" and "run-wide approval" in a["detail"] and a["tool"] == "switch"
               for a in audit)
    await hub.services.approvals.decide(commit["id"], "approve", user="boss", roles=ADMIN)
    await state(hub, run_id, "idle")
    assert hub.services.fake.calls == ["switch:a", "switch:b", "commit:c"]  # type: ignore[attr-defined]
    assert len(await hub.services.store.list_approvals(run_id=run_id)) == 2


async def _pending_tool(hub: Any, run_id: str, tool: str) -> dict[str, Any] | None:
    rows = await hub.services.store.list_approvals(state="pending", run_id=run_id)
    return next((r for r in rows if r["tool"] == tool), None)


async def test_questions_wait_for_an_answer_of_the_same_run(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("ask_user", question="Is the valve open?", options=["Yes", "No"])]}),
        ({"tool": "ask_user", "ok": True}, {"text": "${summary}"}),
    ))
    runs = hub.services.runs
    run_id = await start(hub, "Ask", user="tech", roles={"operator"})
    await state(hub, run_id, "waiting_answer")
    (question,) = await events(hub, run_id, "question")
    assert (question["text"], question["options"]) == ("Is the valve open?", ["Yes", "No"])
    other = await start(hub, "Ask too", user="tech", roles={"operator"})
    await state(hub, other, "waiting_answer")
    with pytest.raises(NotFound):
        await runs.answer(other, question["question_id"], "Yes", user="tech", roles={"operator"})
    with pytest.raises(InvalidRequest, match="one of: Yes, No"):
        await runs.answer(run_id, question["question_id"], "Maybe", user="tech", roles={"operator"})
    with pytest.raises(PolicyDenied):
        await runs.answer(run_id, question["question_id"], "Yes", user="guest", roles={"viewer"})
    await runs.answer(run_id, question["question_id"], "Yes", user="tech", roles={"operator"})
    await state(hub, run_id, "idle")
    with pytest.raises(Conflict, match="already answered"):
        await runs.answer(run_id, question["question_id"], "No", user="tech", roles={"operator"})
    (answered,) = await events(hub, run_id, "question.answered")
    assert (answered["answer"], answered["user"]) == ("Yes", "tech")
    done = await events(hub, run_id, "message.done")
    assert done[-1]["text"] == "tech answered: Yes"
    states = [e["state"] for e in await events(hub, run_id, "run.state")]
    assert states == ["running", "waiting_answer", "running", "idle"]
    await runs.cancel(other, user="tech", roles={"operator"})


async def test_cancel_stops_the_run_and_closes_what_it_waits_for(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0, "user": "block"}, {"tool_calls": [call("block"), call("echo", text="later")]}),
        ({"turn": 0, "user": "switch"}, {"tool_calls": [call("switch", text="x")]}),
        ({"always": True}, {"text": "finished"}),
    ))
    runs = hub.services.runs
    blocked = await start(hub, "block", user="tech", roles={"viewer"})
    await until(lambda: _called(hub, "block"), what="the block call")
    with pytest.raises(PolicyDenied):
        await runs.cancel(blocked, user="guest", roles={"viewer"})
    await runs.cancel(blocked, user="tech", roles={"viewer"})
    run = await hub.services.store.get_run(blocked)
    assert run is not None and run["state"] == "cancelled"
    results = await events(hub, blocked, "tool.result")
    assert [r["summary"] for r in results] == ["not run: the run was cancelled by tech"] * 2
    last = (await events(hub, blocked, "run.state"))[-1]
    assert (last["state"], last["reason"]) == ("cancelled", "cancelled by tech")
    audit = await hub.services.store.list_audit(run_id=blocked)
    assert {(a["action"], a["outcome"]) for a in audit} >= {("tool", "cancelled"), ("run.cancel", "ok")}
    await runs.cancel(blocked, user="tech", roles={"viewer"})  # a stopped run: nothing to do

    await runs.post_message(blocked, user="tech", roles={"viewer"}, message="go on")
    await state(hub, blocked, "idle")
    history = await hub.services.store.load_messages(blocked)
    assert [m["role"] for m in history][-4:] == ["tool", "system", "user", "assistant"]

    waiting = await start(hub, "switch")
    request = await pending(hub, waiting)
    await runs.cancel(waiting, user="boss", roles={"operator"})
    (decided,) = await events(hub, waiting, "approval.decided")
    assert decided["approval"]["id"] == request["id"] and decided["approval"]["state"] == "expired"
    assert decided["approval"]["comment"] == "cancelled by boss"


async def _called(hub: Any, name: str) -> bool:
    return name in hub.services.fake.calls


async def test_messages_wait_for_the_turn_to_end(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(({"turn": 0}, {"tool_calls": [call("block")]}), ({"always": True}, {"text": "ok"})))
    runs = hub.services.runs
    run_id = await start(hub, "block")
    await until(lambda: _called(hub, "block"))
    with pytest.raises(Conflict, match="running"):
        await runs.post_message(run_id, user="dev", roles=ADMIN, message="more")
    hub.services.fake.release.set()  # type: ignore[attr-defined]
    await state(hub, run_id, "idle")
    await runs.post_message(run_id, user="ops", roles={"operator"}, message="more")
    await state(hub, run_id, "idle")
    users = [e["user"] for e in await events(hub, run_id, "message.user")]
    assert users == ["dev", "ops"]
    assert (await hub.services.store.run_meta(run_id))["user"] == "ops"
    with pytest.raises(NotFound):
        await runs.post_message("r_missing", user="dev", roles=ADMIN, message="hi")


async def test_the_tool_call_budget_ends_the_turn(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(({"always": True}, {"tool_calls": [call("echo", text="again")] * 2})),
                          hub={"agent": {"max_tool_calls": 3}})
    run_id = await start(hub, "loop")
    await state(hub, run_id, "idle")
    (error,) = await events(hub, run_id, "error")
    assert error["code"] == "budget" and "3 tool calls" in error["message"]
    assert hub.services.fake.calls == ["echo:again"] * 3  # type: ignore[attr-defined]
    results = await events(hub, run_id, "tool.result")
    assert [r["ok"] for r in results] == [True, True, True, False]
    history = await hub.services.store.load_messages(run_id)
    assert history[-1]["role"] == "tool" and "not run" in json.loads(history[-1]["content"])["summary"]


async def test_two_identical_failures_hand_over_to_the_user(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(({"always": True}, {"tool_calls": [call("fail")]})))
    run_id = await start(hub, "fail please")
    await state(hub, run_id, "idle")
    (error,) = await events(hub, run_id, "error")
    assert error["code"] == "repeated_failure" and error["message"].startswith("fail failed twice")
    assert hub.services.fake.calls == ["fail", "fail"]  # type: ignore[attr-defined]


async def test_an_invalid_call_is_a_result_the_model_can_correct(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [{"name": "echo", "args": "{not json"}]}),
        ({"turn": 1}, {"tool_calls": [call("echo", text="fixed")]}),
        ({"always": True}, {"text": "done"}),
    ))
    run_id = await start(hub, "go")
    await state(hub, run_id, "idle")
    results = await events(hub, run_id, "tool.result")
    assert [r["ok"] for r in results] == [False, True]
    assert "not valid JSON" in results[0]["summary"]
    calls = await events(hub, run_id, "tool.call")
    assert calls[1]["args"] == {}


@pytest.mark.parametrize(("respond", "code"), [
    ({"text": "partial", "error": "the provider fell over"}, "llm"),
    ({"text": "flagged words", "finish_reason": "sensitive"}, "sensitive"),
    ({"text": "a long answer that", "tool_calls": [call("echo", text="cut")], "finish_reason": "length"}, "length"),
])
async def test_a_stopped_answer_ends_the_turn_with_an_error(agent_hub: AgentHub, respond: dict[str, Any],
                                                            code: str) -> None:
    hub = await agent_hub(rules(({"turn": 0, "user": "^go$"}, respond), ({"always": True}, {"text": "fine"})))
    run_id = await start(hub, "go")
    await state(hub, run_id, "idle")
    (error,) = await events(hub, run_id, "error")
    assert error["code"] == code
    assert hub.services.fake.calls == []  # type: ignore[attr-defined]
    history = await hub.services.store.load_messages(run_id)
    if code == "length":
        assert history[-1] == {"role": "assistant", "content": "a long answer that"}
        (result,) = await events(hub, run_id, "tool.result")
        assert not result["ok"]
    else:
        assert history[-1]["role"] == "user"
    await hub.services.runs.post_message(run_id, user="dev", roles=ADMIN, message="again")
    await state(hub, run_id, "idle")
    assert (await events(hub, run_id, "message.done"))[-1]["text"] == "fine"


async def test_a_disabled_model_is_an_error_event(agent_hub: AgentHub) -> None:
    from uc_hub.llm import DisabledProvider

    hub = await agent_hub(DisabledProvider())
    run_id = await start(hub, "hello")
    await state(hub, run_id, "idle")
    (error,) = await events(hub, run_id, "error")
    assert error["code"] == "llm" and "llm.provider: none" in error["message"]


class BreaksOff(LlmProvider):
    """Starts a tool call, then fails (``error``) or hangs until the run is
    cancelled or the hub restarts (``hang``)."""

    name = "breaks-off"
    model = "breaks-off"

    def __init__(self, how: str) -> None:
        self.how = how
        self.hanging = asyncio.Event()

    async def stream(self, messages: list[dict[str, Any]], tools: list[ToolDef], *,
                     reasoning_effort: str | None = None, max_tokens: int | None = None) -> Any:
        yield ToolCallStart(0, "call_a", "echo")
        yield ToolCallDelta(0, '{"text": ')
        if self.how == "error":
            raise LlmError("the connection was reset")
        self.hanging.set()
        await asyncio.Event().wait()


@pytest.mark.parametrize("how", ["error", "cancel", "restart"])
async def test_a_call_whose_answer_broke_off_gets_a_result(agent_hub: AgentHub, how: str) -> None:
    llm = BreaksOff("error" if how == "error" else "hang")
    hub = await agent_hub(llm)
    run_id = await start(hub, "go")
    if how == "cancel":
        await asyncio.wait_for(llm.hanging.wait(), 5)
        await hub.services.runs.cancel(run_id, user="dev", roles=ADMIN)
    elif how == "restart":
        await asyncio.wait_for(llm.hanging.wait(), 5)
        await hub.restart()
    await state(hub, run_id, "idle", "cancelled")
    assert [e["call_id"] for e in await events(hub, run_id, "tool.call")] == ["call_a"]
    (result,) = await events(hub, run_id, "tool.result")
    reason = {"error": "the model could not answer", "cancel": "cancelled by dev",
              "restart": "interrupted by a hub restart"}[how]
    assert result["call_id"] == "call_a" and not result["ok"] and result["summary"].startswith(f"not run: {reason}")
    assert [m["role"] for m in await hub.services.store.load_messages(run_id)] == ["system", "user"]


class DuplicateIds(LlmProvider):
    """Reuses one tool call id, as a careless provider might."""

    name = "dup"
    model = "dup"

    async def stream(self, messages: list[dict[str, Any]], tools: list[ToolDef], *,
                     reasoning_effort: str | None = None, max_tokens: int | None = None) -> Any:
        if sum(1 for m in messages if m["role"] == "tool") >= 2:
            yield TextDelta("done")
            yield Completed("done", "")
            return
        yield ToolCallStart(0, "call_x", "echo")
        yield ToolCallDelta(0, '{"text": "a"}')
        yield Completed("", "", [ToolCall("call_x", "echo", '{"text": "a"}')], "tool_calls")


async def test_tool_call_ids_are_unique_within_a_run(agent_hub: AgentHub) -> None:
    hub = await agent_hub(DuplicateIds())
    run_id = await start(hub, "go")
    await state(hub, run_id, "idle")
    ids = [e["call_id"] for e in await events(hub, run_id, "tool.result")]
    assert len(ids) == 2 and len(set(ids)) == 2 and ids[0] == "call_x"


async def test_waiting_runs_survive_a_restart_and_running_ones_are_interrupted(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0, "user": "switch"}, {"tool_calls": [call("switch", text="on")]}),
        ({"turn": 0, "user": "ask"}, {"tool_calls": [call("ask_user", question="Open?", options=["Yes", "No"])]}),
        ({"turn": 0, "user": "block"}, {"tool_calls": [call("block")]}),
        ({"always": True}, {"text": "after restart: ${summary}"}),
    ))
    waiting_approval = await start(hub, "switch", user="tech", roles={"operator"})
    request = await pending(hub, waiting_approval)
    waiting_answer = await start(hub, "ask", user="tech", roles={"operator"})
    await state(hub, waiting_answer, "waiting_answer")
    (question,) = await events(hub, waiting_answer, "question")
    running = await start(hub, "block")
    await until(lambda: _called(hub, "block"))

    controls = hub.services.fake  # type: ignore[attr-defined]
    await hub.restart()
    install_fakes(hub, controls)
    assert (await hub.services.store.get_run(waiting_approval))["state"] == "waiting_approval"  # type: ignore[index]
    assert (await hub.services.store.get_run(waiting_answer))["state"] == "waiting_answer"  # type: ignore[index]
    interrupted = await hub.services.store.get_run(running)
    assert interrupted is not None and interrupted["state"] == "idle"
    error = (await events(hub, running, "error"))[-1]
    assert (error["code"], error["message"]) == ("restart", "interrupted by a hub restart")
    closing = json.loads((await hub.services.store.load_messages(running))[-1]["content"])
    assert "restarted while this call ran" in closing["summary"]

    await hub.services.approvals.decide(request["id"], "approve", user="boss", roles={"operator"})
    await state(hub, waiting_approval, "idle")
    assert controls.calls[-1] == "switch:on"
    assert (await events(hub, waiting_approval, "message.done"))[-1]["text"] == "after restart: switched on"

    await hub.services.runs.answer(waiting_answer, question["question_id"], "No", user="tech", roles={"operator"})
    await state(hub, waiting_answer, "idle")
    assert (await events(hub, waiting_answer, "message.done"))[-1]["text"] == "after restart: tech answered: No"
    for run_id in (waiting_approval, waiting_answer, running):
        assert (await hub.services.store.get_run(run_id))["state"] not in ACTIVE_STATES  # type: ignore[index]


async def test_an_approval_decided_before_a_restart_does_not_run(agent_hub: AgentHub) -> None:
    hub = await agent_hub(rules(
        ({"turn": 0}, {"tool_calls": [call("switch", text="on")]}),
        ({"always": True}, {"text": "${summary}"}),
    ))
    run_id = await start(hub, "switch")
    request = await pending(hub, run_id)
    controls = hub.services.fake  # type: ignore[attr-defined]
    await hub.services.runs.stop()  # the hub goes down while the call waits ...
    await hub.services.store.decide_approval(request["id"], "approve", user="boss")  # ... and a decision lands
    await hub.restart()
    install_fakes(hub, controls)
    await state(hub, run_id, "idle")
    (result,) = await events(hub, run_id, "tool.result")
    assert not result["ok"] and "restarted before it ran" in result["summary"]
    assert controls.calls == []


async def test_the_site_status_opens_every_turn(agent_hub: AgentHub) -> None:
    hub = await agent_hub(scripted([{"when": {"always": True}, "respond": {"text": "ok"}}]))
    run_id = await start(hub, "one")
    await state(hub, run_id, "idle")
    await hub.services.runs.post_message(run_id, user="dev", roles=ADMIN, message="two")
    await state(hub, run_id, "idle")
    history = await hub.services.store.load_messages(run_id)
    assert [m["role"] for m in history] == ["system", "user", "assistant", "system", "user", "assistant"]
    status = history[3]["content"]
    assert "Site hq (Agent test site): 0 devices" in status and "Manifest: live revision 1." in status
    assert "The next message is from dev (roles: admin)" in status
    assert len(status) < 8000

