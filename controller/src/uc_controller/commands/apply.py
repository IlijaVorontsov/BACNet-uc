"""uc-ctl apply | confirm | rollback-config | check (DESIGN.md §5.3, §5.4)."""

from __future__ import annotations

import argparse

from .. import apply as _apply
from ..cli import output
from ..roles import parse_only
from ..runner import Runner


def _opts(args, **kw) -> _apply.ApplyOptions:
    return _apply.ApplyOptions(config=args.config, only=parse_only(getattr(args, "only", None)), **kw)


def _print_result(args, res: _apply.ApplyResult) -> int:
    if args.json:
        output(args, res.to_dict())
        return res.exit_code
    if res.diff is not None:
        print(res.diff, end="")
    lines = [f"{res.result}: txid {res.txid}"]
    if res.changed:
        lines.append("changed: " + ", ".join(res.changed))
    for v in res.validators:
        state = "skipped" if v["skipped"] else "ok" if v["ok"] else f"FAILED rc={v['rc']}"
        lines.append(f"validator {v['role']}/{v['name']}: {state}")
    for unit, verb in res.units.items():
        lines.append(f"unit {unit}: {verb}")
    for pr in res.probes:
        lines.append(f"probe {pr['role']}/{pr['name']}: {'ok' if pr['ok'] else 'FAILED ' + pr['detail']}")
    for d in res.deferred:
        lines.append(f"deferred: {d}")
    for e in res.errors:
        lines.append(f"error: {e}")
    if res.confirm:
        lines.append(f"CONFIRM within {res.confirm['timeout_s']} s from a new SSH session: uc-ctl confirm")
    print("\n".join(lines))
    return res.exit_code


def cmd_apply(args) -> int:
    opts = _opts(args, dry_run=args.dry_run, first_boot=args.first_boot, no_systemd=args.no_systemd)
    return _print_result(args, _apply.apply(opts))


def cmd_check(args) -> int:
    return _print_result(args, _apply.check(_opts(args)))


def cmd_confirm(args) -> int:
    code, msg = _apply.confirm(Runner())
    output(args, {"result": "ok", "message": msg}, msg)
    return code


def cmd_rollback(args) -> int:
    code, msg = _apply.rollback_config(args.if_unconfirmed, Runner(no_systemd=args.no_systemd))
    output(args, {"result": "ok" if code == 0 else "error", "message": msg}, msg)
    return code


def register(sub) -> None:
    p = sub.add_parser("apply", help="render, validate and install the configuration")
    p.add_argument("--dry-run", action="store_true", help="show the diff (secrets masked), change nothing")
    p.add_argument("--first-boot", action="store_true", help="enable units without starting them")
    p.add_argument("--no-systemd", action="store_true", help="no systemctl calls and no probes (containers)")
    p.add_argument("--only", metavar="ROLES", help="comma-separated roles, e.g. network,ssh")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("check", help="validate controller.yaml and the rendered files; install nothing")
    p.add_argument("--only", metavar="ROLES", help="comma-separated roles")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("confirm", help="keep the network/SSH change of the last apply")
    p.set_defaults(func=cmd_confirm)

    p = sub.add_parser("rollback-config", help="restore the files replaced by the last apply")
    p.add_argument("--if-unconfirmed", metavar="TXID", help="only if TXID is still unconfirmed (confirm timer)")
    p.add_argument("--no-systemd", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_rollback)

