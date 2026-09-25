"""Approvals of tier L and C tool calls, shared by agent runs and the MCP
server.

``request`` stores the approval card the runner built (``Prepared.approval``)
and announces it (``approval.request``); the caller then ``wait``s for the
decision. ``decide`` is the API's ``POST /api/approvals/{id}``: an approve
re-checks the stored call with ``ToolRunner.prepare`` (the policy, the plan
and the approval card may have changed since the request; targets and
admin-only marks are recomputed, not read from the row) and requires
``can_approve`` for the approver's roles, so the approval rules hold
whatever the client sends. A refused call stays pending: the approver sees
why and can reject it. ``scope: "run"`` (tier L only) also approves the
run's later tier L calls; the run keeps that grant (``RunManager.grant``).

Decisions, expiries (``sweep``, called periodically) and closing a run's
approvals are announced as ``approval.decided``, audited and handed to the
waiting call. A decision reached while nobody waits (the hub restarted
since the request) is recorded but runs nothing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import Conflict, InvalidRequest, NotFound, PolicyDenied, error_for
from ..policy import Policy
from ..tools import Prepared, ToolCallContext

if TYPE_CHECKING:
    from ..runtime.services import Services

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Decision:
    """How an approval ended. ``prepared`` (the call as re-checked when it
    was approved) and ``roles`` (the approver's) are set only for an
    approval decided while its call was waiting."""

    approval: dict[str, Any]
    prepared: Prepared | None = None
    roles: frozenset[str] = frozenset()

    @property
    def state(self) -> str:
        return str(self.approval["state"])


class ApprovalBroker:
    def __init__(self, services: Services) -> None:
        self.services = services
        self._waiting: dict[str, asyncio.Future[Decision]] = {}

    async def request(self, prepared: Prepared, *, requested_by: str) -> dict[str, Any]:
        """Store the approval of a prepared call and announce it. The
        decision is kept for ``wait`` even if it arrives before ``wait`` is called."""
        if prepared.action != "approval" or prepared.approval is None or prepared.tool is None:
            raise ValueError(f"{prepared.name} does not need an approval")
        info, ctx = prepared.approval, prepared.ctx
        row = await self.services.store.create_approval(
            run_id=ctx.run_id, call_id=ctx.call_id, tool=prepared.name, tier=prepared.tool.tier, title=info.title,
            requested_by=requested_by, summary=info.summary, diff=info.diff, rollback=info.rollback,
            plan_id=info.plan_id, args=prepared.args, ttl_s=self.services.policy.approval_ttl_s,
        )
        self._waiting[row["id"]] = asyncio.get_running_loop().create_future()
        if ctx.run_id is not None:
            await self.services.events.append(ctx.run_id, "approval.request", approval=row)
        return row

    async def wait(self, approval_id: str, timeout_s: float | None = None) -> Decision:
        """The decision of an approval: at once when it is no longer pending,
        else when someone decides or it expires. ``TimeoutError`` after
        ``timeout_s``: the approval stays pending and a later ``wait`` still
        gets its decision (``expire`` it to stop waiting for good)."""
        future = self._waiting.get(approval_id)
        if future is None:
            future = self._waiting[approval_id] = asyncio.get_running_loop().create_future()
        if not future.done():
            row = await self.services.store.get_approval(approval_id)
            if row is None or row["state"] != "pending":
                self._drop(approval_id, future)
                if row is None:
                    raise NotFound(f"no approval {approval_id}")
                return Decision(row)
        try:
            async with asyncio.timeout(timeout_s):
                decision = await asyncio.shield(future)
        except asyncio.CancelledError:
            self._drop(approval_id, future)
            raise
        self._drop(approval_id, future)
        return decision

    def _drop(self, approval_id: str, future: asyncio.Future[Decision]) -> None:
        if self._waiting.get(approval_id) is future:
            del self._waiting[approval_id]

    async def decide(self, approval_id: str, decision: str, *, user: str, roles: set[str] | frozenset[str],
                     comment: str | None = None, scope: str = "call") -> dict[str, Any]:
        """Approve or reject a pending approval (``POST /api/approvals/{id}``)."""
        store, policy = self.services.store, self.services.policy
        row = await store.get_approval(approval_id, with_args=True)
        if row is None:
            raise NotFound(f"no approval {approval_id}")
        if decision not in ("approve", "reject"):
            raise InvalidRequest(f"decision must be 'approve' or 'reject', got {decision!r}")
        if scope not in ("call", "run"):
            raise InvalidRequest(f"scope must be 'call' or 'run', got {scope!r}")
        if row["state"] != "pending":
            raise Conflict(f"approval {approval_id} is already {row['state']}")
        prepared: Prepared | None = None
        if decision == "approve":
            if scope == "run" and row["tier"] != "L":
                raise InvalidRequest("only tier L approvals can cover the rest of the run; tier C calls are "
                                     "approved one by one")
            prepared = await self.services.runner.prepare(await self._context(row), row["tool"], row["args"])
            if prepared.action == "refused":
                assert prepared.result is not None
                raise error_for(prepared.result.error_code or "error",
                                f"the hub refuses this call now: {prepared.reason}")
            if prepared.action != "approval" or not self.services.runner.can_approve(prepared, roles):
                raise PolicyDenied(_who_approves(row, prepared, policy))
        elif user != row["requested_by"] and not policy.can_approve(row["tier"], roles):
            raise PolicyDenied(f"only its requester or someone who may approve tier {row['tier']} calls may reject "
                               "this approval")
        decided = await store.decide_approval(approval_id, decision, user=user, comment=comment,
                                              scope=scope if decision == "approve" else "call")
        detail = row["title"] + (" (and the run's later tier L calls)" if decided["scope"] == "run" else "")
        await self._audit(decided, row["args"], user, detail + (f"; comment: {comment}" if comment else ""))
        if decided["scope"] == "run" and decided["run_id"] is not None:
            await self.services.runs.grant(decided["run_id"], decided, frozenset(roles))
        await self._announce(decided)
        self._resolve(Decision(decided, prepared, frozenset(roles)))
        return decided

    async def sweep(self) -> list[dict[str, Any]]:
        """Expire the approvals whose time is up; returns them."""
        expired = await self.services.store.expire_approvals(reason="nobody decided within the approval time")
        await self._closed(expired)
        return expired

    async def expire(self, ids: list[str], reason: str) -> list[dict[str, Any]]:
        """Expire these pending approvals (their caller stopped waiting)."""
        expired = await self.services.store.expire_approvals(ids=ids, reason=reason)
        await self._closed(expired)
        return expired

    async def expire_run(self, run_id: str, reason: str) -> list[dict[str, Any]]:
        """Expire the pending approvals of a run that stopped."""
        expired = await self.services.store.expire_approvals(run_id=run_id, reason=reason)
        await self._closed(expired)
        return expired

    async def _closed(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            await self._audit(row, None, "hub", f"{row['title']}: {row['comment'] or 'expired'}")
            await self._announce(row)
            self._resolve(Decision(row))

    async def _context(self, row: dict[str, Any]) -> ToolCallContext:
        """The context the call was requested in: the run's current user and roles."""
        meta: dict[str, Any] = {}
        if row["run_id"] is not None:
            try:
                meta = await self.services.store.run_meta(row["run_id"])
            except NotFound:
                meta = {}
        return self.services.context(meta.get("user") or row["requested_by"], meta.get("roles") or (),
                                     run_id=row["run_id"], call_id=row["call_id"])

    async def _announce(self, row: dict[str, Any]) -> None:
        if row["run_id"] is not None:
            try:
                await self.services.events.append(row["run_id"], "approval.decided", approval=row)
            except NotFound:
                logger.warning("approval %s belongs to run %s, which does not exist", row["id"], row["run_id"])

    async def _audit(self, row: dict[str, Any], args: Any, user: str, detail: str) -> None:
        try:
            await self.services.store.add_audit(user=user, run_id=row["run_id"], action="approval", tool=row["tool"],
                                                tier=row["tier"], args=args, outcome=row["state"], detail=detail)
        except Exception:
            logger.exception("audit of approval %s failed", row["id"])

    def _resolve(self, decision: Decision) -> None:
        future = self._waiting.get(decision.approval["id"])
        if future is not None and not future.done():
            future.set_result(decision)


def _who_approves(row: dict[str, Any], prepared: Prepared, policy: Policy) -> str:
    if prepared.approval is not None and prepared.approval.admin_only:
        return "only an admin may approve this call (it adds or removes a life-safety mark)"
    targets = prepared.approval.targets if prepared.approval is not None else 0
    if row["tier"] == "C" and targets > policy.settings.max_devices_per_stage:
        return (f"only an admin may approve a plan with {targets} targets (the site allows "
                f"{policy.settings.max_devices_per_stage} per stage)")
    who = "an operator, commissioner or admin" if row["tier"] == "L" else "a commissioner or admin"
    return f"only {who} may approve tier {row['tier']} calls"
