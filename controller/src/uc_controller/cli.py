"""uc-ctl command line (DESIGN.md §5.3, §19.3).

Global options: ``--config`` (default /etc/uc-controller/controller.yaml),
``--json`` and ``-v``. They are accepted before and after the command, so
``uc-ctl apply --json`` and ``uc-ctl --json apply`` mean the same.

Command modules are imported at start-up; a module that another work
package has not delivered yet is skipped quietly.

Exit codes: 0 ok, 1 error, 2 validation refused, 3 probes failed and rolled back.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys

from . import paths as _paths
from .config import ConfigError

log = logging.getLogger("uc_controller")

COMMAND_MODULES = ["apply", "render", "secret", "bms", "time", "pki", "device", "history",
                   "hub", "token", "status", "selftest", "support", "backup", "restore",
                   "update", "firstboot", "eeprom"]


class _Subparsers:
    """Wraps argparse's subparsers so every command also accepts the global options."""

    def __init__(self, action, common: argparse.ArgumentParser):
        self._action = action
        self._common = common

    def add_parser(self, name, **kw):
        kw["parents"] = list(kw.get("parents", [])) + [self._common]
        parser = self._action.add_parser(name, **kw)
        # nested command groups ("secret list") get the global options too
        plain = parser.add_subparsers
        common = self._common
        parser.add_subparsers = lambda **skw: _Subparsers(plain(**skw), common)
        return parser

    def __getattr__(self, name):
        return getattr(self._action, name)


def _common_options(parser: argparse.ArgumentParser, top: bool) -> None:
    default = (lambda v: v) if top else (lambda v: argparse.SUPPRESS)
    parser.add_argument("--config", metavar="FILE", default=default(_paths.current().controller_yaml),
                        help="site file (default: %(default)s)" if top else argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true", default=default(False),
                        help="machine-readable output" if top else argparse.SUPPRESS)
    parser.add_argument("-v", "--verbose", action="count", default=default(0),
                        help="more logging (-vv: debug)" if top else argparse.SUPPRESS)


def import_command(name: str):
    modname = f"{__package__}.commands.{name}"
    try:
        return importlib.import_module(modname)
    except ImportError as exc:
        if exc.name != modname:
            log.warning("command module %s failed to import: %s", name, exc)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uc-ctl", description="uc-controller operator CLI")
    _common_options(parser, top=True)
    common = argparse.ArgumentParser(add_help=False)
    _common_options(common, top=False)
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    wrapped = _Subparsers(sub, common)
    for name in COMMAND_MODULES:
        module = import_command(name)
        if module is not None:
            module.register(wrapped)
    return parser


def setup_logging(verbose: int) -> None:
    level = logging.WARNING if verbose <= 0 else logging.INFO if verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="uc-ctl: %(levelname)s: %(message)s", stream=sys.stderr)


def output(args, data, text: str | None = None) -> None:
    """Print ``data`` as JSON with --json, otherwise ``text`` (or a JSON dump)."""
    if getattr(args, "json", False) or text is None:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    elif text:
        print(text)


def main(argv: list[str] | None = None) -> int:
    # Logging first, so import warnings of command modules are visible.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("-v", "--verbose", action="count", default=0)
    known, _ = pre.parse_known_args(argv)
    setup_logging(known.verbose)
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.getLogger().setLevel(logging.WARNING if args.verbose <= 0 else
                                 logging.INFO if args.verbose == 1 else logging.DEBUG)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 1
    try:
        return int(func(args) or 0)
    except ConfigError as exc:
        if args.json:
            output(args, {"result": "refused", "errors": exc.errors})
        else:
            for err in exc.errors:
                print(f"uc-ctl: invalid: {err}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - last line of defence for the CLI
        if args.verbose >= 2:
            raise
        print(f"uc-ctl: error: {exc}", file=sys.stderr)
        return 1
