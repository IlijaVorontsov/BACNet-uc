"""What the model sees: the system prompt, the conversation and a short
status of the site.

The status is added as a system message before every user message, so it
is fresh when a turn starts and the conversation before it never changes:
each model call extends the previous one, which is what the provider's
context cache reuses. It is built from the manifest and the hub's own state
(names, counts, revisions), never from text a device reported, and it is
kept to about 2k tokens; everything else the model finds with the tools.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ..core.errors import HubError
from ..policy.policy import ranked_roles

if TYPE_CHECKING:
    from ..runtime.services import Services
    from ..runtime.site import SiteRuntime

#: About 2k tokens.
MAX_STATUS_CHARS = 8000
_MAX_SPACES = 60
_MAX_NAMES = 20


async def site_status(services: Services, user: str, roles: Iterable[str]) -> str:
    """The status message of a new turn, for a message from ``user``."""
    site = services.site
    overview = site.overview()
    summary = overview["summary"]
    stamp = dt.datetime.fromtimestamp(time.time(), dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    protocols = ", ".join(f"{p} {n}" for p, n in overview["protocols"].items()) or "none"
    description = f" ({site.manifest.description})" if site.manifest.description else ""
    out = [
        f"Site status at {stamp} (refreshed for every user message). The next message is from {user} "
        f"(roles: {', '.join(ranked_roles(roles)) or 'none'}); its tier L and C calls wait for an approval by an operator (L) or "
        "a commissioner (C).",
        f"Site {site.name}{description}: {summary['devices']} devices, {summary['online']} online, "
        f"{summary['points']} points; protocols: {protocols}.",
    ]
    out += _spaces(site, overview)
    if overview["unplaced"]:
        out.append("Not placed in a space: " + _names(overview["unplaced"]))
    if overview["offline"]:
        out.append("Offline: " + _names(overview["offline"]))
    if overview["not_described"]:
        out.append("Not described yet (points unknown): " + _names(list(overview["not_described"])))
    out.append(_manifest(services))
    leases = services.leases.active()
    if leases:
        out.append(f"Agent leases in force: {len(leases)} (priority_array and report_generate show them).")
    try:
        pending = await services.store.list_approvals(state="pending")
    except HubError:
        pending = []
    if pending:
        out.append(f"Approvals waiting for a person: {len(pending)}.")
    if summary["unassigned"]:
        out.append(f"The last discovery found {summary['unassigned']} devices that are not in the manifest.")
    text = "\n".join(out)
    return text if len(text) <= MAX_STATUS_CHARS else text[: MAX_STATUS_CHARS - 20] + "\n[status truncated]"


def _spaces(site: SiteRuntime, overview: dict[str, Any]) -> list[str]:
    counts = {s["id"]: s for s in overview["spaces"]}
    lines: list[str] = []

    def walk(nodes: list[dict[str, Any]], depth: int) -> None:
        for node in nodes:
            if len(lines) >= _MAX_SPACES:
                return
            c = counts.get(node["id"], {})
            devices = c.get("devices", 0)
            detail = f": {c.get('online', 0)}/{devices} devices online, {c.get('points', 0)} points" if devices else ""
            lines.append(f"{'  ' * depth}- {node['id']} {node['name']}{detail}")
            walk(node["children"], depth + 1)

    walk(site.manifest.space_tree(), 0)
    if not lines:
        return []
    more = len(overview["spaces"]) - len(lines)
    tail = [f"  (and {more} more spaces; site_tree lists them)"] if more > 0 else []
    return ["Spaces (devices placed directly in each):", *lines, *tail]


def _manifest(services: Services) -> str:
    manifests = services.manifests
    text = f"Manifest: live revision {manifests.live_revision}"
    if manifests.draft is not None:
        sections = ", ".join(manifests.changed_sections()) or "nothing"
        text += f"; draft revision {manifests.draft_revision} changes {sections}"
    plan = manifests.current_plan()
    if plan is not None:
        state = "incomplete, cannot be applied" if plan.blocked else "not applied yet"
        text += f"; plan {plan.id} has {len(plan.changes)} changes on {', '.join(plan.targets) or 'nothing'} ({state})"
    return text + "."


def _names(names: list[str]) -> str:
    shown = names[:_MAX_NAMES]
    more = len(names) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def llm_messages(system: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages of a model call: the system prompt, then the conversation."""
    return [{"role": "system", "content": system}, *history]
