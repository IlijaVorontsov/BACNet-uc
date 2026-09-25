from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, AsyncGenerator, AsyncIterator, Callable

import httpx
import pytest

from uc_hub.llm.base import (
    Completed,
    LlmError,
    LlmEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallStart,
    ToolDef,
    Usage,
)
from uc_hub.llm.zai import DONE, SseDecoder, ZaiProvider

KEY = "sk-test-secret-0123456789"
USER = [{"role": "user", "content": "Which rooms are above 24 °C?"}]
SEARCH = ToolDef(
    "site_search",
    "Search devices and points",
    {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
)

Handler = Callable[[httpx.Request], Any]


def chunk(
    delta: dict[str, Any] | None = None,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "resp-1",
        "created": 1790290000,
        "model": "glm-5.3",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def tool_frag(
    index: int, *, id: str | None = None, name: str | None = None, args: str | None = None
) -> dict[str, Any]:
    frag: dict[str, Any] = {"index": index, "function": {}}
    if id is not None:
        frag["id"] = id
        frag["type"] = "function"
    if name is not None:
        frag["function"]["name"] = name
    if args is not None:
        frag["function"]["arguments"] = args
    return frag


def sse(*events: dict[str, Any] | str, done: bool = True) -> bytes:
    lines = [e if isinstance(e, str) else f"data: {json.dumps(e)}\n\n" for e in events]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def stream_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def error_response(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"code": code, "message": message}}, headers=headers
    )


class Recorder:
    """MockTransport handler that replays a list of responses and records requests."""

    def __init__(self, *responses: httpx.Response | Exception | Handler) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        if callable(item) and not isinstance(item, httpx.Response):
            return item(request)
        return item

    def body(self, n: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[n].content)


@pytest.fixture
async def zai() -> AsyncIterator[Callable[..., ZaiProvider]]:
    created: list[ZaiProvider] = []

    def factory(handler: Handler, **kwargs: Any) -> ZaiProvider:
        options: dict[str, Any] = {
            "api_key": KEY,
            "retry_backoff_s": 0.0,
            "retry_max_delay_s": 0.0,
        }
        options.update(kwargs)
        provider = ZaiProvider(transport=httpx.MockTransport(handler), **options)
        created.append(provider)
        return provider

    yield factory
    for provider in created:
        await provider.aclose()


async def collect(
    provider: ZaiProvider, tools: list[ToolDef] | None = None, **kwargs: Any
) -> list[LlmEvent]:
    return [e async for e in provider.stream(USER, tools or [], **kwargs)]


def completed(events: list[LlmEvent]) -> Completed:
    assert isinstance(events[-1], Completed)
    assert sum(isinstance(e, Completed) for e in events) == 1
    return events[-1]


# Streaming ---------------------------------------------------------------------
async def test_text_streaming(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(
                chunk({"role": "assistant"}),
                chunk({"content": "Rooms 204"}),
                chunk({"content": " and 310."}),
                chunk({}, finish="stop", usage={"prompt_tokens": 120, "completion_tokens": 9}),
            )
        )
    )
    events = await collect(zai(rec))
    assert events[:-1] == [TextDelta("Rooms 204"), TextDelta(" and 310.")]
    done = completed(events)
    assert (done.text, done.reasoning, done.tool_calls) == ("Rooms 204 and 310.", "", [])
    assert done.finish_reason == "stop"
    assert done.usage == Usage(prompt_tokens=120, completion_tokens=9, cached_tokens=0)


async def test_reasoning_then_text(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(
                chunk({"role": "assistant", "reasoning_content": "The user wants"}),
                chunk({"reasoning_content": " warm rooms."}),
                chunk({"content": "Room 204."}),
                chunk({}, finish="stop"),
            )
        )
    )
    events = await collect(zai(rec))
    assert events[:-1] == [
        ThinkingDelta("The user wants"),
        ThinkingDelta(" warm rooms."),
        TextDelta("Room 204."),
    ]
    done = completed(events)
    assert done.reasoning == "The user wants warm rooms."
    assert done.text == "Room 204."


async def test_single_tool_call_with_fragmented_arguments(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(
                chunk({"reasoning_content": "Search first."}),
                chunk({"tool_calls": [tool_frag(0, id="call_9f", name="site_search", args="")]}),
                chunk({"tool_calls": [tool_frag(0, args='{"que')]}),
                chunk({"tool_calls": [tool_frag(0, args='ry": "room ')]}),
                chunk({"tool_calls": [tool_frag(0, args='204"}')]}),
                chunk({}, finish="tool_calls"),
            )
        )
    )
    events = await collect(zai(rec), [SEARCH])
    assert events[1:-1] == [
        ToolCallStart(0, "call_9f", "site_search"),
        ToolCallDelta(0, '{"que'),
        ToolCallDelta(0, 'ry": "room '),
        ToolCallDelta(0, '204"}'),
    ]
    done = completed(events)
    assert done.finish_reason == "tool_calls"
    assert done.tool_calls == [ToolCall("call_9f", "site_search", '{"query": "room 204"}')]
    assert json.loads(done.tool_calls[0].arguments) == {"query": "room 204"}


async def test_parallel_tool_calls_interleaved(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(
                chunk({"content": "Reading both."}),
                chunk(
                    {
                        "tool_calls": [
                            tool_frag(0, id="call_a", name="point_read", args='{"points": ')
                        ]
                    }
                ),
                chunk({"tool_calls": [tool_frag(1, id="call_b", name="device_describe")]}),
                chunk(
                    {
                        "tool_calls": [
                            tool_frag(0, args='["hq/r204-ctl/analog-input:1"]}'),
                            tool_frag(1, args='{"device": '),
                        ]
                    }
                ),
                chunk({"tool_calls": [tool_frag(1, args='"r204-ctl"}')]}),
                chunk({}, finish="tool_calls", usage={"prompt_tokens": 5, "completion_tokens": 7}),
            )
        )
    )
    events = await collect(zai(rec), [SEARCH])
    starts = [e for e in events if isinstance(e, ToolCallStart)]
    assert starts == [
        ToolCallStart(0, "call_a", "point_read"),
        ToolCallStart(1, "call_b", "device_describe"),
    ]
    deltas: dict[int, str] = {}
    for e in events:
        if isinstance(e, ToolCallDelta):
            deltas[e.index] = deltas.get(e.index, "") + e.arguments_delta
    done = completed(events)
    assert [(c.id, c.name) for c in done.tool_calls] == [
        ("call_a", "point_read"),
        ("call_b", "device_describe"),
    ]
    assert [deltas[0], deltas[1]] == [c.arguments for c in done.tool_calls]
    assert json.loads(done.tool_calls[0].arguments) == {"points": ["hq/r204-ctl/analog-input:1"]}
    assert json.loads(done.tool_calls[1].arguments) == {"device": "r204-ctl"}
    assert done.text == "Reading both."


async def test_tool_call_edge_cases(zai: Callable[..., ZaiProvider]) -> None:
    """Arguments before the name are buffered; a missing id is synthesised;
    an object instead of a string is serialised; no arguments become {}."""
    rec = Recorder(
        stream_response(
            sse(
                chunk({"tool_calls": [tool_frag(0, args='{"a": ')]}),
                chunk({"tool_calls": [tool_frag(0, name="plan", args="1}")]}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_x",
                                "function": {
                                    "name": "manifest_get",
                                    "arguments": {"path": "/tags"},
                                },
                            }
                        ]
                    }
                ),
                chunk({"tool_calls": [tool_frag(2, id="call_y", name="test_run")]}),
                chunk({}, finish="tool_calls"),
            )
        )
    )
    events = await collect(zai(rec), [SEARCH])
    start = events[0]
    assert (
        isinstance(start, ToolCallStart) and start.name == "plan" and start.id.startswith("call_")
    )
    assert events[1:3] == [ToolCallDelta(0, '{"a": '), ToolCallDelta(0, "1}")]
    done = completed(events)
    assert [c.arguments for c in done.tool_calls] == ['{"a": 1}', '{"path": "/tags"}', "{}"]


async def test_tool_call_without_name_is_an_error(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(
                chunk({"tool_calls": [tool_frag(0, id="call_1", args="{}")]}),
                chunk({}, finish="tool_calls"),
            )
        )
    )
    with pytest.raises(LlmError, match="without a function name"):
        await collect(zai(rec), [SEARCH])


async def test_done_ends_the_stream(zai: Callable[..., ZaiProvider]) -> None:
    body = sse(chunk({"content": "ok"}), chunk({}, finish="stop")) + sse(
        chunk({"content": " ignored"}), done=False
    )
    events = await collect(zai(Recorder(stream_response(body))))
    assert completed(events).text == "ok"


async def test_finish_reason_without_done_is_accepted(zai: Callable[..., ZaiProvider]) -> None:
    body = sse(chunk({"content": "ok"}), chunk({}, finish="stop"), done=False)
    events = await collect(zai(Recorder(stream_response(body))))
    assert completed(events).finish_reason == "stop"


async def test_truncated_stream_is_an_error(zai: Callable[..., ZaiProvider]) -> None:
    body = sse(chunk({"content": "partial"}), done=False)
    events: list[LlmEvent] = []
    with pytest.raises(LlmError, match="ended before") as info:
        async for event in zai(Recorder(stream_response(body))).stream(USER, []):
            events.append(event)
    assert events == [TextDelta("partial")]
    assert info.value.retryable


async def test_done_without_finish_reason_infers_it(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(sse(chunk({"tool_calls": [tool_frag(0, id="c1", name="plan", args="{}")]})))
    )
    assert completed(await collect(zai(rec), [SEARCH])).finish_reason == "tool_calls"


@pytest.mark.parametrize("reason", ["sensitive", "length", "model_context_window_exceeded"])
async def test_finish_reasons_pass_through(zai: Callable[..., ZaiProvider], reason: str) -> None:
    rec = Recorder(stream_response(sse(chunk({"content": "Par"}), chunk({}, finish=reason))))
    done = completed(await collect(zai(rec)))
    assert (done.finish_reason, done.text) == (reason, "Par")


async def test_network_error_finish_reason_raises(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(sse(chunk({"content": "Par"}), chunk({}, finish="network_error")))
    )
    with pytest.raises(LlmError, match="network_error"):
        await collect(zai(rec))
    assert len(rec.requests) == 1


async def test_usage_with_cached_tokens(zai: Callable[..., ZaiProvider]) -> None:
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 42,
        "total_tokens": 5042,
        "prompt_tokens_details": {"cached_tokens": 4096},
    }
    rec = Recorder(stream_response(sse(chunk({"content": "x"}), chunk({}, "stop", usage))))
    assert completed(await collect(zai(rec))).usage == Usage(5000, 42, 4096)


async def test_malformed_and_keepalive_lines_are_skipped(zai: Callable[..., ZaiProvider]) -> None:
    body = (
        ": keep-alive\n\n"
        "event: message\r\n"
        "id: 1\r\n"
        f"data: {json.dumps(chunk({'content': 'A'}))}\r\n\r\n"
        "retry: 1000\n"
        "data: not json at all\n\n"
        "data: 42\n\n"
        'data: {"choices": null}\n\n'
        'data: {"choices": [{"index": 0, "delta": null}]}\n\n'
        'data: {"choices": ["junk", {"index": 1, "delta": {"content": "other choice"}}]}\n\n'
        f"data:{json.dumps(chunk({'content': 'B'}))}\n\n"
        "\n\n"
        'data: {"choices": [{"index": 0,\n'
        'data: "delta": {"content": "C"}}]}\n\n'
        "data: {broken\n"
        f"data: {json.dumps(chunk({'content': 'D'}, finish='stop'))}\n"
        "data: [DONE]\n"
    ).encode()
    events = await collect(zai(Recorder(stream_response(body))))
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["A", "B", "C", "D"]
    assert completed(events).text == "ABCD"


# Request -------------------------------------------------------------------------
async def test_request_shape_with_tools(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    provider = zai(rec, reasoning_effort="low", max_tokens=8192)
    await collect(provider, [SEARCH], reasoning_effort="high")
    request = rec.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.z.ai/api/paas/v4/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert request.headers["accept"] == "text/event-stream"
    body = rec.body()
    assert body == {
        "model": "glm-5.3",
        "messages": USER,
        "stream": True,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 8192,
        "tools": [SEARCH.to_openai()],
        "tool_choice": "auto",
        "tool_stream": True,
    }


async def test_request_shape_without_tools(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    provider = zai(
        rec,
        base_url="http://llm.local/v4/",
        model="glm-5.2",
        reasoning_effort="max",
        clear_thinking=False,
        temperature=0.6,
    )
    await collect(provider, max_tokens=256)
    assert str(rec.requests[0].url) == "http://llm.local/v4/chat/completions"
    body = rec.body()
    assert "tools" not in body and "tool_choice" not in body and "tool_stream" not in body
    assert body["model"] == "glm-5.2"
    assert body["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert (body["reasoning_effort"], body["max_tokens"], body["temperature"]) == ("max", 256, 0.6)


async def test_defaults_omit_optional_fields(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    await collect(zai(rec))
    assert set(rec.body()) == {"model", "messages", "stream", "thinking"}


# API key -----------------------------------------------------------------------------
async def test_missing_api_key(
    zai: Callable[..., ZaiProvider], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UC_TEST_ZAI_KEY", raising=False)
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    provider = zai(rec, api_key=None, api_key_env="UC_TEST_ZAI_KEY")
    assert not provider.configured
    with pytest.raises(LlmError, match="UC_TEST_ZAI_KEY"):
        await collect(provider)
    assert rec.requests == []

    monkeypatch.setenv("UC_TEST_ZAI_KEY", "from-env")
    assert provider.configured
    await collect(provider)
    assert rec.requests[0].headers["authorization"] == "Bearer from-env"


@pytest.mark.parametrize("key", ["sk-\u200bpasted", "sk-caf\u00e9", "sk-secret\nX-Evil: 1"])
async def test_api_key_with_illegal_characters(zai: Callable[..., ZaiProvider], key: str) -> None:
    """A mis-pasted key must give a clear LlmError, not a UnicodeEncodeError
    or an HTTP-layer error that quotes the header (and the key) back."""
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    with pytest.raises(LlmError, match="not allowed in an HTTP header") as info:
        await collect(zai(rec, api_key=key))
    assert key.split()[0] not in str(info.value)
    assert rec.requests == []


async def test_api_key_with_newline_is_not_echoed_by_a_real_transport() -> None:
    async def swallow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            await reader.read(1024)
        writer.close()

    server = await asyncio.start_server(swallow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    provider = ZaiProvider(
        api_key="sk-SECRET-1234\nX-Evil: 1", base_url=f"http://127.0.0.1:{port}", max_retries=0
    )
    try:
        with pytest.raises(LlmError) as info:
            await collect(provider)
        assert "SECRET" not in str(info.value)
    finally:
        await provider.aclose()
        server.close()
        await server.wait_closed()


async def test_closed_provider_raises_llm_error(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(stream_response(sse(chunk({"content": "ok"}, "stop"))))
    provider = zai(rec)
    await provider.aclose()
    with pytest.raises(LlmError, match="closed"):
        await collect(provider)
    assert rec.requests == []


async def test_api_key_never_logged(
    zai: Callable[..., ZaiProvider], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rec = Recorder(
        error_response(429, "1302", "Rate limit reached for requests"),
        error_response(401, "1000", "Authentication Failed"),
    )
    with pytest.raises(LlmError) as info:
        await collect(zai(rec), [SEARCH])
    assert KEY not in caplog.text and KEY not in str(info.value)
    assert USER[0]["content"] not in caplog.text


# Errors and retries ----------------------------------------------------------------------
async def test_retry_on_429_then_success(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        error_response(429, "1302", "Rate limit reached for requests"),
        stream_response(sse(chunk({"content": "ok"}, "stop"))),
    )
    assert completed(await collect(zai(rec))).text == "ok"
    assert len(rec.requests) == 2
    assert rec.body(0) == rec.body(1)


async def test_retry_on_connect_error_then_success(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        httpx.ConnectError("connection refused"),
        httpx.Response(503, text="upstream unavailable"),
        stream_response(sse(chunk({"content": "ok"}, "stop"))),
    )
    assert completed(await collect(zai(rec))).text == "ok"
    assert len(rec.requests) == 3


async def test_retries_are_bounded(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(error_response(503, "1305", "The service may be temporarily overloaded"))
    with pytest.raises(LlmError) as info:
        await collect(zai(rec, max_retries=2))
    assert len(rec.requests) == 3
    assert info.value.status == 503 and info.value.retryable
    assert "1305" in str(info.value) and "3 attempts" in str(info.value)


async def test_connect_error_exhausted(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(httpx.ConnectError("connection refused"))
    with pytest.raises(LlmError, match="cannot reach Z.ai"):
        await collect(zai(rec, max_retries=1))
    assert len(rec.requests) == 2


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("1113", "Insufficient balance or no resource package."),
        ("1308", "Usage limit reached for 5 hours."),
        ("1313", "Your account's usage pattern does not comply with the Fair Usage Policy."),
        ("1314", "Your enterprise package has expired."),
        ("1315", "This API Key is limited to enterprise coding package scenarios."),
        ("1321", "Monthly spend limit reached."),
    ],
)
async def test_quota_429_is_not_retried(
    zai: Callable[..., ZaiProvider], code: str, message: str
) -> None:
    rec = Recorder(error_response(429, code, message))
    with pytest.raises(LlmError) as info:
        await collect(zai(rec))
    assert len(rec.requests) == 1
    assert info.value.status == 429 and not info.value.retryable
    assert f"code {code}" in str(info.value) and message in str(info.value)


@pytest.mark.parametrize("code", ["1302", "1305"])
async def test_rate_limit_429_is_retried(zai: Callable[..., ZaiProvider], code: str) -> None:
    rec = Recorder(
        error_response(429, code, "slow down"),
        stream_response(sse(chunk({"content": "ok"}, "stop"))),
    )
    assert completed(await collect(zai(rec))).text == "ok"
    assert len(rec.requests) == 2


async def test_api_error_body(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(error_response(400, "1214", "Parameter `messages` is invalid."))
    with pytest.raises(LlmError) as info:
        await collect(zai(rec))
    assert str(info.value) == "Z.ai API error 400 (code 1214): Parameter `messages` is invalid."
    assert info.value.status == 400 and not info.value.retryable
    assert len(rec.requests) == 1


async def test_error_body_without_json(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(httpx.Response(502, text="<html>Bad   gateway</html>"))
    with pytest.raises(LlmError, match=r"502: <html>Bad gateway</html>"):
        await collect(zai(rec, max_retries=0))


async def test_json_error_with_status_200(zai: Callable[..., ZaiProvider]) -> None:
    body = json.dumps({"error": {"code": "1261", "message": "Prompt too long"}}, indent=2)
    rec = Recorder(httpx.Response(200, text=body, headers={"content-type": "application/json"}))
    with pytest.raises(LlmError, match=r"instead of a stream \(code 1261\): Prompt too long"):
        await collect(zai(rec))
    assert len(rec.requests) == 1


async def test_failure_after_streaming_started_is_not_retried(
    zai: Callable[..., ZaiProvider],
) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield sse(chunk({"content": "Room 204 is"}), done=False)
        raise httpx.ReadError("connection reset by peer")

    rec = Recorder(
        lambda request: httpx.Response(200, content=body()),
        stream_response(sse(chunk({"content": "second"}, "stop"))),
    )
    events: list[LlmEvent] = []
    with pytest.raises(LlmError, match="interrupted") as info:
        async for event in zai(rec).stream(USER, []):
            events.append(event)
    assert events == [TextDelta("Room 204 is")]
    assert info.value.retryable
    assert len(rec.requests) == 1


async def test_error_chunk_mid_stream_is_not_retried(zai: Callable[..., ZaiProvider]) -> None:
    rec = Recorder(
        stream_response(
            sse(chunk({"content": "Room"}), {"error": {"code": "500", "message": "Internal Error"}})
        ),
        stream_response(sse(chunk({"content": "second"}, "stop"))),
    )
    with pytest.raises(LlmError, match=r"code 500\): Internal Error") as info:
        await collect(zai(rec))
    assert len(rec.requests) == 1
    assert info.value.retryable


@pytest.mark.parametrize("code", ["1301", "1261", "1214", "1113"])
async def test_permanent_error_chunk_is_not_retryable(
    zai: Callable[..., ZaiProvider], code: str
) -> None:
    """``retryable`` tells the agent whether trying the turn again can help:
    not for a content filter hit, a too-long prompt or an empty balance."""
    rec = Recorder(stream_response(sse({"error": {"code": code, "message": "no"}})))
    with pytest.raises(LlmError, match=f"code {code}") as info:
        await collect(zai(rec))
    assert not info.value.retryable


async def test_stalled_stream(zai: Callable[..., ZaiProvider]) -> None:
    async def body() -> AsyncIterator[bytes]:
        yield sse(chunk({"reasoning_content": "hmm"}), done=False)
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(LlmError, match="stalled: no data for 7 s"):
        await collect(zai(lambda request: httpx.Response(200, content=body()), timeout_s=7))


async def test_closing_the_stream_releases_the_response(zai: Callable[..., ZaiProvider]) -> None:
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse(chunk({"content": "first"}), done=False)
            yield sse(chunk({"content": "second"}, "stop"))

        async def aclose(self) -> None:
            Body.closed = True

    stream = zai(lambda request: httpx.Response(200, stream=Body())).stream(USER, [])
    assert isinstance(stream, AsyncGenerator)
    assert await anext(stream) == TextDelta("first")
    await stream.aclose()
    assert Body.closed


def test_retry_delay_honours_retry_after_and_cap() -> None:
    provider = ZaiProvider(api_key=KEY, retry_backoff_s=1.0, retry_max_delay_s=10.0)
    assert 0.5 <= provider._retry_delay(0, None) <= 1.0
    assert 4.0 <= provider._retry_delay(3, None) <= 8.0
    assert provider._retry_delay(0, "6") == 6.0
    assert provider._retry_delay(0, "120") == 10.0
    assert provider._retry_delay(10, None) == 10.0
    assert provider._retry_delay(0, "Wed, 21 Oct 2026 07:28:00 GMT") <= 1.0


def test_invalid_settings() -> None:
    with pytest.raises(ValueError):
        ZaiProvider(timeout_s=0)
    with pytest.raises(ValueError):
        ZaiProvider(max_retries=-1)


# SSE decoder ---------------------------------------------------------------------------------
def test_sse_decoder_multiline_and_done() -> None:
    dec = SseDecoder()
    out: list[Any] = []
    for line in ['data: {"a":', "data: 1}", "", "data: garbage", "data: [DONE]"]:
        out.extend(dec.feed(line))
    assert out == [{"a": 1}, DONE]


def test_sse_decoder_flushes_at_close() -> None:
    dec = SseDecoder()
    assert list(dec.feed('data: {"b":')) == []
    assert list(dec.feed("data: 2}")) == [{"b": 2}]
    assert list(dec.feed('data: {"c":')) == []
    assert list(dec.close()) == []
    assert dec.stray == ""
