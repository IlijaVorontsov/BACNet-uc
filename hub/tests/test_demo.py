"""The demo (``uc-hub demo``) in process: the simulated site with its hub,
driven through the HTTP API like the web app does, through the scripted
"Commission room 204" and "IO checkout" scenarios."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from uc_hub.api import create_app
from uc_hub.demo import Demo, Mosquitto, start_demo

FAST = {
    "llm": {"delay_s": 0},
    "drivers": {"bacnet_uc": {"poll_interval_s": 0.2, "heartbeat_s": 0.5, "refresh_s": 1}},
}


@pytest.fixture
async def demo(tmp_path: Path) -> AsyncIterator[Demo]:
    running = await start_demo(tmp_path, ephemeral_ports=True, overrides=FAST)
    try:
        yield running
    finally:
        await running.stop()


@pytest.fixture
async def api(demo: Demo) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(demo.services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo", timeout=30) as client:
        yield client


async def drive(api: httpx.AsyncClient, demo: Demo, run_id: str, *, scope: str = "call",
                on_question: Callable[[dict[str, Any]], None] | None = None,
                timeout_s: float = 45.0) -> dict[str, Any]:
    """Approve and answer ("Yes") like a person in the web app until the run's turn ends."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = (await api.get(f"/api/runs/{run_id}")).json()
        if run["state"] == "waiting_approval":
            for approval in (await api.get("/api/approvals", params={"state": "pending"})).json()["approvals"]:
                decided = await api.post(f"/api/approvals/{approval['id']}", json={"decision": "approve",
                                                                                   "scope": scope})
                assert decided.status_code == 200, decided.text
        elif run["state"] == "waiting_answer":
            for question in await demo.services.store.list_questions(run_id, pending_only=True):
                if on_question is not None:
                    on_question(question)
                answered = await api.post(f"/api/runs/{run_id}/answer",
                                          json={"question_id": question["id"], "answer": "Yes"})
                assert answered.status_code == 200, answered.text
        elif run["state"] in ("idle", "failed", "cancelled"):
            return run  # type: ignore[no-any-return]
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} did not finish its turn")


async def test_commission_room_204(api: httpx.AsyncClient, demo: Demo) -> None:
    site = (await api.get("/api/site")).json()
    devices = {d["name"]: d for d in site["devices"]}
    assert devices["r204-ctl"]["space"] is None
    assert (await api.get("/api/points", params={"device": "r204-ctl"})).json()["total"] == 0

    run = (await api.post("/api/runs", json={"message": "Commission room 204"})).json()
    done = await drive(api, demo, run["id"])
    assert done["state"] == "idle"
    events = await demo.services.store.list_events(run["id"], limit=10_000)
    tools = {e["call_id"]: e["tool"] for e in events if e["type"] == "tool.call"}
    results = [e for e in events if e["type"] == "tool.result"]
    assert [tools[r["call_id"]] for r in results] == ["site_search", "device_describe", "point_read", "manifest_get",
                                                      "manifest_edit", "plan", "apply", "test_run"]
    assert all(r["ok"] for r in results), [r["summary"] for r in results]
    messages = [e["text"] for e in events if e["type"] == "message.done"]
    assert messages[-1].startswith("Room 204 is commissioned: 4 of 4 tests passed")
    assert "not configured" in messages[1]

    tests = (await api.get("/api/tests")).json()["results"]
    assert {t["name"]: t["status"] for t in tests} == {
        "valve opens when cold": "pass", "valve closes when warm": "pass", "setpoint follows the operator": "pass",
        "heater relay checkout": "pass"}
    manifest = (await api.get("/api/manifest")).json()
    assert manifest["live_revision"] == 2 and manifest["draft_revision"] is None
    assert (await api.get("/api/plan")).json() == {"plan": None}
    site = (await api.get("/api/site")).json()
    assert {d["name"]: d["space"] for d in site["devices"]}["r204-ctl"] == "r204"
    node = demo.nodes["r204-ctl"]
    assert {app.name: app.state for app in node.apps.values()} == {"thermostat": "running", "link": "running"}
    assert (2, 20) in node.objects, "the CO2 bridge's destination"


async def test_io_checkout_with_a_run_wide_approval(api: httpx.AsyncClient, demo: Demo) -> None:
    run = (await api.post("/api/runs", json={"message": "Run IO checkout for r201-ctl.",
                                             "playbook": "io-checkout"})).json()
    run_id = run["id"]
    node = demo.nodes["r201-ctl"]

    def forced(question: dict[str, Any]) -> None:
        channel = question["text"].split()[3]
        assert node.channels[channel].forced is not None, f"{channel} is forced while the technician looks"

    assert (await drive(api, demo, run_id, scope="run", on_question=forced))["state"] == "idle"
    store = demo.services.store
    events = await store.list_events(run_id, limit=10_000)
    calls = [(e["tool"], e["args"].get("channel")) for e in events if e["type"] == "tool.call" and e["args"]]
    assert calls == [("device_describe", None), ("io_force", "ao0"), ("ask_user", None), ("io_release", "ao0"),
                     ("io_force", "do0"), ("ask_user", None), ("io_release", "do0")]
    assert all(e["ok"] for e in events if e["type"] == "tool.result")
    assert len([e for e in events if e["type"] == "approval.request"]) == 1
    assert [e["answer"] for e in events if e["type"] == "question.answered"] == ["Yes", "Yes"]
    assert events[-2]["type"] == "message.done" and events[-2]["text"].startswith("IO checkout of r201-ctl done")
    assert demo.services.leases.active(run_id) == []
    assert all(ch.forced is None for ch in demo.nodes["r201-ctl"].channels.values())


async def test_the_demo_runs_without_mqtt(tmp_path: Path) -> None:
    demo = await start_demo(tmp_path, ephemeral_ports=True, overrides=FAST, mqtt=False)
    try:
        names = {d.name for d in demo.services.site.devices()}
        assert "r204-co2" not in names and "f2-mqtt" not in names and demo.broker is None
        assert demo.services.manifests.live.bridges == []
    finally:
        await demo.stop()


@pytest.mark.skipif(Mosquitto.find() is None, reason="needs the mosquitto broker binary")
async def test_the_mqtt_node_reports_like_firmware_0_3_0(demo: Demo) -> None:
    site = demo.services.site
    deadline = time.monotonic() + 10
    while not site.device("f2-mqtt").online:
        assert time.monotonic() < deadline, "the mqtt_tls node did not come online"
        await asyncio.sleep(0.05)
    assert demo.broker is not None and all(device.connected for device in demo.mqtt)
    assert site.device("f2-mqtt").firmware == "0.3.0"
