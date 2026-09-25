# SPDX-License-Identifier: Apache-2.0
"""BacnetClient against the fake node's BACnet/IP responder."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable

import pytest

from bacnet_uc_harness.bacnet import codec, enums
from bacnet_uc_harness.bacnet.client import BacnetClient, encode_property_value, parse_address
from bacnet_uc_harness.bacnet.codec import BitString, Enumerated, ObjectId
from bacnet_uc_harness.errors import BacnetError, HarnessError, HarnessTimeout
from bacnet_uc_harness.testing import FakeNode


async def test_who_is_unicast(bacnet_client: BacnetClient, fake_node: FakeNode) -> None:
    found = await bacnet_client.who_is(target=fake_node.bacnet_address, timeout=1.0)
    assert len(found) == 1
    iam = found[0]
    assert iam.device == 1001
    assert iam.address == fake_node.bacnet_address
    assert (iam.max_apdu, iam.segmentation, iam.vendor_id) == (1476, 3, 260)
    assert bacnet_client.iams[1001] == iam
    # a matching single-device query returns as soon as the I-Am arrives
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    found = await bacnet_client.who_is(1001, 1001, timeout=5.0, target=fake_node.bacnet_address)
    assert [i.device for i in found] == [1001]
    assert loop.time() - t0 < 1.0
    # out of range: the node stays silent
    assert await bacnet_client.who_is(5, 10, timeout=0.2, target=fake_node.bacnet_address) == []


async def test_who_is_several_nodes(
    bacnet_client: BacnetClient, make_fake_node: Callable[..., Awaitable[FakeNode]]
) -> None:
    nodes = [await make_fake_node(2000 + i) for i in range(3)]
    for node in nodes:
        found = await bacnet_client.who_is(target=f"127.0.0.1:{node.bacnet_port}", timeout=0.5)
        assert [i.device for i in found] == [node.device_instance]
    assert sorted(bacnet_client.iams) == [2000, 2001, 2002]


async def test_read_device_properties(bacnet_client: BacnetClient, fake_node: FakeNode) -> None:
    addr = fake_node.bacnet_address
    assert await bacnet_client.read_property(addr, "device", 1001, "object-name") == "fake-node"
    oid = await bacnet_client.read_property(addr, "device", 1001, "object-identifier")
    assert oid == (8, 1001) and isinstance(oid, ObjectId)
    otype = await bacnet_client.read_property(addr, "device", 1001, "object-type")
    assert otype == 8 and isinstance(otype, Enumerated)
    assert await bacnet_client.read_property(addr, "device", 1001, "protocol-version") == 1
    seg = await bacnet_client.read_property(addr, 8, 1001, "segmentation-supported")
    assert seg == enums.SEGMENTATION_NONE
    services = await bacnet_client.read_property(addr, 8, 1001, "protocol-services-supported")
    assert isinstance(services, BitString) and services[12] and services[15] and services[34]
    # wildcard device instance 4194303
    assert await bacnet_client.read_property(addr, "device", 4194303, "object-name") == (
        "fake-node"
    )


async def test_read_arrays(bacnet_client: BacnetClient, fake_node: FakeNode) -> None:
    addr = fake_node.bacnet_address
    fake_node.add_object("analog-value", 1, "Setpoint", pv=21.0)
    fake_node.add_object("multi-state-value", 3, "Mode", number_of_states=3)
    objects = await bacnet_client.read_property(addr, "device", 1001, "object-list")
    assert objects == [(2, 1), (8, 1001), (19, 3), (56, 1)]
    assert await bacnet_client.read_property(addr, "device", 1001, "object-list", index=0) == 4
    assert await bacnet_client.read_property(addr, "device", 1001, "object-list", index=3) == (
        19,
        3,
    )
    fake_node.add_object("analog-output", 2, "Valve")
    pa = await bacnet_client.read_property(addr, "analog-output", 2, "priority-array")
    assert pa == [None] * 16
    texts = await bacnet_client.read_property(addr, "multi-state-value", 3, "state-text")
    assert texts == ["state-1", "state-2", "state-3"]
    # an array with one element is still a list
    fake_node.objects[(19, 3)][enums.PROPERTIES["state-text"]] = ["only"]
    assert await bacnet_client.read_property(addr, "msv", 3, "state-text") == ["only"]
    flags = await bacnet_client.read_property(addr, "analog-value", 1, "status-flags")
    assert flags == (False, False, False, False)


async def test_write_commandable(bacnet_client: BacnetClient, fake_node: FakeNode) -> None:
    addr = fake_node.bacnet_address
    fake_node.add_object("analog-value", 1, "Setpoint", pv=21.0)
    fake_node.add_object("analog-output", 1, "Valve", pv=0.0)
    fake_node.add_object("binary-output", 2, "Relay")
    await bacnet_client.write_property(addr, "analog-output", 1, "present-value", 23, priority=8)
    assert await bacnet_client.read_property(addr, "analog-output", 1) == 23.0
    await bacnet_client.write_property(addr, "analog-output", 1, "present-value", 24.5)
    # priority 8 still wins over the default priority 16
    assert await bacnet_client.read_property(addr, "analog-output", 1) == 23.0
    pa = await bacnet_client.read_property(addr, "analog-output", 1, "priority-array")
    assert pa[7] == 23.0 and pa[15] == 24.5
    await bacnet_client.write_property(addr, "analog-output", 1, "present-value", None, priority=8)
    assert await bacnet_client.read_property(addr, "analog-output", 1) == 24.5
    with pytest.raises(BacnetError) as exc:  # priority 6: minimum on/off
        await bacnet_client.write_property(addr, "analog-output", 1, "present-value", 1, priority=6)
    assert exc.value.error_code_name == "write-access-denied"
    # value objects have no priority array: the last write wins, NULL does nothing
    await bacnet_client.write_property(addr, "analog-value", 1, "present-value", 23, priority=8)
    await bacnet_client.write_property(addr, "analog-value", 1, "present-value", 24.5)
    assert await bacnet_client.read_property(addr, "analog-value", 1) == 24.5
    await bacnet_client.write_property(addr, "analog-value", 1, "present-value", None, priority=8)
    assert await bacnet_client.read_property(addr, "analog-value", 1) == 24.5
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.read_property(addr, "analog-value", 1, "priority-array")
    assert exc.value.error_code_name == "unknown-property"
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.write_property(addr, "analog-value", 1, "present-value", 1, priority=6)
    assert exc.value.error_code_name == "write-access-denied"
    await bacnet_client.write_property(
        addr, "binary-output", 2, "present-value", "active", priority=1
    )
    assert await bacnet_client.read_property(addr, "binary-output", 2) == 1
    await bacnet_client.write_property(addr, "binary-output", 2, "present-value", None, priority=1)
    assert await bacnet_client.read_property(addr, "binary-output", 2) == 0
    await bacnet_client.write_property(addr, "analog-value", 1, "object-name", "SP 1")
    assert await bacnet_client.read_property(addr, "av", 1, "object-name") == "SP 1"
    await bacnet_client.write_property(addr, "analog-value", 1, "units", "degrees-celsius")
    assert await bacnet_client.read_property(addr, "av", 1, "units") == 62


async def test_errors(bacnet_client: BacnetClient, fake_node: FakeNode) -> None:
    addr = fake_node.bacnet_address
    fake_node.add_object("analog-input", 1, "Temp", pv=20.0)
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.read_property(addr, "analog-input", 99)
    assert (exc.value.kind, exc.value.error_class, exc.value.error_code) == ("error", 1, 31)
    assert exc.value.error_code_name == "unknown-object"
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.read_property(addr, "analog-input", 1, "state-text")
    assert exc.value.error_code_name == "unknown-property"
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.read_property(addr, "analog-input", 1, "present-value", index=1)
    assert exc.value.error_code_name == "property-is-not-an-array"
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.read_property(addr, "device", 1001, "object-list", index=50)
    assert exc.value.error_code_name == "invalid-array-index"
    # input Present_Value is writable only when out of service
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.write_property(addr, "analog-input", 1, "present-value", 5.0)
    assert exc.value.error_code_name == "write-access-denied"
    await bacnet_client.write_property(addr, "analog-input", 1, "out-of-service", True)
    await bacnet_client.write_property(addr, "analog-input", 1, "present-value", 5.0)
    assert await bacnet_client.read_property(addr, "analog-input", 1) == 5.0
    # wrong datatype (explicit tag)
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.write_property(
            addr, "analog-input", 1, "present-value", 5, tag="unsigned"
        )
    assert exc.value.error_code_name == "invalid-data-type"
    # unsupported confirmed service -> Reject
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.confirmed_request(addr, enums.CONFIRMED_SERVICES["read-range"], b"")
    assert (exc.value.kind, exc.value.reason) == ("reject", 9)
    # malformed request -> Reject
    with pytest.raises(BacnetError) as exc:
        await bacnet_client.confirmed_request(addr, 12, b"\x0c\x00")
    assert exc.value.kind == "reject"
    with pytest.raises(HarnessError):
        await bacnet_client.read_property(addr, "no-such-type", 1)


async def test_segmentation_fallback(fake_node: FakeNode) -> None:
    """With max-APDU 128 the object list does not fit: the node aborts with
    segmentation-not-supported and the client reads element by element."""
    for i in range(40):
        fake_node.add_object("analog-value", i, f"AV{i}")
    client = BacnetClient(local_host="127.0.0.1", apdu_timeout=1.0, max_apdu=128)
    await client.start()
    try:
        with pytest.raises(BacnetError) as exc:
            await client.read_property_ack(fake_node.bacnet_address, "device", 1001, "object-list")
        assert (exc.value.kind, exc.value.reason) == ("abort", 4)
        objects = await client.read_property(
            fake_node.bacnet_address, "device", 1001, "object-list"
        )
        assert len(objects) == 42
        assert objects[0] == (2, 0) and objects[39] == (2, 39) and objects[-1] == (56, 1)
    finally:
        await client.close()


async def test_concurrent_requests(
    bacnet_client: BacnetClient, make_fake_node: Callable[..., Awaitable[FakeNode]]
) -> None:
    nodes = [await make_fake_node(3000 + i) for i in range(4)]
    for n in nodes:
        for i in range(10):
            n.add_object("analog-value", i, f"n{n.device_instance}-{i}", pv=float(i))
    tasks = [
        bacnet_client.read_property(n.bacnet_address, "analog-value", i)
        for n in nodes
        for i in range(10)
    ]
    tasks += [
        bacnet_client.read_property(n.bacnet_address, "device", n.device_instance, "object-name")
        for n in nodes
    ]
    results = await asyncio.gather(*tasks)
    assert results[:40] == [float(i) for _ in nodes for i in range(10)]
    assert results[40:] == [f"fake-{n.device_instance}" for n in nodes]
    assert not bacnet_client._pending


async def test_timeout(bacnet_client: BacnetClient) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    try:
        bacnet_client.apdu_timeout = 0.1
        with pytest.raises(HarnessTimeout):
            await bacnet_client.read_property(sock.getsockname(), "device", 1, "object-name")
        # the request was sent retries + 1 times
        sock.setblocking(False)
        received = 0
        while True:
            try:
                sock.recv(1500)
                received += 1
            except BlockingIOError:
                break
        assert received == 2
    finally:
        sock.close()


async def test_iam_broadcast_mode(make_fake_node: Callable[..., Awaitable[FakeNode]]) -> None:
    """A node that broadcasts its I-Am (like the firmware) is seen by a client
    bound to the destination port."""
    listener = BacnetClient(local_host="127.0.0.1")
    await listener.start()
    port = listener.local_address[1]
    node = await make_fake_node(4000, iam_broadcast=("127.0.0.1", port))
    try:
        found = await listener.who_is(target=node.bacnet_address, timeout=1.0)
        assert [i.device for i in found] == [4000]
    finally:
        await listener.close()


async def test_forwarded_npdu_and_routed_i_am(bacnet_client: BacnetClient) -> None:
    """I-Am in a Forwarded-NPDU (from a BBMD) carries the original address;
    SNET/SADR of a routed device are kept."""
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        npdu = bytes.fromhex("01080005010a") + codec.i_am(777, 480, 0, 42)
        pkt = codec.encode_bvlc_forwarded(npdu, ("10.1.2.3", 47809))
        found_task = asyncio.create_task(
            bacnet_client.who_is(777, 777, timeout=1.0, target=("127.0.0.1", 9))
        )
        await asyncio.sleep(0.05)
        sender.sendto(pkt, bacnet_client.local_address)
        found = await found_task
        assert len(found) == 1
        iam = found[0]
        assert iam.address == ("10.1.2.3", 47809)
        assert (iam.network, iam.mac, iam.max_apdu, iam.segmentation, iam.vendor_id) == (
            5,
            b"\x0a",
            480,
            0,
            42,
        )
    finally:
        sender.close()


def test_encode_property_value() -> None:
    assert encode_property_value("analog-value", "present-value", 21) == codec.encode_value(21.0)
    assert encode_property_value("binary-value", "present-value", True) == (
        codec.encode_value(Enumerated(1))
    )
    assert encode_property_value("binary-value", "present-value", "inactive") == (
        codec.encode_value(Enumerated(0))
    )
    assert encode_property_value("multi-state-value", "present-value", 2) == b"\x21\x02"
    assert encode_property_value("analog-input", "out-of-service", 1) == b"\x11"
    assert encode_property_value("analog-input", "units", "percent") == b"\x91\x62"
    assert encode_property_value("analog-value", "present-value", None) == b"\x00"
    assert encode_property_value("multi-state-value", "state-text", ["a", "b"]) == (
        codec.encode_value("a") + codec.encode_value("b")
    )
    assert encode_property_value("device", "object-list", ("ai", 1)) == bytes.fromhex("c400000001")
    assert encode_property_value("device", "object-list", [("ai", 1), "av:2"]) == bytes.fromhex(
        "c400000001c400800002"
    )
    assert encode_property_value("schedule", "present-value", 3) == b"\x21\x03"
    with pytest.raises(HarnessError):
        encode_property_value("binary-value", "present-value", "maybe")


def test_parse_address() -> None:
    assert parse_address(("10.0.0.1", 47809)) == ("10.0.0.1", 47809)
    assert parse_address("10.0.0.1:47810") == ("10.0.0.1", 47810)
    assert parse_address("10.0.0.1") == ("10.0.0.1", 47808)
    assert parse_address(["h", "1"]) == ("h", 1)
