# SPDX-License-Identifier: Apache-2.0
"""Firmware limits of WebAssembly applications on a real native_sim node (see
conftest.py for the requirements): code run at instantiation, stopping apps
that block in host calls, printf output, kv wipe on remove, JSON string
escapes in configuration documents."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from bacnet_uc_harness.errors import SmpError
from bacnet_uc_harness.node import Node
from bacnet_uc_harness.planner import wait_reachable
from bacnet_uc_harness.sim import SimManager
from bacnet_uc_harness.wasm_build import build_c

pytestmark = pytest.mark.e2e
needs_clang = pytest.mark.skipif(shutil.which("clang") is None, reason="clang required")

BACNET = ("127.0.0.1", 47808)
UC_API_VERSION = 0x10000


@pytest.fixture
async def node(single_node: SimManager) -> AsyncIterator[Node]:
    n = Node("e2e", "udp:127.0.0.1:1337", f"{BACNET[0]}:{BACNET[1]}", smp_timeout=30.0,
             smp_retries=0)
    assert await wait_reachable(n, 30.0, 0.2), single_node.log_tail("e2e", 30)
    try:
        yield n
    finally:
        await n.close()


async def _eventually(read, check, timeout: float = 10.0, interval: float = 0.2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    value = await read()
    while not check(value) and loop.time() < deadline:
        await asyncio.sleep(interval)
        value = await read()
    return value


async def _remove_if_installed(node: Node, name: str) -> None:
    if await node.app_status(name) is not None:
        await node.remove_app(name, delete_file=True)


# --- hand-assembled modules --------------------------------------------------------------------

def _uleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _sleb(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if (n == 0 and not b & 0x40) or (n == -1 and b & 0x40):
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def _vec(items: list[bytes]) -> bytes:
    return _uleb(len(items)) + b"".join(items)


def _name(s: str) -> bytes:
    return _uleb(len(s)) + s.encode()


def _section(sid: int, body: bytes) -> bytes:
    return bytes([sid]) + _uleb(len(body)) + body


def _module(*, start: bool = False, export: str | None = None, body: str = "loop",
            log_import: bool = False) -> bytes:
    """uc_app_api_version plus one () -> () function: an endless loop or a
    uc_log call, run as start function and/or exported as ``export``."""
    types = [b"\x60\x00\x01\x7f", b"\x60\x00\x00", b"\x60\x03\x7f\x7f\x7f\x00"]
    imports = [_name("bacnet_uc") + _name("uc_log") + b"\x00" + _uleb(2)] if log_import else []
    base = len(imports)  # imported functions come first
    version_fn, extra_fn = base, base + 1
    if body == "loop":
        code = b"\x03\x40\x0c\x00\x0b\x0b"  # loop br 0 end end
    else:
        assert log_import
        code = b"\x41\x03\x41\x10\x41\x05\x10\x00\x0b"  # uc_log(3, 16, 5)
    bodies = [b"\x00\x41" + _sleb(UC_API_VERSION) + b"\x0b", b"\x00" + code]
    exports = [_name("uc_app_api_version") + b"\x00" + _uleb(version_fn)]
    if export:
        exports.append(_name(export) + b"\x00" + _uleb(extra_fn))
    out = b"\x00asm\x01\x00\x00\x00"
    out += _section(1, _vec(types))
    if imports:
        out += _section(2, _vec(imports))
    out += _section(3, _vec([_uleb(0), _uleb(1)]))
    out += _section(5, _vec([b"\x00\x01"]))  # memory, 1 page
    out += _section(7, _vec(exports))
    if start:
        out += _section(8, _uleb(extra_fn))
    out += _section(10, _vec([_uleb(len(b)) + b for b in bodies]))
    out += _section(11, _vec([b"\x00\x41\x10\x0b" + _vec([bytes([c]) for c in b"hello"])]))
    return out


@pytest.mark.parametrize("variant", [
    {"start": True},                                                  # start section: loop
    {"export": "__wasm_call_ctors"},                                  # ctor loop
    {"export": "__post_instantiate", "body": "log", "log_import": True},  # host call, no slot
    {"start": True, "body": "log", "log_import": True},
], ids=["start-loop", "ctors-loop", "post-instantiate-log", "start-log"])
async def test_code_at_instantiation_refused(node: Node, variant: dict) -> None:
    """Module code WAMR would run inside wasm_runtime_instantiate() (beyond the
    watchdog, the instruction budget and the host functions' slot) is refused:
    the start fails with VERIFY and the node keeps answering."""
    await _remove_if_installed(node, "init")
    with pytest.raises(SmpError) as exc:
        await node.deploy_app("init", _module(**variant), heap_kb=0)
    assert exc.value.rc_name == "VERIFY"
    status = await node.app_status("init")
    assert status is not None and status["state"] == "failed", status
    assert "runs at instantiation" in status["last_error"], status
    assert await node.echo("alive") == "alive"
    await node.remove_app("init", delete_file=True)


async def test_plain_module_runs(node: Node) -> None:
    """Control: the same hand-assembled module without that code runs."""
    await _remove_if_installed(node, "plain")
    res = await node.deploy_app("plain", _module(), heap_kb=0)
    assert res["status"]["state"] == "running", res
    await node.remove_app("plain", delete_file=True)


# --- modules built with the SDK --------------------------------------------------------------

def _build(tmp: Path, name: str, src: str, heap_kb: int = 0) -> bytes:
    path = tmp / f"{name}.c"
    path.write_text(src)
    return build_c([path], tmp / f"{name}.wasm", opt="-O2", heap_kb=heap_kb).path.read_bytes()


_BLOCK_LOOP = r"""
#include <bacnet_uc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
    double v;

    (void)now_ms;
    for (;;) {
        /* device 4000000 is never bound: every call blocks 20 s */
        (void)uc_remote_read(4000000u, UC_OBJ_ANALOG_VALUE, 1, UC_PROP_PRESENT_VALUE,
                     UC_ARRAY_ALL, &v, 20000);
    }
}
"""

_BLOCK_ONCE = r"""
#include <bacnet_uc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_tick) void uc_app_tick(uint64_t now_ms)
{
    double v;

    (void)now_ms;
    (void)uc_remote_read(4000000u, UC_OBJ_ANALOG_VALUE, 1, UC_PROP_PRESENT_VALUE,
                 UC_ARRAY_ALL, &v, 20000);
}
UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
    uc_log_str(UC_LOG_INF, "deinit called");
}
"""


_BLOCK_DEINIT = r"""
#include <bacnet_uc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_deinit) void uc_app_deinit(void)
{
    double v;

    /* blocking calls work again in uc_app_deinit(), under the watchdog */
    (void)uc_remote_read(4000000u, UC_OBJ_ANALOG_VALUE, 1, UC_PROP_PRESENT_VALUE,
                 UC_ARRAY_ALL, &v, 20000);
}
"""


@needs_clang
@pytest.mark.parametrize("name, src, states", [
    ("blk", _BLOCK_LOOP, ("stopped", "failed")),  # never returns by itself
    ("slow", _BLOCK_ONCE, ("stopped",)),           # returns once the call fails
    ("dnt", _BLOCK_DEINIT, ("stopped",)),          # deinit ended by the watchdog
], ids=["loop", "once", "deinit"])
async def test_stop_app_blocked_in_host_call(node: Node, single_node: SimManager,
                                            tmp_path: Path, name: str, src: str,
                                            states: tuple[str, ...]) -> None:
    """A stop cancels blocking host calls: an app blocked in (or looping on)
    uc_remote_read stops within the watchdog period instead of BUSY after
    12 s, and can be removed."""
    await _remove_if_installed(node, name)
    res = await node.deploy_app(name, _build(tmp_path, name, src), period_ms=100,
                                perms=["bacnet.remote"])
    assert res["status"]["state"] == "running", res
    await asyncio.sleep(1.0)  # inside the first tick, blocked in the request
    t0 = time.monotonic()
    status = await node.stop_app(name)
    elapsed = time.monotonic() - t0
    assert elapsed < 6.0, elapsed
    assert status["state"] in states, status
    assert await node.echo("alive") == "alive"
    if name == "dnt":
        assert "uc_app_deinit: watchdog" in status["last_error"], status
    if name == "slow":
        # deferred logging: the line reaches the console a little later
        tail = await _eventually(
            lambda: asyncio.sleep(0, "\n".join(single_node.log_tail("e2e", 300))),
            lambda t: "slow: deinit called" in t, 5.0)
        assert "slow: deinit called" in tail
    t0 = time.monotonic()
    await node.remove_app(name, delete_file=True)
    assert time.monotonic() - t0 < 3.0
    assert await node.app_status(name) is None


_PRINTF = r"""
#include <bacnet_uc.h>
#include <uc_libc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
    for (int i = 0; i < 30; i++) {
        printf("[00:00:01.000,000] <err> uc_bn_node: FORGED line %d\n", i);
    }
    return 0;
}
"""


@needs_clang
async def test_printf_output_is_app_log(node: Node, single_node: SimManager,
                                        tmp_path: Path) -> None:
    """libc-builtin printf output becomes tagged, rate-limited log lines of the
    app (like uc_log), never an untagged line that looks like a firmware log."""
    await _remove_if_installed(node, "pf")
    res = await node.deploy_app("pf", _build(tmp_path, "pf", _PRINTF, heap_kb=4), heap_kb=4)
    assert res["status"]["state"] == "running", res

    def forged() -> list[str]:
        return [ln for ln in single_node.log_tail("e2e", 2000) if "FORGED line" in ln]

    lines = await _eventually(lambda: asyncio.sleep(0, forged()), lambda v: len(v) >= 20, 5.0)
    assert lines, single_node.log_tail("e2e", 50)
    for ln in lines:
        assert re.search(r"<inf> uc_app: pf: \[00:00:01\.000,000\] <err> uc_bn_node: FORGED",
                         ln), ln
    assert len(lines) <= 20  # 20 lines per second and app
    await node.remove_app("pf", delete_file=True)


_KV_FLOOD = r"""
#include <bacnet_uc.h>
#include <uc_libc.h>
UC_APP_DECLARE()
UC_EXPORT(uc_app_init) int32_t uc_app_init(void)
{
    char key[8];
    int32_t rc = 0;

    for (int i = 0; i < 300; i++) {
        int n = snprintf(key, sizeof(key), "k%d", i);

        rc = uc_kv_set(key, (uint32_t)n, "x", 1);
        if (rc != 0) {
            break;
        }
    }
    uc_log_str(rc == 0 ? UC_LOG_INF : UC_LOG_ERR, rc == 0 ? "kv done" : "kv failed");
    return rc;
}
"""


@needs_clang
async def test_remove_wipes_all_kv_keys(node: Node, single_node: SimManager,
                                        tmp_path: Path) -> None:
    """remove(delete_file) deletes every key of the app, not only 256."""
    await _remove_if_installed(node, "kf")
    await node.deploy_app("kf", _build(tmp_path, "kf", _KV_FLOOD, heap_kb=4), heap_kb=4,
                          perms=["kv"])
    status = await _eventually(lambda: node.app_status("kf"),
                               lambda s: s is not None and s["state"] != "starting", 60.0, 0.5)
    assert status is not None and status["state"] == "running", (
        status, single_node.log_tail("e2e", 30))
    smp = await node.smp()
    assert await smp.fs_status("/lfs/data/kf/k299") == 1
    await node.remove_app("kf", delete_file=True)
    for key in ("k0", "k255", "k256", "k299"):
        with pytest.raises(SmpError) as exc:
            await smp.fs_status(f"/lfs/data/kf/{key}")
        assert exc.value.rc_name == "FILE_NOT_FOUND", key
    rc, out = await node.shell(["fs", "ls", "/lfs/data"])
    assert rc == 0 and not re.search(r"\bkf\b", out), out


# --- configuration strings -------------------------------------------------------------------

async def test_config_unicode_escapes(node: Node) -> None:
    """A document with \\uXXXX escapes (Python json.dump's default) gets the
    decoded UTF-8 text, not the escape text."""
    doc = {"schema": 1, "device": {"instance": 3100, "name": "Kühlraum €"},
           "log": {"level": "inf"}}
    text = json.dumps(doc)  # ensure_ascii=True
    assert "\\u00fc" in text
    smp = await node.smp()
    await smp.fs_upload("/lfs/cfg/device.json.new", text.encode())
    await node.reload("device")
    info = await _eventually(node.info,
                             lambda i: i["device"]["name"] == "Kühlraum €", 5.0)
    assert info["device"]["name"] == "Kühlraum €", info


async def test_escaped_param_survives_reload(node: Node) -> None:
    """An installed parameter value that needs many escapes in apps.json
    (40 x 'a"' = 80 bytes, 120 escaped) is read back by a reload."""
    await _remove_if_installed(node, "esc")
    value = 'a"' * 40
    res = await node.deploy_app("esc", _module(), heap_kb=0,
                                params={"note": value, "ctl": "a\x1fb"})
    assert res["installed_via"] == "install" and res["status"]["state"] == "running", res
    await node.reload("apps")
    cfg = await node.get_config("apps")
    entry = next(e for e in cfg["apps"] if e["name"] == "esc")
    assert {p["key"]: p["value"] for p in entry["params"]} == {"note": value, "ctl": "a\x1fb"}
    status = await node.app_status("esc")
    assert status is not None and status["state"] == "running", status
    await node.remove_app("esc", delete_file=True)
