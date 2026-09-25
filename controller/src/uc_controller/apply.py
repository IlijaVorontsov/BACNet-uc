"""The apply transaction (DESIGN.md §5.4).

    load + check config  ->  ensure secrets  ->  validation render (staging)
    ->  validators  ->  prepare  ->  install render + diff  ->  LKG snapshot
    ->  install (atomic)  ->  post_install  ->  enable units  ->  unit actions
    ->  probes (rollback on failure)  ->  confirm guard  ->  backup hook

Exit codes (§19.3): 0 ok or no change, 1 error, 2 validation refused,
3 probes failed and rolled back.

State: ``/var/lib/uc-controller/installed.json`` maps each installed path to
{sha256, mode, owner, group, role}; ``lkg/<txid>/`` holds the files an apply
replaced (``manifest.json`` + ``files/``), with "absent" markers for files
that did not exist; the last 5 are kept.
"""

from __future__ import annotations

import contextlib
import difflib
import errno
import fcntl
import grp
import hashlib
import importlib
import json
import logging
import os
import pwd
import secrets as _random
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field

from . import config as _config
from . import hold as _hold
from . import paths as _paths
from .model import Facts
from .render import Owned, RenderError, Templates, render_roles, write_tree
from .roles import UnknownRole, load_roles
from .roles.base import Probe, ProbeResult, RenderContext, RenderedFile, Role
from .runner import Runner
from .secrets import ReadOnlySecrets, SecretStore
from .validate import run_validators

log = logging.getLogger(__name__)

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_ROLLED_BACK = 0, 1, 2, 3
LKG_KEEP = 5
STAGING_KEEP = 3
CONFIRM_TIMEOUT_S = 180
# Roles whose changes can lock out a remote operator (§5.4 step 9).
CONFIRM_ROLES = ("network", "ssh")
ACTION_RANK = {"reload": 1, "try-restart": 2, "restart": 3}
SYSTEMCTL_VERB = {"reload": "reload-or-restart", "restart": "restart", "try-restart": "try-restart"}


class ApplyError(Exception):
    pass


@dataclass
class ApplyOptions:
    config: str = _paths.CONTROLLER_YAML
    only: list[str] | None = None
    dry_run: bool = False
    first_boot: bool = False
    no_systemd: bool = False


@dataclass
class ApplyResult:
    txid: str
    result: str = "ok"                  # ok | nochange | refused | rolled_back | error
    exit_code: int = EXIT_OK
    changed: list[str] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)       # unit -> systemctl verb
    enabled: dict[str, bool] = field(default_factory=dict)    # unit -> enabled
    validators: list[dict] = field(default_factory=list)
    probes: list[dict] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    dry_run: bool = False
    confirm: dict | None = None
    backup: str | None = None
    diff: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["diff"] is None:
            d.pop("diff")
        return d


@dataclass
class Change:
    path: str
    role: str
    kind: str                           # new | modified | removed
    file: RenderedFile | None           # None when removed


# ---------------------------------------------------------------------------
# helpers


def new_txid() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + _random.token_hex(2)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _write_json(path: str, data) -> None:
    _paths.atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600, dir_mode=0o700)


def load_installed(p: _paths.Paths) -> dict[str, dict]:
    raw = _read(p.installed_json)
    if not raw:
        return {}
    data = json.loads(raw)
    return data if isinstance(data, dict) else {}


def save_installed(p: _paths.Paths, installed: dict[str, dict]) -> None:
    _write_json(p.installed_json, dict(sorted(installed.items())))


def resolve_ids(owner: str, group: str, p: _paths.Paths) -> tuple[int, int]:
    """uid/gid of owner:group. Under UC_ROOT an unknown name falls back to
    the current uid/gid (tests do not create system users)."""
    try:
        uid = pwd.getpwnam(owner).pw_uid
    except KeyError:
        if not p.root:
            raise ApplyError(f"user {owner!r} does not exist") from None
        uid = os.getuid()
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        if not p.root:
            raise ApplyError(f"group {group!r} does not exist") from None
        gid = os.getgid()
    return uid, gid


def install_file(p: _paths.Paths, f: RenderedFile) -> None:
    uid, gid = resolve_ids(f.owner, f.group, p)
    target = p.p(f.path)
    try:
        _paths.atomic_write(target, f.content, mode=f.mode, uid=uid, gid=gid)
    except PermissionError:
        if not p.root:
            raise
        # unprivileged test run under UC_ROOT: keep the mode, skip chown
        _paths.atomic_write(target, f.content, mode=f.mode)


def remove_file(p: _paths.Paths, path: str) -> None:
    target = p.p(path)
    try:
        os.unlink(target)
    except FileNotFoundError:
        return
    _paths.fsync_dir(os.path.dirname(target))


def _mode_str(mode: int) -> str:
    return f"{mode:04o}"


def entry_for(role: str, f: RenderedFile) -> dict:
    return {"sha256": sha256(f.content), "mode": _mode_str(f.mode), "owner": f.owner,
            "group": f.group, "role": role}


@contextlib.contextmanager
def apply_lock(p: _paths.Paths):
    os.makedirs(os.path.dirname(p.apply_lock), mode=0o700, exist_ok=True)
    fd = os.open(p.apply_lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise ApplyError("another uc-ctl apply is running") from None
            raise
        yield
    finally:
        os.close(fd)


def build_context(site, devices, derived, secrets, facts: Facts, p: _paths.Paths,
                  first_boot: bool = False, hold: bool | None = None, prefix: str = "") -> RenderContext:
    return RenderContext(
        site=site, devices=list(devices), derived=derived, secrets=secrets,
        tpl=Templates(prefix), prefix=prefix, facts=facts, first_boot=first_boot,
        paths=p, hold=_hold.is_held(p) if hold is None else hold,
    )


def load_site(config_path: str, p: _paths.Paths):
    """(site, devices, derived); raises ConfigError."""
    site = _config.load(config_path)
    devices = _config.load_devices()
    derived = _config.derive(site, devices, _config.load_board(p))
    return site, devices, derived


# ---------------------------------------------------------------------------
# diff


def compute_changes(roles: list[Role], files: dict[str, Owned], installed: dict[str, dict],
                    p: _paths.Paths) -> list[Change]:
    """Files that are new or differ (in installed.json or on disk), plus
    files a selected role installed before but no longer renders."""
    selected = {r.name for r in roles}
    changes: list[Change] = []
    for path, owned in files.items():
        f = owned.file
        want = entry_for(owned.role, f)
        live_path = p.p(path)
        live = _read(live_path)
        entry = installed.get(path)
        differs = entry is None or any(entry.get(k) != want[k] for k in ("sha256", "mode", "owner", "group", "role"))
        if live is None:
            differs = True
        elif sha256(live) != want["sha256"] or (os.stat(live_path).st_mode & 0o7777) != f.mode:
            differs = True
        if differs:
            changes.append(Change(path, owned.role, "new" if live is None else "modified", f))
    for path, entry in sorted(installed.items()):
        if entry.get("role") in selected and path not in files:
            changes.append(Change(path, entry["role"], "removed", None))
    return sorted(changes, key=lambda c: c.path)


def render_diff(changes: list[Change], p: _paths.Paths) -> str:
    out = []
    for c in changes:
        old = _read(p.p(c.path)) or b""
        new = c.file.content if c.file else b""
        secret = (c.file.secret if c.file else False) or c.path.startswith(_paths.SECRETS_DIR + "/")
        header = f"=== {c.kind}: {c.path}"
        if c.file:
            header += f" ({_mode_str(c.file.mode)} {c.file.owner}:{c.file.group}, role {c.role})"
        out.append(header)
        if secret:
            out.append("    (secret content masked)")
            continue
        try:
            a, b = old.decode().splitlines(True), new.decode().splitlines(True)
        except UnicodeDecodeError:
            out.append("    (binary content differs)")
            continue
        out.extend(line.rstrip("\n") for line in
                   difflib.unified_diff(a, b, f"a{c.path}", f"b{c.path}", n=3))
    return "\n".join(out) + ("\n" if out else "")


# ---------------------------------------------------------------------------
# LKG snapshots


def lkg_dir(p: _paths.Paths, txid: str) -> str:
    return os.path.join(p.lkg, txid)


def snapshot(p: _paths.Paths, txid: str, changes: list[Change], installed: dict[str, dict],
             unit_actions: dict[str, str]) -> str:
    """Save the live state of every affected path before an install."""
    base = lkg_dir(p, txid)
    os.makedirs(os.path.join(base, "files"), mode=0o700, exist_ok=True)
    manifest = {"txid": txid, "time": int(time.time()), "files": {}, "installed": {},
                "unit_actions": unit_actions, "roles": sorted({c.role for c in changes})}
    for c in changes:
        live_path = p.p(c.path)
        data = _read(live_path)
        if data is None:
            manifest["files"][c.path] = {"absent": True}
        else:
            st = os.stat(live_path)
            copy = os.path.join(base, "files", c.path.lstrip("/"))
            os.makedirs(os.path.dirname(copy), mode=0o700, exist_ok=True)
            with open(copy, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(copy, 0o600)
            manifest["files"][c.path] = {"absent": False, "sha256": sha256(data),
                                         "mode": _mode_str(st.st_mode & 0o7777),
                                         "uid": st.st_uid, "gid": st.st_gid}
        manifest["installed"][c.path] = installed.get(c.path)
    _write_json(os.path.join(base, "manifest.json"), manifest)
    _paths.fsync_dir(base)
    return base


def prune_dirs(parent: str, keep: int) -> None:
    try:
        names = sorted(n for n in os.listdir(parent) if not n.startswith("."))
    except FileNotFoundError:
        return
    for name in names[:-keep] if keep else names:
        shutil.rmtree(os.path.join(parent, name), ignore_errors=True)


def list_lkg(p: _paths.Paths) -> list[str]:
    try:
        return sorted(n for n in os.listdir(p.lkg)
                      if os.path.isfile(os.path.join(p.lkg, n, "manifest.json")))
    except FileNotFoundError:
        return []


def restore_snapshot(p: _paths.Paths, txid: str, runner: Runner, result: ApplyResult | None = None) -> list[str]:
    """Put back the files of lkg/<txid>, fix installed.json, re-run the unit actions."""
    base = lkg_dir(p, txid)
    raw = _read(os.path.join(base, "manifest.json"))
    if raw is None:
        raise ApplyError(f"no LKG snapshot {txid}")
    manifest = json.loads(raw)
    restored = []
    for path, meta in sorted(manifest["files"].items()):
        if meta.get("absent"):
            remove_file(p, path)
        else:
            data = _read(os.path.join(base, "files", path.lstrip("/")))
            if data is None:
                log.error("LKG %s: copy of %s is missing", txid, path)
                continue
            try:
                _paths.atomic_write(p.p(path), data, mode=int(meta["mode"], 8), uid=meta["uid"], gid=meta["gid"])
            except PermissionError:
                if not p.root:
                    raise
                _paths.atomic_write(p.p(path), data, mode=int(meta["mode"], 8))
        restored.append(path)
    installed = load_installed(p)
    for path, prev in manifest.get("installed", {}).items():
        if prev is None:
            installed.pop(path, None)
        else:
            installed[path] = prev
    save_installed(p, installed)
    if any(_is_unit_path(x) for x in restored):
        runner.systemctl("daemon-reload")
    for unit, action in manifest.get("unit_actions", {}).items():
        verb = SYSTEMCTL_VERB[action]
        res = runner.systemctl(verb, unit)
        if result is not None:
            result.units[unit] = verb
        if res.returncode != 0:
            log.error("rollback: systemctl %s %s failed: %s", verb, unit, (res.stderr or "").strip())
    log.warning("restored the last known good files of %s (%d paths)", txid, len(restored))
    return restored


def _is_unit_path(path: str) -> bool:
    return path.startswith(("/etc/systemd/system/", "/usr/lib/systemd/system/", "/lib/systemd/system/"))


# ---------------------------------------------------------------------------
# probes and units


def merge_unit_actions(roles: list[Role], changed_by_role: dict[str, list[str]]) -> dict[str, str]:
    """Unit actions of the changed roles, deduplicated in role order; the
    stronger action wins (restart > try-restart > reload)."""
    actions: dict[str, str] = {}
    for role in roles:
        changed = changed_by_role.get(role.name)
        if not changed:
            continue
        for unit, action in role.unit_actions(sorted(changed)).items():
            if action not in ACTION_RANK:
                raise ApplyError(f"role {role.name}: unknown unit action {action!r} for {unit}")
            if unit not in actions or ACTION_RANK[action] > ACTION_RANK[actions[unit]]:
                actions[unit] = action
    return actions


def run_probe(role: str, probe: Probe, ctx: RenderContext, runner: Runner) -> dict:
    attempts = max(1, probe.retries)
    res = ProbeResult(False, "not run")
    for attempt in range(1, attempts + 1):
        try:
            res = probe.run(ctx, runner)
        except Exception as exc:  # a crashing probe is a failed probe
            res = ProbeResult(False, f"{type(exc).__name__}: {exc}")
        if res.ok:
            break
        if attempt < attempts:
            time.sleep(probe.delay_s)
    level = logging.INFO if res.ok else logging.ERROR
    log.log(level, "probe %s/%s: %s %s", role, probe.name, "ok" if res.ok else "FAILED", res.detail)
    return {"role": role, "name": probe.name, "ok": res.ok, "detail": res.detail, "attempts": attempt}


# ---------------------------------------------------------------------------
# the transaction


class Transaction:
    def __init__(self, opts: ApplyOptions, runner: Runner | None = None, facts: Facts | None = None,
                 secrets=None, roles: list[Role] | None = None, out=None):
        self.opts = opts
        self.p = _paths.current()
        self.runner = runner or Runner(no_systemd=opts.no_systemd)
        self.runner.no_systemd = self.runner.no_systemd or opts.no_systemd
        self.facts = facts
        self.secrets = secrets
        self.roles = roles
        self.out = out or sys.stdout
        self.result = ApplyResult(txid=new_txid(), dry_run=opts.dry_run)

    # -- steps ---------------------------------------------------------------

    def _fail(self, result: str, code: int, errors) -> ApplyResult:
        self.result.result = result
        self.result.exit_code = code
        self.result.errors.extend([errors] if isinstance(errors, str) else list(errors))
        return self.result

    def _setup(self):
        """Steps 1-2: config, roles, secrets, contexts. Returns base ctx or None."""
        r = self.result
        try:
            site, devices, derived = load_site(self.opts.config, self.p)
        except _config.ConfigError as exc:
            self._fail("refused", EXIT_REFUSED, exc.errors)
            return None
        if self.roles is None:
            try:
                self.roles = load_roles(self.opts.only)
            except UnknownRole as exc:
                self._fail("error", EXIT_ERROR, str(exc))
                return None
        elif self.opts.only:
            self.roles = [x for x in self.roles if x.name in self.opts.only]
        r.roles = [x.name for x in self.roles]
        store = self.secrets if self.secrets is not None else SecretStore(runner=self.runner)
        if self.opts.dry_run:
            store = ReadOnlySecrets(store) if isinstance(store, SecretStore) else store
        else:
            created = store.ensure_standard(site)
            if created:
                log.info("secrets created: %s", ", ".join(created))
        facts = self.facts or Facts.detect(self.runner)
        return build_context(site, devices, derived, store, facts, self.p, first_boot=self.opts.first_boot)

    def _validate(self, ctx: RenderContext) -> bool:
        """Steps 3-4. Returns True when every validator passed."""
        staging = os.path.join(self.p.staging, self.result.txid)
        os.makedirs(staging, mode=0o700, exist_ok=True)
        vctx = ctx.with_prefix(staging)
        try:
            vfiles = render_roles(self.roles, vctx)
        except _config.ConfigError as exc:
            self._fail("refused", EXIT_REFUSED, exc.errors)
            return False
        write_tree(vfiles, staging)
        items = [(role.name, v) for role in self.roles if role.enabled(vctx) for v in role.validators(vctx)]
        results = run_validators(items, staging, self.runner)
        self.result.validators = [x.to_dict() for x in results]
        failed = [x for x in results if not x.ok]
        if failed:
            self._fail("refused", EXIT_REFUSED,
                       [f"validator {x.role}/{x.name} failed (rc={x.rc}): {x.stderr[-300:]}" for x in failed])
            log.error("staged files kept in %s", staging)
            return False
        shutil.rmtree(staging, ignore_errors=True)
        prune_dirs(self.p.staging, STAGING_KEEP)
        return True

    def run(self) -> ApplyResult:
        r = self.result
        try:
            with apply_lock(self.p):
                self._run()
        except ApplyError as exc:
            self._fail("error", EXIT_ERROR, str(exc))
        except RenderError as exc:
            self._fail("error", EXIT_ERROR, f"render: {exc}")
        r.deferred = list(self.runner.deferred) + [d for d in r.deferred if d not in self.runner.deferred]
        return r

    def _run(self) -> None:
        r = self.result
        ctx = self._setup()
        if ctx is None or not self._validate(ctx):
            return

        # prepare hooks run before the install render: they are idempotent and
        # may create what rendering or installing needs (users, keys).
        if not self.opts.dry_run:
            for role in self.roles:
                if role.enabled(ctx):
                    role.prepare(ctx, self.runner)

        # 5. install render + diff
        try:
            files = render_roles(self.roles, ctx)
        except _config.ConfigError as exc:
            self._fail("refused", EXIT_REFUSED, exc.errors)
            return
        installed = load_installed(self.p)
        changes = compute_changes(self.roles, files, installed, self.p)
        r.changed = [c.path for c in changes]
        if self.opts.dry_run:
            r.diff = render_diff(changes, self.p)
            r.result = "ok" if changes else "nochange"
            return
        changed_by_role: dict[str, list[str]] = {}
        for c in changes:
            changed_by_role.setdefault(c.role, []).append(c.path)
        if not changes and not self.opts.first_boot:
            r.result = "nochange"
            log.info("no change")
            return

        # 6. snapshot + install
        actions = merge_unit_actions(self.roles, changed_by_role)
        if changes:
            snapshot(self.p, r.txid, changes, installed, actions)
            prune_dirs(self.p.lkg, LKG_KEEP)
        for c in changes:
            if c.kind == "removed":
                remove_file(self.p, c.path)
                installed.pop(c.path, None)
            else:
                install_file(self.p, c.file)
                installed[c.path] = entry_for(c.role, c.file)
        save_installed(self.p, installed)

        # 7. post_install, daemon-reload, enable/disable, unit actions
        active_roles = [x for x in self.roles if x.enabled(ctx)]
        hook_roles = active_roles if self.opts.first_boot else [x for x in active_roles if x.name in changed_by_role]
        for role in hook_roles:
            role.post_install(ctx, self.runner)
        if any(_is_unit_path(c.path) for c in changes):
            self.runner.systemctl("daemon-reload")
        enable: dict[str, bool] = {}
        for role in self.roles if self.opts.first_boot else [x for x in self.roles if x.name in changed_by_role]:
            enable.update(role.enable_units(ctx))
        for unit, on in enable.items():
            if on:
                self.runner.systemctl("enable", unit)
            else:
                self.runner.systemctl("disable", unit, *([] if self.opts.first_boot else ["--now"]))
        r.enabled = enable
        failed_units = []
        for unit, action in actions.items():
            verb = SYSTEMCTL_VERB[action]
            if enable.get(unit) is False:
                continue  # disabled (and stopped) above
            if self.opts.first_boot:
                r.deferred.append(f"systemctl {verb} {unit}")
                continue
            res = self.runner.systemctl(verb, unit)
            r.units[unit] = verb
            if res.returncode != 0:
                failed_units.append({"role": "-", "name": f"systemctl {verb} {unit}", "ok": False,
                                     "detail": (res.stderr or "").strip()[-300:], "attempts": 1})

        # 8. probes
        probes_ok = not failed_units
        r.probes.extend(failed_units)
        if probes_ok:
            for role in active_roles:
                if role.name not in changed_by_role:
                    continue
                for probe in role.probes(ctx):
                    if self.opts.first_boot or self.runner.no_systemd:
                        r.deferred.append(f"probe {role.name}/{probe.name}")
                        continue
                    pr = run_probe(role.name, probe, ctx, self.runner)
                    r.probes.append(pr)
                    probes_ok = probes_ok and pr["ok"]
        if not probes_ok:
            restore_snapshot(self.p, r.txid, self.runner, r)
            self._fail("rolled_back", EXIT_ROLLED_BACK,
                       [f"probe {x['role']}/{x['name']} failed: {x['detail']}" for x in r.probes if not x["ok"]])
            self._record()
            return

        # 9. confirm guard for remote sessions
        if (os.environ.get("SSH_CONNECTION") and not self.opts.first_boot
                and any(role in changed_by_role for role in CONFIRM_ROLES)):
            self._arm_confirm(changes)

        # 10. site bundle backup
        r.result = "ok"
        self._backup(ctx)
        self._record()

    def _arm_confirm(self, changes: list[Change]) -> None:
        r = self.result
        unit = f"uc-ctl-confirm-{r.txid}"
        argv = ["systemd-run", f"--on-active={CONFIRM_TIMEOUT_S}", f"--unit={unit}",
                _paths.UC_CTL, "rollback-config", "--if-unconfirmed", r.txid]
        if self.runner.no_systemd:
            r.deferred.append(" ".join(argv))
            return
        res = self.runner.run(argv, check=False)
        if res.returncode != 0:
            # Without the guard a lock-out cannot be undone automatically: roll back now.
            log.error("systemd-run failed (%s); rolling back", (res.stderr or "").strip())
            restore_snapshot(self.p, r.txid, self.runner, r)
            self._fail("rolled_back", EXIT_ROLLED_BACK, "could not arm the confirm timer")
            return
        _write_json(self.p.pending_confirm, {"txid": r.txid, "unit": unit, "armed": int(time.time()),
                                             "timeout_s": CONFIRM_TIMEOUT_S,
                                             "paths": [c.path for c in changes]})
        r.confirm = {"unit": unit, "timeout_s": CONFIRM_TIMEOUT_S}
        log.warning("network/SSH changed: run 'uc-ctl confirm' from a NEW SSH session within %d s, "
                    "or the change is rolled back", CONFIRM_TIMEOUT_S)

    def _backup(self, ctx: RenderContext) -> None:
        modname = f"{__package__}.ops.backup"  # WP5
        try:
            backup = importlib.import_module(modname)
        except ImportError as exc:
            if exc.name not in (f"{__package__}.ops", modname):
                log.warning("backup hook unavailable: %s", exc)
            self.result.backup = "unavailable"
            return
        try:
            backup.after_apply(ctx, self.runner)
            self.result.backup = "ok"
        except Exception as exc:  # the hook must never fail an apply
            log.error("backup after apply failed: %s", exc)
            self.result.backup = f"error: {exc}"

    def _record(self) -> None:
        r = self.result
        _write_json(self.p.last_apply, {"txid": r.txid, "result": r.result, "time": int(time.time()),
                                        "changed": r.changed, "roles": r.roles})


def apply(opts: ApplyOptions, **kw) -> ApplyResult:
    """Run one apply transaction. Keyword arguments go to Transaction."""
    return Transaction(opts, **kw).run()


# ---------------------------------------------------------------------------
# check, confirm, rollback-config


def check(opts: ApplyOptions, **kw) -> ApplyResult:
    """Steps 1-5 without prepare or install: config, render, validators, pending diff."""
    opts = ApplyOptions(**{**asdict(opts), "dry_run": True})
    t = Transaction(opts, **kw)
    r = t.result
    try:
        with apply_lock(t.p):
            ctx = t._setup()
            if ctx is None or not t._validate(ctx):
                return r
            files = render_roles(t.roles, ctx)
            r.changed = [c.path for c in compute_changes(t.roles, files, load_installed(t.p), t.p)]
            r.result = "ok"
    except (ApplyError, RenderError) as exc:
        t._fail("error", EXIT_ERROR, str(exc))
    except _config.ConfigError as exc:
        t._fail("refused", EXIT_REFUSED, exc.errors)
    return r


def pending_confirm(p: _paths.Paths | None = None) -> dict | None:
    raw = _read((p or _paths.current()).pending_confirm)
    return json.loads(raw) if raw else None


def confirm(runner: Runner | None = None) -> tuple[int, str]:
    p = _paths.current()
    runner = runner or Runner()
    pending = pending_confirm(p)
    if not pending:
        return EXIT_OK, "nothing to confirm"
    runner.systemctl("stop", pending["unit"] + ".timer")
    try:
        os.unlink(p.pending_confirm)
    except FileNotFoundError:
        pass
    return EXIT_OK, f"confirmed {pending['txid']}"


def rollback_config(if_unconfirmed: str | None = None, runner: Runner | None = None) -> tuple[int, str]:
    """Restore the files of the last apply (or of TXID while it is unconfirmed)."""
    p = _paths.current()
    runner = runner or Runner()
    with apply_lock(p):
        pending = pending_confirm(p)
        if if_unconfirmed is not None:
            if not pending or pending.get("txid") != if_unconfirmed:
                return EXIT_OK, f"{if_unconfirmed} was confirmed; nothing to do"
            txid = if_unconfirmed
        else:
            snaps = list_lkg(p)
            if not snaps:
                return EXIT_ERROR, "no last-known-good snapshot"
            txid = snaps[-1]
        restored = restore_snapshot(p, txid, runner)
        if pending:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(p.pending_confirm)
        # the snapshot is used up: a second rollback goes one apply further back
        shutil.rmtree(lkg_dir(p, txid), ignore_errors=True)
        _write_json(p.last_apply, {"txid": txid, "result": "rolled_back", "time": int(time.time()),
                                   "changed": restored, "roles": []})
    return EXIT_OK, f"rolled back {txid} ({len(restored)} files)"
