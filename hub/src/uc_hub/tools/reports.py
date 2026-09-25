"""Handover documents as Markdown (tier R): the commissioning report, the
point list and the IO checkout sheet. They quote device names, so the
Markdown is returned under ``device_data``."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from .. import __version__
from ..core.ids import PointRef
from ..core.types import Point, ProtocolName, Quality, Reading
from .common import DEVICE_DATA_NOTE, clean_text, value_text
from .registry import Tool, ToolCallContext, ToolResult


def _cell(value: Any, limit: int = 80) -> str:
    text = clean_text("" if value is None else value, limit)
    return text.replace("|", "\\|") or "-"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return out if rows else out + ["| " + " | ".join(["-"] * len(header)) + " |"]


def _stamp(ts: float | None) -> str:
    if ts is None:
        return "-"
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _value(reading: Reading | None, point: Point | None) -> str:
    if reading is None or reading.value is None:
        return "-" if reading is None else reading.quality.value
    units = f" {point.units}" if point is not None and point.units else ""
    text = clean_text(reading.value, 40) if isinstance(reading.value, str) else value_text(reading.value)
    return f"{text}{units}" + ("" if reading.quality is Quality.GOOD else f" ({reading.quality.value})")


async def _commissioning(ctx: ToolCallContext) -> list[str]:
    site, services = ctx.site, ctx.services
    summary = site.summary()
    tests = (await services.store.test_results("live"))["results"]
    passed = sum(1 for t in tests if t["status"] == "pass")
    md = [
        f"# Commissioning report: {_cell(site.name)}",
        "",
        f"{_cell(site.manifest.description, 200)}",
        "",
        f"Generated {_stamp(time.time())} by uc-hub {__version__}; live manifest revision "
        f"{services.manifests.live_revision}.",
        "",
        "## Summary",
        "",
        f"- Devices: {summary['devices']} ({summary['online']} online), points: {summary['points']}",
        f"- Acceptance tests on the live site: {passed} of {len(tests)} passed",
        f"- Changes not applied yet: {summary['pending_changes']}",
        "",
        "## Devices",
        "",
    ]
    md += _table(["Device", "Protocol", "Address", "Space", "Online", "Model", "Firmware", "Points"], [
        [d.name, d.protocol.value, d.address, d.space, "yes" if d.online else "no", d.model, d.firmware,
         len(site.points(d.name))] for d in site.devices()
    ])
    md += ["", "## Acceptance tests", ""]
    md += _table(["Test", "Status", "Duration (ms)", "Failed step", "Detail"],
                 [[t["name"], t["status"], t["duration_ms"], t["failed_step"], t["detail"]] for t in tests])
    md += ["", "## Gateway bridges", ""]
    md += _table(["From", "To", "Max age (s)", "State"],
                 [[b["from"], b["to"], b["max_age_s"], b["state"]] for b in services.bridges.status()])
    md += ["", "## Agent leases in force", ""]
    md += _table(["Lease", "Kind", "Target", "Priority", "Expires"],
                 [[lease.id, lease.kind, " ".join(f"{k}={v}" for k, v in lease.target.items() if k != "restore"),
                   lease.priority, _stamp(lease.expires_at)] for lease in services.leases.active()])
    return md


async def _points(ctx: ToolCallContext) -> list[str]:
    site = ctx.site
    points = sorted(site.points(), key=lambda p: str(p.ref))
    md = [f"# Point list: {_cell(site.name)}", "", f"Generated {_stamp(time.time())}; {len(points)} points.", ""]
    md += _table(["Point", "Name", "Kind", "Type", "Units", "Writable", "Tags", "Safety", "Space", "Value"], [
        [f"{p.ref.device}/{p.ref.obj}", p.name, p.kind.value, p.datatype, p.units, "yes" if p.writable else "no",
         ", ".join(p.tags), p.safety.value, p.space, _value(site.latest(p.ref), p)] for p in points
    ])
    return md


async def _io_checkout(ctx: ToolCallContext) -> list[str]:
    site = ctx.site
    md = [f"# IO checkout sheet: {_cell(site.name)}", "", f"Generated {_stamp(time.time())}. Check every channel "
          "on site (io_force drives outputs and simulates inputs) and fill in the last two columns.", ""]
    nodes = [d for d in site.devices() if d.protocol is ProtocolName.BACNET_UC]
    for record in nodes:
        io = site.device_spec(record.name).spec.get("io") or []
        refs = [PointRef(site.name, record.name, f"{e['type']}:{e['instance']}") for e in io]
        readings = {r.ref: r for r in await site.read(refs)} if refs else {}
        md += [f"## {_cell(record.name)} ({_cell(record.space)})", ""]
        md += _table(["Channel", "Point", "Name", "Units", "Value", "Checked", "Notes"], [
            [e["channel"], ref.obj, e.get("name", ""), e.get("units"),
             _value(readings.get(ref), site.known_point(ref)), "[ ]", ""] for e, ref in zip(io, refs, strict=True)
        ])
        md.append("")
    if not nodes:
        md.append("The site has no BACnet-uc nodes.")
    return md


_KINDS = {
    "commissioning": ("Commissioning report", _commissioning),
    "points": ("Point list", _points),
    "io-checkout": ("IO checkout sheet", _io_checkout),
}


async def report_generate(ctx: ToolCallContext, args: dict[str, Any]) -> ToolResult:
    title, build = _KINDS[args["kind"]]
    markdown = "\n".join(await build(ctx)).rstrip() + "\n"
    return ToolResult(True, f"{title} of {ctx.site.name}: {markdown.count(chr(10))} lines of Markdown",
                      {"kind": args["kind"], "title": title, "device_data": {"markdown": markdown}})


TOOLS = [
    Tool(
        "report_generate",
        "Generate a handover document as Markdown: the commissioning report (devices, tests, bridges, leases), "
        "the point list, or the IO checkout sheet." + DEVICE_DATA_NOTE,
        "R",
        {"type": "object", "additionalProperties": False, "required": ["kind"],
         "properties": {"kind": {"enum": list(_KINDS)}}},
        report_generate,
    ),
]
