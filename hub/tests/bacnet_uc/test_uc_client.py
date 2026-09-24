from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from typing import Any

import cbor2
import pytest

from uc_hub.core.errors import (
    DeviceError,
    DeviceTimeout,
    InvalidRequest,
    NotFound,
    Unsupported,
)
from uc_hub.drivers.bacnet_uc.api import GROUP_UC_NODE, NODE_INFO
from uc_hub.drivers.bacnet_uc.client import SmpNodeClient, parse_address
from uc_hub.drivers.bacnet_uc.smp import (
    OP_READ,
    OP_READ_RSP,
    decode_frame,
    encode_frame,
    response_op,
)
from uc_hub.sim import EXAMPLE_IO, SimFeatures, SimNetwork, SimNode

from .conftest import make_client

WASM = b"\0asm\x01\0\0\0" + bytes(64)


async def test_echo_and_info(client: SmpNodeClient, node: SimNode) -> None:
    assert await client.echo("hello") == "hello"
    info = await client.info()
    assert info["device"] == {"instance": 2041, "name": "r204-ctl"}
    assert info["board"] == "native_sim/native/64"
    assert info["api"] == 0x10000
    assert info["hwid"] == node.hwid and info["mac"] == node.mac
    assert info["fs"]["ready"] is True


async def test_mtu_from_mcumgr_params(net: SimNetwork) -> None:
    sim = SimNode(name="small", instance=7, mtu=256, network=net)
    await sim.start()
    c = make_client(sim)
    try:
        assert await c.mtu() == 256
    finally:
        await c.close()


async def test_mtu_default_without_mcumgr_params(net: SimNetwork) -> None:
    sim = SimNode(name="old", instance=8, network=net, features=SimFeatures(mcumgr_params=False))
    await sim.start()
    c = make_client(sim)
    try:
        assert await c.mtu() == 1024
    finally:
        await c.close()


async def test_upload_10k_with_small_mtu_and_hash(net: SimNetwork) -> None:
    sim = SimNode(name="small", instance=7, mtu=200, network=net)
    await sim.start()
    c = make_client(sim)
    data = os.urandom(10 * 1024)
    try:
        await c.file_upload("/lfs/apps/blob.bin", data)
        assert sim.fs["/lfs/apps/blob.bin"] == data
        assert sim.requests > 10 * 1024 // 200  # really chunked
        assert await c.file_sha256("/lfs/apps/blob.bin") == hashlib.sha256(data).digest()
        assert await c.file_download("/lfs/apps/blob.bin") == data
    finally:
        await c.close()


async def test_upload_without_hash_support_skips_verification(net: SimNetwork) -> None:
    sim = SimNode(name="nohash", instance=9, network=net, features=SimFeatures(fs_hash=False))
    await sim.start()
    c = make_client(sim)
    try:
        await c.file_upload("/lfs/cfg/io.json", b"{}")
        assert sim.fs["/lfs/cfg/io.json"] == b"{}"
        with pytest.raises(Unsupported):
            await c.file_sha256("/lfs/cfg/io.json")
    finally:
        await c.close()


async def test_upload_detects_corruption(client: SmpNodeClient, node: SimNode, monkeypatch: pytest.MonkeyPatch) -> None:
    original = node._fs_hash

    def corrupt(body: dict[str, Any]) -> dict[str, Any]:
        rsp = original(body)
        rsp["output"] = bytes(32)
        return rsp

    node._handlers[(8, 2)] = (corrupt, None)
    with pytest.raises(DeviceError, match="sha256"):
        await client.file_upload("/lfs/apps/x.wasm", WASM)


async def test_upload_resumes_after_lost_response(net: SimNetwork) -> None:
    sim = SimNode(name="lossy", instance=10, mtu=256, network=net)
    await sim.start()
    c = make_client(sim, timeout_s=0.2, retries=2)
    data = os.urandom(2000)
    original = sim.handle_datagram
    calls = 0

    def lose_third_response(raw: bytes) -> bytes | None:
        nonlocal calls
        calls += 1
        reply = original(raw)
        return None if calls == 3 else reply  # the node wrote the chunk, the answer is lost

    sim.handle_datagram = lose_third_response  # type: ignore[method-assign]
    try:
        await c.file_upload("/lfs/apps/lossy.bin", data)
        assert sim.fs["/lfs/apps/lossy.bin"] == data
    finally:
        await c.close()


async def test_empty_file_and_missing_file(client: SmpNodeClient, node: SimNode) -> None:
    await client.file_upload("/lfs/data/empty", b"")
    assert node.fs["/lfs/data/empty"] == b""
    assert await client.file_sha256("/lfs/data/empty") == hashlib.sha256(b"").digest()
    assert await client.file_download("/lfs/data/empty") == b""
    assert await client.file_sha256("/lfs/nope") is None
    with pytest.raises(NotFound):
        await client.file_download("/lfs/nope")
    with pytest.raises(NotFound):  # outside the /lfs mount point
        await client.file_upload("/tmp/x", b"1")
    with pytest.raises(InvalidRequest):
        await client.file_upload("relative/path", b"1")


async def test_reload_io_and_objects_paging(client: SmpNodeClient, node: SimNode) -> None:
    doc = {"schema": 1, "points": [
        {"channel": "ai1", "type": "analog-input", "instance": 5, "name": "Supply Temp",
         "units": "degrees-celsius", "scale": 0.1, "offset": -50},
        {"channel": "di1", "type": "binary-input", "instance": 5, "name": "Filter Alarm"},
    ]}
    await client.file_upload("/lfs/cfg/io.json", json.dumps(doc).encode())
    assert await client.reload("io") is False
    client.objects_page = 1
    objects = await client.objects()
    assert [(o["type"], o["instance"]) for o in objects] == [
        ("device", 2041), ("network-port", 1), ("analog-input", 5), ("binary-input", 5)]
    ai = objects[2]
    assert ai == {"type": "analog-input", "instance": 5, "name": "Supply Temp", "owner": "io",
                  "pv": pytest.approx(20.0)}
    with pytest.raises(InvalidRequest):
        await client.reload("everything")


async def test_reload_invalid_document(client: SmpNodeClient) -> None:
    await client.file_upload("/lfs/cfg/io.json", b'{"schema": 1, "points": [{"channel": 3}]}')
    with pytest.raises(DeviceError) as err:
        await client.reload("io")
    assert (err.value.rc, err.value.group) == (2, GROUP_UC_NODE)  # INVALID


async def test_reload_device_reports_reboot(client: SmpNodeClient, node: SimNode) -> None:
    doc = {"schema": 1, "device": {"instance": 2041, "name": "R204 Controller", "location": "Room 204"}}
    await client.file_upload("/lfs/cfg/device.json", json.dumps(doc).encode())
    assert await client.reload("device") is False
    assert (await client.info())["device"]["name"] == "R204 Controller"
    assert await client.prop_read("device", 2041, "location") == "Room 204"
    doc["device"]["instance"] = 2042
    await client.file_upload("/lfs/cfg/device.json", json.dumps(doc).encode())
    assert await client.reload("device") is True
    assert node.instance == 2041  # applied at the next boot
    await client.reset()
    assert (await client.info())["device"]["instance"] == 2042


async def test_prop_read_write_names_and_numbers(client: SmpNodeClient) -> None:
    assert await client.prop_read("analog-input", 1) == pytest.approx(20.0)
    assert await client.prop_read(0, 1, 85) == pytest.approx(20.0)
    assert await client.prop_read("analog-input", 1, "units") == 62
    assert await client.prop_read("analog-output", 1, "object-name") == "Valve Position"
    await client.prop_write("analog-output", 1, 42.5, priority=8)
    await client.prop_write(1, 1, 10.0, prop=85, priority=12)
    assert await client.prop_read("analog-output", 1) == 42.5
    assert await client.prop_read("analog-output", 1, "priority-array", index=0) == 16
    assert await client.prop_read("analog-output", 1, "priority-array", index=12) == 10.0
    await client.prop_write("analog-output", 1, None, priority=8)
    assert await client.prop_read("analog-output", 1) == 10.0
    await client.prop_write("analog-output", 1, None, priority=12)
    assert await client.prop_read("analog-output", 1) == 0.0  # relinquish default
    await client.prop_write("analog-output", 1, "Valve R204", prop="object-name")
    assert await client.prop_read("analog-output", 1, "object-name") == "Valve R204"


async def test_prop_errors(client: SmpNodeClient) -> None:
    with pytest.raises(NotFound):
        await client.prop_read("analog-value", 99)
    with pytest.raises(NotFound):
        await client.prop_read("binary-input", 1, "units")
    with pytest.raises(DeviceError) as err:  # inputs are read-only unless out of service
        await client.prop_write("analog-input", 1, 5.0)
    assert err.value.rc == 10  # PERM
    with pytest.raises(DeviceError) as err:
        await client.prop_write("binary-output", 1, 2)
    assert err.value.rc == 2  # INVALID
    with pytest.raises(InvalidRequest):
        await client.prop_write("analog-output", 1, 1.0, priority=17)
    with pytest.raises(InvalidRequest):
        await client.prop_read("nonsense-type", 1)


async def test_identify(client: SmpNodeClient, node: SimNode, clock: Any) -> None:
    await client.identify(5)
    assert node.identifying
    clock.advance(6)
    node.step()
    assert not node.identifying
    await client.identify(30)
    await client.identify(0)
    assert not node.identifying


async def test_identify_unsupported(net: SimNetwork) -> None:
    sim = SimNode(name="old", instance=11, network=net, features=SimFeatures(identify=False))
    await sim.start()
    c = make_client(sim)
    try:
        with pytest.raises(Unsupported):
            await c.identify(10)
    finally:
        await c.close()


async def test_io_commands(client: SmpNodeClient, node: SimNode) -> None:
    catalog = await client.io_catalog()
    assert catalog["board"] == "native_sim/native/64"
    by_name = {ch["name"]: ch for ch in catalog["channels"]}
    assert by_name["ai0"]["object"] == {"type": "analog-input", "instance": 1}
    assert by_name["ai0"]["hw"] == "sim" and by_name["ai0"]["kind"] == "ai"
    assert "object" not in by_name["ai1"]
    assert (await client.io_read("ai0")) == {"ai0": 700.0}
    assert set(await client.io_read()) == set(node.channels)
    await client.io_write("ao1", 55.0)
    assert (await client.io_read("ao1")) == {"ao1": 55.0}
    with pytest.raises(DeviceError) as err:
        await client.io_write("ai1", 1.0)
    assert err.value.rc == 10  # PERM for inputs
    with pytest.raises(NotFound):
        await client.io_read("zz9")
    await client.io_force("di0", 1)
    assert (await client.io_catalog())["channels"][0]["forced"] is True
    assert await client.prop_read("binary-input", 1) == 1
    await client.io_force("di0", None)
    assert await client.prop_read("binary-input", 1) == 0


async def test_app_commands(client: SmpNodeClient, node: SimNode) -> None:
    await client.file_upload("/lfs/apps/idle.wasm", WASM)
    manifest = {"name": "idle", "file": "/lfs/apps/idle.wasm", "period_ms": 500,
                "perms": ["bacnet.local"], "params": {"answer": 42, "on": True},
                "sha256": hashlib.sha256(WASM).hexdigest()}
    await client.app_install(manifest)
    apps = await client.app_list()
    assert [a["name"] for a in apps] == ["idle"]
    status = await client.app_status("idle")
    assert status["state"] == "running" and status["period_ms"] == 500
    stored = json.loads(node.fs["/lfs/cfg/apps.json"])["apps"][0]
    assert stored["params"] == [{"key": "answer", "value": "42"}, {"key": "on", "value": "true"}]
    assert stored["sha256"] == hashlib.sha256(WASM).hexdigest()
    with pytest.raises(DeviceError) as err:  # running, no restart
        await client.app_install(manifest)
    assert err.value.rc == 8
    await client.app_install({**manifest, "restart": True})
    await client.app_stop("idle")
    assert (await client.app_status("idle"))["state"] == "stopped"
    await client.app_start("idle")
    assert (await client.app_status("idle"))["state"] == "running"
    await client.app_remove("idle", delete_file=True)
    assert await client.app_list() == []
    assert "/lfs/apps/idle.wasm" not in node.fs
    with pytest.raises(NotFound):
        await client.app_status("idle")


async def test_app_install_validation(client: SmpNodeClient) -> None:
    with pytest.raises(NotFound):
        await client.app_install({"name": "x", "file": "/lfs/apps/missing.wasm"})
    await client.file_upload("/lfs/apps/bad.wasm", b"not wasm")
    with pytest.raises(DeviceError) as err:
        await client.app_install({"name": "x", "file": "/lfs/apps/bad.wasm"})
    assert err.value.rc == 9  # VERIFY: magic
    await client.file_upload("/lfs/apps/ok.wasm", WASM)
    with pytest.raises(DeviceError) as err:
        await client.app_install({"name": "x", "file": "/lfs/apps/ok.wasm", "sha256": bytes(32)})
    assert err.value.rc == 9  # VERIFY: sha256
    with pytest.raises(DeviceError) as err:
        await client.app_install({"name": "Bad Name", "file": "/lfs/apps/ok.wasm"})
    assert err.value.rc == 2
    with pytest.raises(InvalidRequest):
        await client.app_install({"name": "x"})
    with pytest.raises(InvalidRequest):
        await client.app_install({"name": "x", "file": "/lfs/apps/ok.wasm", "sha256": "zz"})


async def test_retries_against_dropping_node(net: SimNetwork, node: SimNode) -> None:
    c = make_client(node, timeout_s=0.1, retries=3)
    try:
        node.drop = 2
        assert await c.echo("x") == "x"
        assert node.dropped == 2
    finally:
        await c.close()


async def test_timeout_after_retries(node: SimNode) -> None:
    c = make_client(node, timeout_s=0.1, retries=1)
    node.online = False
    try:
        start = time.monotonic()
        with pytest.raises(DeviceTimeout, match="2 attempt"):
            await c.info()
        assert 0.18 <= time.monotonic() - start < 3.0
        node.online = True
        assert (await c.info())["device"]["instance"] == 2041
    finally:
        await c.close()


async def test_concurrent_requests_are_matched(client: SmpNodeClient) -> None:
    texts = [f"msg-{i}" for i in range(40)]
    assert await asyncio.gather(*(client.echo(t) for t in texts)) == texts


async def test_cancellation_cleans_up(node: SimNode) -> None:
    c = make_client(node, timeout_s=5.0, retries=0)
    node.online = False
    try:
        task = asyncio.create_task(c.info())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert c._pending == {}
        node.online = True
        assert await c.echo("still works") == "still works"
    finally:
        await c.close()


async def test_closed_client_fails_pending_requests(node: SimNode) -> None:
    c = make_client(node, timeout_s=5.0, retries=0)
    node.online = False
    task = asyncio.create_task(c.info())
    await asyncio.sleep(0.05)
    await c.close()
    with pytest.raises(DeviceError, match="closed"):
        await task
    with pytest.raises(DeviceError, match="closed"):
        await c.echo("x")


class _ScriptedServer(asyncio.DatagramProtocol):
    """Answers each request with the output of ``script(frame_bytes)``."""

    def __init__(self, script: Any) -> None:
        self.script = script
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: Any) -> None:
        assert self.transport is not None
        for reply in self.script(data):
            self.transport.sendto(reply, addr)


async def _scripted(script: Any) -> tuple[asyncio.DatagramTransport, int]:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _ScriptedServer(script), local_addr=("127.0.0.1", 0)
    )
    return transport, transport.get_extra_info("sockname")[1]


async def test_malformed_and_mismatched_responses_are_ignored() -> None:
    def script(data: bytes) -> list[bytes]:
        req = decode_frame(data)
        good = encode_frame(response_op(req.op), req.group, req.command, req.seq, {"r": "ok"})
        return [
            b"\x09garbage",
            encode_frame(response_op(req.op), req.group, req.command + 1, req.seq, {"r": "wrong cmd"}),
            encode_frame(response_op(req.op), req.group, req.command, (req.seq + 1) & 0xFF, {"r": "other seq"}),
            good,
        ]

    transport, port = await _scripted(script)
    c = SmpNodeClient("127.0.0.1", port, timeout_s=0.5, retries=0)
    try:
        assert await c.echo("x") == "ok"
    finally:
        await c.close()
        transport.close()


async def test_malformed_response_body() -> None:
    def script(data: bytes) -> list[bytes]:
        req = decode_frame(data)
        return [encode_frame(response_op(req.op), req.group, req.command, req.seq, {"nothing": 1})]

    transport, port = await _scripted(script)
    c = SmpNodeClient("127.0.0.1", port, timeout_s=0.5, retries=0)
    try:
        with pytest.raises(DeviceError, match="malformed"):
            await c.objects()
        with pytest.raises(DeviceError, match="malformed"):
            await c.prop_read("analog-input", 1)
    finally:
        await c.close()
        transport.close()


async def test_late_answer_to_first_attempt_counts() -> None:
    requests: list[bytes] = []

    def script(data: bytes) -> list[bytes]:
        requests.append(data)
        if len(requests) < 2:
            return []
        req = decode_frame(requests[0])  # answer the first attempt only now
        return [encode_frame(response_op(req.op), req.group, req.command, req.seq, {"r": "late"})]

    transport, port = await _scripted(script)
    c = SmpNodeClient("127.0.0.1", port, timeout_s=0.1, retries=2)
    try:
        assert await c.echo("x") == "late"
        assert requests[0] == requests[1]  # the retry reuses the sequence number
    finally:
        await c.close()
        transport.close()


async def test_v1_request_gets_legacy_errors(node: SimNode) -> None:
    loop = asyncio.get_running_loop()
    got: asyncio.Future[bytes] = loop.create_future()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: Any) -> None:
            if not got.done():
                got.set_result(data)

    transport, _ = await loop.create_datagram_endpoint(Proto, remote_addr=("127.0.0.1", node.port))
    try:
        body = {"type": "analog-value", "instance": 99, "prop": "present-value"}
        transport.sendto(encode_frame(OP_READ, GROUP_UC_NODE, 3, 5, body, version=0))
        frame = decode_frame(await asyncio.wait_for(got, 2.0))
        assert frame.version == 0 and frame.op == OP_READ_RSP
        assert frame.body["rc"] == 5  # NOT_FOUND translated to MGMT_ERR_ENOENT
    finally:
        transport.close()


async def test_node_rejects_oversized_and_unknown_requests(node: SimNode) -> None:
    big = encode_frame(2, 0, 0, 1, {"d": "x" * 2000})
    reply = node.handle_datagram(big)
    assert reply is not None and cbor2.loads(reply[8:]) == {"rc": 7, "rsn": "request exceeds the 1024 byte buffer"}
    reply = node.handle_datagram(encode_frame(OP_READ, 77, 0, 2, {}))
    assert reply is not None and cbor2.loads(reply[8:]) == {"rc": 8}
    reply = node.handle_datagram(encode_frame(OP_READ, GROUP_UC_NODE, NODE_INFO, 3, {}, version=2))
    assert reply is not None and cbor2.loads(reply[8:]) == {"rc": 13}


def test_parse_address() -> None:
    assert parse_address("10.0.2.51") == ("10.0.2.51", 1337)
    assert parse_address("10.0.2.51:4000") == ("10.0.2.51", 4000)
    assert parse_address("[fe80::1]:1337") == ("fe80::1", 1337)
    for bad in ("", ":1", "host:0", "host:x", "[::1"):
        with pytest.raises(InvalidRequest):
            parse_address(bad)


def test_wire_manifest_conversions() -> None:
    body = SmpNodeClient.wire_manifest({
        "name": "t", "file": "/lfs/apps/t.wasm", "params": [{"key": "a", "value": 1.5}],
        "sha256": "00" * 32,
    })
    assert body["params"] == {"a": "1.5"}
    assert body["sha256"] == bytes(32)


async def test_client_rejects_bad_settings() -> None:
    with pytest.raises(ValueError):
        SmpNodeClient("127.0.0.1", 1, timeout_s=0)
    with pytest.raises(ValueError):
        SmpNodeClient("127.0.0.1", 1, mtu=10)
    c = SmpNodeClient.from_settings("127.0.0.1", 1337, {"timeout_s": 0.3, "retries": 0, "mtu": 512})
    assert (c.timeout_s, c.retries) == (0.3, 0)
    assert await c.mtu() == 512


async def test_example_io_matches_firmware_examples() -> None:
    from pathlib import Path

    example = json.loads((Path(__file__).parents[1] / "example_io.json").read_text())
    assert [dict(p) for p in EXAMPLE_IO] == example["points"]
