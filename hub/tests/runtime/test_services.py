"""Services: first start imports site.yaml, later starts use the store; start
and stop are ordered and idempotent."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from support.site import SITE_OPTIONS, Hub, start_hub

from uc_hub.core.errors import ValidationFailed
from uc_hub.runtime import Services, load_config


async def test_first_start_imports_site_yaml_as_revision_1(hub: Hub) -> None:
    services = hub.services
    assert services.running
    state = await services.store.manifest_state()
    assert (state["live_revision"], state["draft_revision"]) == (1, None)
    assert state["yaml"] == (hub.path / "site.yaml").read_text()
    (revision,) = await services.store.list_revisions()
    assert (revision["author"], revision["message"], revision["live"]) == ("hub", "imported from site.yaml", True)
    assert services.manifests.live_revision == 1
    assert services.live.site == "hq" and services.policy.site == "hq"
    assert services.site.summary()["devices"] == 2


async def test_later_starts_use_the_store_and_warn_about_site_yaml(hub: Hub, caplog: pytest.LogCaptureFixture) -> None:
    await hub.call("manifest_edit", {"json_patch": [{"op": "replace", "path": "/metadata/description",
                                                     "value": "edited"}]})
    edited = yaml.safe_load((hub.path / "site.yaml").read_text())
    edited["metadata"]["description"] = "changed on disk"
    (hub.path / "site.yaml").write_text(yaml.safe_dump(edited))
    with caplog.at_level(logging.WARNING, logger="uc_hub.runtime.manifest"):
        services = await hub.restart()
    assert "is not the live manifest (it differs)" in caplog.text
    assert services.manifests.live.description == "Test building"
    assert services.manifests.draft_revision == 2 and services.manifests.current.description == "edited"
    assert [r["revision"] for r in await services.store.list_revisions()] == [2, 1]


async def test_start_and_stop_are_idempotent(hub: Hub) -> None:
    services = hub.services
    await services.start()
    assert services.running
    await services.stop()
    await services.stop()
    assert not services.running and not services.store.is_open
    with pytest.raises(RuntimeError, match="stopped"):
        await services.start()


async def test_stop_undoes_the_agents_live_writes(tmp_path: Path) -> None:
    """A stopped hub cannot end a lease, so it undoes the agent's writes and
    forces before it goes (a BACnet/IP device would keep them)."""
    hub = await start_hub(tmp_path)
    try:
        assert (await hub.call("point_write", {"point": "r204-ctl/analog-output:1", "value": 55, "lease_s": 600})).ok
        assert (await hub.call("io_force", {"node": "r204-ctl", "channel": "ai0", "value": 650})).ok
        node = hub.nodes["r204-ctl"]
        assert node.objects[(1, 1)].priority[11] == 55  # type: ignore[index]
        store = hub.services.store
        await hub.services.stop()
        assert node.objects[(1, 1)].priority[11] is None  # type: ignore[index]
        assert node.channels["ai0"].forced is None
        await store.open()
        try:
            assert await store.list_leases(state="active") == []
        finally:
            await store.close()
    finally:
        await hub.close()


async def test_parts_need_a_start_and_a_never_started_hub_stops(tmp_path: Path) -> None:
    (tmp_path / "hub.yaml").write_text("{}\n")
    services = Services(load_config(tmp_path / "hub.yaml"))
    for part in ("policy", "site", "leases", "bridges", "manifests", "control"):
        with pytest.raises(RuntimeError, match="not started"):
            getattr(services, part)
    await services.stop()
    assert not services.running


async def test_a_failed_start_cleans_up(tmp_path: Path) -> None:
    (tmp_path / "hub.yaml").write_text("{}\n")
    (tmp_path / "site.yaml").write_text("apiVersion: bacnet-uc/v1\nkind: Site\nmetadata: {name: Bad Name}\n")
    services = Services(load_config(tmp_path / "hub.yaml"), site_options=SITE_OPTIONS)
    with pytest.raises(ValidationFailed):
        await services.start()
    assert not services.running and not services.store.is_open
    with pytest.raises(RuntimeError):
        await services.start()


async def test_an_offline_node_does_not_hold_up_the_start(tmp_path: Path) -> None:
    hub = await start_hub(tmp_path, offline=["r205-ctl"])
    try:
        site = hub.site
        assert site.description("r205-ctl") is None
        assert site.overview()["offline"] == ["r205-ctl"]
        await hub.described("r204-ctl")
    finally:
        await hub.close()
