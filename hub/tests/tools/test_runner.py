"""ToolRunner: argument validation, the policy gate, approvals, execution,
result handles and the audit trail."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from support.site import Hub, start_hub

from uc_hub.core.errors import DeviceError, PolicyDenied
from uc_hub.tools import ApprovalInfo, Tool, ToolRegistry, ToolResult, ToolRunner


async def test_invalid_arguments_are_returned_not_retried(hub: Hub) -> None:
    prepared = await hub.prepare("point_read", {})
    assert prepared.action == "refused" and prepared.result is not None
    assert (prepared.result.ok, prepared.result.error_code) == (False, "invalid")
    assert prepared.result.data == {"errors": ["<root>: 'points' is a required property"]}
    assert await hub.services.runner.execute(prepared) is prepared.result
    too_many = await hub.prepare("site_search", {"limit": 500, "extra": 1})
    assert too_many.result is not None and "500 is greater than the maximum of 200" in too_many.result.summary
    assert "Additional properties" in too_many.result.summary
    broken = await hub.prepare("site_search", '{"query": ')
    assert broken.result is not None and "not valid JSON" in broken.result.summary
    # Python's json module takes NaN and Infinity; JSON (and the model) does not.
    nan = await hub.prepare("io_force", '{"node": "r204-ctl", "channel": "ao0", "value": NaN}')
    assert nan.result is not None and nan.result.error_code == "invalid" and "NaN" in nan.result.summary
    assert (await hub.prepare("site_search", "")).action == "run"
    assert (await hub.prepare("site_search", "[1]")).result.summary == "the arguments must be a JSON object"  # type: ignore[union-attr]
    unknown = await hub.prepare("self_destruct", {})
    assert unknown.result is not None and unknown.result.error_code == "invalid"
    assert "site_search" in unknown.result.summary


async def test_the_policy_decides_by_role_and_tier(hub: Hub) -> None:
    assert (await hub.prepare("site_search", {}, "viewer")).action == "run"
    denied = await hub.prepare("manifest_edit", {"json_patch": [{"op": "test", "path": ""}]}, "viewer")
    assert denied.action == "refused" and denied.result.error_code == "denied"  # type: ignore[union-attr]
    assert "viewers may only run read tools" in denied.reason
    assert (await hub.prepare("plan", {}, "operator")).action == "run"
    assert (await hub.prepare("point_write", {"point": "r204-ctl/analog-output:1", "value": 5}, "viewer")
            ).action == "refused"
    nobody = await hub.prepare("site_search", {}, "janitor")
    assert nobody.action == "refused" and "no role" in nobody.reason


async def test_live_calls_need_an_approval(hub: Hub) -> None:
    runner = hub.services.runner
    prepared = await hub.prepare("point_write", {"point": "r204-ctl/analog-output:1", "value": 55, "lease_s": 60},
                                 "operator")
    assert prepared.action == "approval" and prepared.tier == "L" and prepared.approval is not None
    assert prepared.approval.summary == ["r204-ctl: analog-output:1 (R204 Valve) 0 -> 55 at priority 12, lease 60 s"]
    assert prepared.approval.title == "Write 55 to r204-ctl/analog-output:1 for 60 s"
    assert prepared.approval.rollback == "Relinquished at priority 12 after 60 s, or earlier on request"
    assert runner.can_approve(prepared, {"operator"}) and not runner.can_approve(prepared, {"viewer"})
    with pytest.raises(ValueError, match="needs an approval"):
        await runner.execute(prepared)
    # The runner itself holds the approval rules, whoever calls it.
    with pytest.raises(PolicyDenied, match="guest may not approve this tier L call"):
        await runner.execute(prepared, approved_by="guest", approver_roles={"viewer"})
    assert hub.nodes["r204-ctl"].objects[(1, 1)].priority[11] is None  # type: ignore[index]
    result = await runner.execute(prepared, approved_by="boss", approver_roles={"operator"})
    assert result.ok and hub.nodes["r204-ctl"].objects[(1, 1)].priority[11] == 55  # type: ignore[index]
    (entry, *_) = await hub.services.store.list_audit()
    assert (entry["tool"], entry["tier"], entry["outcome"], entry["user"]) == ("point_write", "L", "ok", "tester")
    assert entry["args"] == {"point": "r204-ctl/analog-output:1", "value": 55, "lease_s": 60}
    assert entry["detail"].endswith("(approved by boss)")


async def test_hub_errors_become_failed_results(hub: Hub) -> None:
    result = await hub.call("device_describe", {"device": "nope"}, "viewer", run_id=None)
    assert (result.ok, result.error_code) == (False, "not_found")
    assert result.summary == "no device 'nope' in the site manifest"
    result = await hub.call("manifest_edit", {"json_patch": [{"op": "add", "path": "/placement/r204-ctl",
                                                              "value": "mars"}]})
    assert (result.ok, result.error_code) == (False, "validation")
    assert result.data == {"errors": ["/placement/r204-ctl: unknown space 'mars'"]}


async def test_timeouts_and_crashes(hub: Hub) -> None:
    async def slow(ctx: Any, args: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    async def broken(ctx: Any, args: dict[str, Any]) -> ToolResult:
        raise KeyError("boom")

    async def device(ctx: Any, args: dict[str, Any]) -> ToolResult:
        raise DeviceError("busy", rc=5)

    registry = ToolRegistry()
    schema = {"type": "object"}
    registry.register(Tool("slow", "sleeps", "R", schema, slow, timeout_s=0.05))
    registry.register(Tool("broken", "crashes", "R", schema, broken))
    registry.register(Tool("device", "fails", "R", schema, device))
    runner = ToolRunner(registry, hub.services.store)
    ctx = hub.ctx("viewer")
    results = [await runner.execute(await runner.prepare(ctx, name, {})) for name in ("slow", "broken", "device")]
    assert [(r.ok, r.error_code) for r in results] == [
        (False, "timeout"), (False, "error"), (False, "device_error")]
    assert results[0].summary == "slow did not finish within 0.05 s"
    assert results[1].summary == "internal error in broken: KeyError: 'boom'"
    outcomes = [(e["tool"], e["outcome"]) for e in await hub.services.store.list_audit(3)]
    assert outcomes == [("device", "device_error"), ("broken", "error"), ("slow", "timeout")]


async def test_unexpected_errors_while_preparing_are_refusals(hub: Hub) -> None:
    """Building an approval card reads devices; a hang or a socket error
    there is a failed result for the model, not an exception in the loop."""
    async def noop(ctx: Any, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("not reached")

    async def hangs(ctx: Any, args: dict[str, Any]) -> ApprovalInfo:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    async def unreachable(ctx: Any, args: dict[str, Any]) -> ApprovalInfo:
        raise OSError(113, "No route to host")

    registry = ToolRegistry()
    schema = {"type": "object"}
    registry.register(Tool("hangs", "hangs", "L", schema, noop, describe_approval=hangs))
    registry.register(Tool("unreachable", "fails", "L", schema, noop, describe_approval=unreachable))
    runner = ToolRunner(registry, hub.services.store, default_timeout_s=0.05)
    ctx = hub.ctx("operator")
    hung = await runner.prepare(ctx, "hangs", {})
    assert hung.action == "refused" and hung.result is not None
    assert (hung.result.error_code, hung.result.summary) == ("timeout", "hangs: the approval could not be "
                                                                        "prepared within 0.05 s")
    failed = await runner.prepare(ctx, "unreachable", {})
    assert failed.action == "refused" and failed.result is not None and failed.result.error_code == "error"
    assert failed.result.summary == "unreachable: the approval could not be prepared: OSError: [Errno 113] No route " \
                                    "to host"
    outcomes = [(e["tool"], e["outcome"]) for e in await hub.services.store.list_audit(2)]
    assert outcomes == [("unreachable", "error"), ("hangs", "timeout")]


async def test_a_cancelled_call_is_audited(hub: Hub) -> None:
    started = asyncio.Event()

    async def slow(ctx: Any, args: dict[str, Any]) -> ToolResult:
        started.set()
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    registry = ToolRegistry()
    registry.register(Tool("slow", "sleeps", "L", {"type": "object"}, slow))
    runner = ToolRunner(registry, hub.services.store)
    prepared = await runner.prepare(hub.ctx("operator"), "slow", {"n": 1})
    call = asyncio.create_task(runner.execute(prepared, approved_by="boss", approver_roles={"operator"}))
    await started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    (entry,) = await hub.services.store.list_audit(1)
    assert (entry["tool"], entry["tier"], entry["outcome"], entry["args"]) == ("slow", "L", "cancelled", {"n": 1})
    assert entry["detail"] == "slow was cancelled (approved by boss)"


async def test_large_results_become_handles_and_page(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, hub={"agent": {"result_inline_limit": 1000}})
    try:
        run = await hub.services.store.create_run(title="t", created_by="tester")
        ids = [str(p.ref) for p in hub.site.points()]
        result = await hub.call("point_read", {"points": ids}, run_id=run["id"])
        assert result.ok and result.data is None and result.handle is not None
        # The summary is for people too; the paging hint is for the model only.
        assert result.handle.startswith("result://r") and "result_get" not in result.summary
        model_text = result.to_model_text()
        assert f'"handle": "{result.handle}"' in model_text and "result_get" in model_text
        seen: list[str] = []
        offset: int | None = 0
        while offset is not None:
            page = await hub.call("result_get", {"handle": result.handle, "offset": offset}, run_id=run["id"])
            assert page.ok and page.handle is None, page.summary
            assert (page.data["key"], page.data["total"]) == ("points", 7)  # type: ignore[index]
            seen += [p["id"] for p in page.data["items"]]  # type: ignore[index]
            offset = page.data["next_offset"]  # type: ignore[index]
        assert seen == ids
        # Without a single list, the JSON text is paged.
        searched = await hub.call("site_search", {}, run_id=run["id"])
        assert searched.handle is not None
        text, offset = "", 0
        while offset is not None:
            page = await hub.call("result_get", {"handle": searched.handle, "offset": offset}, run_id=run["id"])
            text += page.data["text"]  # type: ignore[index]
            offset = page.data["next_offset"]  # type: ignore[index]
        stored = await hub.services.store.get_result(searched.handle)
        assert json.loads(text) == stored and stored["total_points"] == 7
        other_run = await hub.call("result_get", {"handle": result.handle}, run_id="r_other")
        assert other_run.error_code == "not_found"
    finally:
        await hub.close()


async def test_every_call_is_audited(hub: Hub) -> None:
    run = await hub.services.store.create_run(title="t", created_by="tester")
    await hub.call("site_tree", {}, "viewer", run_id=run["id"])
    await hub.prepare("plan", {}, "viewer", run_id=run["id"])
    await hub.prepare("point_read", {"points": []}, "viewer", run_id=run["id"])
    entries = await hub.services.store.list_audit(run_id=run["id"])
    assert [(e["tool"], e["tier"], e["outcome"], e["user"], e["action"]) for e in entries] == [
        ("point_read", "R", "invalid", "tester", "tool"),
        ("plan", "S", "denied", "tester", "tool"),
        ("site_tree", "R", "ok", "tester", "tool"),
    ]
    assert entries[0]["args"] == {"points": []}
