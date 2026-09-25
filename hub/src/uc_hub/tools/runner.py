"""Running tool calls: one path for the agent loop and the MCP server.

``prepare`` validates the arguments against the tool's schema (errors come
back as a failed result with code ``invalid``, which the model can correct;
nothing is retried) and asks the policy whether the caller's roles may run
the tier now, only after an approval, or not at all. For an approval it
builds the card (``ApprovalInfo``) with the tool's ``describe_approval``,
which also rejects calls the policy would refuse anyway (a life-safety
point, an unknown plan), so nobody is asked to approve them. The card gets
the default time limit, and any failure to build it is a refused call.

``execute`` runs an approval call only for an approver whose roles pass
``can_approve`` (the tier's approver roles; an admin for a large plan or a
life-safety mark), runs the handler with a time limit, turns ``HubError``
into a failed result with its ``error_code``, stores results larger than the
inline limit (the model gets a ``result://`` handle and pages with
``result_get``) and audits every call: user, run, tool, tier, arguments and
outcome (also ``cancelled`` when the run is cancelled during the call).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from ..core.errors import HubError, PolicyDenied, ValidationFailed
from ..core.types import Tier
from ..store import Store
from .common import clean_text
from .registry import ApprovalInfo, Tool, ToolCallContext, ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
_DETAIL_LIMIT = 500
_ERROR_LIMIT = 1000


@dataclass(slots=True)
class Prepared:
    """A checked tool call. ``action`` says what the caller does next:
    ``run`` (call ``execute``), ``approval`` (ask a human with ``approval``,
    then ``execute`` with the approver) or ``refused`` (``result`` is the
    failed tool result to hand back)."""

    name: str
    args: dict[str, Any]
    ctx: ToolCallContext
    action: Literal["run", "approval", "refused"]
    reason: str
    tool: Tool | None = None
    approval: ApprovalInfo | None = None
    result: ToolResult | None = None

    @property
    def tier(self) -> Tier | None:
        return self.tool.tier if self.tool is not None else None


class ToolRunner:
    def __init__(self, registry: ToolRegistry, store: Store, *, inline_limit: int = 4000,
                 default_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.registry = registry
        self.store = store
        self.inline_limit = inline_limit
        self.default_timeout_s = default_timeout_s

    async def prepare(self, ctx: ToolCallContext, name: str, args: dict[str, Any] | str) -> Prepared:
        """``args`` may be the model's raw JSON text."""
        tool = self.registry.get(name)
        parsed: Any = args
        if isinstance(args, str):
            try:
                parsed = json.loads(args, parse_constant=_no_constant) if args.strip() else {}
            except json.JSONDecodeError as e:
                return await self._refuse(ctx, name, {}, tool, "invalid",
                                          f"the arguments are not valid JSON ({e.msg} at position {e.pos})")
            except ValueError as e:
                return await self._refuse(ctx, name, {}, tool, "invalid", f"the arguments are not valid JSON ({e})")
        if tool is None:
            known = ", ".join(sorted(t.name for t in self.registry.all()))
            return await self._refuse(ctx, name, parsed if isinstance(parsed, dict) else {}, None, "invalid",
                                      f"there is no tool {name!r} (tools: {known})")
        if not isinstance(parsed, dict):
            return await self._refuse(ctx, name, {}, tool, "invalid", "the arguments must be a JSON object")
        errors = tool.validate(parsed)
        if errors:
            return await self._refuse(ctx, name, parsed, tool, "invalid",
                                      "invalid arguments: " + "; ".join(errors[:10]), {"errors": errors})
        decision = ctx.services.policy.check_tool(tool.tier, ctx.roles, allowed_roles=tool.roles)
        if decision.denied:
            return await self._refuse(ctx, name, parsed, tool, "denied", decision.reason)
        if decision.allowed:
            return Prepared(name, parsed, ctx, "run", decision.reason, tool)
        # The card reads devices (current values, IO bindings), so it gets the same bound as a call.
        limit = self.default_timeout_s
        try:
            async with asyncio.timeout(limit):
                info = await (tool.describe_approval or _generic_approval(tool))(ctx, parsed)
        except HubError as e:
            return await self._refuse(ctx, name, parsed, tool, e.code, clean_text(str(e), _ERROR_LIMIT),
                                      _error_data(e))
        except TimeoutError:
            return await self._refuse(ctx, name, parsed, tool, "timeout",
                                      f"{name}: the approval could not be prepared within {limit:g} s")
        except Exception as e:
            logger.exception("preparing the approval of %s failed", name)
            return await self._refuse(ctx, name, parsed, tool, "error", clean_text(
                f"{name}: the approval could not be prepared: {type(e).__name__}: {e}", _ERROR_LIMIT))
        return Prepared(name, parsed, ctx, "approval", decision.reason, tool, approval=info)

    def can_approve(self, prepared: Prepared, roles: set[str] | frozenset[str]) -> bool:
        """Whether a user with ``roles`` may approve this call."""
        if prepared.action != "approval" or prepared.tool is None or prepared.approval is None:
            return False
        policy = prepared.ctx.services.policy
        if not policy.can_approve(prepared.tool.tier, roles, targets=prepared.approval.targets):
            return False
        return not prepared.approval.admin_only or "admin" in roles

    async def execute(self, prepared: Prepared, *, approved_by: str | None = None,
                      approver_roles: Iterable[str] = ()) -> ToolResult:
        """Run a prepared call. An ``approval`` call needs ``approved_by``,
        whose ``approver_roles`` must pass ``can_approve``: ``PolicyDenied``
        otherwise, so the approval rules hold whoever drives the runner."""
        if prepared.action == "refused":
            assert prepared.result is not None
            return prepared.result
        tool, ctx = prepared.tool, prepared.ctx
        assert tool is not None
        if prepared.action == "approval":
            if approved_by is None:
                raise ValueError(f"{prepared.name} needs an approval before it runs")
            if not self.can_approve(prepared, frozenset(approver_roles)):
                raise PolicyDenied(f"{approved_by} may not approve this tier {tool.tier} call")
        limit = tool.timeout_s if tool.timeout_s is not None else self.default_timeout_s
        started = time.monotonic()
        approval = f" (approved by {approved_by})" if approved_by is not None else ""
        try:
            async with asyncio.timeout(None if math.isinf(limit) else limit):
                result = await tool.handler(ctx, prepared.args)
        except asyncio.CancelledError:
            # The run was cancelled mid-call; a live or commit call may have acted already.
            await self._audit(ctx, prepared.name, tool.tier, prepared.args, "cancelled",
                              f"{tool.name} was cancelled{approval}")
            raise
        except HubError as e:
            # Error texts can quote a device (an SMP "rsn", a payload value).
            result = ToolResult(False, clean_text(str(e) or type(e).__name__, _ERROR_LIMIT), _error_data(e), e.code)
        except TimeoutError:
            result = ToolResult(False, f"{tool.name} did not finish within {limit:g} s", error_code="timeout")
        except Exception as e:
            logger.exception("tool %s failed", tool.name)
            result = ToolResult(False, clean_text(f"internal error in {tool.name}: {type(e).__name__}: {e}",
                                                  _ERROR_LIMIT), error_code="error")
        result = await self._store_large(result, ctx)
        result.duration_ms = round((time.monotonic() - started) * 1000)
        detail = result.summary[:_DETAIL_LIMIT] + approval
        outcome = "ok" if result.ok else (result.error_code or "failed")
        await self._audit(ctx, prepared.name, tool.tier, prepared.args, outcome, detail)
        logger.info("tool %s by %s (run %s): %s in %d ms", tool.name, ctx.user, ctx.run_id, outcome,
                    result.duration_ms)
        return result

    async def _store_large(self, result: ToolResult, ctx: ToolCallContext) -> ToolResult:
        if result.data is None:
            return result
        size = len(json.dumps(result.data, default=str, ensure_ascii=False).encode())
        if size <= self.inline_limit:
            return result
        handle = f"result://{await self.store.put_result(result.data, run_id=ctx.run_id)}"
        return ToolResult(result.ok, result.summary, None, result.error_code, handle)

    async def _refuse(self, ctx: ToolCallContext, name: str, args: dict[str, Any], tool: Tool | None, code: str,
                      message: str, data: Any = None) -> Prepared:
        result = ToolResult(False, message, data, code)
        await self._audit(ctx, name, tool.tier if tool is not None else None, args, code, message[:_DETAIL_LIMIT])
        return Prepared(name, args, ctx, "refused", message, tool, result=result)

    async def _audit(self, ctx: ToolCallContext, name: str, tier: Tier | None, args: dict[str, Any],
                     outcome: str, detail: str) -> None:
        try:
            await self.store.add_audit(user=ctx.user, run_id=ctx.run_id, action="tool", tool=name, tier=tier,
                                       args=args, outcome=outcome, detail=detail)
        except Exception:
            logger.exception("audit of %s failed", name)


def _no_constant(name: str) -> Any:
    """``NaN`` and ``Infinity`` are not JSON; Python's parser would take them."""
    raise ValueError(f"{name} is not a JSON value")


def _error_data(error: HubError) -> Any:
    return {"errors": error.errors} if isinstance(error, ValidationFailed) and error.errors else None


def _generic_approval(tool: Tool) -> Any:
    async def describe(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
        text = json.dumps(args, ensure_ascii=False, sort_keys=True)
        return ApprovalInfo(title=f"{tool.name} (tier {tool.tier})", summary=[f"{tool.name}: {text[:300]}"])

    return describe
