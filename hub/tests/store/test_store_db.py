from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from uc_hub.core.errors import InvalidRequest, NotFound
from uc_hub.core.types import TestResult
from uc_hub.store import SCHEMA_VERSION, Conflict, PlanBackupStore, Store
from uc_hub.store.db import MIGRATIONS

from .conftest import FakeClock

APPROVAL_KEYS = {
    "id", "run_id", "call_id", "tool", "tier", "title", "summary", "diff", "rollback", "plan_id", "state",
    "requested_at", "expires_at", "requested_by", "decided_by", "decided_at", "comment",
}
RUN_KEYS = {"id", "title", "state", "created_at", "updated_at", "created_by", "last_seq", "model"}


# -- schema -------------------------------------------------------------------------------
async def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    for _ in range(3):
        async with Store(path) as s:
            assert await s.schema_version() == SCHEMA_VERSION
    with sqlite3.connect(path) as db:
        versions = [r[0] for r in db.execute("SELECT version FROM schema_version ORDER BY version")]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert versions == [v for v, _ in MIGRATIONS]
    assert {"manifest_revisions", "manifest_heads", "runs", "run_events", "approvals", "questions", "audit",
            "results", "leases", "plan_backups", "test_results"} <= tables
    assert mode == "wal"


async def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "hub.db"
    async with Store(path):
        pass
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, 0)", (SCHEMA_VERSION + 1,))
    s = Store(path)
    with pytest.raises(RuntimeError, match="newer"):
        await s.open()
    assert not s.is_open


async def test_data_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "hub.db"
    async with Store(path) as s:
        run = await s.create_run(title="t", created_by="dev")
        await s.append_event(run["id"], "message.user", {"text": "hi", "user": "dev"})
    async with Store(path) as s:
        assert (await s.get_run(run["id"]))["last_seq"] == 1
        assert [e["text"] for e in await s.list_events(run["id"])] == ["hi"]


async def test_closed_store_raises() -> None:
    s = Store(":memory:")
    with pytest.raises(RuntimeError, match="not open"):
        await s.get_run("r_x")
    await s.open()
    await s.close()
    await s.close()
    with pytest.raises(RuntimeError):
        await s.list_runs()


# -- manifest revisions ---------------------------------------------------------------------
async def test_manifest_revisions(store: Store, clock: FakeClock) -> None:
    assert await store.manifest_state() == {"live_revision": None, "draft_revision": None, "yaml": "",
                                            "draft_yaml": None}
    r1 = await store.add_revision("a: 1\n", author="hub", message="initial", head="live")
    clock.advance(10)
    r2 = await store.save_draft("a: 2\n", author="agent (run r_1)", message="edit", run_id="r_1")
    assert (r1, r2) == (1, 2)
    assert await store.manifest_state() == {"live_revision": 1, "draft_revision": 2, "yaml": "a: 1\n",
                                            "draft_yaml": "a: 2\n"}
    revisions = await store.list_revisions()
    assert revisions == [
        {"revision": 2, "created_at": clock.now, "author": "agent (run r_1)", "message": "edit", "live": False},
        {"revision": 1, "created_at": clock.now - 10, "author": "hub", "message": "initial", "live": True},
    ]
    await store.set_live(2)
    state = await store.manifest_state()
    assert (state["live_revision"], state["draft_revision"], state["draft_yaml"]) == (2, None, None)
    assert (await store.get_revision(2))["live"] is True
    assert await store.revision_yaml(1) == "a: 1\n"
    assert await store.get_revision(99) is None

    r3 = await store.save_draft("a: 3\n", author="dev")
    await store.set_live(1)  # a rollback keeps an unrelated draft
    assert (await store.live_revision(), await store.draft_revision()) == (1, r3)
    await store.discard_draft()
    assert await store.draft_revision() is None
    with pytest.raises(NotFound):
        await store.set_live(42)
    with pytest.raises(NotFound):
        await store.set_draft(42)


# -- runs and events --------------------------------------------------------------------------
async def test_runs_crud(store: Store, clock: FakeClock) -> None:
    run = await store.create_run(title="Commission room 204", created_by="dev", model="glm-5.3",
                                 state="running")
    assert set(run) == RUN_KEYS
    assert run["id"].startswith("r_") and run["state"] == "running" and run["last_seq"] == 0
    clock.advance(5)
    other = await store.create_run(title="second", created_by="ops", run_id="r_fixed")
    assert other["id"] == "r_fixed"
    with pytest.raises(Conflict):
        await store.create_run(title="dup", created_by="ops", run_id="r_fixed")

    assert [r["id"] for r in await store.list_runs()] == ["r_fixed", run["id"]]
    assert [r["id"] for r in await store.list_runs(limit=1)] == ["r_fixed"]
    assert [r["id"] for r in await store.list_runs(states=["running"])] == [run["id"]]
    assert await store.list_runs(states=[]) == []

    clock.advance(1)
    updated = await store.update_run(run["id"], state="waiting_approval", title="new title")
    assert updated["state"] == "waiting_approval" and updated["title"] == "new title"
    assert updated["updated_at"] == clock.now
    with pytest.raises(InvalidRequest):
        await store.update_run(run["id"], state="bogus")
    with pytest.raises(NotFound):
        await store.update_run("r_missing", state="idle")
    assert await store.get_run("r_missing") is None

    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "site_search", "arguments": "{}"}}]}]
    await store.save_messages(run["id"], messages)
    assert await store.load_messages(run["id"]) == messages
    assert await store.load_messages("r_fixed") == []
    with pytest.raises(NotFound):
        await store.save_messages("r_missing", [])
    with pytest.raises(NotFound):
        await store.load_messages("r_missing")


async def test_event_rows(store: Store) -> None:
    run = await store.create_run(title="t", created_by="dev")
    rid = run["id"]
    e1 = await store.append_event(rid, "run.state", {"state": "running"})
    e2 = await store.append_event(rid, "tool.result", {"call_id": "c1", "ok": True, "data": (1, 2)})
    assert e1 == {"seq": 1, "run_id": rid, "ts": e1["ts"], "type": "run.state", "state": "running"}
    assert e2["seq"] == 2 and e2["data"] == [1, 2]
    assert await store.list_events(rid) == [e1, e2]
    assert await store.list_events(rid, after_seq=1) == [e2]
    assert await store.list_events(rid, after_seq=0, limit=1) == [e1]
    assert (await store.get_run(rid))["last_seq"] == 2
    with pytest.raises(NotFound):
        await store.append_event("r_missing", "x", {})
    with pytest.raises(ValueError, match="reserved"):
        await store.append_event(rid, "x", {"seq": 3})


async def test_concurrent_appends_get_distinct_seqs(store: Store) -> None:
    run = await store.create_run(title="t", created_by="dev")
    events = await asyncio.gather(*(store.append_event(run["id"], "message.delta", {"text": str(i)})
                                    for i in range(50)))
    assert sorted(e["seq"] for e in events) == list(range(1, 51))
    assert [e["seq"] for e in await store.list_events(run["id"])] == list(range(1, 51))


async def test_cancelled_write_leaves_store_consistent(store: Store) -> None:
    run = await store.create_run(title="t", created_by="dev")
    tasks = [asyncio.create_task(store.append_event(run["id"], "x", {"i": i})) for i in range(20)]
    await asyncio.sleep(0)
    for t in tasks[::2]:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Shielded writes complete even when their caller was cancelled.
    events = await store.list_events(run["id"])
    assert [e["seq"] for e in events] == list(range(1, 21))
    assert (await store.get_run(run["id"]))["last_seq"] == 20
    await store.append_event(run["id"], "x", {})


# -- approvals --------------------------------------------------------------------------------
async def _approval(store: Store, **kw: object) -> dict:
    fields: dict = {"run_id": "r_1", "call_id": "call_3", "tool": "apply", "tier": "C", "title": "Apply plan p17",
                    "requested_by": "dev", "summary": ["r204-ctl: io.json +1 point"], "diff": "--- a\n+++ b\n",
                    "rollback": "Apply revision 16", "plan_id": "p17", "args": {"plan_id": "p17"}, "ttl_s": 1800}
    fields.update(kw)
    return await store.create_approval(**fields)  # type: ignore[arg-type]


async def test_approval_round_trip(store: Store, clock: FakeClock) -> None:
    a = await _approval(store)
    assert set(a) == APPROVAL_KEYS
    assert a["state"] == "pending" and a["summary"] == ["r204-ctl: io.json +1 point"]
    assert a["requested_at"] == clock.now and a["expires_at"] == clock.now + 1800
    assert (a["decided_by"], a["decided_at"], a["comment"]) == (None, None, None)
    assert await store.get_approval(a["id"]) == a
    assert (await store.get_approval(a["id"], with_args=True))["args"] == {"plan_id": "p17"}
    assert await store.get_approval("a_missing") is None

    clock.advance(3)
    decided = await store.decide_approval(a["id"], "approve", user="ilija", comment="go")
    assert decided["state"] == "approved"
    assert (decided["decided_by"], decided["decided_at"], decided["comment"]) == ("ilija", clock.now, "go")
    with pytest.raises(Conflict, match="already approved"):
        await store.decide_approval(a["id"], "reject", user="other")
    assert (await store.get_approval(a["id"]))["decided_by"] == "ilija"

    b = await _approval(store, tier="L", tool="point_write", plan_id=None)
    assert (await store.decide_approval(b["id"], "reject", user="ops"))["state"] == "rejected"
    with pytest.raises(Conflict):
        await store.decide_approval(b["id"], "reject", user="ops")
    with pytest.raises(NotFound):
        await store.decide_approval("a_missing", "approve", user="x")
    with pytest.raises(InvalidRequest):
        await store.decide_approval(b["id"], "maybe", user="x")
    with pytest.raises(InvalidRequest):
        await _approval(store, tier="X")


async def test_concurrent_decisions_one_wins(store: Store) -> None:
    a = await _approval(store)
    results = await asyncio.gather(
        *(store.decide_approval(a["id"], "approve" if i % 2 else "reject", user=f"u{i}") for i in range(10)),
        return_exceptions=True,
    )
    wins = [r for r in results if isinstance(r, dict)]
    assert len(wins) == 1
    assert all(isinstance(r, Conflict) for r in results if not isinstance(r, dict))
    assert (await store.get_approval(a["id"]))["decided_by"] == wins[0]["decided_by"]


async def test_approval_expiry(store: Store, clock: FakeClock) -> None:
    old = await _approval(store, ttl_s=60)
    fresh = await _approval(store, ttl_s=600)
    other_run = await _approval(store, run_id="r_2", ttl_s=600)
    clock.advance(61)
    with pytest.raises(Conflict, match="expired"):
        await store.decide_approval(old["id"], "approve", user="x")
    assert (await store.get_approval(old["id"]))["state"] == "pending"  # the sweep changes state, not decide

    expired = await store.expire_approvals()
    assert [e["id"] for e in expired] == [old["id"]]
    assert expired[0]["state"] == "expired" and expired[0]["decided_at"] == clock.now
    assert await store.expire_approvals() == []

    cancelled = await store.expire_approvals(run_id="r_1", reason="run cancelled")
    assert [(e["id"], e["comment"]) for e in cancelled] == [(fresh["id"], "run cancelled")]
    assert [a["id"] for a in await store.list_approvals(state="pending")] == [other_run["id"]]
    assert {a["id"] for a in await store.list_approvals(run_id="r_1")} == {old["id"], fresh["id"]}
    assert len(await store.list_approvals()) == 3
    with pytest.raises(Conflict, match="already expired"):
        await store.decide_approval(fresh["id"], "approve", user="x")
    with pytest.raises(InvalidRequest):
        await store.list_approvals(state="nope")


# -- questions, audit, results ------------------------------------------------------------------
async def test_questions(store: Store) -> None:
    q = await store.create_question(run_id="r_1", call_id="c9", text="Is the valve open?",
                                    options=["Yes", "No", "Skip"])
    assert q["options"] == ["Yes", "No", "Skip"] and q["answer"] is None
    assert [x["id"] for x in await store.list_questions("r_1", pending_only=True)] == [q["id"]]
    answered = await store.answer_question(q["id"], "Yes", user="tech1")
    assert (answered["answer"], answered["answered_by"]) == ("Yes", "tech1")
    with pytest.raises(Conflict, match="tech1"):
        await store.answer_question(q["id"], "No", user="tech2")
    with pytest.raises(NotFound):
        await store.answer_question("q_missing", "No", user="tech2")
    assert await store.list_questions("r_1", pending_only=True) == []
    assert await store.get_question(q["id"]) == answered


async def test_audit(store: Store, clock: FakeClock) -> None:
    for i in range(5):
        clock.advance(1)
        await store.add_audit(user="dev", action="tool.call", tool="point_write", tier="L", run_id="r_1",
                              args={"point": f"hq/r204-ctl/analog-value:{i}"}, outcome="ok", detail=f"n{i}")
    await store.add_audit(user="ilija", action="approval.decide", outcome="approved")
    entries = await store.list_audit(limit=3)
    assert [e["detail"] for e in entries] == ["", "n4", "n3"]
    assert entries[0] == {"ts": clock.now, "user": "ilija", "run_id": None, "action": "approval.decide",
                          "tool": None, "tier": None, "args": None, "outcome": "approved", "detail": ""}
    assert entries[1]["args"] == {"point": "hq/r204-ctl/analog-value:4"}
    assert len(await store.list_audit(run_id="r_1")) == 5
    with pytest.raises(InvalidRequest):
        await store.add_audit(user="x", action="y", outcome="z", tier="Q")


async def test_result_blobs(store: Store, clock: FakeClock) -> None:
    data = {"points": [{"id": f"hq/d/analog-value:{i}", "value": i * 0.5} for i in range(500)]}
    handle = await store.put_result(data, run_id="r_1")
    assert handle.startswith("r")
    assert await store.get_result(handle) == data
    assert await store.get_result(f"result://{handle}", run_id="r_1") == data
    with pytest.raises(NotFound):
        await store.get_result(handle, run_id="r_other")
    for bad in ("r999", "bogus", "result://x"):
        with pytest.raises(NotFound):
            await store.get_result(bad)
    clock.advance(100)
    await store.put_result([1], run_id="r_1")
    assert await store.prune_results(older_than=clock.now - 50) == 1
    with pytest.raises(NotFound):
        await store.get_result(handle)


# -- leases, backups, tests ------------------------------------------------------------------
def _lease_row(lease_id: str, **kw: object) -> dict:
    row: dict = {"id": lease_id, "kind": "write", "target": {"point": "hq/r204-ctl/analog-value:1"},
                 "priority": 12, "expires_at": 100.0, "created_by": "dev", "run_id": "r_1", "state": "active",
                 "attempts": 0, "created_at": 1.0, "released_at": None, "last_error": None}
    row.update(kw)
    return row


async def test_leases(store: Store) -> None:
    await store.insert_lease(_lease_row("l_1"))
    await store.insert_lease(_lease_row("l_2", kind="force", target={"node": "r204-ctl", "channel": "ai0"},
                                        priority=None, expires_at=50.0, run_id=None))
    with pytest.raises(Conflict):
        await store.insert_lease(_lease_row("l_1"))
    assert await store.get_lease("l_1") == _lease_row("l_1")
    assert [row["id"] for row in await store.list_leases(state="active")] == ["l_2", "l_1"]
    updated = await store.update_lease("l_1", state="released", attempts=1, released_at=90.0)
    assert (updated["state"], updated["attempts"], updated["released_at"]) == ("released", 1, 90.0)
    assert [row["id"] for row in await store.list_leases(state="active")] == ["l_2"]
    assert [row["id"] for row in await store.list_leases(run_id="r_1")] == ["l_1"]
    with pytest.raises(ValueError):
        await store.update_lease("l_1", kind="force")
    with pytest.raises(InvalidRequest):
        await store.update_lease("l_1", state="zombie")
    with pytest.raises(NotFound):
        await store.update_lease("l_missing", attempts=2)
    with pytest.raises(InvalidRequest):
        await store.insert_lease(_lease_row("l_3", kind="hold"))


async def test_plan_backups(store: Store) -> None:
    class Backup:
        plan_id = "p17"
        target = "r204-ctl"

        def to_json(self) -> dict:
            return {"target": self.target, "plan_id": self.plan_id, "files": {"/lfs/cfg/io.json": "e30="}}

    await PlanBackupStore(store).save(Backup())
    await store.save_plan_backup("p17", "gateway", {"gateway": {"bridges": []}})
    await store.save_plan_backup("p17", "r204-ctl", {"files": {}})
    latest = await store.get_plan_backup("p17", "r204-ctl")
    assert latest is not None and latest["documents"] == {"files": {}}
    assert [(b["target"], b["documents"].get("plan_id")) for b in await store.list_plan_backups("p17")] == [
        ("r204-ctl", "p17"), ("gateway", None), ("r204-ctl", None)]
    assert await store.get_plan_backup("p18", "r204-ctl") is None


async def test_test_results(store: Store, clock: FakeClock) -> None:
    assert await store.test_results() == {"results": [], "updated_at": None}
    await store.save_test_results([TestResult("valve opens", "pass", 4210, target="sim"),
                                   TestResult("fan starts", "pass", 100, target="sim")], revision=7)
    assert await store.tests_passed(7)
    assert not await store.tests_passed(8)
    assert not await store.tests_passed(7, target="live")

    clock.advance(10)
    await store.save_test_results([{"name": "fan starts", "status": "fail", "duration_ms": 50, "failed_step": 1,
                                    "detail": "fan off", "target": "sim"}], revision=7, replace=False)
    info = await store.test_results("sim")
    assert info["updated_at"] == clock.now
    assert info["results"] == [
        {"name": "valve opens", "status": "pass", "duration_ms": 4210, "failed_step": None, "detail": "",
         "target": "sim"},
        {"name": "fan starts", "status": "fail", "duration_ms": 50, "failed_step": 1, "detail": "fan off",
         "target": "sim"},
    ]
    assert not await store.tests_passed(7)

    await store.save_test_results([TestResult("valve opens", "pass", 3000)], revision=7)
    assert [(r["name"], r["target"]) for r in (await store.test_results())["results"]] == [
        ("valve opens", "live"), ("valve opens", "sim"), ("fan starts", "sim")]
    await store.save_test_results([TestResult("fan starts", "pass", 10, target="sim")], revision=8)
    assert [r["name"] for r in (await store.test_results("sim"))["results"]] == ["fan starts"]
    assert await store.tests_passed(8)
    with pytest.raises(InvalidRequest):
        await store.save_test_results([{"name": "x", "status": "pass", "target": "mars"}])
    with pytest.raises(InvalidRequest):
        await store.save_test_results([{"status": "pass"}])
