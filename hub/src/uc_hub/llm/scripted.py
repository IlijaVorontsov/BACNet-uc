"""Deterministic provider for tests and the offline demo.

A script is an ordered list of rules. Each call looks only at the
conversation it is given, picks the first rule whose ``when`` matches and
streams its ``respond`` through the same events ``ZaiProvider`` emits, so the
agent loop, the UI and the tests exercise the real code paths without a
network or an API key.

    model: scripted-demo          # optional
    delay_s: 0.02                 # pause before every chunk (0 in tests)
    chunk_size: 12                # characters per streamed chunk
    rules:
      - id: start                 # optional, shown in logs
        when:                     # every given condition must hold
          user: '(?i)commission room (?P<room>\\d+)'  # regex on the last user message
          turn: 0                 # assistant turns since that message (int or {min, max})
          tool: site_search       # name (or list of names) of the last tool result in this turn
          ok: true                # its "ok" field
          result: '"plan_id": "(?P<plan_id>[^"]+)"'  # regex on its raw content
          always: true            # matches anything (for catch-all rules)
        respond:
          thinking: ...
          text: 'Looking up room ${room}.'
          tool_calls:
            - {name: site_search, args: {query: 'room ${room}'}}
          finish_reason: stop     # optional override
          error: ...              # optional: raise LlmError after thinking/text
    fallback: {text: ...}         # when no rule matches

Strings in ``respond`` are templates: ``${name}`` expands to a named group of
the ``user`` or ``result`` regex, or to ``user``, ``turn``, ``tool``,
``summary`` (of the last tool result), ``result`` (its raw content) and
``args.<key>`` (a top-level argument of the call that produced it). Unknown
placeholders are left as they are. ``args`` given as a string is sent
verbatim, which lets tests feed the agent invalid JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Mapping, Sequence

import yaml

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

DEMO_SCRIPT = Path(__file__).with_name("scripts") / "demo.yaml"

_WHEN_KEYS = frozenset({"user", "turn", "tool", "ok", "result", "always"})
_RESPOND_KEYS = frozenset({"thinking", "text", "tool_calls", "finish_reason", "error"})
_RULE_KEYS = frozenset({"id", "when", "respond"})
_SCRIPT_KEYS = frozenset({"model", "delay_s", "chunk_size", "rules", "fallback"})

_DEFAULT_FALLBACK_TEXT = "The scripted model has no response for this conversation."


class _Template(string.Template):
    #: Allows ``${args.device}``.
    idpattern = r"(?a:[_a-z][_a-z0-9]*(?:\.[_a-z0-9]+)*)"


@dataclass(slots=True, frozen=True)
class ScriptedToolCall:
    name: str
    #: Rendered to JSON, or sent verbatim when it is a string.
    args: Any = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class Response:
    thinking: str = ""
    text: str = ""
    tool_calls: tuple[ScriptedToolCall, ...] = ()
    finish_reason: str | None = None
    error: str | None = None


@dataclass(slots=True, frozen=True)
class When:
    user: re.Pattern[str] | None = None
    turn: tuple[int, int | None] | None = None
    tool: frozenset[str] | None = None
    ok: bool | None = None
    result: re.Pattern[str] | None = None
    always: bool = False


@dataclass(slots=True, frozen=True)
class Rule:
    id: str
    when: When
    respond: Response


@dataclass(slots=True)
class ConversationState:
    """What rules can see: derived from the messages alone."""

    user: str = ""
    #: Assistant messages since the last user message.
    turn: int = 0
    #: Last tool result of the current user turn, if any.
    tool: str | None = None
    tool_ok: bool | None = None
    result: str = ""
    summary: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    #: Tool calls in the whole conversation; makes call ids unique within it.
    call_count: int = 0
    prompt_chars: int = 0

    @classmethod
    def from_messages(cls, messages: Sequence[Mapping[str, Any]]) -> ConversationState:
        state = cls()
        calls: dict[str, tuple[str, str]] = {}
        last_user = -1
        for i, msg in enumerate(messages):
            content = _content_text(msg.get("content"))
            state.prompt_chars += len(content)
            role = msg.get("role")
            if role == "user":
                last_user = i
            elif role == "assistant":
                for tc in msg.get("tool_calls") or ():
                    if not isinstance(tc, Mapping):
                        continue
                    state.call_count += 1
                    fn = tc.get("function")
                    fn = fn if isinstance(fn, Mapping) else {}
                    if isinstance(tc.get("id"), str):
                        calls[tc["id"]] = (
                            str(fn.get("name") or ""),
                            str(fn.get("arguments") or ""),
                        )
        if last_user >= 0:
            state.user = _content_text(messages[last_user].get("content"))
        for msg in messages[last_user + 1 :]:
            role = msg.get("role")
            if role == "assistant":
                state.turn += 1
            elif role == "tool":
                name, raw_args = calls.get(str(msg.get("tool_call_id")), ("", ""))
                state.tool = str(msg.get("name") or name)
                state.result = _content_text(msg.get("content"))
                state.tool_ok, state.summary = _result_status(state.result)
                parsed = _loads(raw_args)
                state.args = parsed if isinstance(parsed, dict) else {}
        return state

    def variables(self) -> dict[str, str]:
        out = {
            "user": self.user,
            "turn": str(self.turn),
            "tool": self.tool or "",
            "summary": self.summary,
            "result": self.result,
        }
        for key, value in self.args.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[f"args.{key}"] = value if isinstance(value, str) else json.dumps(value)
        return out


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _result_status(content: str) -> tuple[bool | None, str]:
    """``ok`` and ``summary`` of a tool result in ``ToolResult.to_model_text`` form."""
    data = _loads(content)
    if not isinstance(data, dict):
        return None, content
    ok = data.get("ok")
    summary = data.get("summary")
    return (ok if isinstance(ok, bool) else None), (summary if isinstance(summary, str) else "")


def match_rule(when: When, state: ConversationState) -> dict[str, str] | None:
    """Captured template variables when ``when`` holds, else None."""
    captured: dict[str, str] = {}
    if when.user is not None:
        m = when.user.search(state.user)
        if m is None:
            return None
        captured.update({k: v for k, v in m.groupdict().items() if v is not None})
    if when.turn is not None:
        low, high = when.turn
        if state.turn < low or (high is not None and state.turn > high):
            return None
    if when.tool is not None and state.tool not in when.tool:
        return None
    if when.ok is not None and (state.tool is None or state.tool_ok is not when.ok):
        return None
    if when.result is not None:
        if state.tool is None:
            return None
        m = when.result.search(state.result)
        if m is None:
            return None
        captured.update({k: v for k, v in m.groupdict().items() if v is not None})
    return captured


def _render(value: Any, variables: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return _Template(value).safe_substitute(variables)
    if isinstance(value, list):
        return [_render(v, variables) for v in value]
    if isinstance(value, dict):
        return {_render(k, variables): _render(v, variables) for k, v in value.items()}
    return value


def _chunks(text: str, size: int) -> Iterator[str]:
    for start in range(0, len(text), size):
        yield text[start : start + size]


class ScriptedProvider(LlmProvider):
    name = "scripted"
    configured = True

    def __init__(
        self,
        rules: Sequence[Rule],
        fallback: Response | None = None,
        *,
        model: str = "scripted",
        delay_s: float = 0.0,
        chunk_size: int = 16,
    ) -> None:
        if delay_s < 0:
            raise ValueError("delay_s must not be negative")
        if chunk_size < 1:
            raise ValueError("chunk_size must be at least 1")
        self.rules = list(rules)
        self.fallback = fallback or Response(text=_DEFAULT_FALLBACK_TEXT)
        self.model = model
        self.delay_s = delay_s
        self.chunk_size = chunk_size

    # Loading -----------------------------------------------------------------
    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any] | Sequence[Any], **overrides: Any
    ) -> ScriptedProvider:
        """Build from a parsed script (a mapping, or a bare list of rules).

        ``overrides`` (model, delay_s, chunk_size) win over the script."""
        if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
            data = {"rules": list(data)}
        if not isinstance(data, Mapping):
            raise ValueError("script must be a mapping or a list of rules")
        unknown = set(data) - _SCRIPT_KEYS
        if unknown:
            raise ValueError(f"script: unknown keys {sorted(unknown)}")
        raw_rules = data.get("rules")
        if raw_rules is None:
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise ValueError("script: rules must be a list")
        rules = [_parse_rule(raw, i) for i, raw in enumerate(raw_rules)]
        fallback = data.get("fallback")
        options: dict[str, Any] = {
            k: data[k] for k in ("model", "delay_s", "chunk_size") if data.get(k) is not None
        }
        options.update({k: v for k, v in overrides.items() if v is not None})
        try:
            return cls(
                rules,
                _parse_response(fallback, "fallback") if fallback is not None else None,
                model=str(options.get("model", "scripted")),
                delay_s=float(options.get("delay_s", 0.0)),
                chunk_size=int(options.get("chunk_size", 16)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"script: {exc}") from exc

    @classmethod
    def from_yaml(cls, text: str, **overrides: Any) -> ScriptedProvider:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"script is not valid YAML: {exc}") from exc
        return cls.from_dict(data if data is not None else {}, **overrides)

    @classmethod
    def from_file(cls, path: str | Path, **overrides: Any) -> ScriptedProvider:
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"cannot read script {path}: {exc}") from exc
        try:
            return cls.from_yaml(text, **overrides)
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc

    @classmethod
    def demo(cls, **overrides: Any) -> ScriptedProvider:
        return cls.from_file(DEMO_SCRIPT, **overrides)

    # Answering ---------------------------------------------------------------
    def select(self, state: ConversationState) -> tuple[Rule | None, dict[str, str]]:
        for rule in self.rules:
            captured = match_rule(rule.when, state)
            if captured is not None:
                return rule, captured
        return None, {}

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[ToolDef],
        *,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LlmEvent]:
        state = ConversationState.from_messages(messages)
        rule, captured = self.select(state)
        response = rule.respond if rule else self.fallback
        logger.debug(
            "scripted: turn=%d tool=%s -> %s",
            state.turn,
            state.tool,
            rule.id if rule else "fallback",
        )
        variables = state.variables() | captured

        thinking = _render(response.thinking, variables)
        text = _render(response.text, variables)
        for piece in _chunks(thinking, self.chunk_size):
            await self._pause()
            yield ThinkingDelta(piece)
        for piece in _chunks(text, self.chunk_size):
            await self._pause()
            yield TextDelta(piece)
        if response.error is not None:
            await self._pause()
            raise LlmError(_render(response.error, variables), retryable=True)

        calls: list[ToolCall] = []
        for index, scripted in enumerate(response.tool_calls):
            call_id = f"call_{state.call_count + index + 1}"
            args = scripted.args
            arguments = (
                _render(args, variables)
                if isinstance(args, str)
                else json.dumps(_render(args, variables), ensure_ascii=False)
            )
            await self._pause()
            yield ToolCallStart(index, call_id, scripted.name)
            for piece in _chunks(arguments, self.chunk_size):
                await self._pause()
                yield ToolCallDelta(index, piece)
            calls.append(ToolCall(call_id, scripted.name, arguments))

        completion_chars = len(thinking) + len(text) + sum(len(c.arguments) for c in calls)
        yield Completed(
            text=text,
            reasoning=thinking,
            tool_calls=calls,
            finish_reason=response.finish_reason or ("tool_calls" if calls else "stop"),
            usage=Usage(
                prompt_tokens=state.prompt_chars // 4, completion_tokens=completion_chars // 4
            ),
        )

    async def _pause(self) -> None:
        # sleep(0) still yields to the loop, so a cancelled run stops promptly.
        await asyncio.sleep(self.delay_s)


# Script parsing ----------------------------------------------------------------
def _parse_rule(raw: Any, position: int) -> Rule:
    if not isinstance(raw, Mapping):
        raise ValueError(f"rule {position}: must be a mapping")
    rule_id = str(raw.get("id") or f"rule-{position}")
    where = f"rule {rule_id!r}"
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown keys {sorted(unknown)}")
    if "when" not in raw or "respond" not in raw:
        raise ValueError(f"{where}: needs both 'when' and 'respond'")
    return Rule(rule_id, _parse_when(raw["when"], where), _parse_response(raw["respond"], where))


def _parse_when(raw: Any, where: str) -> When:
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(f"{where}: 'when' must be a non-empty mapping (use always: true)")
    unknown = set(raw) - _WHEN_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown conditions {sorted(unknown)}")
    if "always" in raw and raw["always"] is not True:
        raise ValueError(f"{where}: 'always' can only be true")
    ok = raw.get("ok")
    if ok is not None and not isinstance(ok, bool):
        raise ValueError(f"{where}: 'ok' must be true or false")
    tool = raw.get("tool")
    tools: frozenset[str] | None = None
    if tool is not None:
        names = [tool] if isinstance(tool, str) else tool
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError(f"{where}: 'tool' must be a tool name or a list of names")
        tools = frozenset(names)
    return When(
        user=_compile(raw.get("user"), where, "user"),
        turn=_parse_turn(raw.get("turn"), where),
        tool=tools,
        ok=ok,
        result=_compile(raw.get("result"), where, "result"),
        always=raw.get("always") is True,
    )


def _compile(pattern: Any, where: str, key: str) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    if not isinstance(pattern, str):
        raise ValueError(f"{where}: '{key}' must be a regular expression string")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{where}: invalid '{key}' regex: {exc}") from exc


def _parse_turn(raw: Any, where: str) -> tuple[int, int | None] | None:
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw, raw
    if isinstance(raw, Mapping) and raw and set(raw) <= {"min", "max"}:
        low, high = raw.get("min", 0), raw.get("max")
        valid = isinstance(low, int) and low >= 0 and (high is None or isinstance(high, int))
        if valid and (high is None or high >= low):
            return low, high
    raise ValueError(f"{where}: 'turn' must be a count or {{min, max}}")


def _parse_response(raw: Any, where: str) -> Response:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: 'respond' must be a mapping")
    unknown = set(raw) - _RESPOND_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown response keys {sorted(unknown)}")
    for key in ("thinking", "text", "finish_reason", "error"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            raise ValueError(f"{where}: '{key}' must be a string")
    calls = raw.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError(f"{where}: 'tool_calls' must be a list")
    parsed: list[ScriptedToolCall] = []
    for call in calls:
        if (
            not isinstance(call, Mapping)
            or not isinstance(call.get("name"), str)
            or not call["name"]
        ):
            raise ValueError(f"{where}: every tool call needs a name")
        if set(call) - {"name", "args"}:
            raise ValueError(f"{where}: tool call {call['name']!r} has keys other than name, args")
        args = call.get("args")
        # A bare "args:" in YAML is null; the model contract is a JSON object.
        parsed.append(ScriptedToolCall(call["name"], {} if args is None else args))
    if not (raw.get("thinking") or raw.get("text") or parsed or raw.get("error")):
        raise ValueError(f"{where}: response is empty")
    return Response(
        thinking=raw.get("thinking") or "",
        text=raw.get("text") or "",
        tool_calls=tuple(parsed),
        finish_reason=raw.get("finish_reason"),
        error=raw.get("error"),
    )
