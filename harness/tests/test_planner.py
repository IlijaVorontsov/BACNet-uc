# SPDX-License-Identifier: Apache-2.0
"""Planner: diffs against (fake) live state, apply order, integration with FakeNode."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from bacnet_uc_harness import manifest as m
from bacnet_uc_harness import planner as pl
from bacnet_uc_harness import render as r
from bacnet_uc_harness.errors import HarnessError
from bacnet_uc_harness.node import Node
from bacnet_uc_harness.testing import FakeNode

REPO = Path(__file__).resolve().parents[2]
needs_clang = pytest.mark.skipif(shutil.which("clang") is None, reason="clang required")


def make_system(tmp_path: Path) -> m.System:
    for f in ("app.wasm", "other.wasm"):
        (tmp_path / f).write_bytes(b"\0asm\1\0\0\0" + f.encode())
    doc = {
        "apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "p"},
        "nodes": [
            {"name": "a", "board": "native_sim/native/64", "transport": {"kind": "sim"},
             "device": {"instance": 1, "name": "a"},
             "io": [{"channel": "ai0", "type": "analog-input", "instance": 1}]},
            {"name": "b", "board": "native_sim/native/64", "transport": {"kind": "sim"},
             "device": {"instance": 2, "name": "b"},
             "io": [{"channel": "do0", "type": "binary-output", "instance": 1}]},
        ],
        "apps": [{"name": "app", "node": "b", "wasm": "app.wasm", "perms": ["bacnet.local"],
                  "params": {"k": 1}}],
        "links": [{"from": "a/analog-input:1", "to": "b/analog-value:5"}],
    }
    return m.load_system_dict(doc, base_dir=tmp_path)


def artifacts(system: m.System, tmp_path: Path) -> dict[tuple[str, str], r.AppArtifact]:
    out = {}
    for n in system.nodes:
        for app in r.node_apps(system, n.name):
            path = app.wasm or (tmp_path / "other.wasm")
            data = path.read_bytes()
            out[(n.name, app.name)] = r.AppArtifact(app.name, n.name, path,
                                                   hashlib.sha256(data).hexdigest(), len(data))
    return out


def synced_live(system: m.System, renders: dict[str, r.NodeRender],
                arts: dict[tuple[str, str], r.AppArtifact]) -> dict[str, pl.LiveState]:
    """Live state equal to the desired state."""
    live = {}
    for n in system.nodes:
        nr = renders[n.name]
        st = pl.LiveState(node=n.name, info={"device": {"instance": n.instance},
                                             "net": {"bacnet_port": 47808}})
        st.config_sha256 = {"device": r.doc_sha256(nr.device), "io": r.doc_sha256(nr.io)}
        for ra in nr.app_list:
            entry = dict(ra.entry)
            entry["sha256"] = arts[(n.name, ra.name)].sha256
            st.apps_cfg.append(entry)
            st.apps_status[ra.name] = {"name": ra.name, "file": ra.file, "state": "running"}
            st.files_sha256[ra.file] = arts[(n.name, ra.name)].sha256
        st.objects = [{"type": p["type"], "instance": p["instance"], "owner": "io"}
                      for p in nr.io["points"]]
        live[n.name] = st
    return live


@pytest.fixture
def setup(tmp_path: Path) -> tuple[m.System, dict[str, r.NodeRender],
                                   dict[tuple[str, str], r.AppArtifact]]:
    s = make_system(tmp_path)
    arts = artifacts(s, tmp_path)
    return s, r.render_system(s, artifacts=arts), arts


def kinds(p: pl.Plan) -> list[tuple[str, str, str]]:
    return [(a.node, a.kind, a.target) for a in p.ordered()]


def test_empty_nodes_get_everything(setup: Any) -> None:
    s, renders, arts = setup
    live = {n: pl.LiveState(node=n) for n in ("a", "b")}
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [
        ("a", "push_config", "device"), ("b", "push_config", "device"),
        ("a", "push_config", "io"), ("b", "push_config", "io"),
        ("b", "deploy_app", "app"), ("b", "deploy_app", "link"),
    ]
    deploy = p.ordered()[4]
    assert deploy.reason == "not installed" and deploy.data["upload"] is True
    assert deploy.data["entry"]["sha256"] == arts[("b", "app")].sha256
    assert p.ordered()[5].phase == pl.PHASE_LINKS
    d = p.to_dict()
    assert d["actions"][0]["phase"] == "device" and not d["in_sync"]
    assert "push_config device" in p.summary()


def test_in_sync(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, synced_live(s, renders, arts), renders=renders, artifacts=arts)
    assert p.empty and not p.notes
    assert p.summary() == "system p: in sync"


def test_changes_detected(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"].config_sha256["io"] = "00" * 32
    live["b"].files_sha256["/lfs/apps/app.wasm"] = "11" * 32
    live["b"].apps_cfg[1]["params"] = [{"key": "count", "value": "1"},
                                       {"key": "l0", "value": "9 0 1 2 5 cov 1000 0 1 0"}]
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [("a", "push_config", "io"), ("b", "deploy_app", "app"),
                        ("b", "deploy_app", "link")]
    acts = p.ordered()
    assert acts[0].reason == "content differs"
    assert acts[1].reason == "module changed" and acts[1].data["upload"]
    assert acts[2].reason == "manifest changed (params)" and not acts[2].data["upload"]


def test_entry_diff_defaults() -> None:
    desired = {"file": "/lfs/apps/x.wasm", "perms": ["kv", "bacnet.local"],
               "params": [{"key": "a", "value": "1"}]}
    live = {"file": "/lfs/apps/x.wasm", "autostart": True, "period_ms": 1000, "heap_kb": 8,
            "stack_kb": 4, "perms": ["bacnet.local", "kv"], "params": {"a": "1"}}
    assert pl.entry_diff(desired, live) == []
    assert pl.entry_diff({**desired, "period_ms": 5}, live) == ["period_ms"]


def test_stopped_app_is_started(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["b"].apps_status["app"].update(state="failed", last_error="trap")
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [("b", "start_app", "app")]
    assert p.actions[0].reason == "state failed: trap"


def test_extra_app_note_or_prune(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"].apps_status["old"] = {"name": "old", "state": "running"}
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert p.empty and p.notes[0].kind == "warning" and "'old'" in p.notes[0].message
    p = pl.plan(s, live, renders=renders, artifacts=arts, prune=True)
    assert kinds(p) == [("a", "remove_app", "old")]


def test_unreachable_and_missing_artifact(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"] = pl.LiveState(node="a", reachable=False, error="HarnessTimeout: no answer")
    arts2 = dict(arts)
    del arts2[("b", "link")]
    p = pl.plan(s, live, renders=renders, artifacts=arts2)
    assert p.empty
    assert [n.kind for n in p.notes] == ["unreachable", "warning"]
    assert "no answer" in p.notes[0].message


def test_io_reload_when_objects_missing(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["b"].objects = []
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [("b", "reload", "io")]
    assert "binary-output:1" in p.actions[0].reason


def test_reboot_notes(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"].info = {"device": {"instance": 260001}, "net": {"bacnet_port": 47808,
                                                              "ipv4": "127.0.0.1"}}
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert p.empty
    assert p.notes[0].kind == "reboot_required" and "pending" in p.notes[0].message
    assert "260001 -> 1" in p.notes[0].message
    assert "IPv4 127.0.0.1 -> 10.47.0.1" in p.notes[0].message
    live["a"].config_sha256["device"] = None
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [("a", "push_config", "device")]
    assert "after device.json" in p.notes[0].message


def test_io_push_restarts_dependent_apps(setup: Any) -> None:
    """The link on b reads a's IO object analog-input:1: re-created by the
    io.json push on a, so the link is restarted after the apps phase."""
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"].config_sha256["io"] = "00" * 32
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert kinds(p) == [("a", "push_config", "io"), ("b", "restart_app", "link")]
    restart = p.ordered()[1]
    assert restart.phase == pl.PHASE_LINKS and restart.data["objects"] == ["a/analog-input:1"]
    assert "re-created IO objects a/analog-input:1" in restart.describe()
    assert p.to_dict()["actions"][1]["kind"] == "restart_app"
    # an app that is (re)deployed anyway is not restarted as well
    live["b"].files_sha256["/lfs/apps/link.wasm"] = "11" * 32
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    assert ("b", "restart_app", "link") not in kinds(p)
    # nor one that is not installed / not reachable
    live = synced_live(s, renders, arts)
    live["a"].config_sha256["io"] = "00" * 32
    del live["b"].apps_status["link"]
    assert ("b", "restart_app", "link") not in kinds(pl.plan(s, live, renders=renders,
                                                             artifacts=arts))


def test_io_dependents() -> None:
    doc = {
        "apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "d"},
        "nodes": [
            {"name": "s", "board": "native_sim/native/64", "transport": {"kind": "sim"},
             "device": {"instance": 1, "name": "s"},
             "io": [{"channel": "ai0", "type": "analog-input", "instance": 1}]},
            {"name": "o", "board": "native_sim/native/64", "transport": {"kind": "sim"},
             "device": {"instance": 2, "name": "o"},
             "io": [{"channel": "ao0", "type": "analog-output", "instance": 1},
                    {"channel": "do0", "type": "binary-output", "instance": 3}]},
        ],
        "apps": [
            {"name": "thermostat", "node": "o", "wasm": "t.wasm",
             "params": {"sensor_device": 1, "out_instance": 1}},
            {"name": "alarm", "node": "s", "wasm": "alarm.wasm", "params": {"src_instance": 1}},
            {"name": "blinky", "node": "o", "wasm": "blinky.wasm",
             "params": {"type": 4, "instance": 3}},
        ],
        "links": [{"from": "s/analog-input:1", "to": "o/analog-value:9", "mode": "poll"}],
    }
    s = m.load_system_dict(doc, check_files=False)
    assert pl.io_dependents(s, {"s"}) == {
        ("o", "thermostat"): ["s/analog-input:1"], ("s", "alarm"): ["s/analog-input:1"],
        ("o", "link"): ["s/analog-input:1"]}
    assert pl.io_dependents(s, {"o"}) == {
        ("o", "thermostat"): ["o/analog-output:1"], ("o", "blinky"): ["o/binary-output:3"]}


async def test_apply_restart_action(setup: Any) -> None:
    s, renders, arts = setup
    live = synced_live(s, renders, arts)
    live["a"].config_sha256["io"] = "00" * 32
    p = pl.plan(s, live, renders=renders, artifacts=arts)
    log: list[str] = []
    rep = await pl.apply(p, {"a": RecordingNode("a", log), "b": RecordingNode("b", log)})
    assert rep.ok and log == ["a:push io", "b:restart link"]
    assert rep.results[1].detail == {"name": "link", "state": "running"}


def test_plan_renders_itself(setup: Any) -> None:
    s, _, arts = setup
    p = pl.plan(s, {n: pl.LiveState(node=n) for n in ("a", "b")}, artifacts=arts)
    assert len(p.actions) == 6


# --- apply with fakes -------------------------------------------------------------------------


class RecordingNode:
    def __init__(self, name: str, log: list[str], fail: set[str] | None = None,
                 reboot_on: set[str] | None = None) -> None:
        self.name = name
        self.log = log
        self.fail = fail or set()
        self.reboot_on = reboot_on or set()
        self.reboots = 0

    def _rec(self, what: str) -> None:
        self.log.append(f"{self.name}:{what}")
        if what in self.fail:
            raise HarnessError(f"{what} failed")

    async def push_config(self, doc: str, content: dict[str, Any]) -> dict[str, Any]:
        self._rec(f"push {doc}")
        return {"doc": doc, "reboot_required": doc in self.reboot_on}

    async def reload(self, doc: str) -> bool:
        self._rec(f"reload {doc}")
        return False

    async def deploy_app(self, name: str, data: bytes, **kw: Any) -> dict[str, Any]:
        self._rec(f"deploy {name}")
        assert data[:4] == b"\0asm"
        return {"name": name, "uploaded": True, "status": {"state": "running"}}

    async def remove_app(self, name: str, delete_file: bool = True) -> dict[str, Any]:
        self._rec(f"remove {name}")
        return {"name": name}

    async def start_app(self, name: str) -> dict[str, Any]:
        self._rec(f"start {name}")
        return {"name": name}

    async def restart_app(self, name: str) -> dict[str, Any]:
        self._rec(f"restart {name}")
        return {"name": name, "state": "running"}

    async def reboot(self) -> None:
        self.reboots += 1

    async def info(self) -> dict[str, Any]:
        return {}


async def test_apply_order_and_results(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, {n: pl.LiveState(node=n) for n in ("a", "b")}, renders=renders,
                artifacts=arts)
    log: list[str] = []
    nodes = {"a": RecordingNode("a", log), "b": RecordingNode("b", log)}
    rep = await pl.apply(p, nodes)
    assert rep.ok
    assert log == ["a:push device", "b:push device", "a:push io", "b:push io",
                   "b:deploy app", "b:deploy link"]
    d = rep.to_dict()
    assert d["results"][4]["result"]["state"] == "running"
    assert all(x["status"] == "ok" for x in d["results"])


async def test_apply_dry_run(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, {n: pl.LiveState(node=n) for n in ("a", "b")}, renders=renders,
                artifacts=arts)
    log: list[str] = []
    rep = await pl.apply(p, {"a": RecordingNode("a", log)}, dry_run=True)
    assert not log and rep.dry_run
    assert {x.status for x in rep.results} == {"dry-run"}


async def test_apply_failure_skips_rest_of_node(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, {n: pl.LiveState(node=n) for n in ("a", "b")}, renders=renders,
                artifacts=arts)
    log: list[str] = []
    nodes = {"a": RecordingNode("a", log), "b": RecordingNode("b", log, fail={"push io"})}
    rep = await pl.apply(p, nodes)
    assert not rep.ok
    statuses = [(x.action.node, x.action.target, x.status) for x in rep.results]
    assert statuses == [("a", "device", "ok"), ("b", "device", "ok"), ("a", "io", "ok"),
                        ("b", "io", "failed"), ("b", "app", "skipped"), ("b", "link", "skipped")]
    assert rep.results[3].error == "push io failed"


async def test_apply_missing_connection(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, {"b": pl.LiveState(node="b")}, renders=renders, artifacts=arts)
    rep = await pl.apply(p, {})
    assert not rep.ok and "no connection" in (rep.results[0].error or "")


async def test_apply_reboot(setup: Any) -> None:
    s, renders, arts = setup
    p = pl.plan(s, {n: pl.LiveState(node=n) for n in ("a", "b")}, renders=renders,
                artifacts=arts)
    log: list[str] = []
    a = RecordingNode("a", log, reboot_on={"device"})
    b = RecordingNode("b", log)
    rep = await pl.apply(p, {"a": a, "b": b}, reboot=False)
    assert rep.reboot_required == ["a"] and not rep.rebooted and a.reboots == 0
    rebooted: list[str] = []

    async def rebooter(name: str) -> None:
        rebooted.append(name)

    rep = await pl.apply(p, {"a": a, "b": b}, reboot=True, rebooter=rebooter)
    assert rebooted == ["a"] and rep.rebooted == ["a"]
    rep = await pl.apply(p, {"a": a, "b": b}, reboot=True)
    assert a.reboots == 1


# --- integration with FakeNode ----------------------------------------------------------------


@needs_clang
async def test_plan_apply_fake_nodes(make_fake_node: Callable[..., Awaitable[FakeNode]],
                                     tmp_path: Path) -> None:
    fa = await make_fake_node(3001)
    fb = await make_fake_node(3002)
    thermostat = REPO / "wasm" / "examples" / "thermostat" / "thermostat.c"
    doc = {
        "apiVersion": "bacnet-uc/v1", "kind": "System", "metadata": {"name": "fk"},
        "nodes": [
            {"name": "a", "board": "native_sim/native/64",
             "transport": {"kind": "udp", "host": "127.0.0.1", "port": fa.smp_port},
             "bacnet_address": f"127.0.0.1:{fa.bacnet_port}",
             "bacnet": {"udp_port": fa.bacnet_port},
             "device": {"instance": 3001, "name": "fa"},
             "io": [{"channel": "ai0", "type": "analog-input", "instance": 1, "scale": 0.01}]},
            {"name": "b", "board": "native_sim/native/64",
             "transport": {"kind": "udp", "host": "127.0.0.1", "port": fb.smp_port},
             "bacnet_address": f"127.0.0.1:{fb.bacnet_port}",
             "bacnet": {"udp_port": fb.bacnet_port},
             "device": {"instance": 3002, "name": "fb"},
             "io": [{"channel": "ao0", "type": "analog-output", "instance": 1}]},
        ],
        "apps": [{"name": "thermostat", "node": "b", "source": str(thermostat),
                  "params": {"sensor_device": "{{ nodes.a.device.instance }}"}}],
        "links": [{"from": "a/analog-input:1", "to": "b/analog-value:7"}],
    }
    system = m.load_system_dict(doc)
    nodes = {"a": Node("a", f"udp:127.0.0.1:{fa.smp_port}", f"127.0.0.1:{fa.bacnet_port}",
                       smp_timeout=1.0),
             "b": Node("b", f"udp:127.0.0.1:{fb.smp_port}", f"127.0.0.1:{fb.bacnet_port}",
                       smp_timeout=1.0)}
    builder = pl.ArtifactBuilder(tmp_path / "cache")
    try:
        p, renders, arts, live = await pl.plan_system(system, nodes, builder=builder)
        assert len(p.actions) == 6 and all(s.reachable for s in live.values())
        rep = await pl.apply(p, nodes)
        assert rep.ok, rep.to_dict()
        assert fb.apps["thermostat"]["state"] == "running"
        assert fb.apps["link"]["params"]["l0"] == "3001 0 1 2 7 cov 1000 0 1 0"
        assert fb.files["/lfs/cfg/device.json"] == r.doc_bytes(renders["b"].device)
        p2, _, _, _ = await pl.plan_system(system, nodes, builder=builder)
        assert p2.empty, p2.summary()
        # drift: the app is stopped and io.json replaced on the node
        await nodes["b"].stop_app("thermostat")
        await nodes["a"].push_config("io", {"schema": 1, "points": []})
        p3, _, _, _ = await pl.plan_system(system, nodes, artifacts=arts)
        # the link on b reads a's re-created analog-input:1: restarted
        assert kinds(p3) == [("a", "push_config", "io"), ("b", "start_app", "thermostat"),
                             ("b", "restart_app", "link")]
        assert (await pl.apply(p3, nodes)).ok
        assert (await pl.plan_system(system, nodes, artifacts=arts))[0].empty
        # a stale staged io.json (set_config without reload) is not "in sync":
        # the next reload or reboot would activate it (HAR-2)
        await nodes["a"].push_config("io", {"schema": 1, "points": []}, reload=False)
        p4, _, _, live4 = await pl.plan_system(system, nodes, artifacts=arts)
        assert live4["a"].staged_sha256["io"] is not None
        assert kinds(p4) == [("a", "clear_staged", "io")]
        assert (await pl.apply(p4, nodes, reboot=True)).ok
        assert "/lfs/cfg/io.json.new" not in fa.files
        assert fa.files["/lfs/cfg/io.json"] == r.doc_bytes(renders["a"].io)
        assert (await pl.plan_system(system, nodes, artifacts=arts))[0].empty
    finally:
        for n in nodes.values():
            await n.close()


# --- build cache ------------------------------------------------------------------------------


def test_source_dependencies_follow_local_includes(tmp_path: Path) -> None:
    (tmp_path / "inc").mkdir()
    (tmp_path / "app.c").write_text('#include <bacnet_uc.h>\n#include "cfg.h"\n'
                                    '#if 0\n  #  include "inc/opt.h"\n#endif\n'
                                    '#include "missing.h"\n')
    (tmp_path / "cfg.h").write_text('#include "cfg.h"\n#define SETPOINT 21\n')
    (tmp_path / "inc" / "opt.h").write_text('#include "../cfg.h"\n')
    deps = pl.source_dependencies(tmp_path / "app.c")
    assert deps == sorted([(tmp_path / f).resolve() for f in ("app.c", "cfg.h", "inc/opt.h")])


@needs_clang
def test_build_cache_sees_local_header_changes(tmp_path: Path) -> None:
    """A change of a header next to the app source invalidates the cached
    module (HAR-7)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.c").write_text(
        '#include <bacnet_uc.h>\n#include "cfg.h"\nUC_APP_DECLARE()\n'
        'UC_EXPORT(uc_app_init) int32_t uc_app_init(void) '
        '{ return uc_set_tick_period(SETPOINT); }\n')
    (src / "cfg.h").write_text("#define SETPOINT 21\n")
    app = m.AppSpec(name="app", node="n", source=src / "app.c")
    builder = pl.ArtifactBuilder(tmp_path / "cache")
    first = builder.wasm_for(app)
    assert builder.wasm_for(app) == first and len(builder.log) == 1  # cached
    (src / "cfg.h").write_text("#define SETPOINT 35\n")
    second = builder.wasm_for(app)
    assert second != first and len(builder.log) == 2
    assert second.read_bytes() != first.read_bytes()
    # another opt level or heap size is another key as well
    assert pl.ArtifactBuilder(tmp_path / "cache", opt="-O2").wasm_key(app) != \
        builder.wasm_key(app)
