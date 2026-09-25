"""Manifest acceptance tests on the live site: every step goes through the
policy and holds a lease, and nothing is left forced or written afterwards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from support.site import Hub, start_hub

EXTRA_TESTS: list[dict[str, Any]] = [
    {"name": "denied valve", "steps": [
        {"write": {"point": "r205-ctl/analog-output:1", "value": 50, "priority": 12}},
    ]},
    {"name": "relinquish default", "steps": [
        {"force": {"node": "r204-ctl", "channel": "ai0", "value": 600}},
        {"write": {"point": "r204-ctl/analog-output:1", "property": "relinquish-default", "value": 0}},
    ]},
    {"name": "valve never opens", "steps": [
        {"force": {"node": "r204-ctl", "channel": "ao0", "value": 10}},
        {"write": {"point": "r204-ctl/binary-output:1", "value": 1, "priority": 14}},
        {"expect": {"point": "r204-ctl/analog-output:1", "op": "gt", "value": 50, "within_ms": 200}},
    ]},
]


def nothing_left(hub: Hub) -> None:
    for sim in hub.nodes.values():
        assert not any(ch.forced is not None for ch in sim.channels.values()), sim.name
        for obj in sim.objects.values():
            if obj.priority is not None:
                assert obj.priority[11:] == [None] * 5, (sim.name, obj.key, obj.priority)
    assert hub.services.leases.active() == []


async def test_live_tests_pass_and_are_stored(hub: Hub) -> None:
    services = hub.services
    run = await services.store.create_run(title="checkout", created_by="tech")
    results = await services.run_live_tests(None, user="tech", run_id=run["id"])
    assert [(r.name, r.status, r.target) for r in results] == [
        ("heater relay checkout", "pass", "live"), ("cold room input", "pass", "live")]
    nothing_left(hub)
    stored = await services.store.test_results("live")
    assert [r["status"] for r in stored["results"]] == ["pass", "pass"]
    (event,) = [e for e in await services.store.list_events(run["id"]) if e["type"] == "tests.updated"]
    assert [r["name"] for r in event["results"]] == ["heater relay checkout", "cold room input"]
    audit = [e["action"] for e in await services.store.list_audit()]
    assert audit.count("lease.released") == 2  # the relay write and the input force


async def test_live_tests_follow_the_policy_and_clean_up(tmp_path: Path) -> None:
    def edit(doc: dict[str, Any]) -> None:
        doc["system"]["tests"].extend(EXTRA_TESTS)

    hub = await start_hub(tmp_path, edit=edit)
    try:
        await hub.services.run_live_tests(["heater relay checkout"], user="tech")
        results = {r.name: r for r in await hub.services.run_live_tests(
            [t["name"] for t in EXTRA_TESTS], user="tech")}
        denied = results["denied valve"]
        assert (denied.status, denied.failed_step) == ("error", 0)
        assert "deny pattern" in denied.detail
        permanent = results["relinquish default"]
        assert permanent.status == "error" and "present values only" in permanent.detail
        failing = results["valve never opens"]
        assert (failing.status, failing.failed_step) == ("fail", 2)
        nothing_left(hub)
        # Runs of some tests merge into the stored results.
        stored = [(r["name"], r["status"]) for r in (await hub.services.store.test_results("live"))["results"]]
        assert stored == [("heater relay checkout", "pass"), ("denied valve", "error"),
                          ("relinquish default", "error"), ("valve never opens", "fail")]
    finally:
        await hub.close()
