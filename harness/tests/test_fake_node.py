# SPDX-License-Identifier: Apache-2.0
"""FakeNode behaviour: IO bindings, configuration reloads, reboot."""

from __future__ import annotations

import asyncio
import json

import pytest

from bacnet_uc_harness.bacnet.client import BacnetClient
from bacnet_uc_harness.errors import HarnessError, SmpError
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.client import SmpClient
from bacnet_uc_harness.smp.codec import encode_frame
from bacnet_uc_harness.testing import FakeNode

IO_JSON = {
    "schema": 1,
    "points": [
        {"channel": "di0", "type": "binary-input", "instance": 1, "name": "Button"},
        {"channel": "di1", "type": "multi-state-input", "instance": 2, "invert": True},
        {"channel": "do0", "type": "binary-output", "instance": 1, "name": "Relay"},
        {
            "channel": "ai0",
            "type": "analog-input",
            "instance": 1,
            "name": "Temp",
            "units": "degrees-celsius",
            "scale": 0.1,
            "offset": -50.0,
            "cov_increment": 0.5,
        },
        {
            "channel": "ao0",
            "type": "analog-value",
            "instance": 7,
            "units": "percent",
            "min": 10,
            "max": 90,
            "scale": 2.0,
        },
    ],
}


async def _upload_json(smp: SmpClient, path: str, doc: dict) -> None:
    await smp.fs_upload(path, json.dumps(doc).encode())


async def test_default_catalog(fake_node: FakeNode) -> None:
    assert list(fake_node.io_channels) == ["di0", "di1", "do0", "do1", "ai0", "ai1", "ao0", "ao1"]
    assert all(c.hw == "sim" for c in fake_node.io_channels.values())
    assert fake_node.io_channels["ai0"].value == 2000.0
    assert sorted(fake_node.objects) == [(8, 1001), (56, 1)]


async def test_io_json_binding(
    fake_node: FakeNode, smp_client: SmpClient, bacnet_client: BacnetClient
) -> None:
    await _upload_json(smp_client, "/lfs/cfg/io.json", IO_JSON)
    assert await smp_client.node_reload("io") is False
    addr = fake_node.bacnet_address
    # objects exist with the configured properties
    assert await bacnet_client.read_property(addr, "analog-input", 1, "object-name") == "Temp"
    assert await bacnet_client.read_property(addr, "analog-input", 1, "units") == 62
    assert await bacnet_client.read_property(addr, "analog-input", 1, "cov-increment") == 0.5
    assert await bacnet_client.read_property(addr, "multi-state-input", 2, "object-name") == "di1"
    assert await bacnet_client.read_property(addr, "binary-input", 1, "polarity") == 0
    # analog input follows the channel: 2000 mV * 0.1 - 50 = 150
    assert await bacnet_client.read_property(addr, "analog-input", 1) == pytest.approx(150.0)
    await smp_client.io_force("ai0", 700)
    assert await bacnet_client.read_property(addr, "analog-input", 1) == pytest.approx(20.0)
    # binary input and inverted multi-state input
    assert await bacnet_client.read_property(addr, "binary-input", 1) == 0
    assert await bacnet_client.read_property(addr, "multi-state-input", 2) == 2
    await smp_client.io_force("di0", 1)
    await smp_client.io_force("di1", 1)
    assert await bacnet_client.read_property(addr, "binary-input", 1) == 1
    assert await bacnet_client.read_property(addr, "multi-state-input", 2) == 1
    # out-of-service freezes the input
    await bacnet_client.write_property(addr, "binary-input", 1, "out-of-service", True)
    await smp_client.io_force("di0", 0)
    assert await bacnet_client.read_property(addr, "binary-input", 1) == 1
    # outputs follow Present_Value
    await bacnet_client.write_property(addr, "binary-output", 1, "present-value", 1, priority=8)
    assert await smp_client.io_read("do0") == {"do0": 1.0}
    await bacnet_client.write_property(addr, "analog-value", 7, "present-value", 50.0)
    assert await smp_client.io_read("ao0") == {"ao0": 25.0}  # 50 / scale 2
    await bacnet_client.write_property(addr, "analog-value", 7, "present-value", 95.0)
    assert await smp_client.io_read("ao0") == {"ao0": 45.0}  # clamped to max 90
    # a forced output ignores its object until released
    await smp_client.io_force("do0", 0)
    assert await smp_client.io_read("do0") == {"do0": 0.0}
    await smp_client.io_release("do0")
    assert await smp_client.io_read("do0") == {"do0": 1.0}
    # catalog reports the bindings
    cat = {c["name"]: c for c in (await smp_client.io_catalog())["channels"]}
    assert cat["ao0"]["object"] == {"type": "analog-value", "instance": 7}
    assert "object" not in cat["do1"]
    # objects listing marks them as io-owned
    objs = await smp_client.node_objects_all()
    assert {o["owner"] for o in objs} == {"system", "io"}


async def test_io_reload_replaces_objects(fake_node: FakeNode, smp_client: SmpClient) -> None:
    await _upload_json(smp_client, "/lfs/cfg/io.json", IO_JSON)
    await smp_client.node_reload("io")
    assert (0, 1) in fake_node.objects
    doc = {"schema": 1, "points": [{"channel": "ai1", "type": "analog-input", "instance": 9}]}
    await _upload_json(smp_client, "/lfs/cfg/io.json", doc)
    await smp_client.node_reload("io")
    assert sorted(fake_node.objects) == [(0, 9), (8, 1001), (56, 1)]
    assert fake_node.io_channels["ai0"].obj is None


@pytest.mark.parametrize(
    "points",
    [
        [{"channel": "ai0", "type": "analog-input"}],
        [{"channel": "ai0", "type": "device", "instance": 1}],
        [{"channel": "ai0", "type": "analog-input", "instance": 1, "units": "nope"}],
        [{"channel": "ai0", "type": "analog-input", "instance": 4194303}],
        [{"channel": "ao0", "type": "analog-output", "instance": 1, "min": 5, "max": 1}],
        [{"channel": "ai0", "type": "analog-input", "instance": 1, "sample_ms": 5}],
        [
            {"channel": "ai0", "type": "analog-input", "instance": 1},
            {"channel": "ai1", "type": "analog-input", "instance": 1},
        ],
        [{"channel": f"c{i}", "type": "binary-input", "instance": i} for i in range(33)],
    ],
)
async def test_io_reload_invalid_keeps_config(
    fake_node: FakeNode, smp_client: SmpClient, points: list
) -> None:
    """Parse-stage errors (uc_config_parse_io) reject the whole document."""
    await _upload_json(smp_client, "/lfs/cfg/io.json", IO_JSON)
    await smp_client.node_reload("io")
    before = sorted(fake_node.objects)
    await _upload_json(smp_client, "/lfs/cfg/io.json", {"schema": 1, "points": points})
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("io")
    assert (exc.value.group, exc.value.rc_name) == (66, "INVALID")
    assert exc.value.response is not None and exc.value.response["reboot_required"] is False
    assert sorted(fake_node.objects) == before


async def test_io_reload_skips_unbindable_points(
    fake_node: FakeNode, smp_client: SmpClient
) -> None:
    """Points that cannot be bound are skipped, the others bound (uc_io.c)."""
    fake_node.add_object("analog-value", 3, owner="app:x")
    points = [
        {"channel": "zz", "type": "analog-input", "instance": 5},  # unknown channel
        {"channel": "ai0", "type": "binary-input", "instance": 1},  # kind mismatch
        {"channel": "di0", "type": "binary-input", "instance": 2},
        {"channel": "di0", "type": "binary-input", "instance": 3},  # channel bound twice
        {"channel": "ao0", "type": "analog-value", "instance": 3},  # object of an app
        {"channel": "ao1", "type": "analog-output", "instance": 1, "scale": 0},  # scale 0
        {"channel": "ai1", "type": "analog-input", "instance": 1},
    ]
    await _upload_json(smp_client, "/lfs/cfg/io.json", {"schema": 1, "points": points})
    assert await smp_client.node_reload("io") is False
    io_objects = sorted(k for k, o in fake_node.owners.items() if o == "io")
    assert io_objects == [(0, 1), (3, 2)]
    assert fake_node.owners[(2, 3)] == "app:x"
    cat = {c["name"]: c.get("object") for c in (await smp_client.io_catalog())["channels"]}
    assert cat["di0"] == {"type": "binary-input", "instance": 2}
    assert cat["ai1"] == {"type": "analog-input", "instance": 1}
    assert cat["ai0"] is None and cat["ao0"] is None and cat["ao1"] is None


async def test_reload_invalid_json(fake_node: FakeNode, smp_client: SmpClient) -> None:
    await smp_client.fs_upload("/lfs/cfg/io.json", b"{not json")
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("io")
    assert exc.value.rc_name == "INVALID"
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("everything")
    assert exc.value.rc_name == "INVALID"
    # missing documents mean defaults
    fake_node.files.pop("/lfs/cfg/io.json")
    assert await smp_client.node_reload("all") is False


async def test_staged_documents(fake_node: FakeNode, smp_client: SmpClient) -> None:
    """``<doc>.json.new`` is activated by reload when valid, deleted when not."""
    await _upload_json(smp_client, "/lfs/cfg/io.json", IO_JSON)
    await smp_client.node_reload("io")
    active = fake_node.files["/lfs/cfg/io.json"]
    before = sorted(fake_node.objects)
    # invalid: deleted, active document and objects kept
    await smp_client.fs_upload("/lfs/cfg/io.json.new", b'{"schema":1,"points":[{"chan')
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("io")
    assert exc.value.rc_name == "INVALID"
    assert "/lfs/cfg/io.json.new" not in fake_node.files
    assert fake_node.files["/lfs/cfg/io.json"] == active
    assert sorted(fake_node.objects) == before
    # valid: renamed over the active document and applied
    staged = {"schema": 1, "points": [{"channel": "di0", "type": "binary-input", "instance": 9}]}
    await _upload_json(smp_client, "/lfs/cfg/io.json.new", staged)
    await smp_client.node_reload("all")
    assert "/lfs/cfg/io.json.new" not in fake_node.files
    assert json.loads(fake_node.files["/lfs/cfg/io.json"]) == staged
    assert (3, 9) in fake_node.objects
    # at boot a staged document is activated as well
    await _upload_json(
        smp_client,
        "/lfs/cfg/device.json.new",
        {"schema": 1, "device": {"instance": 1007, "name": "s"}},
    )
    await smp_client.os_reset()
    assert (await smp_client.node_info())["device"] == {"instance": 1007, "name": "s"}
    assert "/lfs/cfg/device.json.new" not in fake_node.files


async def test_device_reload_and_reboot(
    fake_node: FakeNode, smp_client: SmpClient, repo_root
) -> None:  # type: ignore[no-untyped-def]
    doc = json.loads((repo_root / "schemas/examples/device.json").read_text())
    await _upload_json(smp_client, "/lfs/cfg/device.json", doc)
    # name/location apply now; instance and network need a reboot
    assert await smp_client.node_reload("device") is True
    info = await smp_client.node_info()
    assert info["device"] == {"instance": 1001, "name": "uc-sensor"}
    assert await smp_client.prop_read("device", 1001, "location") == "Lab 1"
    doc["device"]["instance"] = 1005
    await _upload_json(smp_client, "/lfs/cfg/device.json", doc)
    assert await smp_client.node_reload("device") is True
    await smp_client.os_reset()
    info = await smp_client.node_info()
    assert info["device"]["instance"] == 1005
    assert (8, 1005) in fake_node.objects and (8, 1001) not in fake_node.objects


async def test_error_response_keeps_reboot_flag(smp_client: SmpClient) -> None:
    await smp_client.fs_upload(
        "/lfs/cfg/device.json",
        json.dumps({"schema": 1, "device": {"instance": 7, "name": "x"}}).encode(),
    )
    await smp_client.fs_upload("/lfs/cfg/io.json", b"[]")
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("all")
    assert exc.value.rc_name == "INVALID"
    assert exc.value.response["reboot_required"] is True


async def test_apps_json_reload_and_boot(
    fake_node: FakeNode, smp_client: SmpClient, wasm_module: bytes
) -> None:
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    doc = {
        "schema": 1,
        "apps": [
            {"name": "a", "file": "/lfs/apps/a.wasm", "params": [{"key": "k", "value": "v"}]},
            {"name": "b", "file": "/lfs/apps/a.wasm", "autostart": False},
            {"name": "c", "file": "/lfs/apps/missing.wasm"},
        ],
    }
    await _upload_json(smp_client, "/lfs/cfg/apps.json", doc)
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("apps")
    assert exc.value.rc_name == "NOT_FOUND"  # app c cannot start
    states = {a["name"]: a["state"] for a in await smp_client.app_list()}
    assert states == {"a": "running", "b": "stopped", "c": "failed"}
    assert fake_node.apps["a"]["params"] == {"k": "v"}
    status = await smp_client.app_status("c")
    assert status["errors"] == 1 and "missing.wasm" in status["last_error"]
    # removing an entry from apps.json removes the app
    doc["apps"] = doc["apps"][:1]
    await _upload_json(smp_client, "/lfs/cfg/apps.json", doc)
    await smp_client.node_reload("apps")
    assert [a["name"] for a in await smp_client.app_list()] == ["a"]
    # after a reboot the autostart app runs again
    await smp_client.app_stop("a")
    await smp_client.os_reset()
    assert (await smp_client.app_status("a"))["state"] == "running"
    info = await smp_client.node_info()
    assert info["apps"] == {"installed": 1, "running": 1}


async def test_boot_from_files(wasm_module: bytes) -> None:
    files = {
        "/lfs/cfg/io.json": json.dumps(IO_JSON).encode(),
        "/lfs/cfg/device.json": json.dumps(
            {"schema": 1, "device": {"instance": 42, "name": "pre"}}
        ).encode(),
        "/lfs/apps/a.wasm": wasm_module,
        "/lfs/cfg/apps.json": json.dumps(
            {"schema": 1, "apps": [{"name": "a", "file": "/lfs/apps/a.wasm"}]}
        ).encode(),
    }
    node = FakeNode(files=files)
    await node.start()
    try:
        assert node.device_instance == 42 and node.device_name == "pre"
        assert (0, 1) in node.objects
        assert node.apps["a"]["state"] == "running"
        assert node.get_pv("analog-input", 1) == pytest.approx(150.0)
    finally:
        await node.stop()


def test_handle_smp_frame_without_sockets() -> None:
    from bacnet_uc_harness.smp import groups as g
    from bacnet_uc_harness.smp.codec import decode_frame, encode_frame

    node = FakeNode()
    rsp = node.handle_smp_frame(encode_frame(g.OP_READ, g.GROUP_UC_NODE, g.UC_NODE_INFO, 3, {}))
    assert rsp is not None
    hdr, body = decode_frame(rsp)
    assert (hdr.op, hdr.seq, body["device"]["instance"]) == (g.OP_READ_RSP, 3, 1001)
    # responses and garbage are ignored
    assert node.handle_smp_frame(encode_frame(g.OP_READ_RSP, 0, 0, 1, {})) is None
    assert node.handle_smp_frame(b"\x00") is None


def test_add_object_and_remove() -> None:
    node = FakeNode()
    props = node.add_object("multi-state-output", 1, "Mode", number_of_states=4, pv=3)
    assert props[85] == 3 and props[87] == [None] * 16 and props[104] == 3
    with pytest.raises(HarnessError):
        node.add_object("mso", 1)
    node.remove_object("mso", 1)
    assert (14, 1) not in node.objects


async def test_paging_stops_when_the_response_is_full(wasm_module: bytes) -> None:
    """list/catalog/objects hold only what fits 1016 bytes of CBOR."""
    catalog = tuple((f"ch{i}", "ai", 0.0, "x" * 90) for i in range(20))
    node = FakeNode(catalog=catalog)
    await node.start()
    try:
        from bacnet_uc_harness.smp.client import connect_udp

        smp = await connect_udp(node.host, node.smp_port, timeout=1.0)
        try:
            page = await smp.read(g.GROUP_UC_IO, g.UC_IO_CATALOG, {})
            assert page["total"] == 20 and 0 < len(page["channels"]) < 20
            assert len(encode_frame(1, 65, 0, 0, page)) <= 1024
            assert [c["id"] for c in (await smp.io_catalog())["channels"]] == list(range(20))
            for i in range(12):
                node.add_object("analog-value", i, f"{'long name ' * 6}{i}")
            page = await smp.node_objects(count=100)
            assert page["total"] == 14 and len(page["objects"]) < 14
            assert len(await smp.node_objects_all()) == 14
            assert (await smp.node_objects())["objects"].__len__() == 8  # default count
        finally:
            await smp.close()
    finally:
        await node.stop()


async def test_io_force_requires_value_or_release(smp_client: SmpClient) -> None:
    for req in ({"name": "ai0"}, {"name": "ai0", "value": 1.0, "release": True},
                {"name": "ai0", "release": False}):
        with pytest.raises(SmpError) as exc:
            await smp_client.write(g.GROUP_UC_IO, g.UC_IO_FORCE, req)
        assert exc.value.rc_name == "INVALID"
    with pytest.raises(SmpError) as exc:
        await smp_client.io_write("ai0", 1.0)
    assert exc.value.rc_name == "PERM"


@pytest.mark.parametrize("password", ["", "x" * 21, "tab\\there", "é"])
async def test_device_password_parse_errors(smp_client: SmpClient, fake_node: FakeNode,
                                            password: str) -> None:
    doc = {"schema": 1, "device": {"instance": 1001, "name": "n"},
           "bacnet": {"password": password.replace("\\t", "\t")}}
    await _upload_json(smp_client, "/lfs/cfg/device.json.new", doc)
    with pytest.raises(SmpError) as exc:
        await smp_client.node_reload("device")
    assert exc.value.rc_name == "INVALID"
    assert "/lfs/cfg/device.json.new" not in fake_node.files


async def test_password_services(fake_node: FakeNode, smp_client: SmpClient,
                                 bacnet_client: BacnetClient) -> None:
    from bacnet_uc_harness.errors import BacnetError

    addr = fake_node.bacnet_address
    # no password configured: refused (CONFIG_UC_BACNET_REQUIRE_PASSWORD=y)
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.device_communication_control(addr, "disable", password="pw")
    assert (exc.value.error_class_name, exc.value.error_code_name) == (
        "security", "password-failure")
    await _upload_json(smp_client, "/lfs/cfg/device.json", {
        "schema": 1, "device": {"instance": 1001, "name": "n"},
        "bacnet": {"password": "S3cret pw"}})
    assert await smp_client.node_reload("device") is False  # applies without reboot
    with pytest.raises(BacnetError):
        await bacnet_client.device_communication_control(addr, "disable", password="wrong")
    with pytest.raises(BacnetError):
        await bacnet_client.reinitialize_device(addr, "warmstart")
    await bacnet_client.device_communication_control(addr, "disable", 5, password="S3cret pw")
    assert fake_node.dcc_state == "disable"
    await bacnet_client.reinitialize_device(addr, "warmstart", password="S3cret pw")
    assert fake_node.reinitialized == ["warmstart"]
    await asyncio.sleep(0.05)
    assert fake_node.resets == 1  # restarted after the SimpleACK
    with pytest.raises(HarnessError):
        await bacnet_client.reinitialize_device(addr, "reboot-now")
    # without REQUIRE_PASSWORD an unconfigured password lets everything through
    open_node = FakeNode(require_password=False)
    open_node._check_password(None)


async def test_reboot_required_relative_to_boot(fake_node: FakeNode,
                                                smp_client: SmpClient) -> None:
    base = {"schema": 1, "device": {"instance": 1001, "name": "n"}}
    await _upload_json(smp_client, "/lfs/cfg/device.json",
                       {**base, "network": {"dhcp": False, "ipv4": "10.0.0.5"}})
    assert await smp_client.node_reload("device") is True
    # back to what the node booted with (DHCP): no reboot needed any more
    await _upload_json(smp_client, "/lfs/cfg/device.json", base)
    assert await smp_client.node_reload("device") is False
    # static bindings, APDU settings and the password apply at once
    await _upload_json(smp_client, "/lfs/cfg/device.json", {**base, "bacnet": {
        "apdu_retries": 1, "password": "p",
        "static_bindings": [{"device": 7, "address": "10.0.0.7"}]}})
    assert await smp_client.node_reload("device") is False
    await _upload_json(smp_client, "/lfs/cfg/device.json",
                       {**base, "bacnet": {"udp_port": 47809}})
    assert await smp_client.node_reload("device") is True


async def test_apps_json_parse_errors(fake_node: FakeNode, smp_client: SmpClient,
                                      wasm_module: bytes) -> None:
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    entry = {"name": "a", "file": "/lfs/apps/a.wasm"}
    for doc in ({"schema": 1},  # "apps" is required
                {"schema": 1, "apps": [entry] * 2},  # duplicate name
                {"schema": 1, "apps": [{**entry, "name": f"a{i}"} for i in range(5)]},
                {"schema": 1, "apps": [{**entry, "sha256": "zz"}]},
                {"schema": 1, "apps": [{**entry, "perms": ["io", "io"]}]}):
        await _upload_json(smp_client, "/lfs/cfg/apps.json.new", doc)
        with pytest.raises(SmpError) as exc:
            await smp_client.node_reload("apps")
        assert exc.value.rc_name == "INVALID", doc
    assert fake_node.apps == {}
