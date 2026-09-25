# SPDX-License-Identifier: Apache-2.0
"""``bacnet-uc`` command line interface."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from bacnet_uc_harness import cli, firmware
from bacnet_uc_harness.manifest import load_system
from bacnet_uc_harness.render import doc_bytes, render_system
from bacnet_uc_harness.testing import FakeNode

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "systems"


@pytest.fixture
def threaded_node() -> Iterator[FakeNode]:
    """A FakeNode served from a background event loop (the CLI runs its own loop)."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    node = FakeNode(1234, "cli-node")
    asyncio.run_coroutine_threadsafe(node.start(), loop).result(5)
    try:
        yield node
    finally:
        asyncio.run_coroutine_threadsafe(node.stop(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def run(capsys: pytest.CaptureFixture[str], home: Path, *args: str) -> tuple[int, str, str]:
    rc = cli.main(["--home", str(home), *args])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_validate_examples(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out, _ = run(capsys, tmp_path, "system", "validate", str(EXAMPLES / "hvac-demo.yaml"))
    assert rc == 0 and "ok: true" in out
    rc, out, _ = run(capsys, tmp_path, "--json", "system", "validate",
                     str(EXAMPLES / "sim-demo.yaml"))
    data = json.loads(out)
    assert rc == 0 and data["ok"] and set(data["documents"]) == {"sim-a", "sim-b"}


def test_validate_invalid(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("apiVersion: bacnet-uc/v1\nkind: System\nmetadata: {name: x}\n")
    rc, out, _ = run(capsys, tmp_path, "--json", "system", "validate", str(bad))
    assert rc == 1 and json.loads(out)["errors"][0]["message"] == "'nodes' is a required property"


def test_render_writes_documents(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    rc, out, _ = run(capsys, tmp_path, "--json", "system", "render",
                     str(EXAMPLES / "sim-demo.yaml"), "-o", str(out_dir))
    assert rc == 0
    data = json.loads(out)
    expected = render_system(load_system(EXAMPLES / "sim-demo.yaml"))
    for node in ("sim-a", "sim-b"):
        for doc in ("device", "io", "apps"):
            assert (out_dir / node / f"{doc}.json").read_bytes() == \
                doc_bytes(expected[node].doc(doc))
        assert data[node]["written"] == str(out_dir / node)
    rc, out, _ = run(capsys, tmp_path, "--json", "system", "render",
                     str(EXAMPLES / "sim-demo.yaml"), "--node", "sim-b")
    assert list(json.loads(out)) == ["sim-b"]


def test_inventory_commands(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out, _ = run(capsys, tmp_path, "node", "add", "bench", "--serial", "/dev/ttyACM0:57600",
                     "--board", "nucleo_f767zi", "--no-probe")
    assert rc == 0 and "serial:/dev/ttyACM0:57600" in out
    rc, out, _ = run(capsys, tmp_path, "node", "add", "lab", "--udp", "192.168.1.9",
                     "--bacnet", "192.168.1.9:47809", "--no-probe")
    assert rc == 0
    rc, out, _ = run(capsys, tmp_path, "--json", "node", "list")
    nodes = {n["name"]: n for n in json.loads(out)["nodes"]}
    assert nodes["lab"]["smp"] == "udp:192.168.1.9:1337"
    assert nodes["lab"]["bacnet"] == "192.168.1.9:47809"
    assert nodes["bench"]["baud"] == 57600
    assert (tmp_path / ".bacnet-uc" / "inventory.yaml").is_file()
    rc, _, _ = run(capsys, tmp_path, "node", "remove", "lab")
    assert rc == 0
    rc, _, err = run(capsys, tmp_path, "node", "remove", "lab")
    assert rc == 1 and "not in the inventory" in err


def test_node_commands_against_fake_node(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                                         threaded_node: FakeNode) -> None:
    rc, out, _ = run(capsys, tmp_path, "--json", "node", "add", "n1", "--udp",
                     f"127.0.0.1:{threaded_node.smp_port}", "--bacnet",
                     f"127.0.0.1:{threaded_node.bacnet_port}")
    assert rc == 0 and json.loads(out)["probe"]["ok"]
    rc, out, _ = run(capsys, tmp_path, "--json", "node", "info", "n1")
    assert json.loads(out)["info"]["device"]["instance"] == 1234
    points = tmp_path / "io.yaml"
    points.write_text("- {channel: ai0, type: analog-input, instance: 1, scale: 0.01}\n")
    rc, out, _ = run(capsys, tmp_path, "io", "configure", "n1", str(points))
    assert rc == 0 and "added:\n  - ai0" in out
    rc, _, _ = run(capsys, tmp_path, "io", "force", "n1", "ai0", "1234")
    assert rc == 0
    rc, out, _ = run(capsys, tmp_path, "--json", "prop", "read", "n1", "analog-input:1")
    assert json.loads(out)["value"] == pytest.approx(12.34)
    rc, out, _ = run(capsys, tmp_path, "--json", "prop", "write", "n1", "analog-input:1", "true",
                     "--property", "out-of-service")
    assert rc == 0 and json.loads(out)["readback"] is True
    rc, out, _ = run(capsys, tmp_path, "--json", "io", "read", "n1", "ai0")
    assert json.loads(out)["values"] == {"ai0": 1234.0}
    rc, out, _ = run(capsys, tmp_path, "--json", "config", "get", "n1", "io")
    assert json.loads(out)["content"]["points"][0]["channel"] == "ai0"
    rc, _, err = run(capsys, tmp_path, "--json", "io", "read", "n1", "zz9")
    assert rc == 1 and json.loads(err)["rc_name"] == "NOT_FOUND"
    rc, _, err = run(capsys, tmp_path, "node", "shell", "n1", "uc", "info")
    assert rc == 1 and "BACNET_UC_ALLOW_SHELL" in err


def test_unknown_node_error(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, _, err = run(capsys, tmp_path, "node", "info", "ghost")
    assert rc == 1 and err.startswith("error: unknown node 'ghost'")


def test_sdk_and_usage(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out, _ = run(capsys, tmp_path, "--json", "sdk")
    assert rc == 0 and json.loads(out)["import_module"] == "bacnet_uc"
    with pytest.raises(SystemExit) as exc:
        cli.main(["node"])
    assert exc.value.code == 2
    capsys.readouterr()


def test_firmware_info_and_flash_preview(capsys: pytest.CaptureFixture[str],
                                         tmp_path: Path) -> None:
    bdir = tmp_path / "build"
    (bdir / "zephyr").mkdir(parents=True)
    shutil.copy(Path(sys.executable).resolve(), bdir / "zephyr" / "zephyr.elf")
    (bdir / "CMakeCache.txt").write_text("CACHED_BOARD:STRING=nucleo_f767zi\n")
    rc, out, _ = run(capsys, tmp_path, "--json", "firmware", "info", str(bdir))
    info = json.loads(out)
    assert rc == 0 and info["board"] == "nucleo_f767zi"
    assert info["sizes"]["text"] > 0 and not info["updatable"]
    rc, out, _ = run(capsys, tmp_path, "--json", "firmware", "flash", str(bdir))
    assert rc == 0 and json.loads(out)["confirm_required"] is True


def test_elf_sizes_and_west_command() -> None:
    sizes = firmware.elf_sizes(Path(sys.executable).resolve())
    assert sizes["flash"] == sizes["text"] + sizes["data"]
    assert firmware.west_command()[-1].endswith("west")
    with pytest.raises(firmware.HarnessError):
        firmware.firmware_info("/nonexistent/build")


def test_kv_and_value_parsing() -> None:
    assert cli._kv(["a=1", "b=x=y"]) == {"a": "1", "b": "x=y"}
    with pytest.raises(SystemExit):
        cli._kv(["novalue"])
    assert cli._value("1.5") == 1.5 and cli._value("null") is None
    assert cli._value("active") == "active"


def test_plan_text_output(capsys: pytest.CaptureFixture[str], tmp_path: Path,
                          threaded_node: FakeNode) -> None:
    mf = tmp_path / "one.yaml"
    mf.write_text(f"""
apiVersion: bacnet-uc/v1
kind: System
metadata: {{name: one}}
nodes:
  - name: n
    board: native_sim/native/64
    transport: {{kind: udp, host: 127.0.0.1, port: {threaded_node.smp_port}}}
    bacnet_address: 127.0.0.1:{threaded_node.bacnet_port}
    bacnet: {{udp_port: {threaded_node.bacnet_port}}}
    device: {{instance: 1234, name: cli-node}}
    io: [{{channel: di0, type: binary-input, instance: 1}}]
tests:
  - name: t
    steps:
      - force: {{node: n, channel: di0, value: 1}}
      - expect: {{point: n/binary-input:1, op: eq, value: 1, within_ms: 500}}
""")
    rc, out, _ = run(capsys, tmp_path, "system", "plan", str(mf))
    assert rc == 0
    assert out.splitlines() == ["n: push_config device (missing on the node)",
                                "n: push_config io (missing on the node)"]
    rc, out, _ = run(capsys, tmp_path, "system", "apply", str(mf), "--no-dry-run")
    assert rc == 0 and out.startswith("[ok] n: push_config device")
    rc, out, _ = run(capsys, tmp_path, "system", "plan", str(mf))
    assert out.strip() == "in sync: nothing to do"
    rc, out, _ = run(capsys, tmp_path, "system", "test", str(mf))
    assert rc == 0 and out.splitlines()[0].startswith("PASS t")
    assert out.splitlines()[-1] == "1 passed, 0 failed"


def test_mcuboot_image_hash(tmp_path: Path) -> None:
    import struct

    body = b"\xaa" * 100
    digest = bytes(range(32))
    hdr = struct.pack("<IIHHI", 0x96F3B83D, 0, 32, 8, len(body)).ljust(32, b"\0")
    prot = struct.pack("<HH", 0x6908, 8) + struct.pack("<HH", 0x50, 0)
    tlvs = struct.pack("<HH", 0x01, 4) + b"key!" + struct.pack("<HH", 0x10, 32) + digest
    unprot = struct.pack("<HH", 0x6907, 4 + len(tlvs)) + tlvs
    img = tmp_path / "zephyr.signed.bin"
    img.write_bytes(hdr + body + prot + unprot)
    assert firmware.image_hash(img) == digest
    img.write_bytes(b"\0" * 64)
    with pytest.raises(firmware.HarnessError, match="not an MCUboot image"):
        firmware.image_hash(img)
    bdir = tmp_path / "b"
    (bdir / "zephyr").mkdir(parents=True)
    shutil.copy(Path(sys.executable).resolve(), bdir / "zephyr" / "zephyr.elf")
    with pytest.raises(firmware.HarnessError, match="sysbuild"):
        firmware.update_image(bdir)
    (bdir / "zephyr" / "zephyr.signed.bin").write_bytes(hdr + body + prot + unprot)
    assert firmware.update_image(bdir).name == "zephyr.signed.bin"
    assert firmware.firmware_info(bdir)["updatable"] is True


def test_node_add_serial_url(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    rc, out, _ = run(capsys, tmp_path, "node", "add", "bench", "--serial",
                     "socket://localhost:7777", "--no-probe")
    assert rc == 0 and "serial:socket://localhost:7777:115200" in out
