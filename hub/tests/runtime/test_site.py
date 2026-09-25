"""SiteRuntime over simulated nodes: describe with retry, the point model
overlay, search, watches, history, discovery and reload."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from support.site import R204_IO, Hub, eventually, start_hub

from uc_hub.core.errors import InvalidRequest, NotFound, Unsupported
from uc_hub.core.types import PointKind, ProtocolName, Quality, SafetyClass
from uc_hub.manifest import SiteManifest
from uc_hub.sim import SimNode


async def test_points_carry_the_manifest_overlay(hub: Hub) -> None:
    site = hub.site
    temp = await site.point(hub.ref("r204-ctl/analog-input:1"))
    assert (temp.name, temp.kind, temp.units, temp.space, temp.source) == (
        "R204 Temp", PointKind.INPUT, "degrees-celsius", "r204", "io:ai0")
    assert temp.tags == ["Zone_Air_Temperature_Sensor"]
    damper = await site.point(hub.ref("r205-ctl/binary-output:1"))
    assert damper.safety is SafetyClass.LIFE_SAFETY and damper.datatype == "enum"
    record = site.device("r204-ctl")
    assert record.online and record.space == "r204" and record.instance == 2041 and record.managed
    assert site.overview()["not_described"] == {}
    with pytest.raises(NotFound):
        await site.point(hub.ref("r204-ctl/analog-value:99"))
    with pytest.raises(NotFound):
        site.device("nope")
    with pytest.raises(InvalidRequest, match="site 'hq'"):
        site.point_ref("other/r204-ctl/analog-input:1")


async def test_describe_retries_until_the_node_answers(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, offline=["r205-ctl"])
    try:
        site = hub.site
        await eventually(lambda: "r205-ctl" in site.overview()["not_described"], what="a describe failure")
        assert site.description("r205-ctl") is None and site.points("r205-ctl") == []
        hub.nodes["r205-ctl"].online = True
        await hub.described("r205-ctl")
        assert site.overview()["not_described"] == {}
        assert len(site.points("r205-ctl")) == 3
    finally:
        await hub.close()


async def test_search_text_and_filters(hub: Hub) -> None:
    site = hub.site
    total, points = site.search("room 204")
    assert total == 4 and {p.ref.device for p in points} == {"r204-ctl"}
    total, points = site.search("temp")
    assert [p.ref.device for p in points] == ["r204-ctl", "r205-ctl"]
    # A space includes its children.
    assert site.search(space="f2")[0] == 7
    assert site.search(space="r205")[0] == 3
    assert site.search(space="plant")[0] == 0
    total, points = site.search(tag="zone_air_temperature_sensor")
    assert total == 2 and all(p.ref.obj == "analog-input:1" for p in points)
    assert site.search(kind="output", space="r204")[0] == 2
    assert site.search(writable=False)[0] == 3
    assert site.search(protocol="bacnet-ip")[0] == 0
    assert site.search(device="r205-ctl", query="valve")[0] == 1
    total, page = site.search(limit=2, offset=6)
    assert total == 7 and len(page) == 1
    # The exact id ranks first.
    assert site.search("r205-ctl/analog-input:1")[1][0].ref == hub.ref("r205-ctl/analog-input:1")
    assert [d.name for d in site.search_devices("room", space="r205")] == ["r205-ctl"]
    with pytest.raises(InvalidRequest, match="unknown space"):
        site.search(space="mars")


async def test_watches_are_reference_counted(hub: Hub) -> None:
    site = hub.site
    ref = hub.ref("r205-ctl/analog-output:1")
    tagged = hub.ref("r204-ctl/analog-input:1")
    assert site.watch_count(tagged) == 1  # tagged points are trended
    assert site.watch_count(ref) == 0
    await site.ensure_watched([ref])
    await site.ensure_watched([ref])
    assert site.watch_count(ref) == 2
    node = hub.nodes["r205-ctl"]
    node.local_write(1, 1, 85, 40.0, 16)
    await eventually(lambda: (r := site.latest(ref)) is not None and r.value == 40.0, what="valve at 40")
    await site.release_watched([ref])
    node.local_write(1, 1, 85, 41.0, 16)
    await eventually(lambda: (r := site.latest(ref)) is not None and r.value == 41.0, what="still watched")
    await site.release_watched([ref])
    await site.release_watched([ref])
    assert site.watch_count(ref) == 0
    node.local_write(1, 1, 85, 42.0, 16)
    samples = len(site.history(ref, 0))
    await eventually(lambda: site.latest(tagged) is not None and len(site.history(tagged, 0)) > 2)
    assert site.latest(ref).value == 41.0 and len(site.history(ref, 0)) == samples
    with pytest.raises(NotFound):
        await site.ensure_watched([hub.ref("nope/analog-input:1")])


async def test_history_records_published_readings(hub: Hub) -> None:
    site = hub.site
    ref = hub.ref("r204-ctl/analog-input:1")
    hub.nodes["r204-ctl"].force("ai0", 650)
    await eventually(lambda: any(v == pytest.approx(15.0) for _, v, _ in site.history(ref, 0)), what="15 degC")
    hub.nodes["r204-ctl"].online = False
    await eventually(lambda: site.history(ref, 0)[-1][2] is Quality.OFFLINE, timeout_s=8, what="offline sample")
    samples = site.history(ref, 0)
    assert [ts for ts, _, _ in samples] == sorted(ts for ts, _, _ in samples)
    assert site.history(ref, samples[-1][0] + 1) == []


async def test_summary_tree_site_and_discovery(tmp_path: Path) -> None:
    stranger = SimNode(name="r206-ctl", instance=2061)
    await stranger.start()
    hub = await start_hub(tmp_path, hub={"drivers": {"bacnet_uc": {
        "discover_broadcast": f"127.0.0.1:{stranger.port}"}}})
    try:
        site = hub.site
        assert site.summary() == {"devices": 2, "online": 2, "points": 7, "unassigned": 0, "pending_changes": 0}
        found = await site.discover(ProtocolName.BACNET_UC, 0.5)
        by_name = {d["device"] or d["name"]: d for d in found["devices"]}
        assert by_name["r204-ctl"]["known"] and by_name["r205-ctl"]["known"]
        assert not by_name["r206-ctl"]["known"] and by_name["r206-ctl"]["instance"] == 2061
        assert site.summary()["unassigned"] == 1
        tree = site.tree()
        (root,) = tree["spaces"]
        assert (root["id"], root["device_count"], root["online"], root["points"]) == ("hq", 2, 2, 7)
        floor = root["children"][0]
        assert [c["id"] for c in floor["children"]] == ["r204", "r205"]
        assert floor["children"][0]["devices"][0]["name"] == "r204-ctl"
        assert tree["unplaced"] == []
        assert site.tree("r205")["spaces"][0]["points"] == 3
        with pytest.raises(NotFound):
            site.tree("mars")
        doc = site.site_json()
        assert doc["name"] == "hq" and doc["summary"]["unassigned"] == 1
        assert {"id": "r204", "name": "Room 204", "parent": "f2"} in doc["spaces"]
        assert {d["name"]: d["points"] for d in doc["devices"]} == {"r204-ctl": 4, "r205-ctl": 3}
        overview = site.overview()
        assert overview["protocols"] == {"bacnet-uc": 2} and overview["offline"] == []
        assert {s["id"]: s["devices"] for s in overview["spaces"]}["r204"] == 1
        with pytest.raises(Unsupported, match="no bacnet-ip driver"):
            await site.discover(ProtocolName.BACNET_IP)
    finally:
        await hub.close()
        await stranger.stop()


async def test_reload_follows_a_new_revision(hub: Hub) -> None:
    site = hub.site
    ref = hub.ref("r205-ctl/analog-input:1")
    await eventually(lambda: site.latest(ref) is not None, what="a trended reading of r205")
    doc = site.manifest.to_dict()
    doc["system"]["nodes"] = [n for n in doc["system"]["nodes"] if n["name"] != "r205-ctl"]
    doc["placement"] = {"r204-ctl": "plant"}
    doc["tags"] = {"r204-ctl/analog-output:1": ["Reheat_Valve_Command"]}
    doc["safety"] = {"r204-ctl/analog-output:1": "critical"}
    doc["system"]["tests"] = []
    doc["external_devices"] = [{"name": "r204-co2", "protocol": "mqtt", "profile": "generic-json",
                                "topic": "sensors/r204/co2", "points": [{"id": "co2", "path": "$.ppm"}]}]
    await site.reload(SiteManifest.from_dict(doc))
    assert [d.name for d in site.devices()] == ["r204-ctl", "r204-co2"]
    assert site.points("r204-ctl")[0].space == "plant"
    valve = await site.point(hub.ref("r204-ctl/analog-output:1"))
    assert valve.safety is SafetyClass.CRITICAL and valve.tags == ["Reheat_Valve_Command"]
    temp = await site.point(hub.ref("r204-ctl/analog-input:1"))
    assert temp.tags == [] and site.watch_count(temp.ref) == 0
    assert site.watch_count(valve.ref) == 1
    assert site.latest(ref) is None and site.history(ref, 0) == []
    assert ProtocolName.MQTT in site.drivers
    co2 = await site.point(hub.ref("r204-co2/co2"))
    assert co2.kind is PointKind.INPUT
    with pytest.raises(NotFound):
        site.device("r205-ctl")
    # A changed node spec is registered again and described again.
    doc["system"]["nodes"][0]["io"] = copy.deepcopy(R204_IO[:2])
    await site.reload(SiteManifest.from_dict(doc))
    await hub.described("r204-ctl")
    assert site.device("r204-ctl").space == "plant"
    renamed = SiteManifest.from_dict({**doc, "metadata": {"name": "other"}})
    with pytest.raises(InvalidRequest):
        await site.reload(renamed)
