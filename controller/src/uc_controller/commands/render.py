"""uc-ctl render --out DIR [--only ROLES] (DESIGN.md §5.3).

Renders the install pass (absolute paths inside the files are the real
ones) into DIR/<abs path>. Nothing on the system changes; missing secrets
are shown as placeholders.

Test options: ``--fake`` uses FakeFacts and deterministic fake secrets (the
golden tests use it), ``--staged`` renders like the validation pass, with
DIR as the prefix of every absolute path inside the files.
"""

from __future__ import annotations

import os

from .. import apply as _apply
from .. import paths as _paths
from ..cli import output
from ..model import Facts
from ..render import render_roles, write_tree
from ..roles import load_roles, parse_only
from ..runner import Runner
from ..secrets import ReadOnlySecrets, SecretStore


def cmd_render(args) -> int:
    p = _paths.current()
    site, devices, derived = _apply.load_site(args.config, p)
    out = os.path.abspath(args.out)
    if args.fake:
        from ..testing import FakeFacts, FakeSecrets
        facts, store = FakeFacts(), FakeSecrets()
    else:
        facts, store = Facts.detect(Runner()), ReadOnlySecrets(SecretStore())
    ctx = _apply.build_context(site, devices, derived, store, facts, p,
                               prefix=out if args.staged else "")
    files = render_roles(load_roles(parse_only(args.only)), ctx)
    os.makedirs(out, exist_ok=True)
    write_tree(files, out)
    output(args, {"out": out, "files": {k: v.role for k, v in files.items()}},
           "\n".join(f"{v.role:10} {k}" for k, v in files.items()))
    return 0


def register(sub) -> None:
    p = sub.add_parser("render", help="render the configuration into a directory")
    p.add_argument("--out", required=True, metavar="DIR")
    p.add_argument("--only", metavar="ROLES", help="comma-separated roles")
    p.add_argument("--fake", action="store_true", help="test mode: fake facts and deterministic fake secrets")
    p.add_argument("--staged", action="store_true", help="prefix absolute paths inside the files with DIR")
    p.set_defaults(func=cmd_render)
