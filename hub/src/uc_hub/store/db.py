"""SQLite persistence: manifest revisions, runs and their events, approvals,
questions, the audit log, result blobs, leases, plans, plan backups and test
results.

One aiosqlite connection serves the hub. An asyncio lock serializes its use,
so a multi-statement transaction never interleaves with another coroutine's
statements. Every operation runs shielded: a cancelled caller never leaves a
transaction open or half applied (the operation completes, the caller just
does not see the result).

The ``*_json`` row helpers produce the shapes of ``docs/ai-harness/API.md``
where the API defines one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import aiosqlite

from ..core.errors import Conflict, InvalidRequest, NotFound
from ..core.types import TestResult

logger = logging.getLogger(__name__)

T = TypeVar("T")

RUN_STATES = ("running", "waiting_approval", "waiting_answer", "idle", "failed", "cancelled")
APPROVAL_STATES = ("pending", "approved", "rejected", "expired")
TIERS = ("R", "S", "L", "C")
LEASE_KINDS = ("write", "force")
LEASE_STATES = ("active", "released", "failed")
TEST_TARGETS = ("live", "sim")
PLAN_STATES = ("planned", "applying", "applied", "failed")
#: ``call``: the decision covers one tool call; ``run``: a tier L approval that
#: also approves the run's later tier L calls.
APPROVAL_SCOPES = ("call", "run")
#: Keys every run event carries; event fields may not reuse them.
EVENT_KEYS = frozenset({"seq", "run_id", "ts", "type"})

_DECISIONS = {"approve": "approved", "approved": "approved", "reject": "rejected", "rejected": "rejected"}
_LEASE_COLUMNS = frozenset({"state", "attempts", "expires_at", "released_at", "last_error"})
_HANDLE_RE = re.compile(r"^(?:result://)?r([0-9]{1,18})$")
_MAX_LIMIT = 10_000


# -- schema -------------------------------------------------------------------------------
# Migrations are append-only: never edit a released one, add the next version.
_V1: tuple[str, ...] = (
    """CREATE TABLE manifest_revisions (
        rev INTEGER PRIMARY KEY AUTOINCREMENT,
        yaml TEXT NOT NULL,
        author TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        run_id TEXT,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE manifest_heads (
        name TEXT PRIMARY KEY CHECK (name IN ('live', 'draft')),
        rev INTEGER NOT NULL REFERENCES manifest_revisions (rev),
        updated_at REAL NOT NULL
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('running', 'waiting_approval', 'waiting_answer', 'idle', 'failed', 'cancelled')),
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        last_seq INTEGER NOT NULL DEFAULT 0,
        messages TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX runs_by_created ON runs (created_at)",
    """CREATE TABLE run_events (
        run_id TEXT NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        ts REAL NOT NULL,
        data TEXT NOT NULL,
        PRIMARY KEY (run_id, seq)
    ) WITHOUT ROWID""",
    """CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        call_id TEXT,
        tool TEXT NOT NULL,
        tier TEXT NOT NULL CHECK (tier IN ('R', 'S', 'L', 'C')),
        title TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '[]',
        diff TEXT NOT NULL DEFAULT '',
        rollback TEXT NOT NULL DEFAULT '',
        plan_id TEXT,
        args TEXT NOT NULL DEFAULT '{}',
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending', 'approved', 'rejected', 'expired')),
        requested_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        requested_by TEXT NOT NULL,
        decided_by TEXT,
        decided_at REAL,
        comment TEXT
    )""",
    "CREATE INDEX approvals_by_state ON approvals (state, expires_at)",
    "CREATE INDEX approvals_by_run ON approvals (run_id)",
    """CREATE TABLE questions (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        call_id TEXT,
        text TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        asked_at REAL NOT NULL,
        answer TEXT,
        answered_by TEXT,
        answered_at REAL
    )""",
    "CREATE INDEX questions_by_run ON questions (run_id)",
    """CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        user TEXT NOT NULL,
        run_id TEXT,
        action TEXT NOT NULL,
        tool TEXT,
        tier TEXT,
        args TEXT NOT NULL DEFAULT 'null',
        outcome TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX audit_by_run ON audit (run_id)",
    """CREATE TABLE results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT,
        created_at REAL NOT NULL,
        data TEXT NOT NULL
    )""",
    """CREATE TABLE leases (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('write', 'force')),
        target TEXT NOT NULL,
        priority INTEGER,
        expires_at REAL NOT NULL,
        created_by TEXT NOT NULL,
        run_id TEXT,
        state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        released_at REAL,
        last_error TEXT
    )""",
    "CREATE INDEX leases_by_state ON leases (state)",
    """CREATE TABLE plan_backups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL,
        target TEXT NOT NULL,
        documents TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    "CREATE INDEX plan_backups_by_plan ON plan_backups (plan_id, target)",
    """CREATE TABLE test_results (
        target TEXT NOT NULL CHECK (target IN ('live', 'sim')),
        name TEXT NOT NULL,
        position INTEGER NOT NULL,
        status TEXT NOT NULL,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        failed_step INTEGER,
        detail TEXT NOT NULL DEFAULT '',
        revision INTEGER,
        updated_at REAL NOT NULL,
        PRIMARY KEY (target, name)
    )""",
)

# Plans are kept with their change payloads so an approved plan can be applied
# after a restart; backups are keyed by apply attempt so a re-apply never
# replaces what a target looked like before the first attempt.
_V2: tuple[str, ...] = (
    """CREATE TABLE plans (
        id TEXT PRIMARY KEY,
        revision INTEGER NOT NULL,
        base_revision INTEGER NOT NULL,
        data TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'planned'
            CHECK (state IN ('planned', 'applying', 'applied', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )""",
    "CREATE INDEX plans_by_revision ON plans (revision, created_at)",
    "ALTER TABLE plan_backups ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1",
    # Earlier backups of one plan target were separate attempts, in id order.
    """UPDATE plan_backups SET attempt = (
        SELECT COUNT(*) FROM plan_backups AS b
        WHERE b.plan_id = plan_backups.plan_id AND b.target = plan_backups.target AND b.id <= plan_backups.id
    )""",
    "CREATE UNIQUE INDEX plan_backups_by_attempt ON plan_backups (plan_id, target, attempt)",
)

# Run metadata (who drives the run, its playbook, a run-wide approval) lets a
# run resume after a restart; the approval scope and question expiry record
# decisions that were implicit before.
_V3: tuple[str, ...] = (
    "ALTER TABLE runs ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE approvals ADD COLUMN scope TEXT NOT NULL DEFAULT 'call' CHECK (scope IN ('call', 'run'))",
    "ALTER TABLE questions ADD COLUMN expired_at REAL",
)

MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((1, _V1), (2, _V2), (3, _V3))
SCHEMA_VERSION = MIGRATIONS[-1][0]


# -- JSON ---------------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> str:
    """Compact, strict JSON; objects with ``to_json`` (core types) serialize as
    their JSON form. Browsers parse this text as it is, so NaN and infinities
    (a faulty sensor) become null, as in JavaScript. Non-ASCII is escaped: a
    lone surrogate, which json.loads accepts from an untrusted payload, would
    make SQLite reject the text."""
    try:
        return _dumps(value)
    except ValueError:
        return _dumps(_finite(value))


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False, default=_json_default)


def _finite(value: Any) -> Any:
    """``value`` with non-finite floats replaced by None (the slow path of ``dumps``)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    try:
        return _finite(_json_default(value))
    except TypeError:
        return value  # json.dumps reports it


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def _limit(value: int) -> int:
    return max(1, min(int(value), _MAX_LIMIT))


# -- row -> JSON ----------------------------------------------------------------------------
def _revision_json(row: sqlite3.Row, live: int | None) -> dict[str, Any]:
    return {
        "revision": row["rev"],
        "created_at": row["created_at"],
        "author": row["author"],
        "message": row["message"],
        "live": row["rev"] == live,
    }


def _run_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "state": row["state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "created_by": row["created_by"],
        "last_seq": row["last_seq"],
        "model": row["model"],
    }


def _event_json(run_id: str, row: sqlite3.Row) -> dict[str, Any]:
    return {"seq": row["seq"], "run_id": run_id, "ts": row["ts"], "type": row["type"], **json.loads(row["data"])}


def _approval_json(row: sqlite3.Row, *, with_args: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "run_id": row["run_id"],
        "call_id": row["call_id"],
        "tool": row["tool"],
        "tier": row["tier"],
        "title": row["title"],
        "summary": json.loads(row["summary"]),
        "diff": row["diff"],
        "rollback": row["rollback"],
        "plan_id": row["plan_id"],
        "state": row["state"],
        "scope": row["scope"],
        "requested_at": row["requested_at"],
        "expires_at": row["expires_at"],
        "requested_by": row["requested_by"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "comment": row["comment"],
    }
    if with_args:
        out["args"] = json.loads(row["args"])
    return out


def _question_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "call_id": row["call_id"],
        "text": row["text"],
        "options": json.loads(row["options"]),
        "asked_at": row["asked_at"],
        "answer": row["answer"],
        "answered_by": row["answered_by"],
        "answered_at": row["answered_at"],
        "expired_at": row["expired_at"],
    }


def _audit_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ts": row["ts"],
        "user": row["user"],
        "run_id": row["run_id"],
        "action": row["action"],
        "tool": row["tool"],
        "tier": row["tier"],
        "args": json.loads(row["args"]),
        "outcome": row["outcome"],
        "detail": row["detail"],
    }


def _lease_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target": json.loads(row["target"]),
        "priority": row["priority"],
        "expires_at": row["expires_at"],
        "created_by": row["created_by"],
        "run_id": row["run_id"],
        "state": row["state"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "released_at": row["released_at"],
        "last_error": row["last_error"],
    }


def _backup_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "plan_id": row["plan_id"],
        "target": row["target"],
        "attempt": row["attempt"],
        "documents": json.loads(row["documents"]),
        "created_at": row["created_at"],
    }


def _plan_json(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "revision": row["revision"],
        "base_revision": row["base_revision"],
        "data": json.loads(row["data"]),
        "state": row["state"],
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _test_json(row: sqlite3.Row) -> dict[str, Any]:
    return TestResult(
        name=row["name"],
        status=row["status"],
        duration_ms=row["duration_ms"],
        failed_step=row["failed_step"],
        detail=row["detail"],
        target=row["target"],
    ).to_json()


# -- low-level helpers ----------------------------------------------------------------------
async def _all(db: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    async with db.execute(sql, params) as cur:
        return list(await cur.fetchall())


async def _one(db: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    async with db.execute(sql, params) as cur:
        return await cur.fetchone()


async def _exec(db: aiosqlite.Connection, sql: str, params: Sequence[Any] = ()) -> tuple[int, int | None]:
    """Run a write statement; returns (rowcount, lastrowid)."""
    async with db.execute(sql, params) as cur:
        return cur.rowcount, cur.lastrowid


async def _rollback(db: aiosqlite.Connection) -> None:
    try:
        await _exec(db, "ROLLBACK")
    except sqlite3.Error as e:
        # Also raised when COMMIT itself failed and already ended the transaction.
        logger.debug("rollback: %s", e)


async def _migrate(db: aiosqlite.Connection, now: float) -> None:
    await _exec(db, "CREATE TABLE IF NOT EXISTS schema_version "
                    "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
    for version, statements in MIGRATIONS:
        await _exec(db, "BEGIN IMMEDIATE")
        try:
            row = await _one(db, "SELECT COALESCE(MAX(version), 0) FROM schema_version")
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {current} is newer than this hub supports ({SCHEMA_VERSION})")
            if version > current:
                for sql in statements:
                    await _exec(db, sql)
                await _exec(db, "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, now))
                logger.info("database migrated to schema version %d", version)
            await _exec(db, "COMMIT")
        except BaseException:
            await _rollback(db)
            raise


class Store:
    """The hub database. ``await open()`` (or ``async with``) before use."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = str(path)
        self.clock = clock
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # -- life cycle ---------------------------------------------------------------------
    async def open(self) -> None:
        async with self._lock:
            if self._db is not None:
                return
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = await aiosqlite.connect(self.path, isolation_level=None)
            try:
                db.row_factory = sqlite3.Row
                await _exec(db, "PRAGMA busy_timeout = 5000")
                await _exec(db, "PRAGMA foreign_keys = ON")
                row = await _one(db, "PRAGMA journal_mode = WAL")
                mode = str(row[0]).lower() if row else "?"
                if self.path != ":memory:" and mode != "wal":
                    logger.warning("%s: journal mode is %s, not WAL", self.path, mode)
                await _exec(db, "PRAGMA synchronous = NORMAL")
                await _migrate(db, self.clock())
            except BaseException:
                await db.close()
                raise
            self._db = db

    async def close(self) -> None:
        async with self._lock:
            db, self._db = self._db, None
            if db is not None:
                await db.close()

    async def __aenter__(self) -> Store:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def is_open(self) -> bool:
        return self._db is not None

    async def schema_version(self) -> int:
        row = await self._fetchone("SELECT COALESCE(MAX(version), 0) FROM schema_version")
        return int(row[0]) if row else 0

    # -- plumbing -----------------------------------------------------------------------
    async def _run(self, fn: Callable[[aiosqlite.Connection], Awaitable[T]], *, write: bool) -> T:
        return await asyncio.shield(self._locked(fn, write))

    async def _locked(self, fn: Callable[[aiosqlite.Connection], Awaitable[T]], write: bool) -> T:
        async with self._lock:
            db = self._db
            if db is None:
                raise RuntimeError("the store is not open")
            if not write:
                return await fn(db)
            await _exec(db, "BEGIN IMMEDIATE")
            try:
                result = await fn(db)
                await _exec(db, "COMMIT")
            except BaseException:
                await _rollback(db)
                raise
            return result

    async def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        async def op(db: aiosqlite.Connection) -> sqlite3.Row | None:
            return await _one(db, sql, params)

        return await self._run(op, write=False)

    async def _fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        async def op(db: aiosqlite.Connection) -> list[sqlite3.Row]:
            return await _all(db, sql, params)

        return await self._run(op, write=False)

    # -- manifest revisions ----------------------------------------------------------------
    async def add_revision(
        self,
        yaml_text: str,
        *,
        author: str,
        message: str = "",
        run_id: str | None = None,
        head: Literal["live", "draft"] | None = None,
    ) -> int:
        """Store an immutable manifest revision; ``head`` also makes it the
        live revision or the current draft. Returns the revision number."""
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> int:
            _, rev = await _exec(
                db,
                "INSERT INTO manifest_revisions (yaml, author, message, run_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (yaml_text, author, message, run_id, now),
            )
            assert rev is not None
            if head is not None:
                await _set_head(db, head, rev, now)
            return rev

        return await self._run(op, write=True)

    async def save_draft(self, yaml_text: str, *, author: str, message: str = "", run_id: str | None = None) -> int:
        return await self.add_revision(yaml_text, author=author, message=message, run_id=run_id, head="draft")

    async def set_live(self, rev: int) -> None:
        """Mark ``rev`` live (after it was applied). A draft that is this
        revision is no longer a draft."""
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> None:
            if await _one(db, "SELECT 1 FROM manifest_revisions WHERE rev = ?", (rev,)) is None:
                raise NotFound(f"manifest revision {rev} not found")
            await _set_head(db, "live", rev, now)
            await _exec(db, "DELETE FROM manifest_heads WHERE name = 'draft' AND rev = ?", (rev,))

        await self._run(op, write=True)

    async def set_draft(self, rev: int | None) -> None:
        """Point the draft at an existing revision (None discards the draft)."""
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> None:
            if rev is None:
                await _exec(db, "DELETE FROM manifest_heads WHERE name = 'draft'")
                return
            if await _one(db, "SELECT 1 FROM manifest_revisions WHERE rev = ?", (rev,)) is None:
                raise NotFound(f"manifest revision {rev} not found")
            await _set_head(db, "draft", rev, now)

        await self._run(op, write=True)

    async def discard_draft(self) -> None:
        await self.set_draft(None)

    async def live_revision(self) -> int | None:
        row = await self._fetchone("SELECT rev FROM manifest_heads WHERE name = 'live'")
        return None if row is None else int(row["rev"])

    async def draft_revision(self) -> int | None:
        row = await self._fetchone("SELECT rev FROM manifest_heads WHERE name = 'draft'")
        return None if row is None else int(row["rev"])

    async def revision_yaml(self, rev: int) -> str | None:
        row = await self._fetchone("SELECT yaml FROM manifest_revisions WHERE rev = ?", (rev,))
        return None if row is None else str(row["yaml"])

    async def get_revision(self, rev: int) -> dict[str, Any] | None:
        async def op(db: aiosqlite.Connection) -> dict[str, Any] | None:
            row = await _one(db, "SELECT rev, created_at, author, message FROM manifest_revisions WHERE rev = ?",
                             (rev,))
            return None if row is None else _revision_json(row, await _head(db, "live"))

        return await self._run(op, write=False)

    async def list_revisions(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first, in the ``GET /api/manifest/revisions`` item shape."""

        async def op(db: aiosqlite.Connection) -> list[dict[str, Any]]:
            live = await _head(db, "live")
            rows = await _all(db, "SELECT rev, created_at, author, message FROM manifest_revisions "
                                  "ORDER BY rev DESC LIMIT ?", (_limit(limit),))
            return [_revision_json(r, live) for r in rows]

        return await self._run(op, write=False)

    async def manifest_state(self) -> dict[str, Any]:
        """The ``GET /api/manifest`` shape. ``live_revision`` is None (and
        ``yaml`` empty) only before the first revision went live."""

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            live = await _head(db, "live")
            draft = await _head(db, "draft")
            live_yaml = await _yaml(db, live) if live is not None else None
            draft_yaml = await _yaml(db, draft) if draft is not None else None
            return {"live_revision": live, "draft_revision": draft, "yaml": live_yaml or "", "draft_yaml": draft_yaml}

        return await self._run(op, write=False)

    # -- runs -----------------------------------------------------------------------------
    async def create_run(
        self,
        *,
        title: str,
        created_by: str,
        model: str = "",
        state: str = "idle",
        run_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``meta`` is the agent's own record of the run (``run_meta``); it
        is not part of the API shape."""
        _check_choice("run state", state, RUN_STATES)
        now = self.clock()
        rid = run_id or new_id("r")
        meta_text = dumps(dict(meta or {}))

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            try:
                await _exec(
                    db,
                    "INSERT INTO runs (id, title, state, created_by, created_at, updated_at, model, meta) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (rid, title, state, created_by, now, now, model, meta_text),
                )
            except sqlite3.IntegrityError as e:
                raise Conflict(f"run {rid} already exists") from e
            row = await _one(db, "SELECT * FROM runs WHERE id = ?", (rid,))
            assert row is not None
            return _run_json(row)

        return await self._run(op, write=True)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        return None if row is None else _run_json(row)

    async def list_runs(self, limit: int = 20, *, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Newest first (``GET /api/runs``)."""
        sql = "SELECT * FROM runs"
        params: list[Any] = []
        if states is not None:
            wanted = list(states)
            if not wanted:
                return []
            sql += f" WHERE state IN ({', '.join('?' * len(wanted))})"
            params.extend(wanted)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(_limit(limit))
        return [_run_json(r) for r in await self._fetchall(sql, params)]

    async def update_run(
        self,
        run_id: str,
        *,
        state: str | None = None,
        title: str | None = None,
        model: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        sets: dict[str, Any] = {"updated_at": self.clock()}
        if state is not None:
            _check_choice("run state", state, RUN_STATES)
            sets["state"] = state
        if title is not None:
            sets["title"] = title
        if model is not None:
            sets["model"] = model
        if meta is not None:
            sets["meta"] = dumps(dict(meta))

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            assignments = ", ".join(f"{k} = ?" for k in sets)
            await _exec(db, f"UPDATE runs SET {assignments} WHERE id = ?", (*sets.values(), run_id))
            row = await _one(db, "SELECT * FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise NotFound(f"run {run_id} not found")
            return _run_json(row)

        return await self._run(op, write=True)

    async def run_meta(self, run_id: str) -> dict[str, Any]:
        row = await self._fetchone("SELECT meta FROM runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFound(f"run {run_id} not found")
        meta: dict[str, Any] = json.loads(row["meta"])
        return meta

    async def save_messages(self, run_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """Replace the run's LLM message history (OpenAI chat format)."""
        text = dumps(list(messages))
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> None:
            count, _ = await _exec(db, "UPDATE runs SET messages = ?, updated_at = ? WHERE id = ?", (text, now, run_id))
            if count == 0:
                raise NotFound(f"run {run_id} not found")

        await self._run(op, write=True)

    async def load_messages(self, run_id: str) -> list[dict[str, Any]]:
        row = await self._fetchone("SELECT messages FROM runs WHERE id = ?", (run_id,))
        if row is None:
            raise NotFound(f"run {run_id} not found")
        messages: list[dict[str, Any]] = json.loads(row["messages"])
        return messages

    # -- run events -------------------------------------------------------------------------
    async def append_event(
        self, run_id: str, event_type: str, data: Mapping[str, Any], *, ts: float | None = None
    ) -> dict[str, Any]:
        """Persist an event with the run's next ``seq``. Use ``EventBus.append``
        so subscribers see it live; this only writes the row."""
        clash = EVENT_KEYS & data.keys()
        if clash:
            raise ValueError(f"event fields may not use the reserved keys {sorted(clash)}")
        text = dumps(dict(data))
        stamp = self.clock() if ts is None else ts

        async def op(db: aiosqlite.Connection) -> int:
            row = await _one(db, "SELECT last_seq FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise NotFound(f"run {run_id} not found")
            seq = int(row["last_seq"]) + 1
            await _exec(db, "INSERT INTO run_events (run_id, seq, type, ts, data) VALUES (?, ?, ?, ?, ?)",
                        (run_id, seq, event_type, stamp, text))
            await _exec(db, "UPDATE runs SET last_seq = ?, updated_at = ? WHERE id = ?", (seq, stamp, run_id))
            return seq

        seq = await self._run(op, write=True)
        # Decoded from the stored text so a live event equals its replayed copy.
        return {"seq": seq, "run_id": run_id, "ts": stamp, "type": event_type, **json.loads(text)}

    async def list_events(self, run_id: str, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT seq, type, ts, data FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after_seq, _limit(limit)),
        )
        return [_event_json(run_id, r) for r in rows]

    # -- approvals ------------------------------------------------------------------------
    async def create_approval(
        self,
        *,
        run_id: str | None,
        call_id: str | None,
        tool: str,
        tier: str,
        title: str,
        requested_by: str,
        summary: Sequence[str] = (),
        diff: str = "",
        rollback: str = "",
        plan_id: str | None = None,
        args: Mapping[str, Any] | None = None,
        ttl_s: float = 1800.0,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        _check_choice("tier", tier, TIERS)
        if not ttl_s > 0:
            raise InvalidRequest(f"approval ttl must be positive, got {ttl_s}")
        now = self.clock()
        aid = approval_id or new_id("a")
        params = (aid, run_id, call_id, tool, tier, title, dumps(list(summary)), diff, rollback, plan_id,
                  dumps(dict(args or {})), now, now + ttl_s, requested_by)

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            try:
                await _exec(
                    db,
                    "INSERT INTO approvals (id, run_id, call_id, tool, tier, title, summary, diff, rollback, "
                    "plan_id, args, requested_at, expires_at, requested_by) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    params,
                )
            except sqlite3.IntegrityError as e:
                raise Conflict(f"approval {aid} already exists") from e
            row = await _one(db, "SELECT * FROM approvals WHERE id = ?", (aid,))
            assert row is not None
            return _approval_json(row)

        return await self._run(op, write=True)

    async def get_approval(self, approval_id: str, *, with_args: bool = False) -> dict[str, Any] | None:
        """The API ``Approval``; ``with_args`` adds the tool call's ``args``."""
        row = await self._fetchone("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return None if row is None else _approval_json(row, with_args=with_args)

    async def list_approvals(
        self, *, state: str | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Newest first."""
        clauses, params = [], []
        if state is not None:
            _check_choice("approval state", state, APPROVAL_STATES)
            clauses.append("state = ?")
            params.append(state)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._fetchall(
            f"SELECT * FROM approvals{where} ORDER BY requested_at DESC, rowid DESC LIMIT ?",
            (*params, _limit(limit)),
        )
        return [_approval_json(r) for r in rows]

    async def decide_approval(
        self, approval_id: str, decision: str, *, user: str, comment: str | None = None, scope: str = "call"
    ) -> dict[str, Any]:
        """pending -> approved/rejected, atomically. Raises ``Conflict`` when the
        approval was already decided or its time is up, ``NotFound`` when unknown."""
        state = _DECISIONS.get(decision)
        if state is None:
            raise InvalidRequest(f"decision must be 'approve' or 'reject', got {decision!r}")
        _check_choice("approval scope", scope, APPROVAL_SCOPES)
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            count, _ = await _exec(
                db,
                "UPDATE approvals SET state = ?, decided_by = ?, decided_at = ?, comment = ?, scope = ? "
                "WHERE id = ? AND state = 'pending' AND expires_at > ?",
                (state, user, now, comment, scope, approval_id, now),
            )
            row = await _one(db, "SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                raise NotFound(f"approval {approval_id} not found")
            if count == 0:
                if row["state"] == "pending":
                    raise Conflict(f"approval {approval_id} expired")
                raise Conflict(f"approval {approval_id} is already {row['state']}")
            return _approval_json(row)

        return await self._run(op, write=True)

    async def expire_approvals(self, *, run_id: str | None = None, ids: Sequence[str] | None = None,
                               reason: str | None = None) -> list[dict[str, Any]]:
        """pending -> expired for every approval whose time is up, with
        ``run_id`` for every pending approval of that run (the run ended), or
        with ``ids`` for those approvals (their requester stopped waiting).
        Returns the approvals that changed so the caller can tell the runs."""
        now = self.clock()
        params: tuple[Any, ...]
        if ids is not None:
            if not ids:
                return []
            where = f"state = 'pending' AND id IN ({', '.join('?' * len(ids))})"
            params = tuple(ids)
        elif run_id is None:
            where, params = "state = 'pending' AND expires_at <= ?", (now,)
        else:
            where, params = "state = 'pending' AND run_id = ?", (run_id,)

        async def op(db: aiosqlite.Connection) -> list[dict[str, Any]]:
            rows = await _all(db, f"SELECT id FROM approvals WHERE {where} ORDER BY requested_at", params)
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            marks = ", ".join("?" * len(ids))
            await _exec(db, f"UPDATE approvals SET state = 'expired', decided_at = ?, comment = ? "
                            f"WHERE id IN ({marks})", (now, reason, *ids))
            changed = await _all(db, f"SELECT * FROM approvals WHERE id IN ({marks}) ORDER BY requested_at", ids)
            return [_approval_json(r) for r in changed]

        return await self._run(op, write=True)

    # -- questions ------------------------------------------------------------------------
    async def create_question(
        self, *, run_id: str, call_id: str | None, text: str, options: Sequence[str] = (),
        question_id: str | None = None,
    ) -> dict[str, Any]:
        qid = question_id or new_id("q")
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            try:
                await _exec(db, "INSERT INTO questions (id, run_id, call_id, text, options, asked_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)", (qid, run_id, call_id, text, dumps(list(options)), now))
            except sqlite3.IntegrityError as e:
                raise Conflict(f"question {qid} already exists") from e
            row = await _one(db, "SELECT * FROM questions WHERE id = ?", (qid,))
            assert row is not None
            return _question_json(row)

        return await self._run(op, write=True)

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM questions WHERE id = ?", (question_id,))
        return None if row is None else _question_json(row)

    async def list_questions(self, run_id: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
        """``pending_only``: neither answered nor expired."""
        sql = "SELECT * FROM questions WHERE run_id = ?"
        if pending_only:
            sql += " AND answer IS NULL AND expired_at IS NULL"
        rows = await self._fetchall(sql + " ORDER BY asked_at, rowid", (run_id,))
        return [_question_json(r) for r in rows]

    async def answer_question(self, question_id: str, answer: str, *, user: str) -> dict[str, Any]:
        """Record the first answer; a second one, or one after the question
        expired, raises ``Conflict``."""
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            count, _ = await _exec(
                db,
                "UPDATE questions SET answer = ?, answered_by = ?, answered_at = ? "
                "WHERE id = ? AND answer IS NULL AND expired_at IS NULL",
                (answer, user, now, question_id),
            )
            row = await _one(db, "SELECT * FROM questions WHERE id = ?", (question_id,))
            if row is None:
                raise NotFound(f"question {question_id} not found")
            if count == 0:
                if row["answer"] is None:
                    raise Conflict(f"question {question_id} expired; nobody waits for the answer any more")
                raise Conflict(f"question {question_id} was already answered by {row['answered_by']}")
            return _question_json(row)

        return await self._run(op, write=True)

    async def expire_questions(self, *, run_id: str | None = None, ids: Sequence[str] = ()) -> list[str]:
        """Mark unanswered questions expired (``ids``, or every open one of
        ``run_id``): nobody waits for them any more, so a late answer is
        refused. Returns the ids that changed."""
        now = self.clock()
        params: tuple[Any, ...]
        if run_id is not None:
            where, params = "run_id = ?", (run_id,)
        elif ids:
            where, params = f"id IN ({', '.join('?' * len(ids))})", tuple(ids)
        else:
            return []

        async def op(db: aiosqlite.Connection) -> list[str]:
            rows = await _all(db, f"SELECT id FROM questions WHERE {where} AND answer IS NULL "
                                  "AND expired_at IS NULL", params)
            changed = [r["id"] for r in rows]
            if changed:
                await _exec(db, f"UPDATE questions SET expired_at = ? WHERE id IN ({', '.join('?' * len(changed))})",
                            (now, *changed))
            return changed

        return await self._run(op, write=True)

    # -- audit ------------------------------------------------------------------------------
    async def add_audit(
        self,
        *,
        user: str,
        action: str,
        outcome: str,
        run_id: str | None = None,
        tool: str | None = None,
        tier: str | None = None,
        args: Any = None,
        detail: str = "",
        ts: float | None = None,
    ) -> dict[str, Any]:
        if tier is not None:
            _check_choice("tier", tier, TIERS)
        stamp = self.clock() if ts is None else ts
        params = (stamp, user, run_id, action, tool, tier, dumps(args), outcome, detail)

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            _, rowid = await _exec(db, "INSERT INTO audit (ts, user, run_id, action, tool, tier, args, outcome, "
                                       "detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
            row = await _one(db, "SELECT * FROM audit WHERE id = ?", (rowid,))
            assert row is not None
            return _audit_json(row)

        return await self._run(op, write=True)

    async def list_audit(self, limit: int = 100, *, run_id: str | None = None) -> list[dict[str, Any]]:
        """Newest first (``GET /api/audit``)."""
        if run_id is None:
            rows = await self._fetchall("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (_limit(limit),))
        else:
            rows = await self._fetchall("SELECT * FROM audit WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                                        (run_id, _limit(limit)))
        return [_audit_json(r) for r in rows]

    # -- result blobs -----------------------------------------------------------------------
    async def put_result(self, data: Any, *, run_id: str | None = None) -> str:
        """Store a large tool result; returns its handle (``r42``, shown to the
        model as ``result://r42``)."""
        text = dumps(data)
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> str:
            _, rowid = await _exec(db, "INSERT INTO results (run_id, created_at, data) VALUES (?, ?, ?)",
                                   (run_id, now, text))
            return f"r{rowid}"

        return await self._run(op, write=True)

    async def get_result(self, handle: str, *, run_id: str | None = None, owner_only: bool = False) -> Any:
        """The stored data of ``r42`` or ``result://r42``. With ``run_id``, only
        that run's results are visible; with ``owner_only`` also a ``run_id``
        of None is a filter (results stored without a run), as for a caller
        whose own results are the only ones it may read."""
        m = _HANDLE_RE.match(handle.strip())
        if not m:
            raise NotFound(f"no result {handle!r}")
        row = await self._fetchone("SELECT run_id, data FROM results WHERE id = ?", (int(m[1]),))
        if row is None or ((run_id is not None or owner_only) and row["run_id"] != run_id):
            raise NotFound(f"no result {handle!r}")
        return json.loads(row["data"])

    async def prune_results(self, older_than: float) -> int:
        async def op(db: aiosqlite.Connection) -> int:
            count, _ = await _exec(db, "DELETE FROM results WHERE created_at < ?", (older_than,))
            return count

        return await self._run(op, write=True)

    # -- leases -----------------------------------------------------------------------------
    async def insert_lease(self, lease: Mapping[str, Any]) -> None:
        """Persist a new lease (the ``Lease.to_json`` form of ``policy.leases``)."""
        _check_choice("lease kind", lease["kind"], LEASE_KINDS)
        _check_choice("lease state", lease.get("state", "active"), LEASE_STATES)
        params = (
            lease["id"], lease["kind"], dumps(lease["target"]), lease.get("priority"), lease["expires_at"],
            lease["created_by"], lease.get("run_id"), lease.get("state", "active"), lease.get("attempts", 0),
            lease["created_at"], lease.get("released_at"), lease.get("last_error"),
        )

        async def op(db: aiosqlite.Connection) -> None:
            try:
                await _exec(db, "INSERT INTO leases (id, kind, target, priority, expires_at, created_by, run_id, "
                                "state, attempts, created_at, released_at, last_error) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", params)
            except sqlite3.IntegrityError as e:
                raise Conflict(f"lease {lease['id']} already exists") from e

        await self._run(op, write=True)

    async def update_lease(self, lease_id: str, **fields: Any) -> dict[str, Any]:
        """Update ``state``, ``attempts``, ``expires_at``, ``released_at`` or ``last_error``."""
        unknown = fields.keys() - _LEASE_COLUMNS
        if unknown or not fields:
            raise ValueError(f"cannot update lease fields {sorted(unknown) or '(none)'}")
        if "state" in fields:
            _check_choice("lease state", fields["state"], LEASE_STATES)

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            await _exec(db, f"UPDATE leases SET {assignments} WHERE id = ?", (*fields.values(), lease_id))
            row = await _one(db, "SELECT * FROM leases WHERE id = ?", (lease_id,))
            if row is None:
                raise NotFound(f"lease {lease_id} not found")
            return _lease_json(row)

        return await self._run(op, write=True)

    async def get_lease(self, lease_id: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM leases WHERE id = ?", (lease_id,))
        return None if row is None else _lease_json(row)

    async def list_leases(
        self, *, state: str | None = None, run_id: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Soonest expiry first."""
        clauses, params = [], []
        if state is not None:
            _check_choice("lease state", state, LEASE_STATES)
            clauses.append("state = ?")
            params.append(state)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await self._fetchall(f"SELECT * FROM leases{where} ORDER BY expires_at, rowid LIMIT ?",
                                    (*params, _limit(limit)))
        return [_lease_json(r) for r in rows]

    # -- plans ---------------------------------------------------------------------------------
    async def save_plan(self, plan_id: str, *, revision: int, base_revision: int, data: Any) -> dict[str, Any]:
        """Store a computed plan with its change payloads (``data``); plans are
        immutable, so an existing id raises ``Conflict``."""
        text = dumps(data)
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            try:
                await _exec(db, "INSERT INTO plans (id, revision, base_revision, data, created_at, updated_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)", (plan_id, revision, base_revision, text, now, now))
            except sqlite3.IntegrityError as e:
                raise Conflict(f"plan {plan_id} already exists") from e
            row = await _one(db, "SELECT * FROM plans WHERE id = ?", (plan_id,))
            assert row is not None
            return _plan_json(row)

        return await self._run(op, write=True)

    async def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        row = await self._fetchone("SELECT * FROM plans WHERE id = ?", (plan_id,))
        return None if row is None else _plan_json(row)

    async def latest_plan(self, revision: int) -> dict[str, Any] | None:
        """The newest plan computed for manifest ``revision``."""
        row = await self._fetchone("SELECT * FROM plans WHERE revision = ? ORDER BY created_at DESC, rowid DESC "
                                   "LIMIT 1", (revision,))
        return None if row is None else _plan_json(row)

    async def plan_count(self, revision: int) -> int:
        """How many plans were computed for manifest ``revision``."""
        row = await self._fetchone("SELECT COUNT(*) FROM plans WHERE revision = ?", (revision,))
        return int(row[0]) if row else 0

    async def update_plan(
        self, plan_id: str, *, state: str | None = None, attempts: int | None = None
    ) -> dict[str, Any]:
        sets: dict[str, Any] = {"updated_at": self.clock()}
        if state is not None:
            _check_choice("plan state", state, PLAN_STATES)
            sets["state"] = state
        if attempts is not None:
            sets["attempts"] = attempts

        async def op(db: aiosqlite.Connection) -> dict[str, Any]:
            assignments = ", ".join(f"{k} = ?" for k in sets)
            await _exec(db, f"UPDATE plans SET {assignments} WHERE id = ?", (*sets.values(), plan_id))
            row = await _one(db, "SELECT * FROM plans WHERE id = ?", (plan_id,))
            if row is None:
                raise NotFound(f"plan {plan_id} not found")
            return _plan_json(row)

        return await self._run(op, write=True)

    # -- plan backups -------------------------------------------------------------------------
    async def save_plan_backup(self, plan_id: str, target: str, documents: Any, *, attempt: int = 1) -> int:
        """Keep one backup per (plan, target, apply attempt); saving the same
        attempt again raises ``Conflict`` instead of replacing it."""
        text = dumps(documents)
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> int:
            try:
                _, rowid = await _exec(db, "INSERT INTO plan_backups (plan_id, target, attempt, documents, "
                                           "created_at) VALUES (?, ?, ?, ?, ?)", (plan_id, target, attempt, text, now))
            except sqlite3.IntegrityError as e:
                raise Conflict(f"plan {plan_id} already has a backup of {target} for attempt {attempt}") from e
            assert rowid is not None
            return rowid

        return await self._run(op, write=True)

    async def get_plan_backup(self, plan_id: str, target: str, *, first: bool = False) -> dict[str, Any] | None:
        """The latest backup of ``target`` taken for ``plan_id``; with
        ``first`` the earliest one, taken before any attempt changed it."""
        order = "ASC" if first else "DESC"
        row = await self._fetchone(
            f"SELECT * FROM plan_backups WHERE plan_id = ? AND target = ? ORDER BY attempt {order}, id {order} "
            "LIMIT 1", (plan_id, target))
        return None if row is None else _backup_json(row)

    async def list_plan_backups(self, plan_id: str) -> list[dict[str, Any]]:
        rows = await self._fetchall("SELECT * FROM plan_backups WHERE plan_id = ? ORDER BY id", (plan_id,))
        return [_backup_json(r) for r in rows]

    # -- test results -------------------------------------------------------------------------
    async def save_test_results(
        self,
        results: Iterable[TestResult | Mapping[str, Any]],
        *,
        revision: int | None = None,
        replace: bool = True,
    ) -> None:
        """Record the latest result per (target, test name). ``replace`` drops
        the earlier results of the targets present in ``results`` (a full
        run); otherwise results are merged (a run of some tests)."""
        items = [r.to_json() if isinstance(r, TestResult) else dict(r) for r in results]
        for item in items:
            if not isinstance(item.get("name"), str) or not isinstance(item.get("status"), str):
                raise InvalidRequest(f"test result needs a name and a status: {item!r}")
            _check_choice("test target", item.setdefault("target", "live"), TEST_TARGETS)
        now = self.clock()

        async def op(db: aiosqlite.Connection) -> None:
            if replace:
                for target in {i["target"] for i in items}:
                    await _exec(db, "DELETE FROM test_results WHERE target = ?", (target,))
            row = await _one(db, "SELECT COALESCE(MAX(position), -1) FROM test_results")
            base = int(row[0]) + 1 if row else 0
            for pos, i in enumerate(items, start=base):
                await _exec(
                    db,
                    "INSERT INTO test_results (target, name, position, status, duration_ms, failed_step, detail, "
                    "revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (target, name) DO UPDATE SET status = excluded.status, "
                    "duration_ms = excluded.duration_ms, failed_step = excluded.failed_step, "
                    "detail = excluded.detail, revision = excluded.revision, updated_at = excluded.updated_at",
                    (i["target"], i["name"], pos, i["status"], int(i.get("duration_ms") or 0), i.get("failed_step"),
                     str(i.get("detail") or ""), revision, now),
                )

        await self._run(op, write=True)

    async def test_results(self, target: str | None = None) -> dict[str, Any]:
        """The ``GET /api/tests`` shape."""
        if target is None:
            rows = await self._fetchall("SELECT * FROM test_results ORDER BY target, position")
        else:
            _check_choice("test target", target, TEST_TARGETS)
            rows = await self._fetchall("SELECT * FROM test_results WHERE target = ? ORDER BY position", (target,))
        return {
            "results": [_test_json(r) for r in rows],
            "updated_at": max((r["updated_at"] for r in rows), default=None),
        }

    async def tests_passed(self, revision: int, *, target: str = "sim") -> bool:
        """True when the latest results of ``target`` are all for ``revision``,
        none failed and at least one passed (the simulation-first rule)."""
        _check_choice("test target", target, TEST_TARGETS)
        rows = await self._fetchall("SELECT status, revision FROM test_results WHERE target = ?", (target,))
        return (
            any(r["status"] == "pass" for r in rows)
            and all(r["revision"] == revision and r["status"] in ("pass", "skipped") for r in rows)
        )


async def _head(db: aiosqlite.Connection, name: str) -> int | None:
    row = await _one(db, "SELECT rev FROM manifest_heads WHERE name = ?", (name,))
    return None if row is None else int(row["rev"])


async def _set_head(db: aiosqlite.Connection, name: str, rev: int, now: float) -> None:
    await _exec(db, "INSERT INTO manifest_heads (name, rev, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (name) DO UPDATE SET rev = excluded.rev, updated_at = excluded.updated_at",
                (name, rev, now))


async def _yaml(db: aiosqlite.Connection, rev: int) -> str | None:
    row = await _one(db, "SELECT yaml FROM manifest_revisions WHERE rev = ?", (rev,))
    return None if row is None else str(row["yaml"])


def _check_choice(what: str, value: Any, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise InvalidRequest(f"{what} must be one of {', '.join(choices)}; got {value!r}")


class _JsonBackup(Protocol):
    @property
    def plan_id(self) -> str: ...

    @property
    def target(self) -> str: ...

    def to_json(self) -> dict[str, Any]: ...


class PlanBackupStore:
    """``manifest.apply.BackupStore`` over the database for one apply
    attempt: each backup is saved in its ``to_json`` form under ``attempt``,
    so the backups of earlier attempts stay; read them back with
    ``Store.get_plan_backup`` (``first=True`` for the pre-apply state)."""

    def __init__(self, store: Store, attempt: int = 1) -> None:
        self._store = store
        self.attempt = attempt

    async def save(self, backup: _JsonBackup) -> None:
        await self._store.save_plan_backup(backup.plan_id, backup.target, backup.to_json(), attempt=self.attempt)
