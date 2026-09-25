"""``uc-hub``: run the hub, check a site, show a plan, serve MCP, run the demo.

    uc-hub serve -c hub.yaml [--host H] [--port P] [--insecure-listen]
    uc-hub validate site.yaml
    uc-hub plan -c hub.yaml [--diff]
    uc-hub mcp -c hub.yaml [--insecure-listen]
    uc-hub demo [--llm scripted|zai] [--host H] [--port P] [--dir DIR] [--free-ports] [--token-env VAR]

Logs go to stderr (for ``mcp``, stdout carries the protocol). Exit codes:
0 ok, 1 an error, 2 a plan with blocked targets.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any

import uvicorn

from . import __version__
from .core.errors import HubError, ValidationFailed
from .core.types import Plan
from .manifest import SiteManifest

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_RedactTokens())
    try:
        return int(args.run(args))
    except ValidationFailed as e:
        print(f"error: {e}", file=sys.stderr)
        for line in e.errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    except (HubError, OSError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uc-hub", description="Site gateway and AI harness for BACnet-uc.")
    parser.add_argument("--version", action="version", version=f"uc-hub {__version__}")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR (default INFO)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    serve = commands.add_parser("serve", help="run the hub: drivers, agent, HTTP API and web app")
    _config_args(serve)
    serve.add_argument("--host", help="listen address (default: listen.host of hub.yaml)")
    serve.add_argument("--port", type=int, help="listen port (default: listen.port of hub.yaml)")
    serve.set_defaults(run=_serve)

    validate = commands.add_parser("validate", help="check a site.yaml")
    validate.add_argument("site", type=Path, help="the site manifest")
    validate.set_defaults(run=_validate)

    plan = commands.add_parser("plan", help="show what applying the manifest would change (hub stopped)")
    plan.add_argument("-c", "--config", type=Path, required=True, help="hub.yaml")
    plan.add_argument("--diff", action="store_true", help="print each change's diff")
    plan.set_defaults(run=_plan)

    mcp = commands.add_parser("mcp", help="run the hub and serve its tools over MCP on stdio")
    _config_args(mcp)
    mcp.set_defaults(run=_mcp)

    demo = commands.add_parser("demo", help="run the simulated demo site with a hub on it")
    demo.add_argument("--llm", choices=("scripted", "zai"), default="scripted",
                      help="the offline scripted model (default) or Z.ai GLM (needs ZAI_API_KEY)")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8080)
    demo.add_argument("--dir", type=Path, help="the demo files (default: hub/examples/demo of this checkout)")
    demo.add_argument("--free-ports", action="store_true",
                      help="put the simulated devices on free ports instead of those site.yaml names "
                           "(to run next to another demo)")
    demo.add_argument("--token-env", metavar="VAR",
                      help="require the bearer token in this environment variable (user demo, role admin) "
                           "instead of dev mode; then --host may be a LAN address")
    demo.set_defaults(run=_demo)
    return parser


def _config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", type=Path, required=True, help="hub.yaml")
    parser.add_argument("--insecure-listen", action="store_true",
                        help="allow a non-loopback listen address without auth tokens (dev mode)")


def _load(args: argparse.Namespace) -> Any:
    from .runtime import load_config

    listen = {k: v for k, v in (("host", getattr(args, "host", None)), ("port", getattr(args, "port", None)))
              if v is not None}
    return load_config(args.config, insecure_listen=getattr(args, "insecure_listen", False),
                       overrides={"listen": listen} if listen else None)


# -- serve and mcp ----------------------------------------------------------------------------
def _serve(args: argparse.Namespace) -> int:
    config = _load(args)
    asyncio.run(_run_hub(config))
    return 0


def _mcp(args: argparse.Namespace) -> int:
    config = _load(args)
    asyncio.run(_run_hub(config, mcp=True))
    return 0


async def _run_hub(config: Any, *, mcp: bool = False) -> None:
    """The hub until a stop signal (with ``mcp``, also until the MCP client leaves)."""
    from .api import HubServer, create_app
    from .runtime import Services

    services = Services(config)
    await services.start()
    try:
        server = HubServer(uvicorn.Config(create_app(services), host=config.listen.host, port=config.listen.port,
                                          log_config=None, lifespan="off", timeout_graceful_shutdown=2))
        http = asyncio.create_task(server.serve(), name="http")
        while not server.started and not http.done():
            await asyncio.sleep(0.05)
        if not mcp or http.done():
            await http
            return
        from .mcp_server import HubMcpServer

        serving = asyncio.create_task(HubMcpServer(services).run_stdio_async(), name="mcp")
        try:
            # The client leaving ends the MCP server; a stop signal ends the HTTP server, and then this one.
            await asyncio.wait({http, serving}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            server.should_exit = True
            serving.cancel()
            await asyncio.wait({http, serving})
        if not serving.cancelled():
            serving.result()
        await http
    finally:
        await services.stop()


# -- validate and plan --------------------------------------------------------------------------
def _validate(args: argparse.Namespace) -> int:
    manifest = SiteManifest.load(args.site)
    nodes, devices = len(manifest.nodes), len(manifest.devices)
    print(f"{args.site}: valid; site {manifest.name}, {devices} devices ({nodes} BACnet-uc nodes), "
          f"{len(manifest.spaces)} spaces, {len(manifest.tests)} tests")
    return 0


def _plan(args: argparse.Namespace) -> int:
    from .runtime import Services, load_config

    async def compute() -> Plan:
        services = Services(load_config(args.config), passive=True)
        await services.start()
        try:
            return await services.manifests.plan()
        finally:
            await services.stop()

    plan = asyncio.run(compute())
    print(format_plan(plan, diff=args.diff), end="")
    return 2 if plan.blocked else 0


def format_plan(plan: Plan, *, diff: bool = False) -> str:
    """A plan as text, grouped by target."""
    out = [f"Plan {plan.id}: revision {plan.revision} against live revision {plan.base_revision}, "
           f"{len(plan.changes)} change{'s' if len(plan.changes) != 1 else ''}"
           + (f" on {', '.join(plan.targets)}" if plan.targets else "")]
    for target, reason in plan.blocked.items():
        out.append(f"  BLOCKED {target}: {reason} (this plan cannot be applied)")
    for warning in plan.warnings:
        out.append(f"  warning: {warning}")
    for target in plan.targets:
        out.append(f"{target}:")
        for change in (c for c in plan.changes if c.target == target):
            out.append(f"  {change.id} {change.kind}: {change.summary}")
            if diff and change.diff:
                out.extend(f"      {line}" for line in change.diff.rstrip("\n").splitlines())
    if not plan.changes and not plan.blocked:
        out.append("Nothing to change: the devices and the gateway match the manifest.")
    return "\n".join(out) + "\n"


# -- demo -----------------------------------------------------------------------------------------
def _demo(args: argparse.Namespace) -> int:
    from .demo import DEMO_DIR, run_demo

    asyncio.run(run_demo(host=args.host, port=args.port, llm=args.llm, demo_dir=args.dir or DEMO_DIR,
                         free_ports=args.free_ports, token_env=args.token_env))
    return 0


class _RedactTokens(logging.Filter):
    """Event streams may carry ``?access_token=`` and the web app's sign-in
    link ``?token=``; the access log must not."""

    _TOKEN = re.compile(r"([?&](?:access_)?token=)[^&\s]*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        return True


if __name__ == "__main__":
    sys.exit(main())
