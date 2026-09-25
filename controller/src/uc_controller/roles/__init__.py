"""Role registry (DESIGN.md §19.2).

Role modules are imported lazily. A module that does not exist yet (another
work package has not landed) is skipped with a warning, so every work
package can be built and tested on its own.
"""

from __future__ import annotations

import importlib
import logging

from .base import Role

log = logging.getLogger(__name__)

ROLE_MODULES = ["os", "boot", "network", "ssh", "time", "dhcp",
                "broker", "postgres", "historian", "hub", "proxy", "ops"]


class UnknownRole(Exception):
    pass


def _import(name: str):
    modname = f"{__name__}.{name}"
    try:
        module = importlib.import_module(modname)
    except ImportError as exc:
        if exc.name == modname:
            log.warning("role %s is not available (module %s missing); skipped", name, modname)
        else:
            log.warning("role %s is not available (%s); skipped", name, exc)
        return None
    role = getattr(module, "ROLE", None)
    if not isinstance(role, Role):
        log.warning("role module %s has no ROLE; skipped", modname)
        return None
    return role


def load_roles(only: list[str] | None = None) -> list[Role]:
    """Import the roles and return them in install order (``order``, then list order).

    With ``only``, every named role must exist; unknown or missing names raise
    UnknownRole.
    """
    if only:
        unknown = [n for n in only if n not in ROLE_MODULES]
        if unknown:
            raise UnknownRole(f"unknown role(s): {', '.join(unknown)} (known: {', '.join(ROLE_MODULES)})")
    names = [n for n in ROLE_MODULES if not only or n in only]
    roles = []
    for idx, name in enumerate(names):
        role = _import(name)
        if role is None:
            if only:
                raise UnknownRole(f"role {name} is not available in this installation")
            continue
        roles.append((role.order, idx, role))
    return [r for _, _, r in sorted(roles, key=lambda t: (t[0], t[1]))]


def parse_only(value: str | None) -> list[str] | None:
    """'network,ssh' -> ['network', 'ssh']; None or '' -> None."""
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]
