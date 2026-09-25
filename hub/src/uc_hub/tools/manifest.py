"""Manifest tools: read the draft or live manifest (R), edit the draft and
plan it (S), and apply an approved plan (C)."""

from __future__ import annotations

import json
from typing import Any

from ..core.types import Plan
from ..manifest.plan import GATEWAY
from .common import DEVICE_DATA_NOTE, clean_text, lines
from .registry import ApprovalInfo, Tool, ToolCallContext, ToolResult

_SUMMARY_LIMIT = 300


async def manifest_get(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    path = args.get("path", "")
    which, revision, value = ctx.services.manifests.get(args.get("which"), path)
    shape = (f"object with {len(value)} key{'s' if len(value) != 1 else ''}" if isinstance(value, dict)
             else f"array of {len(value)}" if isinstance(value, list) else json.dumps(value)[:80])
    summary = f"{which} revision {revision}, {path or 'whole manifest'}: {shape}"
    return ToolResult(True, summary, {"which": which, "revision": revision, "path": path, "value": value})


async def manifest_edit(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    manifests = ctx.services.manifests
    result = await manifests.edit(args["json_patch"], user=ctx.user, roles=ctx.roles, message=args.get("message", ""),
                                  run_id=ctx.run_id)
    if not result.changed:
        summary = f"the patch changes nothing; the draft stays revision {result.draft_revision}"
    else:
        changed = ", ".join(result.sections) or "nothing"
        summary = (f"draft revision {result.draft_revision} is valid; it differs from live revision "
                   f"{manifests.live_revision} in: {changed}")
    return ToolResult(True, summary, {"draft_revision": result.draft_revision, "changed": result.changed,
                                      "sections": result.sections, "diff": result.diff})


async def plan(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    manifests = ctx.services.manifests
    draft = manifests.draft is not None
    computed = await manifests.plan(run_id=ctx.run_id)
    data = {
        "plan_id": computed.id,
        "revision": computed.revision,
        "changes": [f"{c.target}: {c.summary}" for c in computed.changes],
        "warnings": computed.warnings,
        "blocked": computed.blocked,
    }
    if computed.blocked:
        summary = (f"plan {computed.id} is incomplete and cannot be applied; not reachable: "
                   + ", ".join(f"{t} ({clean_text(r)})" for t, r in computed.blocked.items()))
        return ToolResult(False, summary + _warnings(computed), data, "blocked")
    if not computed.changes and not draft:
        data["plan_id"] = None
        return ToolResult(True, f"the live site matches revision {computed.revision}; nothing to apply"
                          + _warnings(computed), data)
    if not computed.changes:
        summary = (f"plan {computed.id}: no device changes; applying makes revision {computed.revision} live "
                   f"({', '.join(manifests.changed_sections()) or 'no sections'} changed)")
    else:
        targets = ", ".join(computed.targets)
        summary = f"plan {computed.id}: {len(computed.changes)} change{_s(len(computed.changes))} on {targets}"
    return ToolResult(True, summary + _warnings(computed), data)


def _warnings(computed: Plan) -> str:
    n = len(computed.warnings)
    return f"; {n} warning{_s(n)}" if n else ""


async def describe_apply(ctx: ToolCallContext, args: dict[str, Any]) -> ApprovalInfo:
    manifests = ctx.services.manifests
    computed = await manifests.get_plan(args["plan_id"])
    manifests.check_applicable(computed)
    summary = []
    for target in computed.targets:
        details = [c.summary for c in computed.changes if c.target == target]
        summary.append(_clip(f"{target}: {'; '.join(details)}"))
    sections = manifests.changed_sections()
    if sections:
        summary.append(f"site.yaml: {', '.join(sections)}")
    summary.extend(computed.warnings)
    diffs = [manifests.diff()]
    diffs.extend(c.diff for c in computed.changes if c.diff)
    life_safety = any(
        c.payload.get("life_safety", {}).get(k) for c in computed.changes if c.target == GATEWAY and c.kind == "tags"
        for k in ("added", "removed")
    )
    return ApprovalInfo(
        title=f"Apply plan {computed.id} (revision {computed.revision})",
        summary=summary,
        diff="".join(d if d.endswith("\n") else d + "\n" for d in diffs if d),
        rollback=f"Apply revision {computed.base_revision} again; every target is backed up before it changes",
        plan_id=computed.id,
        targets=len(computed.targets),
        admin_only=life_safety,
    )


async def apply(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    outcome = await ctx.services.manifests.apply(args["plan_id"], run_id=ctx.run_id)
    computed = outcome.plan
    # Details quote the nodes (an app's last error, an SMP reason): data, never the summary.
    results = [{"change_id": r.change_id, "ok": r.ok} for r in outcome.results]
    details = {"details": {r.change_id: clean_text(r.detail, 500) for r in outcome.results if r.detail}}
    if outcome.ok:
        n = len(computed.changes)
        summary = f"applied plan {computed.id}: {n} change{_s(n)}; revision {outcome.live_revision} is live"
        return ToolResult(True, summary, {"plan_id": computed.id, "live_revision": outcome.live_revision,
                                          "results": results, "device_data": details})
    failed = [r for r in outcome.results if not r.ok and not r.detail.startswith("not run")]
    skipped = sum(1 for r in outcome.results if r.detail.startswith("not run"))
    summary = (f"plan {computed.id} stopped at " + lines([r.change_id for r in failed], 3)
               + f"; {skipped} step{_s(skipped)} not run; revision {outcome.live_revision} stays live "
               "(why: device_data.details). The targets that changed are backed up; apply a plan of the live "
               "revision to restore them")
    return ToolResult(False, _clip(summary, 1500), {"plan_id": computed.id, "live_revision": outcome.live_revision,
                                                    "attempt": outcome.attempt, "results": results,
                                                    "device_data": details}, "failed")


def _clip(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


TOOLS = [
    Tool(
        "manifest_get",
        "Read the draft manifest (the default when there is one) or the live one, or a part of it by JSON "
        "Pointer (RFC 6901, e.g. /tags or /system/nodes/0).",
        "R",
        {"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string", "maxLength": 500, "default": ""},
            "which": {"enum": ["draft", "live"]},
        }},
        manifest_get,
    ),
    Tool(
        "manifest_edit",
        "Change the draft manifest with an RFC 6902 JSON Patch (the draft is created from the live revision on "
        "the first edit). The result is validated at once: a failing patch changes nothing and the errors name "
        "the paths to fix. Nothing reaches a device until a plan is applied.",
        "S",
        {"type": "object", "additionalProperties": False, "required": ["json_patch"], "properties": {
            "json_patch": {"type": "array", "minItems": 1, "maxItems": 200, "items": {
                "type": "object", "required": ["op", "path"],
                "properties": {"op": {"enum": ["add", "remove", "replace", "move", "copy", "test"]},
                               "path": {"type": "string"}, "from": {"type": "string"}, "value": {}},
            }},
            "message": {"type": "string", "maxLength": 200},
        }},
        manifest_edit,
    ),
    Tool(
        "plan",
        "Diff the draft against the live site: the changes per device and for the gateway, with warnings and "
        "the targets that could not be reached (a plan with blocked targets cannot be applied). Without a draft "
        "it checks the live revision against the devices.",
        "S",
        {"type": "object", "additionalProperties": False, "properties": {}},
        plan,
        timeout_s=300.0,
    ),
    Tool(
        "apply",
        "Apply a plan to the devices and the gateway in stages, verifying each device and stopping at the first "
        "failure. Always needs a human approval of the plan's diff." + DEVICE_DATA_NOTE,
        "C",
        {"type": "object", "additionalProperties": False, "required": ["plan_id"],
         "properties": {"plan_id": {"type": "string", "minLength": 1, "maxLength": 64}}},
        apply,
        describe_approval=describe_apply,
        timeout_s=3600.0,
    ),
]
