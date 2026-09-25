"""Questions the agent asks the user (``ask_user``).

A question is stored and announced as a ``question`` run event; the asking
tool call waits until someone answers (``POST /api/runs/{id}/answer``) or
the question expires. The answer is stored first, then announced as
``question.answered`` and handed to the waiting call. A question nobody
waits for any more (it timed out, or its run was cancelled) is marked
expired, so a late answer is refused instead of being announced to nobody.
``wait`` also serves a question asked before a hub restart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from ..core.errors import HubError, InvalidRequest, NotFound
from ..store import EventBus, Store

logger = logging.getLogger(__name__)


class QuestionExpired(HubError):
    code = "expired"


class Questions:
    def __init__(self, store: Store, events: EventBus) -> None:
        self.store = store
        self.events = events
        self._waiting: dict[str, asyncio.Future[str]] = {}

    async def ask(self, run_id: str, call_id: str | None, text: str, options: Sequence[str],
                  timeout_s: float) -> dict[str, Any]:
        """Ask and wait for the answer; returns the answered question row.
        Raises ``QuestionExpired`` when nobody answers within ``timeout_s``."""
        question = await self.store.create_question(run_id=run_id, call_id=call_id, text=text, options=options)
        qid = question["id"]
        # Registered before the event goes out, so an answer can never arrive unseen.
        future = self._future(qid)
        try:
            await self.events.append(run_id, "question", question_id=qid, call_id=call_id or "", text=text,
                                     options=list(options))
        except BaseException:
            self._waiting.pop(qid, None)
            raise
        return await self._wait(qid, future, timeout_s)

    async def wait(self, question_id: str, timeout_s: float) -> dict[str, Any]:
        """Wait for the answer of a question asked earlier (before a restart):
        the answered row at once when it is answered already;
        ``QuestionExpired`` when it expired or nobody answers in time."""
        return await self._wait(question_id, self._future(question_id), timeout_s)

    def _future(self, question_id: str) -> asyncio.Future[str]:
        future = self._waiting.get(question_id)
        if future is None or future.done():
            future = self._waiting[question_id] = asyncio.get_running_loop().create_future()
        return future

    async def _wait(self, qid: str, future: asyncio.Future[str], timeout_s: float) -> dict[str, Any]:
        try:
            row = await self.store.get_question(qid)
            if row is None:
                raise NotFound(f"no question {qid}")
            if row["answer"] is None:
                if row["expired_at"] is not None:
                    raise QuestionExpired("the question expired before it was answered")
                try:
                    async with asyncio.timeout(max(0.0, timeout_s)):
                        await future
                except TimeoutError:
                    # Nobody waits any more; an answer that raced the timeout still counts.
                    await self.store.expire_questions(ids=[qid])
                    row = await self.store.get_question(qid)
                    if row is None or row["answer"] is None:
                        raise QuestionExpired(f"nobody answered within {timeout_s:g} s") from None
                    return row
                row = await self.store.get_question(qid)
                assert row is not None
            return row
        finally:
            if self._waiting.get(qid) is future:
                del self._waiting[qid]

    async def answer(self, question_id: str, answer: str, *, user: str, run_id: str) -> dict[str, Any]:
        """Record the first answer (``Conflict`` for a second one, or after
        the question expired) to a question of run ``run_id``: a question of
        another run is not found, so access to one run never answers
        another. With ``options`` the answer must be one of them."""
        question = await self.store.get_question(question_id)
        if question is None or question["run_id"] != run_id:
            raise NotFound(f"run {run_id} has no question {question_id}")
        options = question.get("options") or []
        if options and answer not in options:
            raise InvalidRequest(f"answer with one of: {', '.join(options)}")
        row = await self.store.answer_question(question_id, answer, user=user)
        await self.events.append(question["run_id"], "question.answered", question_id=question_id,
                                 answer=answer, user=user)
        future = self._waiting.get(question_id)
        if future is not None and not future.done():
            future.set_result(answer)
        return row

    async def expire_run(self, run_id: str) -> list[str]:
        """Expire the open questions of a run that stopped (cancelled, or
        interrupted by a restart). Returns their ids."""
        expired = await self.store.expire_questions(run_id=run_id)
        for qid in expired:
            future = self._waiting.get(qid)
            if future is not None and not future.done():
                future.set_exception(QuestionExpired("the run stopped before the question was answered"))
        return expired
