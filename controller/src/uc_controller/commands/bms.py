"""uc-ctl bms up | down (DESIGN.md §5.3, §6.3 hold mode, §14.3).

``bms down`` sets the hold marker (the BMS zone of the firewall only drops,
uc-hub does not start), re-applies the network role and stops uc-hub.
``bms up`` does the reverse.
"""

from __future__ import annotations

from .. import apply as _apply
from .. import hold as _hold
from ..cli import output
from ..runner import Runner
from .apply import _print_result


def _run(args, up: bool) -> int:
    runner = Runner(no_systemd=args.no_systemd)
    if up:
        existed = _hold.clear()
    else:
        existed = _hold.is_held()
        _hold.set("uc-ctl bms down")
    opts = _apply.ApplyOptions(config=args.config, only=["network"], no_systemd=args.no_systemd)
    res = _apply.apply(opts, runner=runner)
    if res.exit_code != 0:
        # keep the marker consistent with the firewall that is actually loaded
        if up and existed:
            _hold.set("uc-ctl bms up failed")
        elif not up and not existed:
            _hold.clear()
        return _print_result(args, res)
    hub = runner.systemctl("start" if up else "stop", "uc-hub")
    res.units["uc-hub"] = "start" if up else "stop"
    if hub.returncode != 0:
        res.errors.append(f"systemctl {'start' if up else 'stop'} uc-hub failed: {(hub.stderr or '').strip()}")
    res.deferred = list(runner.deferred)
    if args.json:
        output(args, {**res.to_dict(), "hold": not up})
        return 0 if hub.returncode == 0 else 1
    _print_result(args, res)
    print("hold cleared: BMS zone open, uc-hub started" if up else "hold set: BMS zone closed, uc-hub stopped")
    return 0 if hub.returncode == 0 else 1


def register(sub) -> None:
    p = sub.add_parser("bms", help="open or close the BMS side (hold mode)")
    s = p.add_subparsers(dest="bms_cmd", metavar="ACTION", required=True)
    for name, up, text in (("up", True, "clear hold, open the BMS zone, start uc-hub"),
                           ("down", False, "set hold, close the BMS zone, stop uc-hub")):
        q = s.add_parser(name, help=text)
        q.add_argument("--no-systemd", action="store_true", help="no systemctl calls (containers)")
        q.set_defaults(func=lambda args, up=up: _run(args, up))
