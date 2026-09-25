# SPDX-License-Identifier: Apache-2.0
"""``bacnet-uc`` command line interface.

The commands call the same implementations as the MCP tools
(:mod:`bacnet_uc_harness.mcp_server`), so a command and the corresponding
tool behave identically. Output is YAML-like text, or JSON with ``--json``.

Exit status: 0 success, 1 operation failed (error, failed test, invalid
manifest, failed apply), 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from bacnet_uc_harness import __version__, firmware
from bacnet_uc_harness.errors import HarnessError

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]


def _kv(items: Sequence[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or ():
        key, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"bacnet-uc: expected KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def _value(text: str) -> Any:
    """Command line value: JSON literal if it parses (number, true, null), else text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bacnet-uc", description="BACnet-uc development harness")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--home", help="directory holding .bacnet-uc (default $BACNET_UC_HOME or cwd)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    groups = p.add_subparsers(dest="group", required=True, metavar="GROUP")

    # node
    node = groups.add_parser("node", help="inventory and node operations").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    node.add_parser("list", help="list inventory nodes")
    a = node.add_parser("add", help="add a node to the inventory")
    a.add_argument("name")
    t = a.add_mutually_exclusive_group(required=True)
    t.add_argument("--udp", metavar="HOST[:PORT]", help="SMP over UDP")
    t.add_argument("--serial", metavar="DEVICE[:BAUD]", help="SMP over the console UART")
    t.add_argument("--sim", metavar="HOST", help="simulated node address")
    a.add_argument("--bacnet", metavar="HOST[:PORT]", help="BACnet/IP address")
    a.add_argument("--board")
    a.add_argument("--replace", action="store_true")
    a.add_argument("--no-probe", action="store_true")
    a = node.add_parser("remove", help="remove a node from the inventory")
    a.add_argument("name")
    for cmd, hlp in (("info", "node info"), ("objects", "BACnet objects")):
        node.add_parser(cmd, help=hlp).add_argument("name")
    a = node.add_parser("logs", help="read the node log")
    a.add_argument("name")
    a.add_argument("-n", "--lines", type=int, default=200)
    a.add_argument("--grep")
    a = node.add_parser("shell", help="run a shell command (needs BACNET_UC_ALLOW_SHELL=1)")
    a.add_argument("name")
    a.add_argument("command", nargs=argparse.REMAINDER)
    a = node.add_parser("discover", help="BACnet Who-Is")
    a.add_argument("--broadcast", default="255.255.255.255")
    a.add_argument("--target")
    a.add_argument("--timeout", type=float, default=3.0)
    a.add_argument("--low", type=int)
    a.add_argument("--high", type=int)

    # io
    io = groups.add_parser("io", help="IO channels").add_subparsers(dest="cmd", required=True,
                                                                     metavar="CMD")
    io.add_parser("catalog").add_argument("node")
    a = io.add_parser("read")
    a.add_argument("node")
    a.add_argument("channel", nargs="?")
    for cmd in ("write", "force"):
        a = io.add_parser(cmd)
        a.add_argument("node")
        a.add_argument("channel")
        a.add_argument("value", type=float)
    a = io.add_parser("release")
    a.add_argument("node")
    a.add_argument("channel")
    a = io.add_parser("configure", help="merge/replace io.json points from a YAML/JSON file")
    a.add_argument("node")
    a.add_argument("file", help="list of points, or an io.json document")
    a.add_argument("--replace", action="store_true")
    a.add_argument("--dry-run", action="store_true")

    # prop
    prop = groups.add_parser("prop", help="BACnet properties").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = prop.add_parser("read")
    a.add_argument("node")
    a.add_argument("object", help="e.g. analog-input:1")
    a.add_argument("property", nargs="?", default="present-value")
    a.add_argument("--index", type=int)
    a.add_argument("--via", choices=("auto", "bacnet", "smp"), default="auto")
    a = prop.add_parser("write")
    a.add_argument("node")
    a.add_argument("object")
    a.add_argument("value", help="number, true/false, null (relinquish) or text")
    a.add_argument("--property", default="present-value")
    a.add_argument("--priority", type=int)
    a.add_argument("--index", type=int)
    a.add_argument("--via", choices=("auto", "bacnet", "smp"), default="auto")

    # app
    app = groups.add_parser("app", help="WebAssembly applications").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = app.add_parser("build", help="compile C to .wasm")
    a.add_argument("name")
    a.add_argument("source")
    a.add_argument("--aot", metavar="BOARD")
    a.add_argument("-O", dest="opt", default="z")
    a.add_argument("-D", dest="defines", action="append", metavar="NAME=VALUE")
    a.add_argument("-o", "--output-dir")
    a = app.add_parser("deploy", help="upload + install a module")
    a.add_argument("node")
    a.add_argument("name")
    a.add_argument("module")
    a.add_argument("--param", action="append", metavar="KEY=VALUE")
    a.add_argument("--perm", action="append",
                   choices=("bacnet.local", "bacnet.remote", "io", "kv"))
    a.add_argument("--period", type=int, default=1000)
    a.add_argument("--heap", type=int, default=8)
    a.add_argument("--stack", type=int, default=4)
    a.add_argument("--no-autostart", action="store_true")
    a.add_argument("--force", action="store_true")
    app.add_parser("list").add_argument("node")
    for cmd in ("status", "start", "stop", "restart"):
        a = app.add_parser(cmd)
        a.add_argument("node")
        a.add_argument("name")
    a = app.add_parser("remove")
    a.add_argument("node")
    a.add_argument("name")
    a.add_argument("--keep-file", action="store_true")

    # config
    cfg = groups.add_parser("config", help="configuration documents").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = cfg.add_parser("get")
    a.add_argument("node")
    a.add_argument("doc", choices=("device", "io", "apps"))
    a = cfg.add_parser("set")
    a.add_argument("node")
    a.add_argument("doc", choices=("device", "io", "apps"))
    a.add_argument("file", help="YAML or JSON document")
    a.add_argument("--no-reload", action="store_true")
    a.add_argument("--force", action="store_true")
    a = cfg.add_parser("reload")
    a.add_argument("node")
    a.add_argument("doc", nargs="?", default="all", choices=("device", "io", "apps", "all"))

    # system
    sy = groups.add_parser("system", help="distributed system manifests").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = sy.add_parser("validate")
    a.add_argument("manifest")
    a.add_argument("--live-catalogs", action="store_true")
    a = sy.add_parser("render", help="write the per-node documents")
    a.add_argument("manifest")
    a.add_argument("--node")
    a.add_argument("-o", "--output-dir")
    a = sy.add_parser("plan")
    a.add_argument("manifest")
    a.add_argument("--prune", action="store_true")
    a = sy.add_parser("apply")
    a.add_argument("manifest")
    a.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    a.add_argument("--prune", action="store_true")
    a.add_argument("--no-reboot", dest="reboot", action="store_false")
    a = sy.add_parser("test")
    a.add_argument("manifest")
    a.add_argument("tests", nargs="*")
    a.add_argument("--keep-going", action="store_true", help="do not stop a test at a failure")
    sy.add_parser("status").add_argument("manifest")

    # sim
    sim = groups.add_parser("sim", help="native_sim simulation").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = sim.add_parser("up")
    a.add_argument("manifest")
    a.add_argument("--exe", help="zephyr.exe of a native_sim/native/64 build")
    a.add_argument("--mode", choices=("netns", "host", "compose"), default="netns")
    a.add_argument("--erase", action="store_true", help="start with empty flash")
    a.add_argument("--apply", action="store_true", help="apply the manifest afterwards")
    sim.add_parser("down").add_argument("system", help="manifest or system name")
    sim.add_parser("status").add_argument("system", nargs="?")

    # firmware
    fw = groups.add_parser("firmware", help="build and flash the firmware").add_subparsers(
        dest="cmd", required=True, metavar="CMD")
    a = fw.add_parser("build")
    a.add_argument("board", choices=firmware.BOARDS)
    a.add_argument("--pristine", action="store_true")
    a.add_argument("--sysbuild", action="store_true")
    a.add_argument("-S", "--snippet", action="append")
    a.add_argument("--extra-conf", action="append")
    a.add_argument("--build-dir")
    a.add_argument("cmake_args", nargs="*", help="after --: -DCONFIG_...=y")
    a = fw.add_parser("flash")
    a.add_argument("build_dir")
    a.add_argument("--runner")
    a.add_argument("--yes", action="store_true", help="really flash")
    fw.add_parser("info").add_argument("build_dir")
    a = fw.add_parser("update", help="OTA update over SMP (MCUboot)")
    a.add_argument("node")
    a.add_argument("build_dir")
    a.add_argument("--yes", action="store_true")

    groups.add_parser("sdk", help="WebAssembly SDK summary")
    m = groups.add_parser("mcp", help="run the MCP server")
    m.add_argument("mcp_args", nargs=argparse.REMAINDER,
                   help="--http, --host, --port, --allow-shell")
    return p


def _call(tools: dict[str, Any], tool: str, /, **kwargs: Any) -> Any:
    return tools[tool](**{k: v for k, v in kwargs.items() if v is not None})


async def _dispatch(args: argparse.Namespace, tools: dict[str, Any]) -> Any:
    g, c = args.group, getattr(args, "cmd", None)
    if g == "node":
        if c == "list":
            return await _call(tools, "list_nodes")
        if c == "add":
            kw: dict[str, Any] = {"name": args.name, "bacnet_address": args.bacnet,
                                  "board": args.board, "replace": args.replace,
                                  "probe": not args.no_probe}
            if args.udp:
                host, sep, port = args.udp.rpartition(":")
                kw.update(transport="udp", host=host if sep else args.udp,
                          port=int(port) if sep else 1337)
            elif args.serial:
                dev, sep, baud = args.serial.rpartition(":")
                if sep and baud.isdigit():
                    kw.update(transport="serial", device=dev, baud=int(baud))
                else:
                    kw.update(transport="serial", device=args.serial)
            else:
                kw.update(transport="sim", host=args.sim)
            return await _call(tools, "add_node", **kw)
        if c == "remove":
            return await _call(tools, "remove_node", name=args.name)
        if c == "info":
            return await _call(tools, "node_info", node=args.name)
        if c == "objects":
            return await _call(tools, "list_objects", node=args.name)
        if c == "logs":
            return await _call(tools, "read_logs", node=args.name, lines=args.lines,
                               grep=args.grep)
        if c == "shell":
            return await _call(tools, "node_shell", node=args.name,
                               command=" ".join(args.command))
        if c == "discover":
            return await _call(tools, "discover_devices", broadcast=args.broadcast,
                               target=args.target, timeout_s=args.timeout, low=args.low,
                               high=args.high)
    if g == "io":
        if c == "catalog":
            return await _call(tools, "io_catalog", node=args.node)
        if c == "read":
            return await _call(tools, "io_read", node=args.node, channel=args.channel)
        if c in ("write", "force"):
            return await _call(tools, f"io_{c}", node=args.node, channel=args.channel,
                               value=args.value)
        if c == "release":
            return await _call(tools, "io_release", node=args.node, channel=args.channel)
        if c == "configure":
            from bacnet_uc_harness.manifest import read_document

            doc = read_document(args.file)
            points = doc.get("points", []) if isinstance(doc, dict) else doc
            return await _call(tools, "configure_io", node=args.node, points=points,
                               mode="replace" if args.replace else "merge",
                               dry_run=args.dry_run)
    if g == "prop":
        if c == "read":
            return await _call(tools, "bacnet_read", node=args.node, object=args.object,
                               property=args.property, index=args.index, via=args.via)
        if c == "write":
            return await tools["bacnet_write"](node=args.node, object=args.object,
                                               value=_value(args.value), property=args.property,
                                               priority=args.priority, index=args.index,
                                               via=args.via)
    if g == "app":
        if c == "build":
            opt = args.opt if args.opt.startswith("-O") else f"-O{args.opt}"
            return await _call(tools, "build_app", name=args.name, source_path=args.source,
                               aot_board=args.aot, opt=opt, defines=_kv(args.defines) or None,
                               output_dir=args.output_dir)
        if c == "deploy":
            return await _call(tools, "deploy_app", node=args.node, name=args.name,
                               module_path=args.module, autostart=not args.no_autostart,
                               period_ms=args.period, heap_kb=args.heap, stack_kb=args.stack,
                               perms=args.perm, params=_kv(args.param) or None, force=args.force)
        if c == "list":
            return await _call(tools, "list_apps", node=args.node)
        if c == "status":
            return await _call(tools, "app_status", node=args.node, name=args.name)
        if c in ("start", "stop", "restart"):
            return await _call(tools, "app_control", node=args.node, name=args.name, action=c)
        if c == "remove":
            return await _call(tools, "app_control", node=args.node, name=args.name,
                               action="remove", delete_file=not args.keep_file)
    if g == "config":
        if c == "get":
            return await _call(tools, "get_config", node=args.node, doc=args.doc)
        if c == "set":
            from bacnet_uc_harness.manifest import read_document

            return await _call(tools, "set_config", node=args.node, doc=args.doc,
                               content=read_document(args.file), reload=not args.no_reload,
                               force=args.force)
        if c == "reload":
            return await _call(tools, "reload_config", node=args.node, doc=args.doc)
    if g == "system":
        if c == "validate":
            return await _call(tools, "validate_system", system=args.manifest,
                               live_catalogs=args.live_catalogs)
        if c == "render":
            return _render(args)
        if c == "plan":
            return await _call(tools, "plan_system", system=args.manifest, prune=args.prune)
        if c == "apply":
            return await _call(tools, "apply_system", system=args.manifest, dry_run=args.dry_run,
                               prune=args.prune, reboot=args.reboot)
        if c == "test":
            return await _call(tools, "run_system_tests", system=args.manifest,
                               tests=args.tests or None, stop_on_failure=not args.keep_going)
        if c == "status":
            return await _call(tools, "system_status", system=args.manifest)
    if g == "sim":
        if c == "up":
            return await _call(tools, "sim_start", system=args.manifest, firmware_exe=args.exe,
                               mode=args.mode, erase_flash=args.erase, apply_config=args.apply)
        if c == "down":
            return await _call(tools, "sim_stop", system=args.system)
        if c == "status":
            return await _call(tools, "sim_status", system=args.system)
    if g == "firmware":
        if c == "build":
            return await _call(tools, "build_firmware", board=args.board,
                               pristine=args.pristine, sysbuild=args.sysbuild,
                               snippets=args.snippet, extra_conf=args.extra_conf,
                               cmake_args=args.cmake_args or None, build_dir=args.build_dir)
        if c == "flash":
            return await _call(tools, "flash_firmware", build_dir=args.build_dir,
                               runner=args.runner, confirm=args.yes)
        if c == "info":
            return firmware.firmware_info(args.build_dir)
        if c == "update":
            return await _call(tools, "update_firmware", node=args.node,
                               build_dir=args.build_dir, confirm=args.yes)
    if g == "sdk":
        return await _call(tools, "sdk_info")
    raise HarnessError(f"unknown command {g} {c}")


def _render(args: argparse.Namespace) -> dict[str, Any]:
    from bacnet_uc_harness.manifest import load_system
    from bacnet_uc_harness.render import doc_bytes, render_system

    system = load_system(args.manifest)
    renders = render_system(system)
    out: dict[str, Any] = {}
    for name, r in renders.items():
        if args.node and name != args.node:
            continue
        out[name] = r.to_dict()
        if args.output_dir:
            d = Path(args.output_dir) / name
            d.mkdir(parents=True, exist_ok=True)
            for doc, content in r.docs().items():
                (d / f"{doc}.json").write_bytes(doc_bytes(content))
            out[name]["written"] = str(d)
    return out


def _ok(args: argparse.Namespace, result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    if args.group == "system" and args.cmd in ("validate", "apply", "test"):
        return bool(result.get("ok", False))
    if args.group == "firmware" and args.cmd in ("build", "flash") and "ok" in result:
        return bool(result["ok"])
    return True


def _text(args: argparse.Namespace, result: Any) -> str:
    if args.group == "system" and args.cmd == "test" and isinstance(result, dict):
        lines = []
        for t in result.get("tests", []):
            lines.append(f"{'PASS' if t['ok'] else 'FAIL'} {t['name']} ({t['duration_ms']} ms)")
            for s in t["steps"]:
                mark = "  ok " if s["ok"] else "  !! "
                lines.append(f"{mark}{s['index']}: {s['message']}")
        lines.append(f"{result.get('passed', 0)} passed, {result.get('failed', 0)} failed")
        return "\n".join(lines)
    if args.group == "system" and args.cmd in ("plan", "apply") and isinstance(result, dict):
        lines = []
        items = result.get("actions") or result.get("results") or []
        if not items:
            lines.append("in sync: nothing to do")
        for a in items:
            status = f"[{a['status']}] " if "status" in a else ""
            err = f" -> {a['error']}" if a.get("error") else ""
            lines.append(f"{status}{a['node']}: {a['kind']} {a['target']} ({a['reason']}){err}")
        for n in result.get("notes", []):
            lines.append(f"note {n['node']}: {n['message']}")
        if "reboot_required" in result and result["reboot_required"]:
            lines.append(f"reboot required: {', '.join(result['reboot_required'])}")
        if result.get("rebooted"):
            lines.append(f"rebooted: {', '.join(result['rebooted'])}")
        return "\n".join(lines)
    return yaml.safe_dump(result, sort_keys=False, allow_unicode=True, default_flow_style=False,
                          width=100).rstrip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if args.group == "mcp":
        from bacnet_uc_harness import mcp_server

        extra = list(args.mcp_args)
        if args.home:
            extra += ["--home", args.home]
        return mcp_server.main(extra)

    from bacnet_uc_harness.mcp_server import HarnessContext, create_server

    async def run() -> Any:
        ctx = HarnessContext(args.home)
        server = create_server(ctx)
        try:
            return await _dispatch(args, server.harness_tools)
        finally:
            await ctx.close()

    try:
        result = asyncio.run(run())
    except ToolError as exc:
        text = str(exc)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"error": "ToolError", "message": text}
        print(json.dumps(payload, indent=2, default=str) if args.json else
              f"error: {payload.get('message', text)}" + "".join(
                  f"\n  {i['path']}: {i['message']}" for i in payload.get("issues", [])
                  if i.get("severity") == "error"), file=sys.stderr)
        return 1
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(_text(args, result))
    return 0 if _ok(args, result) else 1


if __name__ == "__main__":
    sys.exit(main())
