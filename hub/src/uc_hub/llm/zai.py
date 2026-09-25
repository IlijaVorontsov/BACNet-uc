"""Z.ai GLM provider over the OpenAI-compatible chat completions endpoint.

Wire format (docs.z.ai/api-reference/llm/chat-completion, the tool-streaming
guide and the error-code reference, checked 2026-09):

- ``POST {base_url}/chat/completions`` with ``Authorization: Bearer <key>``.
- ``stream: true`` returns SSE ``data: <chunk>`` lines ending with
  ``data: [DONE]``. ``tool_stream: true`` makes tool-call arguments arrive
  in fragments; a fragment carries ``index``, and ``id``/``name`` normally
  only appear in the first fragment of each call.
- ``choices[0].delta`` holds ``reasoning_content``, ``content`` and
  ``tool_calls``; ``finish_reason`` is one of stop, tool_calls, length,
  sensitive, model_context_window_exceeded or network_error. ``usage`` has
  ``prompt_tokens_details.cached_tokens`` when the context cache was hit.
- Errors are ``{"error": {"code": "1302", "message": ...}}``. HTTP 429 is
  both "slow down" (1302, 1305) and "out of quota" (1113, 1308-1321); only the
  former is worth retrying. Failures after the stream started are reported
  through ``finish_reason`` (``network_error``) or an error chunk.

GLM-5.3 always reasons; ``reasoning_effort`` is low, high or max (default).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, AsyncIterator, Final, Iterator

import httpx

from .base import (
    Completed,
    LlmError,
    LlmEvent,
    LlmProvider,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallDelta,
    ToolCallStart,
    ToolDef,
    Usage,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL: Final = "https://api.z.ai/api/paas/v4"
DEFAULT_MODEL: Final = "glm-5.3"
DEFAULT_API_KEY_ENV: Final = "ZAI_API_KEY"

#: 429 business codes for an exhausted balance or plan limit, an expired
#: package or a key that may not make the call: retrying within seconds only
#: burns the retry budget.
_QUOTA_CODES: Final = frozenset({"1113", *(str(code) for code in range(1308, 1322))})
#: Error codes inside a started stream that another attempt can fix (server
#: errors, rate limits); others (content filter, prompt too long) cannot.
_TRANSIENT_CODES: Final = frozenset({"500", "1200", "1230", "1234", "1302", "1305"})
#: API keys are visible ASCII; anything else cannot go into an HTTP header.
_KEY_PATTERN: Final = re.compile(r"[\x21-\x7e]+")
#: Failures where the request cannot have reached the model, so sending it
#: again is always safe (a stale pooled connection shows up as a protocol error).
_RETRYABLE_TRANSPORT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError)
_MAX_ERROR_BODY: Final = 64 * 1024
_MAX_EVENT_CHARS: Final = 8 * 1024 * 1024


class _Done:
    """Sentinel for ``data: [DONE]``."""


DONE: Final = _Done()


class SseDecoder:
    """Incremental decoder for the ``data:`` payloads of an SSE stream.

    Tolerant by design: comments (keep-alives), ``event:``/``id:`` fields,
    blank lines and payloads that are not JSON objects are skipped. A JSON
    payload is dispatched as soon as it is complete, so a server that omits
    the blank line between events still works; payloads split over several
    ``data:`` lines are joined as the SSE spec requires. Lines that are not
    SSE at all are kept (bounded) in ``stray``: a JSON error body sent with
    status 200 ends up there.
    """

    _SSE_FIELDS: Final = frozenset({"data", "event", "id", "retry"})

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._size = 0
        self._stray: list[str] = []
        self._stray_size = 0

    @property
    def stray(self) -> str:
        return "\n".join(self._stray)

    def feed(self, line: str) -> Iterator[dict[str, Any] | _Done]:
        line = line.rstrip("\r\n")
        if not line:
            yield from self._flush()
            return
        if line.startswith(":"):
            return
        field_name, _, value = line.partition(":")
        if field_name != "data":
            if field_name not in self._SSE_FIELDS and self._stray_size < _MAX_ERROR_BODY:
                self._stray.append(line)
                self._stray_size += len(line)
            return
        if value.startswith(" "):
            value = value[1:]
        if value.strip() == "[DONE]":
            yield from self._flush()
            yield DONE
            return
        if not self._buf:
            parsed = _loads(value)
            if parsed is not None:
                yield from _objects(parsed, value)
                return
        self._buf.append(value)
        self._size += len(value)
        if self._size > _MAX_EVENT_CHARS:
            logger.warning("dropping an oversized SSE event (%d chars)", self._size)
            self._reset()
            return
        parsed = _loads("\n".join(self._buf))
        if parsed is not None:
            self._reset()
            yield from _objects(parsed, value)
            return
        alone = _loads(value)
        if alone is not None:
            logger.debug("skipping %d unparsable SSE data line(s)", len(self._buf) - 1)
            self._reset()
            yield from _objects(alone, value)

    def close(self) -> Iterator[dict[str, Any] | _Done]:
        yield from self._flush()

    def _flush(self) -> Iterator[dict[str, Any] | _Done]:
        if not self._buf:
            return
        payload = "\n".join(self._buf)
        self._reset()
        parsed = _loads(payload)
        if parsed is None:
            logger.debug("skipping non-JSON SSE payload: %.80r", payload)
            return
        yield from _objects(parsed, payload)

    def _reset(self) -> None:
        self._buf.clear()
        self._size = 0


async def _sse_payloads(
    lines: AsyncIterator[str], decoder: SseDecoder
) -> AsyncGenerator[dict[str, Any] | _Done, None]:
    async for line in lines:
        for payload in decoder.feed(line):
            yield payload
    for payload in decoder.close():
        yield payload


def _loads(text: str) -> Any | None:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _objects(parsed: Any, raw: str) -> Iterator[dict[str, Any]]:
    if isinstance(parsed, dict):
        yield parsed
    else:
        logger.debug("skipping SSE payload that is not an object: %.80r", raw)


@dataclass(slots=True)
class _PendingCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    started: bool = False


class TurnAccumulator:
    """Folds streamed chunks into events and the final ``Completed``."""

    def __init__(self) -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, _PendingCall] = {}
        self.finish_reason: str | None = None
        self.usage = Usage()
        self.response_id: str | None = None
        #: True once a chunk arrived, i.e. the server really is streaming.
        self.started = False

    def feed(self, chunk: dict[str, Any]) -> list[LlmEvent]:
        _raise_for_error_chunk(chunk)
        self.started = True
        if self.response_id is None and isinstance(chunk.get("id"), str):
            self.response_id = chunk["id"]
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            self.usage = parse_usage(usage)
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return []
        events: list[LlmEvent] = []
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                events.extend(self._delta(delta))
            finish = choice.get("finish_reason")
            if isinstance(finish, str) and finish:
                self.finish_reason = finish
        return events

    def completed(self) -> Completed:
        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            call = self._calls[index]
            if not call.name:
                raise LlmError(
                    f"model streamed tool call #{index} without a function name", retryable=True
                )
            calls.append(ToolCall(call.id, call.name, "".join(call.arguments) or "{}"))
        return Completed(
            text="".join(self._text),
            reasoning="".join(self._reasoning),
            tool_calls=calls,
            finish_reason=self.finish_reason or ("tool_calls" if calls else "stop"),
            usage=self.usage,
        )

    def _delta(self, delta: dict[str, Any]) -> list[LlmEvent]:
        events: list[LlmEvent] = []
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self._reasoning.append(reasoning)
            events.append(ThinkingDelta(reasoning))
        content = delta.get("content")
        if isinstance(content, str) and content:
            self._text.append(content)
            events.append(TextDelta(content))
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for fragment in tool_calls:
                if isinstance(fragment, dict):
                    events.extend(self._tool_fragment(fragment))
        return events

    def _tool_fragment(self, fragment: dict[str, Any]) -> list[LlmEvent]:
        raw_id = fragment.get("id")
        frag_id = raw_id if isinstance(raw_id, str) else ""
        index = fragment.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = self._index_without_hint(frag_id)
        call = self._calls.setdefault(index, _PendingCall())
        if frag_id and not call.id:
            call.id = frag_id
        function = fragment.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name")
        if isinstance(name, str) and name and not call.name:
            call.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, (dict, list)):
            # Without tool_stream some servers send the arguments as an object.
            arguments = json.dumps(arguments, ensure_ascii=False)

        events: list[LlmEvent] = []
        if not call.started and call.name:
            call.started = True
            if not call.id:
                call.id = f"call_{uuid.uuid4().hex[:16]}"
            events.append(ToolCallStart(index, call.id, call.name))
            if call.arguments:
                events.append(ToolCallDelta(index, "".join(call.arguments)))
        if isinstance(arguments, str) and arguments:
            call.arguments.append(arguments)
            if call.started:
                events.append(ToolCallDelta(index, arguments))
        return events

    def _index_without_hint(self, frag_id: str) -> int:
        """Servers that omit ``index`` open a call with a new id and continue
        the latest call otherwise."""
        if frag_id:
            for index, call in self._calls.items():
                if call.id == frag_id:
                    return index
            return max(self._calls, default=-1) + 1
        return max(self._calls, default=0)


def parse_usage(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = raw.get("cached_tokens")
    return Usage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
        cached_tokens=_as_int(cached),
    )


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def parse_error_body(raw: bytes | str) -> tuple[str | None, str]:
    """``(code, message)`` from any of the error shapes Z.ai documents."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    data = _loads(text)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str) and err:
            return None, err
        if not isinstance(err, dict):
            err = data
        code = err.get("code")
        message = err.get("message") or err.get("msg") or ""
        return (str(code) if code not in (None, "") else None), str(message)
    return None, " ".join(text.split())[:300]


def _raise_for_error_chunk(chunk: dict[str, Any]) -> None:
    err = chunk.get("error")
    if not err and "choices" not in chunk and "code" in chunk and "message" in chunk:
        err = chunk
    if not err:
        return
    code, message = parse_error_body(json.dumps(err) if isinstance(err, dict) else str(err))
    detail = f" (code {code})" if code else ""
    raise LlmError(
        f"Z.ai API error during streaming{detail}: {message or 'no message'}",
        retryable=code is None or code in _TRANSIENT_CODES,
    )


class ZaiProvider(LlmProvider):
    """Streams GLM chat completions with reasoning and incremental tool calls.

    Requests are retried with exponential backoff on connection failures,
    HTTP 429 (except quota codes) and 5xx, but only until the server starts
    streaming: after that a retry could duplicate output the agent already
    forwarded, so failures surface as ``LlmError`` instead.
    """

    name = "zai"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: bool = True,
        clear_thinking: bool | None = None,
        timeout_s: float = 120.0,
        connect_timeout_s: float = 10.0,
        max_retries: int = 3,
        retry_backoff_s: float = 1.0,
        retry_max_delay_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_s <= 0 or connect_timeout_s <= 0:
            raise ValueError("timeouts must be positive")
        if max_retries < 0 or retry_backoff_s < 0 or retry_max_delay_s < 0:
            raise ValueError("retry settings must not be negative")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.thinking = thinking
        self.clear_thinking = clear_thinking
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.retry_max_delay_s = retry_max_delay_s
        self._api_key = api_key
        self._url = f"{self.base_url}/chat/completions"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=min(connect_timeout_s, timeout_s)),
            transport=transport,
        )

    @property
    def configured(self) -> bool:
        return _KEY_PATTERN.fullmatch(self._resolve_key()) is not None

    def _resolve_key(self) -> str:
        key = self._api_key if self._api_key is not None else os.environ.get(self.api_key_env, "")
        return key.strip()

    def _checked_key(self) -> str:
        """The key, or an LlmError that says where it comes from and never
        quotes it (the HTTP layer would echo an invalid header value)."""
        key = self._resolve_key()
        if not key:
            raise LlmError(
                f"Z.ai API key missing: set the {self.api_key_env} environment variable "
                "or configure llm.api_key_env"
            )
        if _KEY_PATTERN.fullmatch(key) is None:
            source = (
                "llm.api_key"
                if self._api_key is not None
                else f"the {self.api_key_env} environment variable"
            )
            raise LlmError(
                f"the Z.ai API key in {source} contains characters that are not allowed in "
                "an HTTP header (spaces, line breaks or characters pasted along with it)"
            )
        return key

    def build_request(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDef],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        thinking: dict[str, Any] = {"type": "enabled" if self.thinking else "disabled"}
        if self.clear_thinking is not None:
            thinking["clear_thinking"] = self.clear_thinking
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "thinking": thinking,
        }
        effort = reasoning_effort or self.reasoning_effort
        if effort:
            body["reasoning_effort"] = effort
        limit = max_tokens if max_tokens is not None else self.max_tokens
        if limit is not None:
            body["max_tokens"] = limit
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if tools:
            body["tools"] = [t.to_openai() for t in tools]
            body["tool_choice"] = "auto"
            body["tool_stream"] = True
        return body

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDef],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmEvent]:
        key = self._checked_key()
        body = self.build_request(
            messages, tools, reasoning_effort=reasoning_effort, max_tokens=max_tokens
        )
        logger.debug(
            "Z.ai request: model=%s messages=%d tools=%d effort=%s max_tokens=%s",
            self.model,
            len(messages),
            len(tools),
            body.get("reasoning_effort"),
            body.get("max_tokens"),
        )
        response = await self._send(body, key)
        acc = TurnAccumulator()
        decoder = SseDecoder()
        done = False
        try:
            async with contextlib.aclosing(
                _sse_payloads(response.aiter_lines(), decoder)
            ) as payloads:
                async for payload in payloads:
                    if isinstance(payload, _Done):
                        done = True
                        break
                    for event in acc.feed(payload):
                        yield event
        except httpx.TimeoutException as exc:
            raise LlmError(
                f"Z.ai stream stalled: no data for {self.timeout_s:g} s", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"Z.ai stream interrupted: {_describe(exc)}", retryable=True) from exc
        finally:
            await response.aclose()

        if acc.finish_reason == "network_error":
            raise LlmError(
                "Z.ai aborted the response (finish_reason network_error)", retryable=True
            )
        if not done and acc.finish_reason is None:
            if decoder.stray and not acc.started:
                code, message = parse_error_body(decoder.stray)
                detail = f" (code {code})" if code else ""
                raise LlmError(f"Z.ai returned an error instead of a stream{detail}: {message}")
            raise LlmError("Z.ai stream ended before the response was complete", retryable=True)
        completed = acc.completed()
        logger.debug(
            "Z.ai response %s: finish=%s prompt=%d completion=%d cached=%d tool_calls=%d",
            acc.response_id,
            completed.finish_reason,
            completed.usage.prompt_tokens,
            completed.usage.completion_tokens,
            completed.usage.cached_tokens,
            len(completed.tool_calls),
        )
        yield completed

    async def _send(self, body: dict[str, Any], key: str) -> httpx.Response:
        """POST the request and return the open streaming response (HTTP 200).

        The caller owns the response and must close it."""
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            if self._client.is_closed:
                # httpx would raise a bare RuntimeError, e.g. when the hub
                # shuts down during a retry pause.
                raise LlmError("the Z.ai provider is closed")
            request = self._client.build_request(
                "POST", self._url, content=content, headers=headers
            )
            retry_after: str | None = None
            try:
                response = await self._client.send(request, stream=True)
            except _RETRYABLE_TRANSPORT as exc:
                error = LlmError(
                    f"cannot reach Z.ai at {self.base_url}: {_describe(exc)}", retryable=True
                )
            except httpx.TimeoutException as exc:
                raise LlmError(
                    f"Z.ai did not respond within {self.timeout_s:g} s", retryable=True
                ) from exc
            except httpx.HTTPError as exc:
                raise LlmError(f"Z.ai request failed: {_describe(exc)}") from exc
            else:
                if response.status_code == 200:
                    return response
                retry_after = response.headers.get("retry-after")
                try:
                    error = await self._read_error(response)
                finally:
                    await response.aclose()

            if not error.retryable or attempt >= self.max_retries:
                if attempt:
                    raise LlmError(
                        f"{error} (gave up after {attempt + 1} attempts)",
                        status=error.status,
                        retryable=error.retryable,
                    ) from error
                raise error
            delay = self._retry_delay(attempt, retry_after)
            logger.warning(
                "Z.ai request failed (%s); retry %d/%d in %.1f s",
                error,
                attempt + 1,
                self.max_retries,
                delay,
            )
            await asyncio.sleep(delay)
            attempt += 1

    async def _read_error(self, response: httpx.Response) -> LlmError:
        status = response.status_code
        raw = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) >= _MAX_ERROR_BODY:
                    break
        except httpx.HTTPError as exc:
            logger.debug("could not read Z.ai error body: %s", _describe(exc))
        code, message = parse_error_body(bytes(raw[:_MAX_ERROR_BODY]))
        detail = f" (code {code})" if code else ""
        message = message or response.reason_phrase or "no message"
        retryable = (status == 429 and code not in _QUOTA_CODES) or status >= 500
        return LlmError(
            f"Z.ai API error {status}{detail}: {message}", status=status, retryable=retryable
        )

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        delay = self.retry_backoff_s * (2.0**attempt) * random.uniform(0.5, 1.0)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass  # HTTP-date form: keep the exponential backoff
        return max(0.0, min(delay, self.retry_max_delay_s))

    async def aclose(self) -> None:
        await self._client.aclose()


def _describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
