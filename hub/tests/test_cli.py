"""The ``uc-hub`` command: validate, plan, serve and mcp."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from mcp import Client, StdioServerParameters
from support.site import start_hub

from uc_hub import cli
from uc_hub.core.types import Change, Plan

DEMO_SITE = Path(__file__).resolve().parents[1] / "examples" / "demo" / "site.yaml"


def bare_hub(tmp_path: Path, **extra: object) -> Path:
    (tmp_path / "site.yaml").write_text(yaml.safe_dump({
        "apiVersion": "bacnet-uc/v1", "kind": "Site", "metadata": {"name": "hq"},
        "spaces": [{"id": "hq", "name": "HQ"}],
    }))
    doc = {"site_file": "site.yaml", "data_dir": "data", "listen": {"host": "127.0.0.1", "port": 0},
           "drivers": {"bacnet_uc": {"discover_broadcast": ""}}, **extra}
    (tmp_path / "hub.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path / "hub.yaml"


def test_validate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["validate", str(DEMO_SITE)]) == 0
    assert "valid; site hq, 8 devices (5 BACnet-uc nodes), 9 spaces, 4 tests" in capsys.readouterr().out
    bad = tmp_path / "site.yaml"
    bad.write_text("apiVersion: bacnet-uc/v1\nkind: Site\nmetadata: {name: Bad Name}\n")
    assert cli.main(["validate", str(bad)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: site manifest is invalid") and "/metadata/name" in err
    assert cli.main(["validate", str(tmp_path / "missing.yaml")]) == 1


def test_dev_mode_refuses_a_public_address(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = bare_hub(tmp_path)
    assert cli.main(["serve", "-c", str(config), "--host", "0.0.0.0"]) == 1
    assert "loopback" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


async def test_plan_prints_the_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def rename(doc: dict[str, object]) -> None:
        doc["system"]["nodes"][0]["device"]["name"] = "Room 204 controller"  # type: ignore[index]

    hub = await start_hub(tmp_path, edit=rename)
    try:
        await hub.services.stop()
        code = await asyncio.to_thread(cli.main, ["plan", "-c", str(tmp_path / "hub.yaml"), "--diff"])
        out = capsys.readouterr().out
        assert code == 0, out
        assert out.startswith("Plan p1: revision 1 against live revision 1, ")
        assert re.search(r"^r204-ctl:\n  c1 upload-doc: device\.json: device\.location, device\.name changed$",
                         out, re.M)
        assert '+    "name": "Room 204 controller"' in out
        hub.nodes["r205-ctl"].online = False
        code = await asyncio.to_thread(cli.main, ["plan", "-c", str(tmp_path / "hub.yaml")])
        assert code == 2 and "BLOCKED r205-ctl" in capsys.readouterr().out
    finally:
        await hub.close()


def test_format_plan() -> None:
    plan = Plan("p3", 3, 2, [Change("c1", "gateway", "tags", "2 points tagged", "--- a\n+++ b\n")],
                warnings=["app x asks for io"])
    assert cli.format_plan(plan) == ("Plan p3: revision 3 against live revision 2, 1 change on gateway\n"
                                     "  warning: app x asks for io\ngateway:\n  c1 tags: 2 points tagged\n")
    assert "      --- a\n      +++ b\n" in cli.format_plan(plan, diff=True)
    empty = cli.format_plan(Plan("p1", 1, 1))
    assert empty.endswith("Nothing to change: the devices and the gateway match the manifest.\n")


@pytest.mark.parametrize(("path", "logged"), [
    ("/api/live?ids=a&access_token=s3cret&x=1", "/api/live?ids=a&access_token=***&x=1"),
    ("/?token=s3cret", "/?token=***"),  # the web app's sign-in link
    ("/runs?x=1&token=s3cret", "/runs?x=1&token=***"),
    ("/api/points?q=max_tokens=5", "/api/points?q=max_tokens=5"),
])
def test_access_tokens_are_redacted_from_the_access_log(path: str, logged: str) -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("127.0.0.1", "GET", path), None)
    cli._RedactTokens().filter(record)
    assert record.getMessage() == f'127.0.0.1 - "GET {logged}"'


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
async def test_serve_runs_until_stopped_and_then_shuts_down(tmp_path: Path, stop: signal.Signals) -> None:
    """SIGTERM is how systemd stops the hub; it must shut down in order (and
    release the agent's leases), not die when uvicorn raises the signal again."""
    config = bare_hub(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uc_hub.cli", "serve", "-c", str(config), stderr=asyncio.subprocess.PIPE)
    try:
        assert process.stderr is not None
        url = None
        async with asyncio.timeout(30):
            while url is None:
                line = (await process.stderr.readline()).decode()
                assert line, "serve exited early"
                found = re.search(r"Uvicorn running on (http://\S+)", line)
                url = found.group(1) if found else None
        async with httpx.AsyncClient(base_url=url) as client:
            assert (await client.get("/api/health")).json()["site"] == "hq"
        process.send_signal(stop)
        async with asyncio.timeout(30):
            await process.stderr.read()
            assert await process.wait() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("stop", [signal.SIGINT, signal.SIGTERM], ids=["ctrl-c", "sigterm"])
async def test_mcp_stops_on_a_signal_while_its_client_is_connected(tmp_path: Path, stop: signal.Signals) -> None:
    """The client keeps stdin open and says nothing: a stop signal must still
    end the MCP server as well as the HTTP API, and then the hub in order."""
    config = bare_hub(tmp_path, mcp={"user": "claude", "roles": ["viewer"]})
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uc_hub.cli", "mcp", "-c", str(config),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        async with asyncio.timeout(30):
            while not re.search(r"Uvicorn running on", line := (await process.stderr.readline()).decode()):
                assert line, "mcp exited early"
        process.send_signal(stop)
        async with asyncio.timeout(30):
            log = (await process.stderr.read()).decode()
            assert await process.wait() == 0, log
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_a_stopped_demo_stops_its_devices_and_removes_its_working_directory() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "uc_hub.cli", "demo", "--free-ports", "--port", str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        assert process.stdout is not None
        async with asyncio.timeout(60):
            line = (await process.stdout.readline()).decode()
        found = re.search(r"working directory (\S+)\)", line)
        assert found, line
        workdir = Path(found.group(1))
        assert (workdir / "hub.yaml").is_file()
        process.send_signal(signal.SIGTERM)
        async with asyncio.timeout(30):
            assert await process.wait() == 0
        assert not workdir.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_mcp_over_stdio(tmp_path: Path) -> None:
    config = bare_hub(tmp_path, mcp={"user": "claude", "roles": ["viewer"]})
    server = StdioServerParameters(command=sys.executable, args=["-m", "uc_hub.cli", "--log-level", "WARNING", "mcp",
                                                                 "-c", str(config)],
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"site_search", "site_tree", "point_read"} <= names and "point_write" not in names
        tree = await client.call_tool("site_tree", {})
    assert not tree.is_error and '"ok": true' in tree.content[0].text  # type: ignore[union-attr]
