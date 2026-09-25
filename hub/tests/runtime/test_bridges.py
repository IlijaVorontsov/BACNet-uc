"""Gateway bridges: copy, relinquish when the source is stale or offline,
retained MQTT values, and the gateway side of a plan."""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import aiomqtt
import pytest
from support.mosquitto import Broker, need_mosquitto
from support.site import Hub, eventually, start_hub

from uc_hub.core.errors import PolicyDenied
from uc_hub.core.types import SafetyClass
from uc_hub.manifest.load import Bridge
from uc_hub.policy import PolicySettings
from uc_hub.runtime.bridges import Bridges, Gateway

AO, AI = 1, 0
BRIDGE = {"from": "r205-ctl/analog-input:1", "to": "r204-ctl/analog-output:1", "max_age_s": 2, "scale": 2.0,
          "offset": 1.0}


def slot(hub: Hub, node: str, priority: int, obj: tuple[int, int] = (AO, 1)) -> Any:
    sim_obj = hub.nodes[node].objects[obj]
    assert sim_obj.priority is not None
    return sim_obj.priority[priority - 1]


def with_bridge(doc: dict[str, Any]) -> None:
    doc["bridges"] = [dict(BRIDGE)]


async def test_bridge_copies_and_relinquishes_when_the_source_goes_offline(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, edit=with_bridge)
    try:
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(41.0), what="20 degC * 2 + 1")
        # BACnet-uc nodes get the bridge's max age as a lease of their own.
        assert (AO, 1, 12) in hub.nodes["r204-ctl"]._leases
        hub.nodes["r205-ctl"].force("ai0", 650)
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(31.0), what="15 degC * 2 + 1")
        (status,) = hub.services.bridges.status()
        assert (status["state"], status["holding"], status["priority"]) == ("active", 12, 12)
        hub.nodes["r205-ctl"].online = False
        await eventually(lambda: slot(hub, "r204-ctl", 12) is None, timeout_s=8, what="relinquished")
        (status,) = hub.services.bridges.status()
        assert status["state"] == "stale" and status["holding"] is None
        assert "offline" in status["detail"]
        hub.nodes["r205-ctl"].online = True
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(31.0), timeout_s=8, what="copied again")
        await hub.services.bridges.stop()
        assert slot(hub, "r204-ctl", 12) is None
    finally:
        await hub.close()


async def test_a_source_older_than_max_age_is_relinquished(hub: Hub) -> None:
    now = [time.time()]
    bridges = Bridges(hub.site, hub.services.policy, clock=lambda: now[0])
    bridge = Bridge(hub.ref(BRIDGE["from"]), hub.ref(BRIDGE["to"]), 2, 1.0, 0.0, 13)
    await bridges.start([bridge])
    try:
        await eventually(lambda: slot(hub, "r204-ctl", 13) == pytest.approx(20.0), what="copied")
        now[0] += 10
        await eventually(lambda: slot(hub, "r204-ctl", 13) is None, what="relinquished when stale")
        assert "s old" in bridges.status()[0]["detail"]
    finally:
        await bridges.stop()


async def test_bridges_follow_the_write_rules(hub: Hub) -> None:
    """A bridge writes like the agent: never a life-safety point, never above
    the agent priority, and without using the agent's write budget."""
    bridges = Bridges(hub.site, hub.services.policy)
    life = Bridge(hub.ref("r204-ctl/analog-input:1"), hub.ref("r205-ctl/binary-output:1"), 5, 0.0, 1.0, 12)
    await bridges.start([life])
    try:
        await eventually(lambda: bridges.status()[0]["state"] == "error", what="refused")
        assert "life-safety" in bridges.status()[0]["detail"]
        assert slot(hub, "r205-ctl", 12, (4, 1)) is None
        high = Bridge(hub.ref("r204-ctl/analog-input:1"), hub.ref("r204-ctl/analog-output:1"), 5, 1.0, 0.0, 10)
        await bridges.replace([high])
        await eventually(lambda: "outrank" in bridges.status()[0]["detail"], what="priority refused")
        for _ in range(40):  # far beyond max_writes_per_minute: bridges are not rate limited
            hub.services.policy.check_point_write(await hub.site.point(high.dest), 12, 1.0, admit=False)
    finally:
        await bridges.stop()


async def test_a_bridge_the_policy_now_refuses_lets_go_of_its_value(tmp_path: Path) -> None:
    """A revision that adds a deny pattern over a running bridge's
    destination: the bridge must not keep its last value in the priority
    array (a BACnet/IP destination has no lease of its own)."""
    def edit(doc: dict[str, Any]) -> None:
        doc["bridges"] = [{**BRIDGE, "max_age_s": 600}]

    hub = await start_hub(tmp_path, edit=edit)
    try:
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(41.0), what="copied")
        policy = hub.services.policy
        policy.configure(PolicySettings(deny=("r204-ctl/analog-output:*",)), hub.site.manifest.safety)
        hub.nodes["r205-ctl"].force("ai0", 650)
        await eventually(lambda: slot(hub, "r204-ctl", 12) is None, what="relinquished once refused")
        (status,) = hub.services.bridges.status()
        assert (status["state"], status["holding"]) == ("error", None) and "deny pattern" in status["detail"]
    finally:
        await hub.close()


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    need_mosquitto()
    b = Broker(tmp_path / "broker")
    b.workdir.mkdir()
    b.start()
    yield b
    b.stop()


def with_co2(trust_retained: bool) -> Any:
    def edit(doc: dict[str, Any]) -> None:
        point: dict[str, Any] = {"id": "co2", "path": "$.ppm", "units": "parts-per-million"}
        if trust_retained:
            point["trust_retained"] = True
        doc["external_devices"] = [{"name": "r204-co2", "protocol": "mqtt", "profile": "generic-json",
                                    "topic": "sensors/r204/co2", "points": [point]}]
        doc["bridges"] = [{"from": "r204-co2/co2", "to": "r204-ctl/analog-output:1", "max_age_s": 60, "scale": 0.1}]
    return edit


async def publish(broker: Broker, payload: bytes, *, retain: bool) -> None:
    async with aiomqtt.Client(broker.host, broker.port, identifier=f"pub-{time.monotonic_ns()}") as client:
        await client.publish("sensors/r204/co2", payload, qos=1, retain=retain)


@pytest.mark.mosquitto
async def test_retained_mqtt_values_wait_for_a_live_message(tmp_path: Path, broker: Broker) -> None:
    await publish(broker, b'{"ppm": 612}', retain=True)
    hub = await start_hub(tmp_path / "hub", edit=with_co2(False),
                          hub={"drivers": {"mqtt": broker.settings()}})
    try:
        co2 = hub.ref("r204-co2/co2")
        await eventually(lambda: (r := hub.site.latest(co2)) is not None and r.value == 612.0, what="retained value")
        assert hub.site.retained_only(co2)
        await eventually(lambda: "retained" in hub.services.bridges.status()[0]["detail"], what="stale: retained")
        assert slot(hub, "r204-ctl", 12) is None
        await publish(broker, b'{"ppm": 700}', retain=False)
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(70.0), what="live value copied")
        assert not hub.site.retained_only(co2)
    finally:
        await hub.close()


@pytest.mark.mosquitto
async def test_points_can_declare_retained_values_current(tmp_path: Path, broker: Broker) -> None:
    await publish(broker, b'{"ppm": 612}', retain=True)
    hub = await start_hub(tmp_path / "hub", edit=with_co2(True), hub={"drivers": {"mqtt": broker.settings()}})
    try:
        await eventually(lambda: slot(hub, "r204-ctl", 12) == pytest.approx(61.2), what="retained value copied")
        assert not hub.site.retained_only(hub.ref("r204-co2/co2"))
    finally:
        await hub.close()


async def test_gateway_replaces_bridges_tags_and_safety(hub: Hub) -> None:
    services = hub.services
    gateway = Gateway(hub.site, services.policy, services.bridges)
    entry = {**BRIDGE, "from": "hq/r205-ctl/analog-input:1", "to": "hq/r204-ctl/analog-output:1", "priority": 14}
    await gateway.apply_bridges([entry])
    await eventually(lambda: slot(hub, "r204-ctl", 14) == pytest.approx(41.0), what="bridge at 14")
    # The same destination and priority keeps its value while the bridge changes.
    await gateway.apply_bridges([{**entry, "offset": 3.0}])
    assert slot(hub, "r204-ctl", 14) == pytest.approx(41.0)
    await eventually(lambda: slot(hub, "r204-ctl", 14) == pytest.approx(43.0), what="updated bridge")
    await gateway.apply_bridges([])
    assert slot(hub, "r204-ctl", 14) is None and services.bridges.status() == []

    valve = hub.ref("r204-ctl/analog-output:1")
    await gateway.apply_tags({str(valve): ["Valve_Command"]}, {str(valve): "life-safety"})
    point = await hub.site.point(valve)
    assert point.tags == ["Valve_Command"] and point.safety is SafetyClass.LIFE_SAFETY
    assert (await hub.site.point(hub.ref("r204-ctl/analog-input:1"))).tags == []
    with pytest.raises(PolicyDenied, match="life-safety"):
        services.policy.check_point_write(point, None, 1.0, admit=False)
    # The damper is no longer life-safety: the safety map was replaced, not merged.
    damper = await hub.site.point(hub.ref("r205-ctl/binary-output:1"))
    assert damper.safety is SafetyClass.NORMAL
    services.policy.check_point_write(damper, None, 1, admit=False)
