"""uc-ctl time status | set (DESIGN.md §5.3, §7).

``time status`` reports chrony's tracking data (``chronyc -c tracking``) and
the RTC decision of uc-time-policy.

``time set "YYYY-MM-DD HH:MM:SS"`` (UTC) is for isolated sites: it feeds the
time to chrony (``chronyc settime``, allowed by the ``manual`` directive),
writes it to the RTC (``hwclock --systohc``) when rtc0 exists, and then
restarts chrony so uc-time-policy sees a valid RTC and chrony serves the
local reference at once.
"""

from __future__ import annotations

import json
import os
import time as _time

from .. import paths as _paths
from ..cli import output
from ..runner import Runner

TRACKING_FIELDS = ("ref_id", "ref_name", "stratum", "ref_time", "system_time", "last_offset",
                   "rms_offset", "frequency", "residual_freq", "skew", "root_delay",
                   "root_dispersion", "update_interval", "leap_status")
LOCAL_REFID = "7F7F0101"


def parse_tracking(text: str) -> dict:
    """Parse the CSV line of ``chronyc -c tracking``."""
    line = text.strip().splitlines()[-1] if text.strip() else ""
    parts = line.split(",")
    if len(parts) < len(TRACKING_FIELDS):
        return {}
    data = dict(zip(TRACKING_FIELDS, parts))
    for key in ("stratum",):
        data[key] = int(data[key])
    for key in TRACKING_FIELDS[3:13]:
        try:
            data[key] = float(data[key])
        except ValueError:
            pass
    data["synchronised"] = data["leap_status"] != "Not synchronised"
    data["local_reference"] = data["ref_id"].upper() == LOCAL_REFID
    return data


def rtc_decision(runner: Runner) -> dict:
    res = runner.run([str(_paths.helper("uc-time-policy")), "--check", "--json"], check=False)
    try:
        return json.loads(res.stdout)
    except ValueError:
        return {"rtc_valid": None, "reason": (res.stderr or res.stdout or "uc-time-policy failed").strip()}


def cmd_status(args) -> int:
    runner = Runner()
    res = runner.run(["chronyc", "-c", "tracking"], check=False)
    tracking = parse_tracking(res.stdout) if res.returncode == 0 else {}
    p = _paths.current()
    data = {
        "chrony": tracking or {"error": (res.stderr or "chronyc failed").strip()},
        "rtc": rtc_decision(runner),
        "time_untrusted": os.path.exists(p.time_untrusted),
        "utc": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
    }
    if tracking:
        state = "synchronised" if tracking["synchronised"] else "NOT synchronised"
        ref = "local reference (RTC)" if tracking["local_reference"] else tracking["ref_name"] or tracking["ref_id"]
        chrony_line = f"chrony: {state}, stratum {tracking['stratum']}, source {ref}, offset {tracking['system_time']:+.6f} s"
    else:
        chrony_line = f"chrony: unavailable ({data['chrony']['error']})"
    rtc = data["rtc"]
    rtc_line = f"rtc: {'valid' if rtc.get('rtc_valid') else 'INVALID'} ({rtc.get('reason', '')})"
    text = "\n".join([f"utc: {data['utc']}", chrony_line, rtc_line]
                     + (["time-untrusted: set (uc-time-ready timed out at boot)"] if data["time_untrusted"] else []))
    output(args, data, text)
    return 0


def cmd_set(args) -> int:
    try:
        stamp = _time.strptime(args.when, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print('uc-ctl: error: expected "YYYY-MM-DD HH:MM:SS" (UTC)', flush=True)
        return 1
    when = _time.strftime("%Y-%m-%d %H:%M:%S", stamp)
    runner = Runner(no_systemd=args.no_systemd)
    steps = []
    runner.run(["chronyc", "settime", when])
    steps.append("chronyc settime")
    if os.path.exists(_paths.p(_paths.DEV_RTC)):
        runner.run(["hwclock", "--systohc", "--utc"])
        steps.append("hwclock --systohc")
        # the RTC is valid now: restart chrony so uc-time-policy serves the local reference
        runner.systemctl("restart", "chrony", check=True)
        steps.append("systemctl restart chrony")
    output(args, {"result": "ok", "time": when, "steps": steps, "deferred": runner.deferred},
           f"time set to {when} UTC ({', '.join(steps)})")
    return 0


def register(sub) -> None:
    p = sub.add_parser("time", help="clock and NTP state")
    s = p.add_subparsers(dest="time_cmd", metavar="ACTION", required=True)
    q = s.add_parser("status", help="chrony tracking and RTC validity")
    q.set_defaults(func=cmd_status)
    q = s.add_parser("set", help='set the clock: time set "YYYY-MM-DD HH:MM:SS" (UTC)')
    q.add_argument("when", metavar='"YYYY-MM-DD HH:MM:SS"')
    q.add_argument("--no-systemd", action="store_true", help="no systemctl calls (containers)")
    q.set_defaults(func=cmd_set)
