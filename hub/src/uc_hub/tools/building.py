"""Read tools (tier R) over the running site: search, tree, devices, values,
trends, priority arrays and discovery."""

from __future__ import annotations

import math
import time
from typing import Any

from ..core.errors import HubError
from ..core.types import ProtocolName, Quality, Value
from .common import (
    DEVICE_DATA_NOTE,
    clean_text,
    device_json,
    lines,
    point_json,
    point_refs,
    point_row,
    reading_json,
    reading_text,
    untrusted,
    value_text,
)
from .registry import Tool, ToolCallContext, ToolResult

MAX_POINTS_READ = 50
MAX_HISTORY_SAMPLES = 200
_PROTOCOLS = [p.value for p in ProtocolName]


async def site_search(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    site = ctx.site
    query = args.get("query", "")
    f = args.get("filters") or {}
    limit = args.get("limit", 20)
    total, points = site.search(query, space=f.get("space"), device=f.get("device"), protocol=f.get("protocol"),
                                tag=f.get("tag"), kind=f.get("kind"), writable=f.get("writable"), limit=limit)
    point_only = any(k in f for k in ("tag", "kind", "writable"))
    devices = [] if point_only else site.search_devices(query, space=f.get("space"), device=f.get("device"),
                                                        protocol=f.get("protocol"))
    data = {
        "devices": [device_json(d, len(site.points(d.name))) for d in devices[:limit]],
        "points": [point_row(p, site.latest(p.ref)) for p in points],
        "total_points": total,
    }
    where = f" in {f['space']}" if f.get("space") else ""
    what = f" matching {query!r}" if query else ""
    summary = (f"{len(devices)} device{_s(len(devices))} and {total} point{_s(total)}{what}{where}"
               + (f" (showing {len(points)})" if len(points) < total else ""))
    return ToolResult(True, summary, data)


async def site_tree(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    tree = ctx.site.tree(args.get("space"))
    spaces = _count_spaces(tree["spaces"])
    devices = sum(s["device_count"] for s in tree["spaces"]) + len(tree.get("unplaced", []))
    online = sum(s["online"] for s in tree["spaces"])
    summary = f"{spaces} space{_s(spaces)} with {devices} device{_s(devices)} ({online} online among the placed ones)"
    if tree.get("unplaced"):
        summary += f"; {len(tree['unplaced'])} without a space"
    return ToolResult(True, summary, tree)


def _count_spaces(spaces: list[dict[str, Any]]) -> int:
    return sum(1 + _count_spaces(s["children"]) for s in spaces)


async def device_describe(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    site = ctx.site
    name = args["device"]
    record = site.device(name)
    note = ""
    try:
        description = await site.describe(name)
    except HubError as e:
        cached = site.description(name)
        if cached is None:
            raise
        description = cached
        note = f"; the device did not answer now ({e.code}), this is its last description"
    readings = {r.ref: r for r in await site.read([p.ref for p in description.points])} if record.online else {}
    spec = site.device_spec(name)
    data = {
        "device": device_json(record, len(description.points)),
        "points": [point_json(p, readings.get(p.ref) or site.latest(p.ref)) for p in description.points],
        "manifest": {k: v for k, v in spec.spec.items() if k in ("equipment", "description", "profile", "board")},
        "device_data": {"apps": untrusted(description.apps), "extra": untrusted(description.extra)},
    }
    apps = len(description.apps)
    state = "online" if record.online else "offline"
    summary = (f"{name} ({record.protocol.value}, {state}): {len(description.points)} point"
               f"{_s(len(description.points))}" + (f", {apps} app{_s(apps)}" if apps else "") + note)
    return ToolResult(True, summary, data)


async def point_read(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    site = ctx.site
    refs = point_refs(site, args["points"])
    readings = await site.read(refs)
    items = []
    texts = []
    for reading in readings:
        point = site.known_point(reading.ref)
        items.append(point_json(point, reading) if point is not None else {"id": str(reading.ref),
                                                                          "reading": reading_json(reading)})
        texts.append(reading_text(reading, point))
    return ToolResult(True, lines(texts), {"points": items})


async def point_history(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    site = ctx.site
    ref = site.point_ref(args["point"])
    site.device(ref.device)
    minutes = args.get("minutes", 60)
    agg = args.get("agg", "avg")
    now = time.time()
    samples = site.history(ref, now - minutes * 60)
    watched = site.watch_count(ref) > 0
    start = now - minutes * 60
    data: dict[str, Any] = {"point": str(ref), "minutes": minutes, "agg": agg, "count": len(samples),
                            "watched": watched, "start": round(start, 1), "offsets_s": [], "values": []}
    if not samples:
        hint = "" if watched else " (it is recorded while it is watched: tagged points, bridge sources, live views)"
        return ToolResult(True, f"no history of {ref.device}/{ref.obj} in the last {minutes} min{hint}", data)
    series = _aggregate(samples, start, now, agg)
    numbers = [v for _, v in series if isinstance(v, (int, float)) and not isinstance(v, bool)]
    stats = (f": min {value_text(min(numbers))}, max {value_text(max(numbers))}, last {value_text(series[-1][1])}"
             if numbers else "")
    point = site.known_point(ref)
    units = f" {point.units}" if point is not None and point.units else ""
    bad = sum(1 for s in samples if s[2] is not Quality.GOOD)
    summary = (f"{ref.device}/{ref.obj}: {len(series)} value{_s(len(series))} ({agg}) from {len(samples)} "
               f"sample{_s(len(samples))} over {minutes} min{stats}{units}"
               + (f"; {bad} sample{_s(bad)} not good" if bad else ""))
    # Compact enough for the model to get the whole series inline.
    data["offsets_s"] = [round(ts - start) for ts, _ in series]
    data["values"] = [_compact(v) for _, v in series]
    return ToolResult(True, summary, data)


def _compact(value: Value) -> Value:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return None if isinstance(value, str) else value


def _aggregate(samples: list[tuple[float, Value, Quality]], start: float, end: float,
               agg: str) -> list[tuple[float, Value]]:
    """At most MAX_HISTORY_SAMPLES values: raw samples when they fit, else
    ``agg`` over equal time buckets (``last`` for non-numeric values)."""
    good: list[tuple[float, Value]] = [(ts, v) for ts, v, q in samples if q is Quality.GOOD and v is not None]
    if len(good) <= MAX_HISTORY_SAMPLES:
        return good
    width = (end - start) / MAX_HISTORY_SAMPLES
    buckets: dict[int, list[tuple[float, Value]]] = {}
    for ts, v in good:
        buckets.setdefault(min(MAX_HISTORY_SAMPLES - 1, int((ts - start) / width)), []).append((ts, v))
    out = []
    for index in sorted(buckets):
        items = buckets[index]
        numbers = [float(v) for _, v in items if isinstance(v, (int, float))]
        ts = start + (index + 0.5) * width
        if agg == "last" or len(numbers) != len(items):
            out.append((ts, items[-1][1]))
        elif agg == "min":
            out.append((ts, min(numbers)))
        elif agg == "max":
            out.append((ts, max(numbers)))
        else:
            out.append((ts, math.fsum(numbers) / len(numbers)))
    return out


async def priority_array(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    site = ctx.site
    ref = site.point_ref(args["point"])
    await site.point(ref)
    slots = await site.priority_array(ref)
    name = f"{ref.device}/{ref.obj}"
    if slots is None:
        return ToolResult(True, f"{name} has no priority array (it is not commandable)",
                          {"point": str(ref), "commandable": False, "slots": []})
    holders = _holders(ctx, str(ref))
    occupied = [(i, v) for i, v in enumerate(slots, start=1) if v is not None]
    used = [{"priority": i, "value": v if not isinstance(v, str) else None, "holder": holders.get(i)}
            for i, v in occupied]
    agent = ctx.services.policy.settings.agent_write_priority
    text = [f"{i} = {value_text(v)}" + (f" ({holders[i]})" if i in holders else "") for i, v in occupied]
    summary = (f"{name}: " + (lines(text, 16) if text else "every slot is empty (relinquish default applies)")
               + f"; the agent writes at {agent}")
    return ToolResult(True, summary, {"point": str(ref), "commandable": True, "active": used[0] if used else None,
                                      "slots": used, "agent_priority": agent})


def _holders(ctx: ToolCallContext, pid: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for lease in ctx.services.leases.active():
        if lease.kind == "write" and lease.target.get("point") == pid and lease.priority is not None:
            out[lease.priority] = f"agent lease {lease.id}, {max(0, lease.expires_at - time.time()):.0f} s left"
    for bridge in ctx.services.bridges.status():
        if bridge["to"] == pid and bridge["holding"] is not None:
            out.setdefault(bridge["holding"], f"bridge from {bridge['from']}")
    return out


async def discover(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    protocol = ProtocolName(args["protocol"]) if "protocol" in args else None
    found = await ctx.site.discover(protocol, args.get("timeout_s", 3.0))
    # An MQTT address is a topic prefix, i.e. the device's chosen client id: device text too.
    devices = [
        {"protocol": d["protocol"], "instance": d["instance"], "bacnet_uc": d["bacnet_uc"], "known": d["known"],
         "device": d["device"], "device_data": {"address": clean_text(d["address"], 128), **untrusted(
             {"name": d["name"], "model": d["model"], "hwid": d["hwid"], "extra": d["extra"]})}}
        for d in found["devices"]
    ]
    new = sum(1 for d in devices if not d["known"])
    summary = f"found {len(devices)} device{_s(len(devices))}, {new} not in the manifest"
    if found["errors"]:
        summary += "; failed: " + ", ".join(sorted(found["errors"]))
    return ToolResult(True, summary, {"devices": devices, "errors": untrusted(found["errors"])})


def _s(n: int) -> str:
    return "" if n == 1 else "s"


_FILTERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "space": {"type": "string", "description": "Space id; child spaces are included."},
        "device": {"type": "string"},
        "protocol": {"enum": _PROTOCOLS},
        "tag": {"type": "string", "description": "A Brick class or marker tag, e.g. Zone_Air_Temperature_Sensor."},
        "kind": {"enum": ["input", "output", "value"]},
        "writable": {"type": "boolean"},
    },
}
_POINT = {"type": "string", "minLength": 3, "maxLength": 200,
          "description": "Point id: site/device/object or device/object."}

TOOLS = [
    Tool(
        "site_search",
        "Search the building's devices and points by text (id, name, description, tags, space) and filters. "
        "This is the main way to find points; the full point list is never sent. Points come as compact rows "
        "with their latest cached value; device_describe and point_read give the details." + DEVICE_DATA_NOTE,
        "R",
        {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "maxLength": 200},
            "filters": _FILTERS,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
        }},
        site_search,
    ),
    Tool(
        "site_tree",
        "Spaces as a tree with their devices and device, online and point counts (a space's counts include "
        "its children).",
        "R",
        {"type": "object", "additionalProperties": False, "properties": {"space": {"type": "string"}}},
        site_tree,
    ),
    Tool(
        "device_describe",
        "Read a device now: its points with current values, installed apps (BACnet-uc), firmware and "
        "online state." + DEVICE_DATA_NOTE,
        "R",
        {"type": "object", "additionalProperties": False, "required": ["device"],
         "properties": {"device": {"type": "string", "minLength": 1, "maxLength": 64}}},
        device_describe,
    ),
    Tool(
        "point_read",
        "Read present values (quality good, stale, fault or offline, with age)." + DEVICE_DATA_NOTE,
        "R",
        {"type": "object", "additionalProperties": False, "required": ["points"],
         "properties": {"points": {"type": "array", "minItems": 1, "maxItems": MAX_POINTS_READ, "items": _POINT}}},
        point_read,
    ),
    Tool(
        "point_history",
        f"Recorded values of a point over the last minutes, aggregated to at most {MAX_HISTORY_SAMPLES} samples: "
        "values[i] was recorded offsets_s[i] seconds after start (Unix time).",
        "R",
        {"type": "object", "additionalProperties": False, "required": ["point"], "properties": {
            "point": _POINT,
            "minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "default": 60},
            "agg": {"enum": ["avg", "min", "max", "last"], "default": "avg"},
        }},
        point_history,
    ),
    Tool(
        "priority_array",
        "Who commands a point: the occupied BACnet priority slots, with the agent's leases and gateway bridges "
        "that hold them.",
        "R",
        {"type": "object", "additionalProperties": False, "required": ["point"], "properties": {"point": _POINT}},
        priority_array,
    ),
    Tool(
        "discover",
        "Sweep the network (BACnet-uc SMP, BACnet/IP Who-Is, MQTT retained info) for devices; each result "
        "says whether the manifest already has it." + DEVICE_DATA_NOTE,
        "R",
        {"type": "object", "additionalProperties": False, "properties": {
            "protocol": {"enum": _PROTOCOLS},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 10, "default": 3},
        }},
        discover,
        timeout_s=30.0,
    ),
]
