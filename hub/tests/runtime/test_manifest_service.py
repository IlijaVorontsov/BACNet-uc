"""Draft edits, plans that survive a restart, apply with per-attempt backups
and rollback, on simulated nodes."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest
from support.site import R204_IO, Hub, eventually

from uc_hub.core.errors import Conflict, InvalidRequest, NotFound, PolicyDenied, ValidationFailed
from uc_hub.core.types import SafetyClass
from uc_hub.manifest.nodedocs import CFG_IO

DOOR = {"channel": "di1", "type": "binary-input", "instance": 2, "name": "R204 Door"}
ADMIN = frozenset({"admin"})
OPERATOR = frozenset({"operator"})


def io_on(hub: Hub, node: str) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = json.loads(hub.nodes[node].fs[CFG_IO])["points"]
    return points


async def test_edits_are_validated_before_the_draft_changes(hub: Hub) -> None:
    manifests = hub.services.manifests
    with pytest.raises(InvalidRequest, match="patch operation 0"):
        await manifests.edit([{"op": "replace", "path": "/placement/nope", "value": "r204"}], user="u", roles=ADMIN)
    with pytest.raises(ValidationFailed) as info:
        await manifests.edit([{"op": "add", "path": "/placement/r204-ctl", "value": "mars"}], user="u", roles=ADMIN)
    assert info.value.errors == ["/placement/r204-ctl: unknown space 'mars'"]
    assert manifests.draft is None and await hub.services.store.draft_revision() is None
    result = await manifests.edit([{"op": "add", "path": "/placement/r204-ctl", "value": "f2"}], user="u",
                                  roles=ADMIN, message="move r204", run_id=None)
    assert (result.draft_revision, result.changed, result.sections) == (2, True, ["placement"])
    assert "-  r204-ctl: r204\n+  r204-ctl: f2" in result.diff
    result = await manifests.edit([{"op": "add", "path": "/tags/r204-ctl~1binary-input:1", "value": ["Window"]}],
                                  user="u", roles=ADMIN)
    assert (result.draft_revision, result.sections) == (3, ["placement", "tags"])
    assert manifests.draft is not None and manifests.draft.placement["r204-ctl"] == "f2"
    unchanged = await manifests.edit([{"op": "test", "path": "/placement/r204-ctl", "value": "f2"}], user="u",
                                     roles=ADMIN)
    assert (unchanged.changed, unchanged.draft_revision) == (False, 3)
    with pytest.raises(InvalidRequest, match="metadata.name"):
        await manifests.edit([{"op": "replace", "path": "/metadata/name", "value": "hq2"}], user="u", roles=ADMIN)
    revisions = await hub.services.store.list_revisions()
    assert [(r["revision"], r["author"], r["message"]) for r in revisions] == [
        (3, "u", "add /tags/r204-ctl~1binary-input:1"), (2, "u", "move r204"), (1, "hub", "imported from site.yaml")]
    assert manifests.get(None, "/placement/r204-ctl") == ("draft", 3, "f2")
    assert manifests.get("live", "/placement/r204-ctl") == ("live", 1, "r204")
    with pytest.raises(NotFound):
        manifests.get(None, "/placement/nope")


async def test_only_an_admin_adds_or_removes_life_safety_marks(hub: Hub) -> None:
    manifests = hub.services.manifests
    add = [{"op": "add", "path": "/safety/r204-ctl~1binary-input:1", "value": "life-safety"}]
    with pytest.raises(PolicyDenied, match="hq/r204-ctl/binary-input:1"):
        await manifests.edit(add, user="op", roles=OPERATOR)
    remove = [{"op": "remove", "path": "/safety/r205-ctl~1binary-output:1"}]
    with pytest.raises(PolicyDenied, match="only a site admin"):
        await manifests.edit(remove, user="op", roles=OPERATOR)
    downgrade = [{"op": "replace", "path": "/safety/r205-ctl~1binary-output:1", "value": "critical"}]
    with pytest.raises(PolicyDenied):
        await manifests.edit(downgrade, user="op", roles=frozenset({"commissioner"}))
    assert manifests.draft is None
    # Critical marks and other edits are anyone's (with tier S rights).
    await manifests.edit([{"op": "add", "path": "/safety/r204-ctl~1binary-input:1", "value": "critical"}],
                         user="op", roles=OPERATOR)
    await manifests.edit(add, user="boss", roles=ADMIN)
    await manifests.edit([{"op": "add", "path": "/placement/r204-ctl", "value": "f2"}], user="op", roles=OPERATOR)
    assert manifests.current.safety["hq/r204-ctl/binary-input:1"] is SafetyClass.LIFE_SAFETY


async def test_plans_persist_and_apply_after_a_restart(hub: Hub) -> None:
    manifests = hub.services.manifests
    await manifests.edit([{"op": "add", "path": "/system/nodes/0/io/-", "value": DOOR}], user="u", roles=ADMIN)
    assert manifests.pending_changes() == 1
    first = await manifests.plan()
    second = await manifests.plan()
    assert (first.id, second.id, second.revision, second.base_revision) == ("p2", "p2-2", 2, 1)
    assert manifests.current_plan() is second
    assert manifests.pending_changes() == len(second.changes) == 6
    assert [c.summary for c in second.changes if c.target == "r204-ctl"][:3] == [
        "device.json: device.location changed", "io.json: +1 (di1)", "reload device"]
    services = await hub.restart()
    manifests = services.manifests
    assert manifests.draft_revision == 2 and manifests.current_plan() is not None
    assert manifests.current_plan().id == "p2-2"  # type: ignore[union-attr]
    restored = await manifests.get_plan("p2")
    assert [c.payload for c in restored.changes] == [c.payload for c in first.changes]
    await hub.described()
    outcome = await manifests.apply("p2-2")
    assert outcome.ok and outcome.attempt == 1 and outcome.live_revision == 2
    assert manifests.live_revision == 2 and manifests.draft is None and manifests.pending_changes() == 0
    assert DOOR["channel"] in {p["channel"] for p in io_on(hub, "r204-ctl")}
    await eventually(lambda: any(p.ref.obj == "binary-input:2" for p in services.site.points("r204-ctl")),
                     what="the new point described")
    assert (await services.store.get_plan("p2-2"))["state"] == "applied"  # type: ignore[index]
    again = await manifests.plan()
    assert (again.id, again.changes, again.revision) == ("p2-3", [], 2)


async def test_a_revision_going_live_updates_the_site_and_policy(hub: Hub) -> None:
    manifests, services = hub.services.manifests, hub.services
    await manifests.edit([
        {"op": "add", "path": "/placement/r204-ctl", "value": "plant"},
        {"op": "add", "path": "/tags/r204-ctl~1binary-input:1", "value": ["Window_Status"]},
        {"op": "replace", "path": "/policy/max_writes_per_minute", "value": 7},
        {"op": "add", "path": "/bridges", "value": [
            {"from": "r205-ctl/analog-input:1", "to": "r204-ctl/analog-output:1", "max_age_s": 5}]},
    ], user="u", roles=ADMIN)
    plan = await manifests.plan()
    assert [c.kind for c in plan.changes if c.target == "gateway"] == ["tags", "bridge-add"]
    outcome = await manifests.apply(plan.id)
    assert outcome.ok, outcome.results
    assert services.site.device("r204-ctl").space == "plant"
    window = await services.site.point(hub.ref("r204-ctl/binary-input:1"))
    assert window.tags == ["Window_Status"] and window.space == "plant"
    assert services.policy.settings.max_writes_per_minute == 7
    assert [b["to"] for b in services.bridges.status()] == ["hq/r204-ctl/analog-output:1"]
    await eventually(lambda: hub.nodes["r204-ctl"].objects[(1, 1)].priority[11] == pytest.approx(20.0),
                     what="bridge running")


async def test_an_interrupted_apply_leaves_a_failed_plan(hub: Hub) -> None:
    """A run cancelled during apply: the attempt is recorded as failed, so
    the plan is still the draft's plan after a restart and can be retried."""
    manifests, store = hub.services.manifests, hub.services.store
    await manifests.edit([{"op": "add", "path": "/system/nodes/0/io/-", "value": DOOR}], user="u", roles=ADMIN)
    plan = await manifests.plan()
    hub.nodes["r204-ctl"].online = False
    applying = asyncio.create_task(manifests.apply(plan.id))
    await asyncio.sleep(0.1)
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    row = await store.get_plan(plan.id)
    assert row is not None and (row["state"], row["attempts"]) == ("failed", 1)
    services = await hub.restart()
    assert services.manifests.current_plan() is not None and services.manifests.current_plan().id == plan.id  # type: ignore[union-attr]
    hub.nodes["r204-ctl"].online = True
    await hub.described()
    outcome = await services.manifests.apply(plan.id)
    assert outcome.ok and outcome.attempt == 2


async def test_stale_and_blocked_plans_are_refused(hub: Hub) -> None:
    manifests = hub.services.manifests
    await manifests.edit([{"op": "add", "path": "/placement/r204-ctl", "value": "f2"}], user="u", roles=ADMIN)
    plan = await manifests.plan()
    await manifests.edit([{"op": "add", "path": "/placement/r205-ctl", "value": "f2"}], user="u", roles=ADMIN)
    with pytest.raises(Conflict, match="plan again"):
        await manifests.apply(plan.id)
    hub.nodes["r205-ctl"].online = False
    blocked = await manifests.plan()
    assert list(blocked.blocked) == ["r205-ctl"]
    with pytest.raises(InvalidRequest, match="unreachable while planning"):
        await manifests.apply(blocked.id)
    with pytest.raises(NotFound):
        await manifests.apply("p99")


async def test_reapply_keeps_the_first_backup_and_rollback_uses_it(hub: Hub) -> None:
    manifests, store = hub.services.manifests, hub.services.store
    original = io_on(hub, "r204-ctl")
    await manifests.edit([
        {"op": "add", "path": "/system/nodes/0/io/-", "value": DOOR},
        {"op": "add", "path": "/system/nodes/1/io/-", "value": {"channel": "di0", "type": "binary-input",
                                                              "instance": 1, "name": "R205 Window"}},
    ], user="u", roles=ADMIN)
    plan = await manifests.plan()
    hub.nodes["r205-ctl"].online = False
    for attempt in (1, 2):
        failed = await manifests.apply(plan.id)
        assert not failed.ok and failed.attempt == attempt and manifests.live_revision == 1
        assert (await store.get_plan(plan.id))["state"] == "failed"  # type: ignore[index]
        assert DOOR["channel"] in {p["channel"] for p in io_on(hub, "r204-ctl")}
    before = await store.get_plan_backup(plan.id, "r204-ctl", first=True)
    latest = await store.get_plan_backup(plan.id, "r204-ctl")
    assert before is not None and latest is not None and (before["attempt"], latest["attempt"]) == (1, 2)
    first_io = json.loads(base64.b64decode(before["documents"]["files"][CFG_IO]))
    assert first_io["points"] == original
    # The second attempt's backup already has the door; rollback restores the first one.
    assert DOOR["channel"] in {p["channel"] for p in json.loads(base64.b64decode(
        latest["documents"]["files"][CFG_IO]))["points"]}
    hub.nodes["r205-ctl"].online = True
    results = await manifests.rollback(plan.id)
    assert [r.change_id for r in results] == ["r204-ctl/rollback"]
    assert all(r.ok for r in results), results
    assert io_on(hub, "r204-ctl") == original == R204_IO
    third = await manifests.apply(plan.id)
    assert third.ok and third.attempt == 3 and manifests.live_revision == 2
    # Undoing a plan that went live would leave the devices and the policy's safety map
    # behind the live manifest; that takes a new plan.
    with pytest.raises(Conflict, match="revision 2 is live"):
        await manifests.rollback(plan.id)
    assert DOOR["channel"] in {p["channel"] for p in io_on(hub, "r204-ctl")}
