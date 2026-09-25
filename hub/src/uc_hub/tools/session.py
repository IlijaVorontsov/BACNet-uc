"""Conversation tools (tier R): page through a stored result, ask the user."""

from __future__ import annotations

import json
import math
from typing import Any

from ..core.errors import InvalidRequest
from .registry import Tool, ToolCallContext, ToolResult

_OVERHEAD = 400


async def result_get(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    """A page of a stored result: items of its list (or of the one list
    inside an object), else a slice of its JSON text. Pages are sized to
    stay under the inline limit, so they are never stored again."""
    handle = args["handle"]
    data = await ctx.services.store.get_result(handle, run_id=ctx.run_id, owner_only=True)
    offset = args.get("offset", 0)
    budget = ctx.services.runner.inline_limit - _OVERHEAD
    items, key = _list_in(data)
    if items is not None:
        limit = args.get("limit", 50)
        page = items[offset:offset + limit]
        while page and len(json.dumps(page, default=str, ensure_ascii=False).encode()) > budget:
            page = page[: max(1, len(page) // 2)] if len(page) > 1 else []
        rest = {k: v for k, v in data.items() if k != key} if key is not None else {}
        fits = len(json.dumps(rest, default=str, ensure_ascii=False).encode()) <= budget // 4
        body = {"handle": handle, "key": key, "total": len(items), "offset": offset, "items": page,
                "next_offset": offset + len(page) if offset + len(page) < len(items) else None,
                "other": rest if fits else None}
        where = f" of {key}" if key else ""
        return ToolResult(True, f"items {offset}..{offset + len(page) - 1}{where} of {len(items)} in {handle}"
                          if page else f"no items at offset {offset} ({len(items)} in {handle})", body)
    text = json.dumps(data, default=str, ensure_ascii=False)
    chunk = text[offset:offset + budget // 2]
    end = offset + len(chunk)
    return ToolResult(True, f"characters {offset}..{end} of {len(text)} of {handle}",
                      {"handle": handle, "total": len(text), "offset": offset, "text": chunk,
                       "next_offset": end if end < len(text) else None})


def _list_in(data: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        lists = [k for k, v in data.items() if isinstance(v, list)]
        if len(lists) == 1:
            return data[lists[0]], lists[0]
    return None, None


async def ask_user(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    if ctx.run_id is None:
        raise InvalidRequest("ask_user needs an agent run whose user can answer; ask your user directly")
    options = list(dict.fromkeys(args.get("options") or []))
    timeout = ctx.services.policy.approval_ttl_s
    question = await ctx.services.questions.ask(ctx.run_id, ctx.call_id, args["question"], options, timeout)
    return answer_result(question)


def answer_result(question: dict[str, Any]) -> ToolResult:
    """The ``ask_user`` result for an answered question row (also used when
    a run waits again for a question asked before a restart)."""
    return ToolResult(True, f"{question['answered_by']} answered: {question['answer']}",
                      {"answer": question["answer"], "answered_by": question["answered_by"]})


TOOLS = [
    Tool(
        "result_get",
        "Page through a large tool result stored as result://rN: items of its list (offset, limit), or, "
        "when it has no list, a slice of its JSON text starting at character offset.",
        "R",
        {"type": "object", "additionalProperties": False, "required": ["handle"], "properties": {
            "handle": {"type": "string", "pattern": "^(result://)?r[0-9]{1,18}$"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        }},
        result_get,
    ),
    Tool(
        "ask_user",
        "Ask the user a question and wait for the answer (buttons for options; free text when there are "
        "none). Use it for what only a person can check on site, e.g. whether a valve moved.",
        "R",
        {"type": "object", "additionalProperties": False, "required": ["question"], "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 500},
            "options": {"type": "array", "maxItems": 10, "items": {"type": "string", "minLength": 1, "maxLength": 80}},
        }},
        ask_user,
        timeout_s=math.inf,
    ),
]
