"""ssh role (order 30): sshd drop-in, authorized keys, admin accounts
(DESIGN.md §18, §5.5 step 3).

Keys live in /etc/ssh/authorized_keys/<user> (root-owned, rendered), so a
user cannot add keys of their own. Admin accounts are in the groups sudo
and ssh-admins and must set a password at their first login (chage -d 0).
"""

from __future__ import annotations

import logging

from ..roles.base import Probe, ProbeResult, RenderContext, RenderedFile, Role, Validator
from ..runner import FakeRunner

log = logging.getLogger(__name__)

DROPIN = "/etc/ssh/sshd_config.d/10-uc.conf"
KEYS_DIR = "/etc/ssh/authorized_keys"
USER_CA = "/etc/ssh/uc-user-ca.pub"
# Rendered in the validation pass only; never installed.
VALIDATE_DIR = "/.uc-validate/ssh"
ADMIN_GROUPS = ("sudo", "ssh-admins")

# sshd -t needs a host key and /run/sshd; both are made here, next to the
# staged files ($1 = staged validation dir).
VALIDATE_SCRIPT = (
    'set -e; d="$1"; rm -f "$d/hostkey" "$d/hostkey.pub"; '
    'ssh-keygen -q -t ed25519 -N "" -f "$d/hostkey" </dev/null; '
    'mkdir -p /run/sshd; exec sshd -t -f "$d/sshd_config"'
)


class SshRole(Role):
    name = "ssh"
    order = 30
    units = {"ssh": "reload"}

    def render(self, ctx: RenderContext) -> list[RenderedFile]:
        site = ctx.site
        tpl = ctx.tpl
        files = [RenderedFile(DROPIN, tpl.render("ssh/10-uc.conf.j2", user_ca=bool(site.ssh_user_ca)).encode())]
        for admin in site.admins:
            files.append(RenderedFile(f"{KEYS_DIR}/{admin.name}",
                                      tpl.render("ssh/authorized_keys.j2", admin=admin).encode()))
        if site.ssh_user_ca:
            files.append(RenderedFile(USER_CA, tpl.render("ssh/user-ca.pub.j2", keys=site.ssh_user_ca).encode()))
        if ctx.prefix:
            text = tpl.render("ssh/validate-wrapper.j2", hostkey=VALIDATE_DIR + "/hostkey")
            files.append(RenderedFile(VALIDATE_DIR + "/sshd_config", text.encode(), mode=0o600))
        return files

    def validators(self, ctx: RenderContext) -> list[Validator]:
        return [Validator("sshd -t", ["sh", "-c", VALIDATE_SCRIPT, "sshd-validate", "{staged}" + VALIDATE_DIR],
                          needs="sshd")]

    def prepare(self, ctx: RenderContext, run) -> None:
        if ctx.paths.root and not isinstance(run, FakeRunner):
            log.warning("UC_ROOT is set: admin accounts are not created on this system")
            return
        if run.run(["getent", "group", "ssh-admins"], check=False).returncode != 0:
            run.run(["groupadd", "-r", "ssh-admins"])
        for admin in ctx.site.admins:
            if run.run(["getent", "passwd", admin.name], check=False).returncode != 0:
                run.run(["useradd", "-m", "-s", "/bin/bash", "-G", ",".join(ADMIN_GROUPS), admin.name])
                # An empty, expired password: the first login (with a key)
                # makes the admin choose the sudo password.
                run.run(["passwd", "-d", admin.name])
                run.run(["chage", "-d", "0", admin.name])
                log.info("admin account %s created", admin.name)
                continue
            groups = run.run(["id", "-nG", admin.name], check=False).stdout.split()
            missing = [g for g in ADMIN_GROUPS if g not in groups]
            if missing:
                run.run(["usermod", "-a", "-G", ",".join(missing), admin.name])

    def enable_units(self, ctx: RenderContext) -> dict[str, bool]:
        return {"ssh": True}

    def probes(self, ctx: RenderContext) -> list[Probe]:
        return [Probe("sshd -t (live config)", _probe_sshd, retries=1)]


def _probe_sshd(ctx: RenderContext, runner) -> ProbeResult:
    res = runner.run(["sshd", "-t"], check=False)
    return ProbeResult(res.returncode == 0, (res.stderr or "live sshd config ok").strip()[-200:])


ROLE = SshRole()
