# SPDX-License-Identifier: Apache-2.0
"""SmpClient + UdpTransport against the fake node."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import zlib

import pytest

from bacnet_uc_harness.errors import HarnessError, HarnessTimeout, SmpError
from bacnet_uc_harness.smp import groups as g
from bacnet_uc_harness.smp.client import SmpClient, connect_udp, normalize_manifest
from bacnet_uc_harness.smp.codec import decode_frame, encode_frame
from bacnet_uc_harness.smp.transport import UdpTransport
from bacnet_uc_harness.testing import FakeNode


async def test_os_group(smp_client: SmpClient, fake_node: FakeNode) -> None:
    assert await smp_client.os_echo("hello") == "hello"
    params = await smp_client.mcumgr_params()
    assert params == {"buf_size": 1152, "buf_count": 2}
    info = await smp_client.os_info()
    assert info == "Zephyr unknown fake 4.4.2 posix x86_64 native_sim/native/64 Zephyr"
    assert await smp_client.os_info("s") == "Zephyr"
    await smp_client.os_reset()
    assert fake_node.resets == 1
    await smp_client.os_reset(force=True)
    assert fake_node.resets == 2


async def test_fs_upload_download_multi_chunk(smp_client: SmpClient, fake_node: FakeNode) -> None:
    data = bytes((i * 31) & 0xFF for i in range(5000))
    progress: list[tuple[int, int]] = []
    await smp_client.fs_upload(
        "/lfs/apps/big.bin", data, progress=lambda d, t: progress.append((d, t))
    )
    assert fake_node.files["/lfs/apps/big.bin"] == data
    # several chunks, each frame within the 1024 byte MTU
    assert len(progress) >= 5 and progress[-1] == (5000, 5000)
    uploads = [e for e in fake_node.smp_log if e[1:] == (g.GROUP_FS, g.FS_FILE)]
    assert len(uploads) == len(progress)
    assert await smp_client.fs_status("/lfs/apps/big.bin") == 5000
    assert await smp_client.fs_hash("/lfs/apps/big.bin") == hashlib.sha256(data).digest()
    crc = await smp_client.fs_hash("/lfs/apps/big.bin", "crc32")
    assert crc == zlib.crc32(data).to_bytes(4, "big")
    assert await smp_client.fs_download("/lfs/apps/big.bin") == data
    await smp_client.fs_close()


async def test_fs_upload_async_progress_and_empty_file(
    smp_client: SmpClient, fake_node: FakeNode
) -> None:
    seen: list[int] = []

    async def progress(done: int, total: int) -> None:
        seen.append(done)

    await smp_client.fs_upload("/lfs/data/empty", b"", progress=progress)
    assert fake_node.files["/lfs/data/empty"] == b""
    assert seen == [0]
    # overwrite shrinks the file
    fake_node.files["/lfs/data/x"] = b"0123456789"
    await smp_client.fs_upload("/lfs/data/x", b"ab")
    assert fake_node.files["/lfs/data/x"] == b"ab"


async def test_fs_errors(smp_client: SmpClient) -> None:
    with pytest.raises(SmpError) as exc:
        await smp_client.fs_status("/lfs/nope")
    assert (exc.value.group, exc.value.rc, exc.value.rc_name) == (8, 3, "FILE_NOT_FOUND")
    with pytest.raises(SmpError) as exc:
        await smp_client.fs_download("/lfs/nope")
    assert exc.value.rc_name == "FILE_NOT_FOUND"
    with pytest.raises(SmpError) as exc:
        await smp_client.fs_upload("/other/x", b"1")
    assert exc.value.rc_name == "MOUNT_POINT_NOT_FOUND"


async def test_legacy_rc_and_unknown_group(smp_client: SmpClient) -> None:
    with pytest.raises(SmpError) as exc:
        await smp_client.read(99, 0, {})
    assert (exc.value.group, exc.value.rc, exc.value.rc_name) == (None, 8, "ENOTSUP")
    with pytest.raises(SmpError) as exc:  # write-only command read
        await smp_client.read(g.GROUP_OS, g.OS_RESET, {})
    assert exc.value.rc == g.MGMT_ERR_ENOTSUP


async def test_shell_exec(smp_client: SmpClient, fake_node: FakeNode) -> None:
    assert await smp_client.shell_exec(["kernel", "version"]) == (0, "\r\nZephyr version 4.4.2\r\n")
    ret, out = await smp_client.shell_exec(["bogus"])
    assert ret == -8 and "not found" in out
    fake_node.files["/lfs/cfg/io.json"] = b"{}"
    ret, out = await smp_client.shell_exec(["fs", "ls", "/lfs"])
    assert ret == 0 and "cfg" in out.split()


async def test_retry_same_sequence(fake_node: FakeNode) -> None:
    client = await connect_udp(fake_node.host, fake_node.smp_port, timeout=0.2)
    try:
        fake_node.drop_next_smp(2)
        assert await client.os_echo("retry") == "retry"
        assert len(fake_node.smp_log) == 1  # two dropped, one answered
        fake_node.drop_next_smp(10)
        with pytest.raises(HarnessTimeout) as exc:
            await client.os_echo("lost")
        assert isinstance(exc.value, TimeoutError)
        assert fake_node._drop_smp == 6  # 1 + 3 retries consumed
    finally:
        await client.close()


async def test_timeout_without_node() -> None:
    # a bound socket that never answers
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    try:
        client = SmpClient(
            UdpTransport("127.0.0.1", sock.getsockname()[1]), timeout=0.05, retries=1
        )
        with pytest.raises(HarnessTimeout):
            await client.os_echo("x")
        await client.close()
    finally:
        sock.close()


async def test_concurrent_requests_are_serialised(fake_node: FakeNode) -> None:
    client = await connect_udp(fake_node.host, fake_node.smp_port, timeout=1.0)
    try:
        results = await asyncio.gather(
            client.os_echo("a"), client.os_echo("b"), client.io_read("ai0"), client.os_echo("c")
        )
        assert results == ["a", "b", {"ai0": 2000.0}, "c"]
    finally:
        await client.close()


class _NoisyServer(asyncio.DatagramProtocol):
    """Answers every request with a stale response (previous sequence
    number) and a request-op echo before the real response."""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        hdr, req = decode_frame(data)
        stale = encode_frame(hdr.op + 1, hdr.group, hdr.cmd, (hdr.seq - 1) & 0xFF, {"r": "old"})
        echo = encode_frame(hdr.op, hdr.group, hdr.cmd, hdr.seq, req)
        real = encode_frame(hdr.op + 1, hdr.group, hdr.cmd, hdr.seq, {"r": req["d"]})
        for frame in (stale, echo, b"\x01\x02", real):
            self.transport.sendto(frame, addr)  # type: ignore[attr-defined]


async def test_stale_and_foreign_frames_are_ignored() -> None:
    loop = asyncio.get_running_loop()
    server, _ = await loop.create_datagram_endpoint(_NoisyServer, local_addr=("127.0.0.1", 0))
    port = server.get_extra_info("sockname")[1]
    client = await connect_udp("127.0.0.1", port, timeout=1.0)
    try:
        for text in ("one", "two", "three"):
            assert await client.os_echo(text) == text
    finally:
        await client.close()
        server.close()


async def test_mtu_limit() -> None:
    transport = UdpTransport("127.0.0.1", 9, mtu=64)
    client = SmpClient(transport, timeout=0.1)
    with pytest.raises(HarnessError):
        await client.os_echo("x" * 100)
    await client.close()


# --- uc_app ------------------------------------------------------------------------------------


async def test_app_lifecycle(
    smp_client: SmpClient, fake_node: FakeNode, wasm_module: bytes
) -> None:
    await smp_client.fs_upload("/lfs/apps/pid.wasm", wasm_module)
    manifest = {
        "name": "pid",
        "file": "/lfs/apps/pid.wasm",
        "period_ms": 100,
        "perms": ["bacnet.local", "io"],
        "params": [{"key": "setpoint", "value": "21.5"}],
        "sha256": hashlib.sha256(wasm_module).hexdigest(),
    }
    await smp_client.app_install(manifest)
    status = await smp_client.app_status("pid")
    assert status["state"] == "running"
    assert status["perms"] == ["bacnet.local", "io"]
    assert set(status) == {
        "name",
        "file",
        "state",
        "autostart",
        "period_ms",
        "heap_kb",
        "stack_kb",
        "perms",
        "ticks",
        "events",
        "errors",
        "last_error",
        "uptime_ms",
    }
    apps_json = json.loads(fake_node.files["/lfs/cfg/apps.json"])
    assert apps_json["apps"][0]["params"] == [{"key": "setpoint", "value": "21.5"}]
    assert apps_json["apps"][0]["sha256"] == manifest["sha256"]
    assert [a["name"] for a in await smp_client.app_list()] == ["pid"]

    # installing a running app again needs restart
    with pytest.raises(SmpError) as exc:
        await smp_client.app_install(manifest)
    assert exc.value.rc_name == "STATE"
    await smp_client.app_install({**manifest, "restart": True})

    with pytest.raises(SmpError) as exc:
        await smp_client.app_start("pid")
    assert exc.value.rc_name == "STATE"
    await smp_client.app_stop("pid")
    assert (await smp_client.app_status("pid"))["state"] == "stopped"
    await smp_client.app_stop("pid")  # idempotent
    await smp_client.app_start("pid")
    await smp_client.app_remove("pid", delete_file=True)
    assert "/lfs/apps/pid.wasm" not in fake_node.files
    assert await smp_client.app_list() == []
    with pytest.raises(SmpError) as exc:
        await smp_client.app_status("pid")
    assert (exc.value.group, exc.value.rc_name) == (64, "NOT_FOUND")


@pytest.mark.parametrize(
    ("manifest", "rc_name"),
    [
        ({"name": "Bad Name", "file": "/lfs/apps/a.wasm"}, "INVALID"),
        ({"name": "a", "file": "/tmp/a.wasm"}, "INVALID"),
        ({"name": "a", "file": "/lfs/apps/a.wasm", "perms": ["root"]}, "INVALID"),
        ({"name": "a", "file": "/lfs/apps/a.wasm", "stack_kb": 0}, "INVALID"),
        ({"name": "a", "file": "/lfs/apps/missing.wasm"}, "NOT_FOUND"),
        ({"name": "a", "file": "/lfs/apps/a.wasm", "sha256": "00" * 32}, "VERIFY"),
        ({"name": "a", "file": "/lfs/apps/junk.wasm"}, "VERIFY"),
    ],
)
async def test_app_install_errors(
    smp_client: SmpClient, fake_node: FakeNode, wasm_module: bytes, manifest: dict, rc_name: str
) -> None:
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    fake_node.files["/lfs/apps/junk.wasm"] = b"\x7fELF" + b"\x00" * 60
    with pytest.raises(SmpError) as exc:
        await smp_client.app_install(manifest)
    assert exc.value.rc_name == rc_name
    assert exc.value.group == g.GROUP_UC_APP


async def test_app_limit(smp_client: SmpClient, fake_node: FakeNode, wasm_module: bytes) -> None:
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    for i in range(4):
        await smp_client.app_install(
            {"name": f"a{i}", "file": "/lfs/apps/a.wasm", "autostart": False}
        )
    with pytest.raises(SmpError) as exc:
        await smp_client.app_install({"name": "a4", "file": "/lfs/apps/a.wasm"})
    assert exc.value.rc_name == "LIMIT"
    assert all(a["state"] == "stopped" for a in await smp_client.app_list())


def test_normalize_manifest() -> None:
    req = normalize_manifest(
        {"name": "x", "params": [{"key": "a", "value": 1}], "sha256": "ab" * 32}
    )
    assert req["params"] == {"a": "1"}
    assert req["sha256"] == bytes([0xAB]) * 32
    assert normalize_manifest({"params": {"a": 2}})["params"] == {"a": "2"}
    with pytest.raises(HarnessError):
        normalize_manifest({"sha256": "xyz"})


# --- uc_io -------------------------------------------------------------------------------------


async def test_io_group(smp_client: SmpClient) -> None:
    cat = await smp_client.io_catalog()
    assert cat["board"] == "native_sim" and cat["total"] == 8
    names = [c["name"] for c in cat["channels"]]
    assert names == ["di0", "di1", "do0", "do1", "ai0", "ai1", "ao0", "ao1"]
    assert cat["channels"][4] == {
        "id": 4,
        "name": "ai0",
        "desc": "Simulated analog input 0 (mV)",
        "kind": "ai",
        "hw": "sim",
        "forced": False,
    }
    values = await smp_client.io_read()
    assert values["ai0"] == 2000.0 and values["do0"] == 0.0
    await smp_client.io_write("do0", 1)
    await smp_client.io_write("ao0", 150)  # clamped to 100 %
    assert await smp_client.io_read("ao0") == {"ao0": 100.0}
    assert (await smp_client.io_read())["do0"] == 1.0
    with pytest.raises(SmpError) as exc:
        await smp_client.io_write("di0", 1)
    assert exc.value.rc_name == "PERM"
    with pytest.raises(SmpError) as exc:
        await smp_client.io_read("zz9")
    assert exc.value.rc_name == "NOT_FOUND"
    await smp_client.io_force("di0", 5)  # normalised to 1
    assert await smp_client.io_read("di0") == {"di0": 1.0}
    cat = await smp_client.io_catalog()
    assert cat["channels"][0]["forced"] is True
    await smp_client.io_release("di0")
    # a simulated input keeps the forced value after release
    assert await smp_client.io_read("di0") == {"di0": 1.0}
    await smp_client.io_force("do0", 0)
    assert await smp_client.io_read("do0") == {"do0": 0.0}
    await smp_client.io_release("do0")
    assert await smp_client.io_read("do0") == {"do0": 1.0}  # commanded value again


# --- uc_node -----------------------------------------------------------------------------------


async def test_node_info(smp_client: SmpClient, fake_node: FakeNode) -> None:
    info = await smp_client.node_info()
    assert info["board"] == "native_sim/native/64"
    assert info["api"] == 0x10000
    assert info["device"] == {"instance": 1001, "name": "fake-node"}
    assert info["net"]["bacnet_port"] == fake_node.bacnet_port
    assert set(info) == {
        "fw",
        "board",
        "api",
        "device",
        "net",
        "uptime_s",
        "fs",
        "bacnet",
        "apps",
        "wasm",
    }


async def test_node_reload_io_and_objects(
    smp_client: SmpClient, fake_node: FakeNode, repo_root
) -> None:  # type: ignore[no-untyped-def]
    io_json = (repo_root / "schemas/examples/io.json").read_bytes()
    await smp_client.fs_upload("/lfs/cfg/io.json", io_json)
    assert await smp_client.node_reload("io") is False
    page = await smp_client.node_objects(count=2)
    assert page["total"] == 6  # device, network port, 4 points
    assert len(page["objects"]) == 2
    objs = await smp_client.node_objects_all()
    # sorted by type and instance, like the firmware
    assert [(o["type"], o["instance"]) for o in objs] == [
        ("analog-input", 1),
        ("analog-output", 1),
        ("binary-input", 1),
        ("binary-output", 1),
        ("device", 1001),
        ("network-port", 1),
    ]
    ai = objs[0]
    assert ai["owner"] == "io" and ai["name"] == "Room Temperature"
    assert ai["pv"] == pytest.approx(2000 * 0.1 - 50.0)
    assert objs[4]["owner"] == "system" and "pv" not in objs[4]
    cat = await smp_client.io_catalog()
    assert cat["channels"][4]["object"] == {"type": "analog-input", "instance": 1}


async def test_prop_read_write(smp_client: SmpClient, fake_node: FakeNode) -> None:
    fake_node.add_object("analog-value", 5, "Setpoint", pv=20.0)
    fake_node.add_object("binary-value", 2, "Enable")
    assert await smp_client.prop_read("analog-value", 5) == 20.0
    assert await smp_client.prop_read("av", 5, "object-name") == "Setpoint"
    assert await smp_client.prop_read(2, 5, 85) == 20.0
    await smp_client.prop_write("analog-value", 5, "present-value", 22.5, priority=8)
    assert await smp_client.prop_read("analog-value", 5) == 22.5
    assert await smp_client.prop_read("analog-value", 5, "priority-array", index=8) == 22.5
    assert await smp_client.prop_read("analog-value", 5, "priority-array", index=0) == 16
    await smp_client.prop_write("analog-value", 5, "present-value", None, priority=8)
    assert await smp_client.prop_read("analog-value", 5) == 20.0
    await smp_client.prop_write("binary-value", 2, "present-value", 1)
    assert await smp_client.prop_read("binary-value", 2) == 1
    await smp_client.prop_write("analog-value", 5, "description", "room")
    assert await smp_client.prop_read("analog-value", 5, "description") == "room"
    # like the firmware: an array read without index yields its first element
    assert await smp_client.prop_read("device", 1001, "object-list") == "(analog-value, 5)"
    assert await smp_client.prop_read("device", 1001, "object-list", index=0) == 4
    assert await smp_client.prop_read("device", 1001, "object-list", index=3) == "(device, 1001)"
    assert await smp_client.prop_read("analog-value", 5, "status-flags") == (
        "{false,false,false,false}"
    )
    with pytest.raises(SmpError) as exc:
        await smp_client.prop_read("analog-value", 99)
    assert (exc.value.group, exc.value.rc_name) == (66, "NOT_FOUND")
    with pytest.raises(SmpError) as exc:
        await smp_client.prop_write("analog-value", 5, "present-value", "text")
    assert exc.value.rc_name == "INVALID"
    with pytest.raises(SmpError) as exc:
        await smp_client.prop_write("analog-value", 5, "object-type", 1)
    assert exc.value.rc_name == "PERM"
    with pytest.raises(SmpError) as exc:
        await smp_client.prop_write("binary-value", 2, "present-value", 2)
    assert exc.value.rc_name == "INVALID"
    with pytest.raises(HarnessError):
        await smp_client.prop_read("no-such-type", 1)


async def test_img_group(smp_client: SmpClient, fake_node: FakeNode) -> None:
    images = await smp_client.img_state()
    assert images[0]["active"] and images[0]["confirmed"]
    fw = bytes(range(256)) * 20
    await smp_client.img_upload(fw)
    images = await smp_client.img_state()
    assert images[1]["slot"] == 1 and images[1]["hash"] == hashlib.sha256(fw).digest()
    images = await smp_client.img_test(images[1]["hash"])
    assert images[1]["pending"] and not images[1]["permanent"]
    await smp_client.img_confirm(images[1]["hash"])
    assert (await smp_client.img_state())[1]["permanent"]
    await smp_client.img_confirm()
    with pytest.raises(SmpError) as exc:
        await smp_client.img_test(b"\x00" * 32)
    assert exc.value.rc_name == "HASH_NOT_FOUND"
    await smp_client.img_erase()
    assert len(await smp_client.img_state()) == 1


async def test_context_manager(fake_node: FakeNode) -> None:
    async with await connect_udp(fake_node.host, fake_node.smp_port) as client:
        assert await client.os_echo("cm") == "cm"


async def test_paging(smp_client: SmpClient, fake_node: FakeNode, wasm_module: bytes) -> None:
    """io catalog, app list and object list follow the firmware's paging."""
    handlers = fake_node._smp_handlers
    cat_read = handlers[(g.GROUP_UC_IO, g.UC_IO_CATALOG)][0]
    app_read = handlers[(g.GROUP_UC_APP, g.UC_APP_LIST)][0]
    # the node returns at most 3 channels / 1 app per response
    handlers[(g.GROUP_UC_IO, g.UC_IO_CATALOG)] = (lambda req: cat_read({**req, "count": 3}), None)
    handlers[(g.GROUP_UC_APP, g.UC_APP_LIST)] = (lambda req: app_read({**req, "count": 1}), None)
    cat = await smp_client.io_catalog()
    assert [c["id"] for c in cat["channels"]] == list(range(8))
    fake_node.files["/lfs/apps/a.wasm"] = wasm_module
    for name in ("a", "b", "c"):
        await smp_client.app_install({"name": name, "file": "/lfs/apps/a.wasm"})
    assert [a["name"] for a in await smp_client.app_list()] == ["a", "b", "c"]
    for i in range(20):
        fake_node.add_object("binary-value", i)
    objects = await smp_client.node_objects_all()
    assert len(objects) == 22
    reads = [e for e in fake_node.smp_log if e[1:] == (g.GROUP_UC_NODE, g.UC_NODE_OBJECTS)]
    assert len(reads) == 3  # pages of 8
