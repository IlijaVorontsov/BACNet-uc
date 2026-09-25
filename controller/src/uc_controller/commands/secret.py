"""uc-ctl secret set NAME | ensure | list (DESIGN.md §5.3, §13).

Values are never printed. ``set`` reads the value from stdin (one trailing
newline is dropped).
"""

from __future__ import annotations

import logging
import os
import sys

from .. import config as _config
from ..cli import output
from ..secrets import SecretStore, check_name, standard_secrets

log = logging.getLogger(__name__)


def cmd_set(args) -> int:
    check_name(args.name)
    data = sys.stdin.buffer.read()
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        print("uc-ctl: error: empty value on stdin", file=sys.stderr)
        return 1
    SecretStore().set(args.name, data)
    output(args, {"result": "ok", "name": args.name}, f"secret {args.name} stored")
    return 0


def cmd_ensure(args) -> int:
    try:
        site = _config.load(args.config)
    except _config.ConfigError as exc:
        # first boot may run this before the site file is final: ensure the fixed set
        log.warning("site file not usable (%s); hub tokens are not ensured", exc.errors[0])
        site = None
    store = SecretStore()
    created = [n for n, k in standard_secrets(site) if store.ensure(n, k)]
    output(args, {"result": "ok", "created": created},
           "created: " + ", ".join(created) if created else "all secrets present")
    return 0


def cmd_list(args) -> int:
    store = SecretStore()
    rows = []
    for name in store.names():
        st = os.stat(store.path(name))
        rows.append({"name": name, "bytes": st.st_size, "mode": f"{st.st_mode & 0o777:04o}", "mtime": int(st.st_mtime)})
    output(args, rows, "\n".join(r["name"] for r in rows))
    return 0


def register(sub) -> None:
    p = sub.add_parser("secret", help="manage secrets in /etc/uc-controller/secrets")
    s = p.add_subparsers(dest="secret_cmd", metavar="ACTION", required=True)
    q = s.add_parser("set", help="store a secret read from stdin")
    q.add_argument("name")
    q.set_defaults(func=cmd_set)
    q = s.add_parser("ensure", help="generate every missing secret apply needs")
    q.set_defaults(func=cmd_ensure)
    q = s.add_parser("list", help="list secret names (never values)")
    q.set_defaults(func=cmd_list)
