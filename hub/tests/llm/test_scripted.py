from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from uc_hub.llm.base import (
    Completed,
    LlmError,
    LlmEvent,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
    ToolCallStart,
)
from uc_hub.llm.scripted import ConversationState, ScriptedProvider
from uc_hub.tools.registry import ToolResult

DEMO_TOOLS = {
    "site_search",
    "device_describe",
    "point_read",
    "manifest_get",
    "manifest_edit",
    "plan",
    "apply",
    "test_run",
    "ask_user",
    "io_force",
}


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def assistant_calls(*calls: tuple[str, str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for cid, name, args in calls
        ],
    }


def tool_result(call_id: str, result: ToolResult) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": result.to_model_text()}


async def collect(provider: ScriptedProvider, messages: list[dict[str, Any]]) -> list[LlmEvent]:
    return [e async for e in provider.stream(messages, [])]


def done(events: list[LlmEvent]) -> Completed:
    assert isinstance(events[-1], Completed)
    return events[-1]


# Rule matching ---------------------------------------------------------------------------
SCRIPT = {
    "model": "scripted-test",
    "rules": [
        {
            "id": "greet",
            "when": {"user": r"(?i)^hello (?P<who>\w+)", "turn": 0},
            "respond": {"text": "Hello ${who}, turn ${turn}."},
        },
        {
            "id": "after-search",
            "when": {
                "tool": "site_search",
                "ok": True,
                "result": r'"device": "(?P<device>[\w-]+)"',
            },
            "respond": {
                "tool_calls": [
                    {
                        "name": "device_describe",
                        "args": {"device": "${device}", "q": "${args.query}"},
                    }
                ]
            },
        },
        {
            "id": "search-failed",
            "when": {"tool": ["site_search", "device_describe"], "ok": False},
            "respond": {"text": "${tool} failed: ${summary}"},
        },
        {"id": "later", "when": {"turn": {"min": 2}}, "respond": {"text": "Turn ${turn}, done."}},
        {"id": "any", "when": {"always": True}, "respond": {"text": "Echo: ${user}"}},
    ],
}


async def test_user_regex_turn_and_captures() -> None:
    provider = ScriptedProvider.from_dict(SCRIPT)
    assert provider.model == "scripted-test" and provider.configured
    events = await collect(provider, [{"role": "system", "content": "sys"}, user("hello Ilija")])
    assert done(events).text == "Hello Ilija, turn 0."
    assert done(events).finish_reason == "stop"
    # "turn: 0" no longer holds once the assistant answered in this user turn.
    msgs = [user("hello Ilija"), {"role": "assistant", "content": "Hello"}]
    assert done(await collect(provider, msgs)).text == "Echo: hello Ilija"


async def test_tool_result_conditions() -> None:
    provider = ScriptedProvider.from_dict(SCRIPT)
    base = [user("find r204"), assistant_calls(("call_1", "site_search", {"query": "r204"}))]
    ok = ToolResult(True, "1 device", data={"device": "r204-ctl"})
    events = await collect(provider, [*base, tool_result("call_1", ok)])
    call = done(events).tool_calls[0]
    assert (call.id, call.name) == ("call_2", "device_describe")
    assert json.loads(call.arguments) == {"device": "r204-ctl", "q": "r204"}

    failed = ToolResult(False, "index not ready", error_code="unavailable")
    events = await collect(provider, [*base, tool_result("call_1", failed)])
    assert done(events).text == "site_search failed: index not ready"

    # A result without the captured field does not match the rule.
    events = await collect(provider, [*base, tool_result("call_1", ToolResult(True, "none"))])
    assert done(events).text == "Echo: find r204"


async def test_tool_results_of_earlier_user_turns_are_ignored() -> None:
    provider = ScriptedProvider.from_dict(SCRIPT)
    msgs = [
        user("find r204"),
        assistant_calls(("call_1", "site_search", {"query": "r204"})),
        tool_result("call_1", ToolResult(False, "boom")),
        {"role": "assistant", "content": "It failed."},
        user("try again"),
    ]
    state = ConversationState.from_messages(msgs)
    assert (state.tool, state.turn, state.call_count, state.user) == (None, 0, 1, "try again")
    assert done(await collect(provider, msgs)).text == "Echo: try again"


async def test_turn_range() -> None:
    provider = ScriptedProvider.from_dict(SCRIPT)
    msgs = [user("x"), {"role": "assistant", "content": "a"}, {"role": "assistant", "content": "b"}]
    assert done(await collect(provider, msgs)).text == "Turn 2, done."


def test_conversation_state_details() -> None:
    msgs = [
        user("first"),
        assistant_calls(("c1", "plan", {}), ("c2", "point_read", {"points": ["a/b/c"], "n": 3})),
        tool_result("c1", ToolResult(True, "plan p1")),
        {"role": "tool", "tool_call_id": "c2", "content": "not json"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "multi"}, {"type": "text", "text": "part"}],
        },
        assistant_calls(("c3", "plan", {})),
        {"role": "tool", "tool_call_id": "c3", "name": "renamed", "content": '{"ok": false}'},
    ]
    state = ConversationState.from_messages(msgs)
    assert state.user == "multipart"
    assert (state.turn, state.tool, state.tool_ok, state.call_count) == (1, "renamed", False, 3)
    earlier = ConversationState.from_messages(msgs[:4])
    assert (earlier.tool, earlier.tool_ok, earlier.summary) == ("point_read", None, "not json")
    assert earlier.variables()["args.n"] == "3"
    assert "args.points" not in earlier.variables()


async def test_fallback() -> None:
    provider = ScriptedProvider.from_dict(
        {
            "rules": [{"when": {"user": "^never$"}, "respond": {"text": "no"}}],
            "fallback": {"thinking": "Nothing matched.", "text": "Sorry, ${user}?"},
        }
    )
    events = await collect(provider, [user("what")])
    assert (done(events).reasoning, done(events).text) == ("Nothing matched.", "Sorry, what?")
    default = ScriptedProvider([])
    assert "no response" in done(await collect(default, [user("x")])).text


# Streaming ------------------------------------------------------------------------------------
async def test_streams_all_event_types_in_small_chunks() -> None:
    provider = ScriptedProvider.from_dict(
        {
            "chunk_size": 4,
            "rules": [
                {
                    "when": {"always": True},
                    "respond": {
                        "thinking": "Find the room first.",
                        "text": "Searching now.",
                        "tool_calls": [
                            {"name": "site_search", "args": {"query": "room 204"}},
                            {"name": "plan"},
                        ],
                    },
                }
            ],
        }
    )
    events = await collect(provider, [user("go")])
    kinds = [e.type for e in events]
    assert kinds[0] == "thinking" and kinds[-1] == "completed"
    assert kinds.index("text") > kinds.index("thinking")
    assert "".join(e.text for e in events if isinstance(e, ThinkingDelta)) == "Find the room first."
    assert all(len(e.text) <= 4 for e in events if isinstance(e, (ThinkingDelta, TextDelta)))
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert starts == [ToolCallStart(0, "call_1", "site_search"), ToolCallStart(1, "call_2", "plan")]
    args0 = "".join(
        e.arguments_delta for e in events if isinstance(e, ToolCallDelta) and e.index == 0
    )
    assert json.loads(args0) == {"query": "room 204"}
    assert events.index(starts[1]) > max(
        i for i, e in enumerate(events) if isinstance(e, ToolCallDelta) and e.index == 0
    )
    final = done(events)
    assert final.finish_reason == "tool_calls"
    assert [c.arguments for c in final.tool_calls] == [args0, "{}"]
    assert final.text == "Searching now." and final.reasoning == "Find the room first."
    assert final.usage.completion_tokens > 0 and final.usage.prompt_tokens == 0


async def test_error_response_raises_after_text() -> None:
    provider = ScriptedProvider.from_dict(
        [
            {
                "when": {"always": True},
                "respond": {"text": "Partial", "error": "simulated outage for ${user}"},
            }
        ]
    )
    events: list[LlmEvent] = []
    with pytest.raises(LlmError, match="simulated outage for q"):
        async for event in provider.stream([user("q")], []):
            events.append(event)
    assert events == [TextDelta("Partial")]


async def test_string_args_are_verbatim_and_finish_override() -> None:
    provider = ScriptedProvider.from_dict(
        [
            {
                "when": {"always": True},
                "respond": {
                    "tool_calls": [{"name": "manifest_edit", "args": '{"json_patch": [${user}'}],
                    "finish_reason": "length",
                },
            }
        ]
    )
    final = done(await collect(provider, [user("oops")]))
    assert final.tool_calls[0].arguments == '{"json_patch": [oops'
    assert final.finish_reason == "length"


async def test_empty_args_are_an_empty_object() -> None:
    """``args:`` left empty in YAML is null; the model contract is a JSON object."""
    provider = ScriptedProvider.from_yaml(
        "rules:\n  - when: {always: true}\n    respond:\n      tool_calls:\n"
        "        - name: plan\n          args:\n"
    )
    final = done(await collect(provider, [user("go")]))
    assert final.tool_calls[0].arguments == "{}"


async def test_delay_is_cancellable() -> None:
    provider = ScriptedProvider.from_dict(
        {"delay_s": 30, "rules": [{"when": {"always": True}, "respond": {"text": "slow"}}]}
    )

    async def consume() -> None:
        async for _ in provider.stream([user("x")], []):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)


# Loading ------------------------------------------------------------------------------------
def test_from_file(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "delay_s: 0.5\nrules:\n  - when: {user: 'hi'}\n    respond: {text: 'hey'}\n",
        encoding="utf-8",
    )
    provider = ScriptedProvider.from_file(path, delay_s=0)
    assert provider.delay_s == 0 and provider.rules[0].id == "rule-0"
    with pytest.raises(ValueError, match="cannot read script"):
        ScriptedProvider.from_file(tmp_path / "missing.yaml")
    path.write_text("rules: [\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid YAML"):
        ScriptedProvider.from_file(path)


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ({"rulez": []}, "unknown keys"),
        ({"rules": {}}, "rules must be a list"),
        ([{"when": {"user": "("}, "respond": {"text": "x"}}], "invalid 'user' regex"),
        ([{"when": {"colour": "red"}, "respond": {"text": "x"}}], "unknown conditions"),
        ([{"when": {}, "respond": {"text": "x"}}], "non-empty mapping"),
        ([{"when": {"always": True}}], "needs both"),
        ([{"when": {"always": True}, "respond": {}}], "response is empty"),
        ([{"when": {"always": True}, "respond": {"tool_calls": [{"args": {}}]}}], "needs a name"),
        ([{"when": {"turn": -1}, "respond": {"text": "x"}}], "'turn'"),
        ([{"when": {"turn": {"min": 3, "max": 1}}, "respond": {"text": "x"}}], "'turn'"),
        ([{"when": {"ok": "yes"}, "respond": {"text": "x"}}], "'ok'"),
        ([{"when": {"always": False}, "respond": {"text": "x"}}], "'always'"),
        ([{"when": {"tool": []}, "respond": {"text": "x"}}], "'tool'"),
        ([{"when": {"always": True}, "respond": {"text": 3}}], "must be a string"),
        ({"chunk_size": 0, "rules": []}, "chunk_size"),
        ("just text", "mapping or a list"),
    ],
)
def test_invalid_scripts(script: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ScriptedProvider.from_dict(script)


# The demo scenario --------------------------------------------------------------------------
Results = dict[str, Callable[[dict[str, Any]], ToolResult]]

HAPPY: Results = {
    "site_search": lambda a: ToolResult(
        True, "2 devices", data=[{"device": "r204-ctl"}, {"device": "r204-co2"}]
    ),
    "device_describe": lambda a: ToolResult(True, f"{a['device']}: online, 1 IO point"),
    "point_read": lambda a: ToolResult(True, "R204 Temp = 21.4 °C (good, 2 s old)"),
    "manifest_get": lambda a: ToolResult(True, "no tags yet", data={}),
    "manifest_edit": lambda a: ToolResult(True, "draft revision 17 is valid"),
    "plan": lambda a: ToolResult(True, "r204-ctl: 1 change", data={"plan_id": "p17", "changes": 1}),
    "apply": lambda a: ToolResult(True, "applied plan " + a["plan_id"]),
    "test_run": lambda a: ToolResult(True, "1/1 tests passed"),
}


async def run_user_turn(
    provider: ScriptedProvider, messages: list[dict[str, Any]], results: Results
) -> list[tuple[str, dict[str, Any]]]:
    """A minimal agent loop: call the model, run its tools, repeat."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for _ in range(20):
        final = done(await collect(provider, messages))
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": final.text,
            "reasoning_content": final.reasoning,
        }
        if final.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in final.tool_calls
            ]
        messages.append(msg)
        if not final.tool_calls:
            return calls
        for c in final.tool_calls:
            assert c.name in DEMO_TOOLS
            args = json.loads(c.arguments)
            calls.append((c.name, args))
            messages.append(tool_result(c.id, results[c.name](args)))
    raise AssertionError("the scenario did not finish")


async def test_demo_commissions_a_room() -> None:
    provider = ScriptedProvider.demo(delay_s=0)
    assert provider.delay_s == 0
    messages = [
        {"role": "system", "content": "You are the uc-hub agent."},
        user("Please commission room 204"),
    ]
    calls = await run_user_turn(provider, messages, HAPPY)
    assert [name for name, _ in calls] == [
        "site_search",
        "device_describe",
        "point_read",
        "manifest_get",
        "manifest_edit",
        "plan",
        "apply",
        "test_run",
    ]
    args = dict(calls)
    assert args["device_describe"] == {"device": "r204-ctl"}
    assert args["point_read"] == {"points": ["r204-ctl/analog-input:1"]}
    assert args["manifest_edit"]["json_patch"][0] == {
        "op": "add",
        "path": "/placement/r204-ctl",
        "value": "r204",
    }
    assert args["apply"] == {"plan_id": "p17"}
    assert messages[-1]["content"] == "Room 204 is commissioned: 1/1 tests passed"
    ids = [tc["id"] for m in messages for tc in m.get("tool_calls", [])]
    assert len(ids) == len(set(ids)) == 8


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("Yes", "wiring and the valve work"), ("No", "check\nthe actuator wiring")],
)
async def test_demo_failed_test_branch(answer: str, expected: str) -> None:
    results: Results = {
        **HAPPY,
        "test_run": lambda a: ToolResult(
            False, "0/1 passed: valve opens when cold", error_code="test"
        ),
        "io_force": lambda a: ToolResult(
            True, f"forced {a['node']}/{a['channel']} for {a['lease_s']} s"
        ),
        "ask_user": lambda a: ToolResult(True, "answered", data={"answer": answer}),
    }
    messages = [user("commission room 310")]
    calls = await run_user_turn(ScriptedProvider.demo(delay_s=0), messages, results)
    assert [name for name, _ in calls][-3:] == ["test_run", "io_force", "ask_user"]
    assert dict(calls)["io_force"] == {
        "node": "r310-ctl",
        "channel": "ai0",
        "value": 650,
        "lease_s": 120,
    }
    assert expected.replace("\n", " ") in messages[-1]["content"]


async def test_demo_failed_question_is_not_read_as_an_answer() -> None:
    """An ask_user that failed (expired, cancelled) must not be taken as "No"."""
    results: Results = {
        **HAPPY,
        "test_run": lambda a: ToolResult(False, "0/1 passed", error_code="test"),
        "io_force": lambda a: ToolResult(True, "forced"),
        "ask_user": lambda a: ToolResult(False, "question expired", error_code="timeout"),
    }
    messages = [user("commission room 204")]
    calls = await run_user_turn(ScriptedProvider.demo(delay_s=0), messages, results)
    assert calls[-1][0] == "ask_user"
    assert messages[-1]["content"].startswith("The ask_user call failed: question expired.")


async def test_demo_stops_on_tool_failure_and_handles_other_prompts() -> None:
    provider = ScriptedProvider.demo(delay_s=0)
    results: Results = {**HAPPY, "device_describe": lambda a: ToolResult(False, "r204-ctl offline")}
    messages = [user("commission room 204")]
    calls = await run_user_turn(provider, messages, results)
    assert [name for name, _ in calls] == ["site_search", "device_describe"]
    assert messages[-1]["content"].startswith("The device_describe call failed: r204-ctl offline.")

    messages = [user("What is the temperature in room 204?")]
    assert [n for n, _ in await run_user_turn(provider, messages, HAPPY)] == ["point_read"]
    assert messages[-1]["content"] == "Room 204: R204 Temp = 21.4 °C (good, 2 s old)"

    messages = [user("hello")]
    assert await run_user_turn(provider, messages, HAPPY) == []
    assert "offline demo model" in messages[-1]["content"]

    messages = [user("order pizza")]
    assert await run_user_turn(provider, messages, HAPPY) == []
    assert "only knows a few scenarios" in messages[-1]["content"]


async def test_demo_plan_without_changes() -> None:
    results: Results = {
        **HAPPY,
        "plan": lambda a: ToolResult(True, "no changes", data={"changes": 0}),
    }
    messages = [user("commission room 204")]
    calls = await run_user_turn(ScriptedProvider.demo(delay_s=0), messages, results)
    assert calls[-1][0] == "plan"
    assert "nothing to apply" in messages[-1]["content"]
