"""time role (order 25): chrony configuration (DESIGN.md §7).

The runtime part lives in rootfs/: uc-time-policy (ExecStartPre of chrony,
writes /run/uc-controller/chrony-local.conf), uc-time-ready.service and the
chrony drop-in. This role renders /etc/chrony/chrony.conf (Debian's defaults
without "pool") and /etc/chrony/conf.d/10-uc.conf.
"""

from __future__ import annotations

from ..roles.base import Probe, ProbeResult, RenderContext, RenderedFile, Role, Validator

CHRONY_CONF = "/etc/chrony/chrony.conf"
UC_CONF = "/etc/chrony/conf.d/10-uc.conf"


class TimeRole(Role):
    name = "time"
    order = 25
    units = {"chrony": "restart"}

    def render(self, ctx: RenderContext) -> list[RenderedFile]:
        kw = dict(site=ctx.site, d=ctx.derived)
        return [
            RenderedFile(CHRONY_CONF, ctx.tpl.render("time/chrony.conf.j2", **kw).encode()),
            RenderedFile(UC_CONF, ctx.tpl.render("time/10-uc.conf.j2", **kw).encode()),
        ]

    def validators(self, ctx: RenderContext) -> list[Validator]:
        return [Validator("chronyd -p", ["chronyd", "-p", "-f", "{staged}" + CHRONY_CONF], needs="chronyd")]

    def enable_units(self, ctx: RenderContext) -> dict[str, bool]:
        return {"chrony": True, "uc-time-ready": True}

    def probes(self, ctx: RenderContext) -> list[Probe]:
        return [Probe("chronyc tracking", _probe_chrony)]


def _probe_chrony(ctx: RenderContext, runner) -> ProbeResult:
    """chronyd answers on its command socket (synchronisation is not required)."""
    res = runner.run(["chronyc", "-c", "tracking"], check=False)
    if res.returncode != 0:
        return ProbeResult(False, (res.stderr or "chronyc failed").strip()[-200:])
    return ProbeResult(True, res.stdout.strip()[-200:])


ROLE = TimeRole()
