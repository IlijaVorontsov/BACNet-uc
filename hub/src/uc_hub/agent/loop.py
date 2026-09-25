"""Agent runs: the conversation loop between the model and the tools.

A run is one conversation (``POST /api/runs``). Each user message starts a
turn, driven by one asyncio task per run: the model's answer is streamed
into run events (``thinking.delta``, ``message.delta``, ``tool.call`` with
``{}`` as soon as a call starts, ``tool.args.delta`` while its arguments
stream, ``tool.call`` again with the complete arguments, ``message.done``),
then its tool calls run in order through the ``ToolRunner``, each ending in
``tool.result``, and the loop repeats until the model answers without tool
calls. Streamed text is coalesced into events of about 0.1 s each.

Tool calls: tier R and S run at once (``ask_user`` puts the run in
``waiting_answer`` until someone answers); tier L and C wait for an
approval (``waiting_approval``), except tier L calls of a run that holds a
run-wide approval (``scope: "run"``), which run under that approver's
roles and are audited as such. A rejected or expired approval is a failed
tool result the model reads.

A turn ends early, with an ``error`` event, when the model provider fails
(``llm``; the run stays usable, send a message to retry), stops the answer
(``sensitive`` or ``length``), the turn's tool call or active time budget
(``agent.max_tool_calls``, ``agent.max_wall_s``; time spent waiting for
people does not count) is used up (``budget``), or the same call fails
twice in a row (``repeated_failure``). Calls the model asked for but that
did not run get a failed result, so the history stays valid.

The history (OpenAI chat messages) is stored after every step. At startup
runs that were waiting for an approval or an answer continue waiting (the
approval or question is in the store); runs that were running become
``idle`` with an ``error`` event, because what their interrupted call did
is unknown. Tool call ids are only unique within a run.

MCP clients get a run of their own (``open_session``), in which their
calls appear with the same events and approvals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.errors import Conflict, HubError, InvalidRequest, NotFound, PolicyDenied
from ..core.types import Tier
from ..llm.base import Completed, LlmError, TextDelta, ThinkingDelta, ToolCallDelta, ToolCallStart, ToolDef
from ..policy.policy import role_rank
from ..tools import Prepared, ToolResult
from ..tools.session import answer_result
from .approvals import Decision
from .context import llm_messages, site_status
from .playbooks import Playbook, get_playbook
from .prompts import system_prompt

if TYPE_CHECKING:
    from ..runtime.services import Services

logger = logging.getLogger(__name__)

AGENT, MCP = "agent", "mcp"
ACTIVE_STATES = frozenset({"running", "waiting_approval", "waiting_answer"})
#: Tools that wait for a person to answer.
ASKING_TOOLS = frozenset({"ask_user"})
MAX_MESSAGE_CHARS = 20_000
_TITLE_CHARS = 60
_DELTA_FLUSH_S = 0.1
_DELTA_FLUSH_CHARS = 400
_OPERATOR = role_rank(["operator"])


@dataclass(slots=True, frozen=True)
class Grant:
    """A run-wide approval of tier L calls."""

    approval_id: str
    user: str
    roles: frozenset[str]


@dataclass(eq=False)
class _Run:
    id: str
    kind: str
    created_by: str
    #: Who sent the last message; tool calls run as this user.
    user: str
    roles: frozenset[str]
    playbook: Playbook | None
    messages: list[dict[str, Any]]
    state: str
    grant: Grant | None = None
    task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Tool calls of the current turn, when it started (monotonic) and time waited for people.
    calls: int = 0
    started: float = field(default_factory=time.monotonic)
    waited_s: float = 0.0
    last_failure: tuple[str, str] | None = None
    #: MCP sessions: calls in flight and the call counter.
    inflight: int = 0
    counter: int = 0

    def meta(self) -> dict[str, Any]:
        grant = self.grant
        return {
            "kind": self.kind,
            "user": self.user,
            "roles": sorted(self.roles),
            "playbook": self.playbook.id if self.playbook else None,
            "grant": {"approval_id": grant.approval_id, "user": grant.user, "roles": sorted(grant.roles)}
            if grant else None,
        }

    def active_s(self) -> float:
        return time.monotonic() - self.started - self.waited_s


class RunManager:
    def __init__(self, services: Services, *, sweep_interval_s: float = 5.0) -> None:
        self.services = services
        self.sweep_interval_s = sweep_interval_s
        self._runs: dict[str, _Run] = {}
        self._sweeper: asyncio.Task[None] | None = None

    # -- life cycle ---------------------------------------------------------------------
    async def start(self) -> None:
        """Pick up the runs a previous hub left active, then sweep expired approvals."""
        for row in await self.services.store.list_runs(10_000, states=ACTIVE_STATES):
            run = await self._get(row["id"])
            if run.kind == AGENT and run.state != "running":
                logger.info("run %s continues %s", run.id, run.state.replace("_", " for an "))
                run.task = asyncio.create_task(self._drive(run), name=f"run-{run.id}")
            else:
                await self._stop(run, "idle", "interrupted by a hub restart", code="restart",
                                 closing="the hub restarted while this call ran; what it did is unknown, check "
                                         "before trying again")
        self._sweeper = asyncio.create_task(self._sweep(), name="approval-sweep")

    async def stop(self) -> None:
        """Stop the run tasks. Their state stays in the store for the next start."""
        tasks = [t for t in (self._sweeper, *(r.task for r in self._runs.values())) if t is not None]
        self._sweeper = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(self.sweep_interval_s)
            try:
                await self.services.approvals.sweep()
            except Exception:
                logger.exception("the approval sweep failed")

    # -- runs -----------------------------------------------------------------------------------
    async def create_run(self, *, user: str, roles: set[str] | frozenset[str], message: str,
                         playbook: str | None = None) -> dict[str, Any]:
        """Start a run with its first message (and a playbook id); returns
        the ``RunSummary``."""
        book = get_playbook(playbook)
        text = _message(message)
        run_id = await self._create(AGENT, user, roles, _title(text, book), self.services.llm.model, book)
        run = self._runs[run_id]
        async with run.lock:
            await self._begin_turn(run, text, user, roles)
        return await self._summary(run_id)

    async def post_message(self, run_id: str, *, user: str, roles: set[str] | frozenset[str],
                           message: str) -> None:
        text = _message(message)
        run = await self._get(run_id)
        if run.kind != AGENT:
            raise Conflict(f"run {run_id} is an MCP session; its calls come from the MCP client")
        async with run.lock:
            if run.state in ACTIVE_STATES:
                raise Conflict(f"run {run_id} is {run.state.replace('_', ' ')}; send the message when its turn "
                               "has ended, or cancel it")
            await self._begin_turn(run, text, user, roles)

    async def cancel(self, run_id: str, *, user: str, roles: set[str] | frozenset[str]) -> None:
        """Stop a run: its task ends, its approvals and questions expire and
        the agent's leases it holds are released. An agent run between turns
        has nothing to stop; an MCP session is closed for good."""
        run = await self._get(run_id)
        _check_participant(run, user, roles, "cancel")
        async with run.lock:
            if run.state == "cancelled" or (run.kind == AGENT and run.state not in ACTIVE_STATES):
                return
            task = run.task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            reason = f"cancelled by {user}"
            await self.services.store.add_audit(user=user, run_id=run.id, action="run.cancel", outcome="ok",
                                                detail=reason)
            await self._stop(run, "cancelled", reason, closing=f"not run: the run was {reason}")
            leases = self.services.leases.active(run.id)
            await asyncio.gather(*(self.services.leases.release(lease.id, reason=f"run {reason}")
                                   for lease in leases), return_exceptions=True)

    async def answer(self, run_id: str, question_id: str, answer: str, *, user: str,
                     roles: set[str] | frozenset[str]) -> None:
        run = await self._get(run_id)
        _check_participant(run, user, roles, "answer questions of")
        if not answer.strip():
            raise InvalidRequest("the answer is empty")
        await self.services.questions.answer(question_id, answer, user=user, run_id=run_id)

    async def grant(self, run_id: str, approval: dict[str, Any], roles: frozenset[str]) -> None:
        """Record a run-wide approval (``ApprovalBroker.decide`` with scope run)."""
        run = await self._get(run_id)
        run.grant = Grant(approval["id"], approval["decided_by"], roles)
        await self.services.store.update_run(run.id, meta=run.meta())

    # -- MCP sessions ------------------------------------------------------------------------
    async def open_session(self, *, user: str, roles: set[str] | frozenset[str], title: str) -> str:
        """A run for the calls of one MCP client; returns its id."""
        return await self._create(MCP, user, roles, title[:_TITLE_CHARS], "mcp", None)

    async def session_call(self, run_id: str, name: str, args: dict[str, Any], *,
                           approval_wait_s: float) -> ToolResult:
        """Run one MCP tool call in its session run, with the run's events.
        An approval that nobody decides within ``approval_wait_s`` expires."""
        run = await self._get(run_id)
        if run.state == "cancelled":
            raise Conflict(f"a user cancelled this MCP session ({run_id}) in the web app; reconnect to start a new one")
        if name in ASKING_TOOLS:
            return ToolResult(False, "ask_user is not available over MCP; ask your own user", error_code="invalid")
        run.counter += 1
        call_id = f"mcp_{run.counter}"
        await self._emit(run, "tool.call", call_id=call_id, tool=name, tier=self._tier(name), args=args)
        run.inflight += 1
        await self._set_state(run, "running")
        started = time.monotonic()
        try:
            result = await self._call(run, call_id, name, args, approval_wait_s=approval_wait_s)
            await self._result_event(run, call_id, result, started)
        finally:
            run.inflight -= 1
            if run.inflight == 0 and run.state in ACTIVE_STATES:
                await self._set_state(run, "idle")
        return result

    # -- turns ------------------------------------------------------------------------------------
    async def _create(self, kind: str, user: str, roles: set[str] | frozenset[str], title: str, model: str,
                      book: Playbook | None) -> str:
        run = _Run(id="", kind=kind, created_by=user, user=user, roles=frozenset(roles), playbook=book,
                   messages=[], state="idle")
        row = await self.services.store.create_run(title=title, created_by=user, model=model, state="idle",
                                                   meta=run.meta())
        run.id = row["id"]
        self._runs[run.id] = run
        return run.id

    async def _begin_turn(self, run: _Run, text: str, user: str, roles: set[str] | frozenset[str]) -> None:
        run.user, run.roles = user, frozenset(roles)
        run.calls, run.waited_s, run.last_failure = 0, 0.0, None
        run.started = time.monotonic()
        run.messages += [{"role": "system", "content": await site_status(self.services, user, roles)},
                         {"role": "user", "content": text}]
        await self.services.store.save_messages(run.id, run.messages)
        await self.services.store.update_run(run.id, meta=run.meta())
        await self._emit(run, "message.user", text=text, user=user)
        await self._set_state(run, "running")
        run.task = asyncio.create_task(self._drive(run), name=f"run-{run.id}")

    async def _drive(self, run: _Run) -> None:
        """The loop of one turn: open tool calls first (also those a restart
        interrupted), then the model, until it answers without tool calls."""
        try:
            while True:
                pending = _open_calls(run.messages)
                if pending:
                    for index, call in enumerate(pending):
                        stop = await self._open_call(run, call)
                        if stop is not None:
                            await self._end_turn(run, stop[0], stop[1], [c["id"] for c in pending[index + 1:]])
                            return
                    continue
                last = run.messages[-1] if run.messages else {}
                if last.get("role") == "assistant":
                    break
                budget = self._budget_left(run)
                if budget is not None:
                    await self._end_turn(run, budget, "budget")
                    return
                if not await self._model_step(run):
                    return
            await self._set_state(run, "idle")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("run %s failed", run.id)
            await self._stop(run, "failed", f"internal error: {type(e).__name__}", code="internal",
                             closing="not run: the run failed")

    def _budget_left(self, run: _Run) -> str | None:
        """Why the turn must stop now, or None."""
        agent = self.services.config.agent
        if run.calls >= agent.max_tool_calls:
            return (f"the turn used all {agent.max_tool_calls} tool calls it may make (agent.max_tool_calls); "
                    "send a message to continue")
        if run.active_s() > agent.max_wall_s:
            return (f"the turn worked for more than {agent.max_wall_s:g} s (agent.max_wall_s); send a message to "
                    "continue")
        return None

    async def _model_step(self, run: _Run) -> bool:
        """One model call; False when the turn ended with it."""
        deltas = _Deltas(self, run)
        ids: dict[int, str] = {}
        completed: Completed | None = None
        messages = llm_messages(system_prompt(run.playbook), run.messages)
        try:
            async for event in self.services.llm.stream(messages, self._tool_defs(run.roles)):
                if isinstance(event, ThinkingDelta):
                    await deltas.add("thinking.delta", None, event.text)
                elif isinstance(event, TextDelta):
                    await deltas.add("message.delta", None, event.text)
                elif isinstance(event, ToolCallStart):
                    await deltas.flush()
                    ids[event.index] = call_id = _unique_id(run.messages, event.id, ids.values())
                    await self._emit(run, "tool.call", call_id=call_id, tool=event.name, tier=self._tier(event.name),
                                     args={})
                elif isinstance(event, ToolCallDelta):
                    if event.index in ids:
                        await deltas.add("tool.args.delta", ids[event.index], event.arguments_delta)
                elif isinstance(event, Completed):
                    completed = event
            await deltas.flush()
        except LlmError as e:
            await deltas.flush()
            await self._end_turn(run, f"the model could not answer: {e}", "llm")
            return False
        if completed is None:
            await self._end_turn(run, "the model's answer ended without being complete", "llm")
            return False
        if completed.text:
            await self._emit(run, "message.done", text=completed.text)
        if completed.finish_reason not in ("stop", "tool_calls"):
            await self._stopped_answer(run, completed, list(ids.values()))
            return False
        message: dict[str, Any] = {"role": "assistant", "content": completed.text}
        if completed.reasoning:
            message["reasoning_content"] = completed.reasoning
        if completed.tool_calls:
            calls: list[dict[str, Any]] = []
            for index, call in enumerate(completed.tool_calls):
                call_id = ids.get(index) or _unique_id(run.messages, call.id, [c["id"] for c in calls])
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": call.name, "arguments": call.arguments}})
                await self._emit(run, "tool.call", call_id=call_id, tool=call.name, tier=self._tier(call.name),
                                 args=_args_object(call.arguments))
            message["tool_calls"] = calls
        run.messages.append(message)
        await self.services.store.save_messages(run.id, run.messages)
        return True

    async def _stopped_answer(self, run: _Run, completed: Completed, started: list[str]) -> None:
        """The provider stopped the answer: its tool calls never run (they
        may be cut off), the calls already shown get a result, and the turn ends."""
        reason = completed.finish_reason
        if reason == "length":
            if completed.text:
                # Kept, so "continue" can pick up where the cut-off answer ended; its calls are dropped.
                run.messages.append({"role": "assistant", "content": completed.text})
                await self.services.store.save_messages(run.id, run.messages)
            text = ("the answer was cut off at the model's output limit (llm.max_tokens); nothing it asked for was "
                    "run. Ask it to continue, or give it a smaller task")
        elif reason == "sensitive":
            text = ("the model provider stopped the answer because it flagged the content as sensitive; rephrase "
                    "the request")
        else:
            text = f"the model stopped before its answer was complete (finish reason {reason})"
        for call_id in started:
            await self._emit(run, "tool.result", call_id=call_id, ok=False, summary="not run: the answer was stopped",
                             duration_ms=0)
        await self._end_turn(run, text, reason)

    async def _open_call(self, run: _Run, call: dict[str, Any]) -> tuple[str, str] | None:
        """Run one tool call of the model and record its result; returns
        (message, code) when the turn must stop."""
        budget = self._budget_left(run)
        if budget is not None:
            await self._close(run, [call["id"]], f"not run: {budget}")
            return budget, "budget"
        run.calls += 1
        function = call.get("function") or {}
        name, raw = str(function.get("name") or ""), str(function.get("arguments") or "")
        started = time.monotonic()
        result = await self._call(run, call["id"], name, raw)
        await self._result_event(run, call["id"], result, started)
        run.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result.to_model_text()})
        await self.services.store.save_messages(run.id, run.messages)
        if result.ok:
            run.last_failure = None
            return None
        failure = (name, _canonical(raw))
        if failure == run.last_failure:
            return (f"{name} failed twice in a row with the same arguments ({result.summary}); over to you",
                    "repeated_failure")
        run.last_failure = failure
        return None

    async def _call(self, run: _Run, call_id: str, name: str, args: dict[str, Any] | str, *,
                    approval_wait_s: float | None = None) -> ToolResult:
        """Prepare, approve and execute one call, or pick up the approval or
        question it was waiting for before a restart."""
        store = self.services.store
        waiting = [a for a in await store.list_approvals(run_id=run.id, limit=10_000) if a["call_id"] == call_id]
        if waiting:
            return await self._decided(run, await self._decision(run, waiting[0]["id"], approval_wait_s))
        if name in ASKING_TOOLS:
            asked = [q for q in await store.list_questions(run.id) if q["call_id"] == call_id]
            if asked:
                return await self._answered(run, asked[-1])
        ctx = self.services.context(run.user, run.roles, run_id=run.id, call_id=call_id)
        runner = self.services.runner
        prepared = await runner.prepare(ctx, name, args)
        if prepared.action == "refused":
            assert prepared.result is not None
            return prepared.result
        if prepared.action == "run":
            if name not in ASKING_TOOLS:
                return await runner.execute(prepared)
            async with self._waiting(run, "waiting_answer"):
                return await runner.execute(prepared)
        grant = run.grant
        if prepared.tier == "L" and grant is not None and runner.can_approve(prepared, grant.roles):
            await store.add_audit(user=grant.user, run_id=run.id, action="approval", tool=name, tier="L",
                                  args=prepared.args, outcome="approved",
                                  detail=f"covered by the run-wide approval {grant.approval_id} of {grant.user}")
            return await self._execute(prepared, f"{grant.user} for the run", grant.roles)
        row = await self.services.approvals.request(prepared, requested_by=run.user)
        return await self._decided(run, await self._decision(run, row["id"], approval_wait_s))

    async def _decision(self, run: _Run, approval_id: str, wait_s: float | None) -> Decision:
        approvals = self.services.approvals
        async with self._waiting(run, "waiting_approval"):
            try:
                return await approvals.wait(approval_id, wait_s)
            except TimeoutError:
                # Expired now, or decided just before: either way the decision is there to pick up.
                await approvals.expire([approval_id], f"nobody decided within {wait_s:g} s while the MCP client "
                                                      "waited")
                return await approvals.wait(approval_id)

    async def _decided(self, run: _Run, decision: Decision) -> ToolResult:
        row = decision.approval
        if decision.state == "approved":
            if decision.prepared is None:
                return ToolResult(False, "the call was approved, but the hub restarted before it ran, so it did not "
                                         "run; call it again if it is still needed", error_code="interrupted")
            return await self._execute(decision.prepared, row["decided_by"], decision.roles)
        if decision.state == "rejected":
            comment = f": {row['comment']}" if row["comment"] else ""
            return ToolResult(False, f"{row['decided_by']} rejected the call{comment}; nothing was changed",
                              error_code="rejected")
        why = row["comment"] or "nobody decided in time"
        return ToolResult(False, f"the approval expired ({why}); nothing was changed", error_code="expired")

    async def _execute(self, prepared: Prepared, approver: str, roles: frozenset[str]) -> ToolResult:
        try:
            return await self.services.runner.execute(prepared, approved_by=approver, approver_roles=roles)
        except PolicyDenied as e:
            return ToolResult(False, str(e), error_code=e.code)

    async def _answered(self, run: _Run, question: dict[str, Any]) -> ToolResult:
        """The result of an ``ask_user`` call whose question outlived a restart."""
        if question["answer"] is not None:
            return answer_result(question)
        left = question["asked_at"] + self.services.policy.approval_ttl_s - time.time()
        try:
            async with self._waiting(run, "waiting_answer"):
                return answer_result(await self.services.questions.wait(question["id"], left))
        except HubError as e:
            return ToolResult(False, str(e), error_code=e.code)

    def _waiting(self, run: _Run, state: str) -> _Waiting:
        return _Waiting(self, run, state)

    async def _end_turn(self, run: _Run, message: str, code: str, open_ids: list[str] | None = None) -> None:
        if open_ids:
            await self._close(run, open_ids, f"not run: {message}")
        await self._emit(run, "error", message=message, code=code)
        await self._set_state(run, "idle")

    async def _stop(self, run: _Run, state: str, reason: str, *, code: str | None = None,
                    closing: str) -> None:
        """End a run that cannot go on: expire what it waits for, close its
        open calls, report (``code``: as an error event) and set ``state``."""
        await self.services.approvals.expire_run(run.id, reason)
        await self.services.questions.expire_run(run.id)
        await self._close(run, [c["id"] for c in _open_calls(run.messages)], closing)
        if code is not None:
            await self._emit(run, "error", message=reason, code=code)
        await self._set_state(run, state, reason)

    async def _close(self, run: _Run, call_ids: list[str], text: str) -> None:
        if not call_ids:
            return
        result = ToolResult(False, text, error_code="not_run")
        for call_id in call_ids:
            run.messages.append({"role": "tool", "tool_call_id": call_id, "content": result.to_model_text()})
            await self._emit(run, "tool.result", call_id=call_id, ok=False, summary=text, duration_ms=0)
        await self.services.store.save_messages(run.id, run.messages)

    # -- events and state -------------------------------------------------------------------------
    async def _emit(self, run: _Run, event: str, **fields: Any) -> None:
        await self.services.events.append(run.id, event, **fields)

    async def _result_event(self, run: _Run, call_id: str, result: ToolResult, started: float) -> None:
        fields: dict[str, Any] = {"call_id": call_id, "ok": result.ok, "summary": result.summary,
                                  "duration_ms": round((time.monotonic() - started) * 1000)}
        if result.data is not None:
            fields["data"] = result.data
        if result.handle:
            fields["handle"] = result.handle
        await self._emit(run, "tool.result", **fields)

    async def _set_state(self, run: _Run, state: str, reason: str | None = None) -> None:
        if run.state == state and reason is None:
            return
        run.state = state
        await self.services.store.update_run(run.id, state=state)
        await self._emit(run, "run.state", state=state, **({"reason": reason} if reason else {}))

    async def _get(self, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is not None:
            return run
        row = await self.services.store.get_run(run_id)
        if row is None:
            raise NotFound(f"no run {run_id}")
        meta = await self.services.store.run_meta(run_id)
        grant = meta.get("grant")
        messages = await self.services.store.load_messages(run_id)
        try:
            book = get_playbook(meta.get("playbook"))
        except InvalidRequest:
            book = None
        run = _Run(
            id=run_id, kind=meta.get("kind", AGENT), created_by=row["created_by"],
            user=meta.get("user") or row["created_by"], roles=frozenset(meta.get("roles") or ()), playbook=book,
            messages=messages, state=row["state"],
            grant=Grant(grant["approval_id"], grant["user"], frozenset(grant["roles"])) if grant else None,
            calls=_calls_this_turn(messages),
        )
        return self._runs.setdefault(run_id, run)

    async def _summary(self, run_id: str) -> dict[str, Any]:
        row = await self.services.store.get_run(run_id)
        assert row is not None
        return row

    def _tier(self, name: str) -> Tier:
        tool = self.services.registry.get(name)
        return tool.tier if tool is not None else "R"

    def _tool_defs(self, roles: frozenset[str]) -> list[ToolDef]:
        """The tools this user's calls could run (a viewer's run sees read tools only)."""
        policy = self.services.policy
        return [t.tool_def() for t in self.services.registry.all()
                if not policy.check_tool(t.tier, roles, allowed_roles=t.roles).denied]


class _Waiting:
    """The run is in ``state`` while a call waits for people; the time does
    not count against the turn's budget."""

    def __init__(self, manager: RunManager, run: _Run, state: str) -> None:
        self.manager, self.run, self.state = manager, run, state
        self.since = 0.0

    async def __aenter__(self) -> None:
        self.since = time.monotonic()
        await self.manager._set_state(self.run, self.state)

    async def __aexit__(self, exc_type: type[BaseException] | None, *exc: object) -> None:
        self.run.waited_s += time.monotonic() - self.since
        # A cancelled or stopped run keeps the state its canceller set.
        if self.run.state == self.state and not (exc_type is not None and issubclass(exc_type, asyncio.CancelledError)):
            await self.manager._set_state(self.run, "running")


class _Deltas:
    """Streamed text as fewer events: one per kind (and tool call) about
    every 0.1 s or 400 characters, each stored like every other event."""

    def __init__(self, manager: RunManager, run: _Run) -> None:
        self.manager, self.run = manager, run
        self.key: tuple[str, str | None] | None = None
        self.parts: list[str] = []
        self.size = 0
        self.since = 0.0

    async def add(self, kind: str, call_id: str | None, text: str) -> None:
        if not text:
            return
        if self.key != (kind, call_id):
            await self.flush()
            self.key = (kind, call_id)
        if not self.parts:
            self.since = time.monotonic()
        self.parts.append(text)
        self.size += len(text)
        if self.size >= _DELTA_FLUSH_CHARS or time.monotonic() - self.since >= _DELTA_FLUSH_S:
            await self.flush()

    async def flush(self) -> None:
        if not self.parts or self.key is None:
            return
        text, (kind, call_id) = "".join(self.parts), self.key
        self.parts, self.size = [], 0
        if call_id is None:
            await self.manager._emit(self.run, kind, text=text)
        else:
            await self.manager._emit(self.run, kind, call_id=call_id, delta=text)


def _open_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls of the last assistant message that have no result yet."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "assistant":
            done = {m.get("tool_call_id") for m in messages[index + 1:] if m.get("role") == "tool"}
            return [c for c in message.get("tool_calls") or () if c.get("id") not in done]
        if message.get("role") == "user":
            return []
    return []


def _calls_this_turn(messages: list[dict[str, Any]]) -> int:
    count = 0
    for message in reversed(messages):
        if message.get("role") == "user":
            break
        if message.get("role") == "tool":
            count += 1
    return count


def _unique_id(messages: list[dict[str, Any]], proposed: str, taken: Any) -> str:
    """The provider's call id, unless it is empty or already used in the run."""
    used = {c.get("id") for m in messages if m.get("role") == "assistant" for c in m.get("tool_calls") or ()}
    used.update(taken)
    if proposed and proposed not in used and len(proposed) <= 64:
        return proposed
    n = len(used) + 1
    while f"call_{n}" in used:
        n += 1
    return f"call_{n}"


def _args_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _canonical(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except ValueError:
        return raw.strip()


def _message(text: Any) -> str:
    message = text.strip() if isinstance(text, str) else ""
    if not message:
        raise InvalidRequest("the message is empty")
    if len(message) > MAX_MESSAGE_CHARS:
        raise InvalidRequest(f"the message is longer than {MAX_MESSAGE_CHARS} characters")
    return message


def _title(message: str, book: Playbook | None) -> str:
    if book is not None:
        return book.title
    first = re.split(r"(?<=[.?!])\s|\n", message, maxsplit=1)[0]
    colon = first.find(":")
    title = (first[:colon] if colon > 0 else first).rstrip(".").strip() or "New run"
    return title if len(title) <= _TITLE_CHARS else title[: _TITLE_CHARS - 1] + "…"


def _check_participant(run: _Run, user: str, roles: set[str] | frozenset[str], what: str) -> None:
    """Who may act on someone's run: the user who started it, or an operator or above."""
    if user != run.created_by and role_rank(roles) < _OPERATOR:
        raise PolicyDenied(f"only {run.created_by}, who started run {run.id}, or an operator may {what} it")

