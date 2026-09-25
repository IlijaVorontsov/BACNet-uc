"""The JSON endpoints of docs/ai-harness/API.md, their shapes and errors."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from support.site import Hub

from .conftest import run_state, wait_for

POINT_KEYS = {"id", "device", "obj", "name", "kind", "datatype", "units", "writable", "commandable", "tags",
              "safety", "description", "source", "space"}
RUN_KEYS = {"id", "title", "state", "created_at", "updated_at", "created_by", "last_seq", "model"}
APPROVAL_KEYS = {"id", "run_id", "call_id", "tool", "tier", "title", "summary", "diff", "rollback", "plan_id",
                 "state", "scope", "requested_at", "expires_at", "requested_by", "decided_by", "decided_at", "comment"}


def error(response: httpx.Response) -> tuple[int, str]:
    body = response.json()
    assert set(body) == {"error"} and isinstance(body["error"]["message"], str)
    return response.status_code, body["error"]["code"]


async def test_health_me_and_site(api: httpx.AsyncClient) -> None:
    health = (await api.get("/api/health")).json()
    assert health == {"ok": True, "version": "0.1.0", "site": "hq",
                      "llm": {"provider": "scripted", "model": "scripted-demo", "configured": True}, "dev_mode": True}
    assert (await api.get("/api/me")).json() == {"user": "dev", "roles": ["viewer", "operator", "commissioner", "admin"]}
    site = (await api.get("/api/site")).json()
    assert set(site) == {"name", "description", "spaces", "devices", "summary"}
    assert {d["name"] for d in site["devices"]} == {"r204-ctl", "r205-ctl"}
    assert set(site["summary"]) == {"devices", "online", "points", "unassigned", "pending_changes"}


async def test_devices(api: httpx.AsyncClient) -> None:
    described = (await api.get("/api/devices/r204-ctl")).json()
    assert set(described) == {"device", "points", "apps", "extra"}
    assert described["device"]["name"] == "r204-ctl" and described["device"]["points"] == len(described["points"])
    assert all(set(p) == POINT_KEYS for p in described["points"])
    assert error(await api.get("/api/devices/nope")) == (404, "not_found")


async def test_points_search_includes_child_spaces(api: httpx.AsyncClient, hub: Hub) -> None:
    await wait_for(lambda: _reading(api))
    everything = (await api.get("/api/points")).json()
    floor = (await api.get("/api/points", params={"space": "f2"})).json()
    room = (await api.get("/api/points", params={"space": "r204"})).json()
    assert floor["total"] == everything["total"] > room["total"] > 0
    assert {p["device"] for p in floor["points"]} == {"r204-ctl", "r205-ctl"}
    assert {p["device"] for p in room["points"]} == {"r204-ctl"}
    assert (await api.get("/api/points", params={"space": "plant"})).json() == {"total": 0, "points": []}
    tagged = (await api.get("/api/points", params={"tag": "Zone_Air_Temperature_Sensor"})).json()
    assert {p["id"] for p in tagged["points"]} == {"hq/r204-ctl/analog-input:1", "hq/r205-ctl/analog-input:1"}
    temp = next(p for p in tagged["points"] if p["device"] == "r204-ctl")
    assert set(temp) == POINT_KEYS | {"reading"} and temp["reading"]["quality"] == "good"
    page = (await api.get("/api/points", params={"limit": 2, "offset": 1})).json()
    assert page["total"] == everything["total"] and len(page["points"]) == 2
    assert page["points"] == everything["points"][1:3]
    searched = (await api.get("/api/points", params={"q": "valve", "device": "r205-ctl"})).json()
    assert [p["id"] for p in searched["points"]] == ["hq/r205-ctl/analog-output:1"]
    assert error(await api.get("/api/points", params={"space": "moon"})) == (400, "invalid")
    assert error(await api.get("/api/points", params={"limit": 0})) == (400, "invalid")


async def _reading(api: httpx.AsyncClient) -> bool:
    points = (await api.get("/api/points", params={"tag": "Zone_Air_Temperature_Sensor"})).json()["points"]
    return bool(points) and all("reading" in p for p in points)


async def test_read_points(api: httpx.AsyncClient) -> None:
    response = await api.post("/api/points/read", json={"ids": ["r204-ctl/analog-input:1", "hq/r205-ctl/nope:1"]})
    first, second = response.json()["readings"]
    assert first["id"] == "hq/r204-ctl/analog-input:1" and first["quality"] == "good"
    assert second["quality"] == "fault" and second["value"] is None
    assert error(await api.post("/api/points/read", json={"ids": ["no slashes"]})) == (400, "invalid")
    assert error(await api.post("/api/points/read", json={"ids": []})) == (400, "invalid")
    assert error(await api.post("/api/points/read", json={"ids": ["a/b"], "extra": 1})) == (400, "invalid")


async def test_discover_and_identify(api: httpx.AsyncClient, hub: Hub) -> None:
    found = (await api.post("/api/discover", json={"protocol": "bacnet-uc", "timeout_s": 0.2})).json()
    assert found["errors"] == {}
    assert {(d["device"], d["known"], d["bacnet_uc"]) for d in found["devices"]} == {
        ("r204-ctl", True, True), ("r205-ctl", True, True)}
    assert error(await api.post("/api/discover", json={"protocol": "zigbee"})) == (400, "invalid")
    assert (await api.post("/api/devices/r204-ctl/identify", json={"seconds": 5})).json() == {}
    assert hub.nodes["r204-ctl"].identifying
    audit = (await api.get("/api/audit")).json()["entries"]
    assert (audit[0]["tool"], audit[0]["user"], audit[0]["outcome"]) == ("device_identify", "dev", "ok")
    assert error(await api.post("/api/devices/nope/identify", json={})) == (404, "not_found")
    assert error(await api.post("/api/devices/r204-ctl/identify", json={"seconds": 0})) == (400, "invalid")
    hub.nodes["r205-ctl"].online = False
    status, code = error(await api.post("/api/devices/r205-ctl/identify", json={}))
    assert status in (502, 504) and code in ("device_error", "timeout")


async def test_manifest_plan_and_tests(api: httpx.AsyncClient, hub: Hub) -> None:
    manifest = (await api.get("/api/manifest")).json()
    assert manifest["live_revision"] == 1 and manifest["draft_revision"] is None
    assert manifest["yaml"].startswith("apiVersion") and manifest["draft_yaml"] is None
    (revision,) = (await api.get("/api/manifest/revisions")).json()["revisions"]
    assert revision["revision"] == 1 and revision["live"] is True
    assert (await api.get("/api/plan")).json() == {"plan": None}
    await hub.services.manifests.edit([{"op": "add", "path": "/placement/r205-ctl", "value": "r204"}], user="dev",
                                      roles={"admin"})
    await hub.services.manifests.plan()
    plan = (await api.get("/api/plan")).json()["plan"]
    assert set(plan) == {"id", "revision", "base_revision", "created_at", "targets", "changes", "warnings", "blocked"}
    assert plan["id"] == "p2" and plan["blocked"] == {}
    assert (await api.get("/api/tests")).json() == {"results": [], "updated_at": None}


async def test_runs(api: httpx.AsyncClient, hub: Hub) -> None:
    created = await api.post("/api/runs", json={"message": "What is the temperature in room 204?"})
    assert created.status_code == 201
    run = created.json()
    assert set(run) == RUN_KEYS and run["state"] == "running" and run["created_by"] == "dev"
    assert run["title"] == "What is the temperature in room 204?" and run["model"] == "scripted-demo"
    done = await run_state(api, run["id"], "idle")
    assert done["last_seq"] > run["last_seq"]
    assert (await api.get("/api/runs")).json()["runs"][0]["id"] == run["id"]
    assert error(await api.get("/api/runs/r_nope")) == (404, "not_found")
    again = await api.post(f"/api/runs/{run['id']}/messages", json={"message": "help"})
    assert again.status_code == 202 and again.json() == {}
    await run_state(api, run["id"], "idle")
    assert (await api.post(f"/api/runs/{run['id']}/cancel")).json() == {}
    assert error(await api.post("/api/runs", json={"message": "hi", "playbook": "golf"})) == (400, "invalid")
    assert error(await api.post("/api/runs", json={"message": ""})) == (400, "invalid")
    assert error(await api.post("/api/runs", json={})) == (400, "invalid")


async def test_a_waiting_run_takes_no_messages_and_answers_only_its_questions(api: httpx.AsyncClient,
                                                                              hub: Hub) -> None:
    checkout = (await api.post("/api/runs", json={"message": "Run IO checkout for r204-ctl.",
                                                  "playbook": "io-checkout"})).json()
    assert checkout["title"] == "IO checkout"
    await run_state(api, checkout["id"], "waiting_approval")
    assert error(await api.post(f"/api/runs/{checkout['id']}/messages", json={"message": "hurry"})) == (
        409, "conflict")
    (approval,) = (await api.get("/api/approvals", params={"state": "pending"})).json()["approvals"]
    assert set(approval) == APPROVAL_KEYS and approval["tool"] == "io_force" and approval["tier"] == "L"
    decided = await api.post(f"/api/approvals/{approval['id']}", json={"decision": "approve", "scope": "run"})
    assert decided.json()["state"] == "approved" and decided.json()["scope"] == "run"
    assert error(await api.post(f"/api/approvals/{approval['id']}", json={"decision": "reject"})) == (409, "conflict")
    await run_state(api, checkout["id"], "waiting_answer")
    question = (await hub.services.store.list_questions(checkout["id"], pending_only=True))[0]

    other = (await api.post("/api/runs", json={"message": "help"})).json()
    await run_state(api, other["id"], "idle")
    assert error(await api.post(f"/api/runs/{other['id']}/answer",
                                json={"question_id": question["id"], "answer": "Yes"})) == (404, "not_found")
    assert error(await api.post(f"/api/runs/{checkout['id']}/answer",
                                json={"question_id": question["id"], "answer": "Perhaps"})) == (400, "invalid")
    assert (await api.post(f"/api/runs/{checkout['id']}/answer",
                           json={"question_id": question["id"], "answer": "Yes"})).json() == {}
    assert error(await api.post(f"/api/runs/{checkout['id']}/answer",
                                json={"question_id": question["id"], "answer": "No"})) == (409, "conflict")
    # The run-wide approval covers the release and the next force: the heater question comes without asking.
    await run_state(api, checkout["id"], "waiting_answer")
    assert (await api.get("/api/approvals", params={"state": "pending"})).json()["approvals"] == []
    assert (await api.post(f"/api/runs/{checkout['id']}/cancel")).json() == {}
    assert (await api.get(f"/api/runs/{checkout['id']}")).json()["state"] == "cancelled"
    assert hub.services.leases.active(checkout["id"]) == []
    assert hub.nodes["r204-ctl"].channels["do0"].forced is None


async def test_approvals_errors(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/approvals")).json() == {"approvals": []}
    assert error(await api.get("/api/approvals", params={"state": "maybe"})) == (400, "invalid")
    assert error(await api.post("/api/approvals/a_nope", json={"decision": "approve"})) == (404, "not_found")
    assert error(await api.post("/api/approvals/a_nope", json={"decision": "yes"})) == (400, "invalid")
    assert error(await api.post("/api/approvals/a_nope", json={"decision": "approve", "scope": "all"})) == (
        400, "invalid")


@pytest.mark.parametrize(("content", "fragment"), [
    (b'{"message": "\\ud800 valve"}', "lone surrogate"),
    (b'{"message": ', "not valid JSON"),
    (b'{"message": NaN}', "not valid JSON"),
    (b'["message"]', "must be a JSON object"),
    (b'{"message": 5}', "message"),
])
async def test_bad_bodies_are_400(api: httpx.AsyncClient, content: bytes, fragment: str) -> None:
    response = await api.post("/api/runs", content=content, headers={"content-type": "application/json"})
    assert error(response) == (400, "invalid") and fragment in response.json()["error"]["message"]


async def test_routes_errors_and_headers(api: httpx.AsyncClient) -> None:
    assert error(await api.get("/api/nope")) == (404, "not_found")
    assert error(await api.delete("/api/runs")) == (405, "invalid")
    response = await api.get("/api/health")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (await api.get("/api/nope")).headers["cache-control"] == "no-store"


async def test_nan_values_are_null(api: httpx.AsyncClient, hub: Hub) -> None:
    from uc_hub.core.types import Reading

    ref = hub.ref("r204-ctl/analog-input:1")
    hub.services.live.publish(Reading(ref, float("nan"), 4102444800.0))
    await wait_for(lambda: _nan(api))


async def _nan(api: httpx.AsyncClient) -> bool:
    points: list[dict[str, Any]] = (await api.get("/api/points", params={"q": "r204-ctl/analog-input:1"})).json()[
        "points"]
    return bool(points) and points[0].get("reading", {}).get("value", 0) is None
