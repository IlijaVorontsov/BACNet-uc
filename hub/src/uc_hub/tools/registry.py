"""Tool registry contract.

A tool is a JSON-Schema-described function with a risk tier. The same
registry serves the agent loop (as LLM tool definitions) and the MCP server.
Tier semantics (docs/ai-harness/DESIGN.md section 7):

    R  read, no side effects                 -> runs automatically
    S  draft manifest / sandbox / simulation -> runs automatically
    L  live and reversible, under a lease    -> approval per policy
    C  commit (apply a plan, firmware)       -> always an explicit approval

The policy engine, not the tool, decides whether a call needs approval; a
handler only runs after the policy allowed it (or a human approved it).
``runner.ToolRunner`` does that for the agent loop and the MCP server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import jsonschema

from ..core.types import Tier
from ..llm.base import ToolDef


@dataclass(slots=True)
class ToolResult:
    ok: bool
    #: Short text for the model and the UI card (keep it under ~2 KB).
    summary: str
    #: Structured data. The runner stores results larger than the inline
    #: limit and replaces them by ``handle``; handlers just return everything.
    data: Any = None
    #: ``core.errors.HubError.code`` when ``ok`` is False.
    error_code: str | None = None
    #: ``result://r42`` when ``data`` was stored (read it with ``result_get``).
    handle: str | None = None

    def to_model_text(self, limit: int = 12000) -> str:
        body: dict[str, Any] = {"ok": self.ok, "summary": self.summary}
        if self.data is not None:
            body["data"] = self.data
        if self.handle:
            body["handle"] = self.handle
        if self.error_code:
            body["error"] = self.error_code
        text = json.dumps(body, default=str, ensure_ascii=False)
        if len(text) > limit:
            text = text[: limit - 40] + ' …"} [truncated]'
        return text


@dataclass(slots=True)
class ApprovalInfo:
    title: str
    #: One line per target: ``"<target>: <detail>"``.
    summary: list[str] = field(default_factory=list)
    diff: str = ""
    rollback: str = ""
    plan_id: str | None = None
    #: Targets the call changes; a tier C call on more than
    #: ``policy.max_devices_per_stage`` of them needs an admin.
    targets: int = 0
    #: Only an admin may approve (a plan adds or removes life-safety marks).
    admin_only: bool = False


@dataclass(slots=True)
class ToolCallContext:
    """Passed to every handler."""

    user: str
    roles: set[str]
    run_id: str | None
    call_id: str | None
    #: The site runtime (``uc_hub.runtime.site.SiteRuntime``); typed loosely
    #: to keep this module import-light.
    site: Any
    #: ``uc_hub.runtime.services.Services``: policy, store, live control,
    #: manifests, questions, ...
    services: Any


Handler = Callable[[ToolCallContext, dict[str, Any]], Awaitable[ToolResult]]
ApprovalDescriber = Callable[[ToolCallContext, dict[str, Any]], Awaitable[ApprovalInfo]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    tier: Tier
    parameters: dict[str, Any]
    handler: Handler
    #: Builds the approval card for L/C calls; a generic one is used if None.
    describe_approval: ApprovalDescriber | None = None
    #: Roles allowed to call the tool at all (empty = everyone).
    roles: frozenset[str] = frozenset()
    #: Seconds the handler may run; None is the runner's default, ``math.inf``
    #: no limit (a handler that waits for a person bounds the wait itself).
    timeout_s: float | None = None

    def tool_def(self) -> ToolDef:
        return ToolDef(self.name, f"[tier {self.tier}] {self.description}", self.parameters)

    def validate(self, args: dict[str, Any]) -> list[str]:
        v = jsonschema.Draft202012Validator(self.parameters)
        return [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in sorted(v.iter_errors(args), key=lambda e: list(e.absolute_path))
        ]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def tool_defs(self, roles: set[str] | None = None) -> list[ToolDef]:
        return [
            t.tool_def()
            for t in self._tools.values()
            if roles is None or not t.roles or t.roles & roles
        ]
