"""Live tools (tier L): leased point writes and IO forces, identify, and the
manifest's acceptance tests on the live site. Each call needs an approval;
``describe_approval`` applies the policy first, so a call the policy refuses
(a life-safety point, a deny pattern, a priority above the agent's) is
rejected before anyone is asked."""

from __future__ import annotations

import time
from typing import Any

from ..core.errors import InvalidRequest
from ..core.types import Point, TestResult
from ..manifest.testrun import PRESENT_VALUE
from .common import DEVICE_DATA_NOTE, clean_text, lines, value_text
from .registry import ApprovalInfo, Tool, ToolCallContext, ToolResult

MAX_IDENTIFY_S = 3600


def _lease_s(ctx: ToolCallContext, args: dict[str, Any]) -> int:
    return int(ctx.services.policy.clamp_lease(args.get("lease_s")))


def _label(point: Point | None, fallback: str) -> str:
    return f"{fallback} ({clean_text(point.name, 60)})" if point is not None and point.name != point.ref.obj \
        else fallback


# -- point_write ---------------------------------------------------------------------------
async def describe_point_write(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    ref = ctx.site.point_ref(args["point"])
    point, priority = await ctx.services.control.check_write(ref, args["value"])
    seconds = _lease_s(ctx, args)
    (current,) = await ctx.site.read([ref])
    before = f"{value_text(current.value)} -> " if current.value is not None else ""
    where = f"at priority {priority}" if priority is not None else "(no priority array)"
    undo = (f"Relinquished at priority {priority} after {seconds} s, or earlier on request" if priority is not None
            else f"The value before the write is written back after {seconds} s")
    return ApprovalInfo(
        title=f"Write {value_text(args['value'])} to {ref.device}/{ref.obj} for {seconds} s",
        summary=[f"{ref.device}: {_label(point, ref.obj)} {before}{value_text(args['value'])} {where}, "
                 f"lease {seconds} s"],
        rollback=undo,
        targets=1,
    )


async def point_write(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    ref = ctx.site.point_ref(args["point"])
    result, lease = await ctx.services.control.write(ref, args["value"], lease_s=args.get("lease_s"),
                                                     user=ctx.user, run_id=ctx.run_id)
    seconds = round(lease.expires_at - lease.created_at)
    where = f" at priority {lease.priority}" if lease.priority is not None else ""
    undo = "relinquished" if lease.priority is not None else "restored"
    summary = (f"wrote {value_text(args['value'])} to {ref.device}/{ref.obj}{where}; {undo} automatically in "
               f"{seconds} s (lease {lease.id})")
    return ToolResult(True, summary, {"point": str(ref), "value": result.value, "priority": lease.priority,
                                      "lease": _lease_json(lease)})


def _lease_json(lease: Any) -> dict[str, Any]:
    return {"id": lease.id, "expires_at": lease.expires_at, "seconds": round(lease.expires_at - lease.created_at)}


# -- io_force / io_release -----------------------------------------------------------------
async def describe_io_force(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    node, channel = args["node"], args["channel"]
    point = await ctx.services.control.check_force(node, channel)
    seconds = _lease_s(ctx, args)
    feeds = f", which {'drives' if point.kind.value == 'output' else 'feeds'} {_label(point, point.ref.obj)}" \
        if point is not None else ""
    return ApprovalInfo(
        title=f"Force {node} {channel} to {value_text(args['value'])} for {seconds} s",
        summary=[f"{node}: force channel {channel} to {value_text(args['value'])}{feeds}, lease {seconds} s"],
        rollback=f"The force is released after {seconds} s, or earlier with io_release",
        targets=1,
    )


async def io_force(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    node, channel = args["node"], args["channel"]
    lease, point = await ctx.services.control.force(node, channel, float(args["value"]), lease_s=args.get("lease_s"),
                                                    user=ctx.user, run_id=ctx.run_id)
    seconds = round(lease.expires_at - lease.created_at)
    feeds = f" ({point.ref.obj})" if point is not None else ""
    return ToolResult(True, f"forced {node} {channel}{feeds} to {value_text(args['value'])}; released automatically "
                            f"in {seconds} s (lease {lease.id})",
                      {"node": node, "channel": channel, "value": args["value"],
                       "point": str(point.ref) if point is not None else None, "lease": _lease_json(lease)})


async def describe_io_release(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    node, channel = args["node"], args["channel"]
    await ctx.services.control.check_force(node, channel)
    return ApprovalInfo(title=f"Release the force of {node} {channel}",
                        summary=[f"{node}: release channel {channel}; it follows its input or object again"],
                        rollback="Force it again with io_force", targets=1)


async def io_release(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    node, channel = args["node"], args["channel"]
    lease = await ctx.services.control.release_force(node, channel)
    how = f" (lease {lease.id})" if lease is not None else ""
    return ToolResult(True, f"released the force of {node} {channel}{how}", {"node": node, "channel": channel})


# -- device_identify ---------------------------------------------------------------------------
async def describe_identify(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    device = args["device"]
    record = ctx.site.device(device)
    seconds = args.get("seconds", 30)
    return ApprovalInfo(
        title=f"Identify {device} for {seconds} s",
        summary=[f"{device}: blink its LED for {seconds} s ({record.protocol.value}, {record.address})"],
        rollback="Identify stops by itself", targets=1,
    )


async def device_identify(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    device = args["device"]
    seconds = args.get("seconds", 30)
    await ctx.site.identify(device, seconds)
    return ToolResult(True, f"{device} identifies itself for {seconds} s (its LED blinks)",
                      {"device": device, "seconds": seconds})


# -- test_run ------------------------------------------------------------------------------------
def _selected(ctx: ToolCallContext, args: dict[str, Any]) -> list[dict[str, Any]]:
    tests = ctx.services.manifests.live.tests
    names = args.get("tests")
    if names is None:
        if not tests:
            raise InvalidRequest("the live manifest has no tests (system.tests)")
        return tests  # type: ignore[no-any-return]
    known = {t["name"]: t for t in tests}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise InvalidRequest(f"no such test in the live manifest: {', '.join(unknown)}")
    return [known[n] for n in dict.fromkeys(names)]


async def describe_test_run(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    tests = _selected(ctx, args)
    summary = []
    for test in tests:
        steps = test.get("steps") or []
        forces = sorted({f"{s['force']['node']}/{s['force']['channel']}" for s in steps if "force" in s})
        writes = sorted({str(s["write"]["point"]) for s in steps if "write" in s
                         and s["write"].get("property", PRESENT_VALUE) == PRESENT_VALUE})
        parts = [f"{len(steps)} steps"]
        if forces:
            parts.append("forces " + ", ".join(forces))
        if writes:
            parts.append("writes " + ", ".join(writes))
        summary.append(f"{test['name']}: {'; '.join(parts)}")
    return ApprovalInfo(
        title=f"Run {len(tests)} test{'s' if len(tests) != 1 else ''} on the live site",
        summary=summary,
        rollback="Every force is released and every write relinquished when a test ends; leases cover a hub crash",
        targets=len({t for test in tests for t in _test_targets(test)}),
    )


def _test_targets(test: dict[str, Any]) -> set[str]:
    out = set()
    for step in test.get("steps") or []:
        body = next(iter(step.values())) if isinstance(step, dict) and step else None
        if isinstance(body, dict):
            if "node" in body:
                out.add(str(body["node"]))
            elif "point" in body:
                out.add(str(body["point"]).split("/")[-2])
    return out


async def test_run(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    tests = _selected(ctx, args)
    started = time.monotonic()
    results: list[TestResult] = await ctx.services.run_live_tests(
        [t["name"] for t in tests] if "tests" in args else None, user=ctx.user, run_id=ctx.run_id)
    passed = sum(1 for r in results if r.status == "pass")
    failed = [r for r in results if r.status != "pass"]
    summary = f"{passed} of {len(results)} test{'s' if len(results) != 1 else ''} passed"
    if failed:
        summary += ": " + lines([f"{r.name} {r.status} at step {r.failed_step}" for r in failed], 5)
    summary += f" ({time.monotonic() - started:.1f} s)"
    # A detail quotes what a step read or the device answered: data, never the summary.
    data = {"results": [{k: v for k, v in r.to_json().items() if k != "detail"} for r in results],
            "device_data": {"details": {r.name: clean_text(r.detail, 500) for r in results if r.detail}}}
    return ToolResult(not failed, summary, data, None if not failed else "tests_failed")


_LEASE = {"type": "number", "exclusiveMinimum": 0, "maximum": 604800,
          "description": "Lease length in seconds; the site's default when left out, never more than its maximum."}
_VALUE = {"type": ["number", "boolean", "string"],
          "description": "real and int points take numbers, enum points 0 (inactive) or 1 (active)."}

TOOLS = [
    Tool(
        "point_write",
        "Write a point on the live site at the agent's priority, under a lease: the value is relinquished "
        "automatically when the lease ends. A point without a priority array (commandable false) gets its "
        "previous value back instead, so its current value must be readable. Life-safety points and points "
        "the site policy denies are never writable. Permanent changes go through manifest_edit, plan and apply.",
        "L",
        {"type": "object", "additionalProperties": False, "required": ["point", "value"], "properties": {
            "point": {"type": "string", "minLength": 3, "maxLength": 200},
            "value": _VALUE,
            "lease_s": _LEASE,
        }},
        point_write,
        describe_approval=describe_point_write,
    ),
    Tool(
        "io_force",
        "IO checkout on a BACnet-uc node: force an output channel, or simulate an input channel, to a raw value "
        "(mV for analog inputs, % for analog outputs, 0/1 for digital) under a lease.",
        "L",
        {"type": "object", "additionalProperties": False, "required": ["node", "channel", "value"], "properties": {
            "node": {"type": "string", "minLength": 1, "maxLength": 64},
            "channel": {"type": "string", "minLength": 1, "maxLength": 32},
            "value": {"type": "number"},
            "lease_s": _LEASE,
        }},
        io_force,
        describe_approval=describe_io_force,
    ),
    Tool(
        "io_release",
        "Release an IO force before its lease ends.",
        "L",
        {"type": "object", "additionalProperties": False, "required": ["node", "channel"], "properties": {
            "node": {"type": "string", "minLength": 1, "maxLength": 64},
            "channel": {"type": "string", "minLength": 1, "maxLength": 32},
        }},
        io_release,
        describe_approval=describe_io_release,
    ),
    Tool(
        "device_identify",
        "Make a device blink its LED so a technician can find it.",
        "L",
        {"type": "object", "additionalProperties": False, "required": ["device"], "properties": {
            "device": {"type": "string", "minLength": 1, "maxLength": 64},
            "seconds": {"type": "integer", "minimum": 1, "maximum": MAX_IDENTIFY_S, "default": 30},
        }},
        device_identify,
        describe_approval=describe_identify,
    ),
    Tool(
        "test_run",
        "Run the live manifest's acceptance tests (system.tests), or the named ones, on the live site. Test "
        "writes and forces follow the same rules as point_write and io_force and are undone when a test ends."
        + DEVICE_DATA_NOTE,
        "L",
        {"type": "object", "additionalProperties": False, "properties": {
            "tests": {"type": "array", "minItems": 1, "maxItems": 100,
                      "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        }},
        test_run,
        describe_approval=describe_test_run,
        timeout_s=3600.0,
    ),
]
