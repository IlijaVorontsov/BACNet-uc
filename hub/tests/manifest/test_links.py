"""uc-link parameters generated from ``system.links``."""

from __future__ import annotations

from typing import Any

import pytest

from uc_hub.core.errors import ValidationFailed
from uc_hub.manifest import SiteManifest
from uc_hub.manifest.nodedocs import UC_LINK_FILE, apps_entry, number_text, param_text


@pytest.fixture
def link_spec() -> Any:
    """The simulator's uc-link emulation parses the parameters like the stock app."""
    return pytest.importorskip("uc_hub.sim").LinkSpec


def link_apps(site: SiteManifest, node: str = "r204-ctl") -> list[Any]:
    return [a for a in site.apps(node) if a.link]


def test_link_from_bacnet_ip_device(doc: dict[str, Any], link_spec: Any) -> None:
    (app,) = link_apps(SiteManifest.from_dict(doc))
    assert app.name == "link"
    assert app.link_indexes == [0]
    assert app.manifest == {
        "name": "link", "file": UC_LINK_FILE, "autostart": True, "period_ms": 1000, "heap_kb": 8,
        "stack_kb": 4, "perms": ["bacnet.local", "bacnet.remote"],
        "params": {"count": "1", "l0": "100 2 3 2 10 cov 1000 0 1 0"},
    }
    spec = link_spec.parse(app.manifest["params"]["l0"])
    assert (spec.src_device, spec.src_type, spec.src_instance, spec.dst_type, spec.dst_instance) == (100, 2, 3, 2, 10)


def test_link_options_and_node_sources(doc: dict[str, Any], link_spec: Any) -> None:
    doc["system"]["links"] = [
        {"from": "r205-ctl/binary-input:1", "to": "r204-ctl/binary-output:1", "mode": "poll",
         "period_ms": 250, "priority": 9, "scale": 0.5, "offset": -2.25},
        {"from": "r204-ctl/analog-input:1", "to": "r204-ctl/analog-value:11", "scale": 1e-5},
    ]
    (app,) = link_apps(SiteManifest.from_dict(doc))
    assert app.manifest["params"] == {
        "count": "2",
        "l0": "2051 3 1 4 1 poll 250 9 0.5 -2.25",
        "l1": "2041 0 1 2 11 cov 1000 0 1e-05 0",
    }
    assert app.manifest["period_ms"] == 250
    assert app.manifest["perms"] == ["bacnet.local", "bacnet.remote"]
    assert link_spec.parse(app.manifest["params"]["l0"]).offset == -2.25
    assert link_spec.parse(app.manifest["params"]["l1"]).scale == pytest.approx(1e-5)


def test_local_links_only_need_local_permission(doc: dict[str, Any]) -> None:
    doc["system"]["links"] = [{"from": "r204-ctl/analog-input:1", "to": "r204-ctl/analog-value:11"}]
    (app,) = link_apps(SiteManifest.from_dict(doc))
    assert app.manifest["perms"] == ["bacnet.local"]


def test_more_than_eight_links_are_split(doc: dict[str, Any]) -> None:
    doc["system"]["links"] = [
        {"from": f"ahu1-ctl/analog-value:{i}", "to": f"r204-ctl/analog-value:{100 + i}"} for i in range(11)
    ]
    site = SiteManifest.from_dict(doc)
    first, second = link_apps(site)
    assert (first.name, second.name) == ("link-1", "link-2")
    assert first.manifest["params"]["count"] == "8" and second.manifest["params"]["count"] == "3"
    assert first.link_indexes == list(range(8)) and second.link_indexes == [8, 9, 10]
    assert second.manifest["params"]["l2"] == "100 2 10 2 110 cov 1000 0 1 0"
    assert first.file == second.file == UC_LINK_FILE
    assert link_apps(site, "r205-ctl") == []


def test_links_to_several_nodes(doc: dict[str, Any]) -> None:
    doc["system"]["links"].append({"from": "r204-ctl/analog-input:1", "to": "r205-ctl/analog-value:1"})
    site = SiteManifest.from_dict(doc)
    (app,) = link_apps(site, "r205-ctl")
    assert app.manifest["params"] == {"count": "1", "l0": "2041 0 1 2 1 cov 1000 0 1 0"}
    assert [a.name for a in site.iter_apps()] == ["thermostat", "link", "link"]


def test_link_apps_entries_match_the_firmware_schema(doc: dict[str, Any]) -> None:
    doc["system"]["links"] = [
        {"from": f"ahu1-ctl/analog-value:{i}", "to": f"r204-ctl/analog-value:{4000000 + i}",
         "scale": -123456.789, "offset": 0.000123} for i in range(8)
    ]
    site = SiteManifest.from_dict(doc)
    (app,) = link_apps(site)
    entry = apps_entry(app, "ab" * 32)
    assert entry["params"][0] == {"key": "count", "value": "8"}
    assert all(len(p["value"]) <= 95 for p in entry["params"])


def test_link_names_collide_with_user_apps_only_on_destination_nodes(doc: dict[str, Any]) -> None:
    doc["system"]["apps"].append({"name": "link", "node": "r205-ctl", "wasm": "apps/other.wasm"})
    SiteManifest.from_dict(doc)
    doc["system"]["apps"][-1]["node"] = "r204-ctl"
    with pytest.raises(ValidationFailed, match="invalid"):
        SiteManifest.from_dict(doc)


@pytest.mark.parametrize(("value", "text"), [
    (1, "1"), (1.0, "1"), (-2.0, "-2"), (0.1, "0.1"), (21.5, "21.5"), (1e-05, "1e-05"),
    (1e20, "1e+20"), (True, "1"),
])
def test_number_text(value: float, text: str) -> None:
    assert number_text(value) == text
    assert float(text) == float(value)


@pytest.mark.parametrize(("value", "text"), [(True, "true"), (False, "false"), (21.5, "21.5"), (3, "3"),
                                             ("x", "x")])
def test_param_text(value: Any, text: str) -> None:
    assert param_text(value) == text
