"""SMP facade (hilrig.smp) and the known-state push (hilrig.rigconfig, D25) against the BACnet
branch's own in-process fake node (bacnet_uc_harness.testing.fake_node).

Needs the harness: ``$HIL_BACNET_HARNESS`` pointing at ``harness/`` of a BACnet branch
checkout, and cbor2. The schema checks also use its ``schemas/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hilrig import smp as smp_mod
from hilrig.bench import Bench
from hilrig.rigconfig import CFG_DIR, RigConfig, RigConfigError, encode, golden, validate
from hilrig.smp import Smp

PROBLEM = smp_mod.harness_problem()
pytestmark = pytest.mark.skipif(PROBLEM is not None, reason=str(PROBLEM))

BENCH = Bench.from_dict(
    {
        "name": "unit",
        "mode": "sil",
        "dut": {"profile": "sil", "board": "native_sim/native/64", "bacnet_instance": 260042},
    }
)
F767 = Bench.from_dict(
    {
        "name": "bench1",
        "mode": "sil",
        "dut": {"profile": "sil", "board": "native_sim/native/64", "bacnet_instance": 260001},
    }
)


def harness_root() -> Path | None:
    import os

    root = os.environ.get(smp_mod.HARNESS_ENV)
    if not root:
        return None
    path = Path(root)
    return path.parent if path.name == "src" else path


class NodeThread:
    """The harness FakeNode in its own event loop thread (SMP on 127.0.0.1)."""

    def __init__(self) -> None:
        fake_node = importlib.import_module("bacnet_uc_harness.testing.fake_node")  # found via hilrig.smp
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.node: Any = fake_node.FakeNode()
        self.run(self.node.start())

    def run(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(10)

    def close(self) -> None:
        self.run(self.node.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


@pytest.fixture
def node() -> Iterator[Any]:
    fake = NodeThread()
    yield fake.node
    fake.close()


@pytest.fixture
def client(node: Any) -> Iterator[Smp]:
    with Smp("127.0.0.1", node.smp_port, timeout=1.0, retries=1) as smp:
        yield smp


def test_smp_facade_calls_the_harness_client(client: Smp, node: Any) -> None:
    assert client.echo("hil") == "hil"
    assert client.node_info()["device"]["instance"] == node.device_instance
    assert client.io_read("di0") == {"di0": 0.0}
    client.io_force("di0", 1)
    assert client.io_read("di0") == {"di0": 1.0}
    client.io_release("di0")
    assert client.sha256("/lfs/cfg/none.json") is None
    client.upload("/lfs/x", b"abc")
    assert client.sha256("/lfs/x") == hashlib.sha256(b"abc").digest()
    assert client.wait_up(5) < 5


def test_golden_documents_follow_the_bench_and_the_examples() -> None:
    root = harness_root()
    examples = root.parent / "schemas" / "examples" if root else None
    docs = golden(F767, examples)
    assert docs["device"]["device"] == {
        "instance": 260001,
        "name": "hil-bench1",
        "description": "HIL bench bench1 (sil)",
        "location": "HIL rig",
    }
    assert docs["device"]["network"] == {"dhcp": True} and "static_bindings" not in docs["device"]["bacnet"]
    points = {p["channel"]: p for p in docs["io"]["points"]}
    assert points["di1"] == {
        "channel": "di1",
        "type": "binary-input",
        "instance": 1,
        "name": "HIL di1",
        "sample_ms": 100,
        "debounce_ms": 20,
    }
    assert points["do1"]["type"] == "binary-output" and points["ao0"]["type"] == "analog-output"
    assert docs["apps"] == {"schema": 1, "apps": []}
    assert encode(docs["apps"]) == b'{"schema":1,"apps":[]}\n'


def test_golden_documents_are_valid_against_the_branch_schemas() -> None:
    root = harness_root()
    if root is None or not (root.parent / "schemas").is_dir():
        pytest.skip("the BACnet branch's schemas/ is not next to the harness")
    schemas = root.parent / "schemas"
    notes = validate(golden(F767, schemas / "examples"), schemas)
    if notes:
        pytest.skip("; ".join(notes))
    import jsonschema

    bad = golden(F767, schemas / "examples")
    bad["io"]["points"][0]["type"] = "binary-output-typo"
    with pytest.raises(jsonschema.ValidationError):
        validate(bad, schemas)


def test_apply_uploads_verifies_reloads_and_reboots_when_required(client: Smp, node: Any) -> None:
    docs = golden(BENCH)
    report = RigConfig(client, docs, reboot_timeout=10).apply()
    assert report.uploaded == ["device", "io", "apps"]
    for name in ("device", "io", "apps"):
        data = node.files[f"{CFG_DIR}/{name}.json"]
        assert json.loads(data) == docs[name] and report.sha256[name] == hashlib.sha256(data).hexdigest()
    assert report.reboot_required and report.rebooted_in_s is not None  # the instance changed
    assert node.resets == 1 and node.device_instance == 260042
    assert report.node["device"] == {"instance": 260042, "name": "hil-unit"}
    bound = {(o, i) for (o, i), owner in node.owners.items() if owner == "io"}
    assert len(bound) == 8  # one object per native_sim catalog channel
    again = RigConfig(client, docs, reboot_timeout=10).apply()  # idempotent: nothing to upload
    assert again.uploaded == [] and not again.reboot_required and node.resets == 1


def test_apply_with_an_override_then_back_to_golden(client: Smp, node: Any) -> None:
    rig = RigConfig(client, golden(BENCH), reboot_timeout=10)
    rig.apply()
    pers = golden(BENCH, name="hil-pers-1", location="PERS-01")
    report = rig.apply(device=pers["device"])
    assert report.uploaded == ["device"] and node.device_name == "hil-pers-1" and node.location == "PERS-01"
    rig.apply()
    assert node.device_name == "hil-unit"


def test_a_node_that_keeps_other_values_is_reported(
    client: Smp, node: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = RigConfig(client, golden(BENCH), reboot_timeout=10)
    monkeypatch.setattr(client, "reload", lambda doc="all": False)  # a node that never applies
    with pytest.raises(RigConfigError, match="node reports instance 1001"):
        rig.apply()


def test_the_name_is_checked_as_object_name(client: Smp, node: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: on the real firmware (native_sim, 2161be7) uc_node info kept reporting the
    boot-time name after 'reload device' applied a new one, so the push looked failed. The
    Object_Name (uc_node prop_read) is what the reload changes."""
    rig = RigConfig(client, golden(BENCH), reboot_timeout=10)
    report = rig.apply()
    assert report.object_name == "hil-unit"
    monkeypatch.setattr(client, "prop_read", lambda *args: "bacnet-uc")
    with pytest.raises(RigConfigError, match="Object_Name is 'bacnet-uc'"):
        rig.apply()
