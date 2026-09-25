"""The role interface (DESIGN.md §19.2). Owned by WP1; other work packages
implement roles against it.

A role renders files from the site configuration, names the validators that
check them, the units to reload when they change, and the probes that prove
the result works. apply.py drives the transaction (§5.4).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from ..model import Derived, Facts, Site
    from ..paths import Paths
    from ..render import Templates
    from ..runner import Runner


@dataclass(frozen=True)
class RenderedFile:
    path: str                  # absolute target path, e.g. "/etc/mosquitto/mosquitto.conf"
    content: bytes
    mode: int = 0o644
    owner: str = "root"
    group: str = "root"
    secret: bool = False       # content masked in diffs and logs


@dataclass(frozen=True)
class Validator:
    name: str
    argv: list[str]            # "{staged}" is replaced by the staging root
    needs: str | None = None   # executable; validator skipped with a warning if absent
    ok_codes: tuple[int, ...] = (0,)
    timeout_s: float = 30.0


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Probe:
    name: str
    run: Callable[["RenderContext", "Runner"], ProbeResult]
    retries: int = 3
    delay_s: float = 2.0


@dataclass(frozen=True)
class RenderContext:
    """Everything a role may use while rendering.

    ``prefix`` is "" for the install pass and the staging root for the
    validation pass; templates write every absolute path of a file that the
    same apply renders as ``{{ P('/etc/...') }}`` so validators see the
    staged copy.
    """
    site: "Site"                   # validated controller.yaml (model.Site)
    devices: list                  # devices.yaml entries (uc_controller.devices.Device)
    derived: "Derived"             # §5.1 table
    secrets: Any                   # SecretStore-like: get, ensure, path, names, exists
    tpl: "Templates"               # tpl.render(name, **vars) -> str
    prefix: str                    # "" (install) or the staging root (validation)
    facts: "Facts"
    first_boot: bool
    paths: "Paths"
    hold: bool = False             # /etc/uc-controller/hold exists (§6.3)
    extra: dict = field(default_factory=dict)   # free slot for roles; not used by WP1

    def P(self, path: str) -> str:  # noqa: N802 (same name as the template global)
        return self.prefix + path

    def with_prefix(self, prefix: str) -> "RenderContext":
        return dataclasses.replace(self, prefix=prefix, tpl=self.tpl.with_prefix(prefix))

    def file(self, path: str, text: str | bytes, **kw) -> RenderedFile:
        """Shortcut: a RenderedFile from text."""
        data = text.encode() if isinstance(text, str) else text
        return RenderedFile(path=path, content=data, **kw)


class Role:
    name: str = ""                         # "broker"
    order: int = 100                       # install/reload order, lower first
    units: dict[str, str] = {}             # unit -> "reload" | "restart" | "try-restart"

    def enabled(self, ctx: RenderContext) -> bool:
        return True

    def render(self, ctx: RenderContext) -> list[RenderedFile]:
        return []

    def validators(self, ctx: RenderContext) -> list[Validator]:
        return []

    def prepare(self, ctx: RenderContext, run: "Runner") -> None:
        """Idempotent setup before install (users, dirs, clusters)."""

    def post_install(self, ctx: RenderContext, run: "Runner") -> None:
        """After files are installed, before reload."""

    def probes(self, ctx: RenderContext) -> list[Probe]:
        return []

    def enable_units(self, ctx: RenderContext) -> dict[str, bool]:
        """unit -> enabled."""
        return {}

    def unit_actions(self, changed: list[str]) -> dict[str, str]:
        """Changed target paths -> unit actions."""
        return dict(self.units) if changed else {}

    def __repr__(self) -> str:
        return f"<role {self.name} order={self.order}>"


UNIT_ACTIONS = ("reload", "try-restart", "restart")
