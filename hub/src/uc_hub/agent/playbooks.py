"""Playbooks: task-specific instructions for a run (DESIGN.md section 8).

``POST /api/runs {"playbook": id}`` picks one; its instructions become part
of the run's system prompt, which stays the same for the whole run so the
provider can cache it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.errors import InvalidRequest


@dataclass(frozen=True, slots=True)
class Playbook:
    id: str
    title: str
    instructions: str


PLAYBOOKS: dict[str, Playbook] = {p.id: p for p in (
    Playbook(
        "commission",
        "Commission",
        "Commission the room or device the user names. Find its devices (site_search with the space filter, "
        "device_describe) and check that inputs read plausible values (point_read). Complete the draft with "
        "manifest_edit: placement, tags, IO, apps and links. Compute the plan, explain the changes and apply "
        "it (the user approves the diff). Then run the acceptance tests with test_run and report which passed. "
        "When a test fails, find out why with IO forces and questions to the technician before changing logic.",
    ),
    Playbook(
        "onboard",
        "Onboard devices",
        "Onboard the devices that discovery finds but the manifest does not know. Run discover, then for each "
        "new device find out where it is: device_identify makes it blink and ask_user lets the technician say "
        "which room it is in. Add it to the draft with manifest_edit (name, instance, placement), propose tags "
        "from its point names and units, plan and apply after approval.",
    ),
    Playbook(
        "io-checkout",
        "IO checkout",
        "Check the inputs and outputs of the named board with the technician on site, one channel at a time. "
        "Force an output (or simulate an input) with io_force and a short lease, ask the technician with "
        "ask_user whether the equipment reacts (offer Yes and No), then release it with io_release before the "
        "next channel. Never force a channel whose point is life-safety. Finish with a summary of every "
        "channel and its answer; report_generate kind io-checkout gives the sheet to fill in.",
    ),
    Playbook(
        "troubleshoot",
        "Troubleshoot",
        "Find out why the named room or device misbehaves. Look at trends (point_history), who commands the "
        "outputs (priority_array), app states and errors (device_describe) and the live values. State a "
        "hypothesis and check it. Propose a fix as a draft change and a plan; never fix by silent writes. "
        "Temporary writes for a test need a lease and the user's approval.",
    ),
    Playbook(
        "handover",
        "Handover",
        "Prepare the handover of the site. Run every acceptance test with test_run, then generate the "
        "commissioning report and the point list with report_generate. Summarize what passed, what failed and "
        "what is still open (a draft not applied, offline devices, agent leases in force).",
    ),
)}


def get_playbook(playbook_id: str | None) -> Playbook | None:
    """The playbook with this id (None for None); ``InvalidRequest`` for an unknown id."""
    if playbook_id is None:
        return None
    found = PLAYBOOKS.get(playbook_id)
    if found is None:
        raise InvalidRequest(f"unknown playbook {playbook_id!r} (known: {', '.join(PLAYBOOKS)})")
    return found
