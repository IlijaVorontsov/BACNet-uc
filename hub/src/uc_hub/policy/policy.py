"""Policy engine: which roles may run and approve which tool tiers, and which
point writes the agent may make.

These rules are enforced in code, whatever the model or the system prompt
says (docs/ai-harness/DESIGN.md section 10). Everything is synchronous and
free of I/O; the only state is the write-rate window, which
``check_point_write`` and ``check_force`` fill when they admit a write and
which survives ``configure`` so a manifest change cannot reset the limit.
"""

from __future__ import annotations

import fnmatch
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from ..core.errors import InvalidRequest, PolicyDenied, ValidationFailed
from ..core.ids import PointRef
from ..core.types import Point, SafetyClass, Tier, Value

#: Roles in increasing order of authority; each includes the ones before it.
ROLES = ("viewer", "operator", "commissioner", "admin")
_RANK = {role: rank for rank, role in enumerate(ROLES)}
#: Least role that may approve a tier.
APPROVER_ROLE: dict[str, str] = {"L": "operator", "C": "commissioner"}
#: Priorities 1..8 belong to life safety, critical equipment and manual
#: operators; the agent never writes there, whatever the site policy says.
RESERVED_PRIORITIES = range(1, 9)
RATE_WINDOW_S = 60.0


class Action(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str

    @property
    def allowed(self) -> bool:
        return self.action is Action.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.action is Action.NEEDS_APPROVAL

    @property
    def denied(self) -> bool:
        return self.action is Action.DENY


@dataclass(frozen=True, slots=True)
class PolicySettings:
    """The manifest's ``policy`` section with defaults applied (SITE.md)."""

    agent_write_priority: int = 12
    default_lease_s: int = 300
    max_lease_s: int = 3600
    #: fnmatch patterns over full or site-relative point ids.
    deny: tuple[str, ...] = ()
    max_writes_per_minute: int = 30
    max_devices_per_stage: int = 5
    approval_ttl_s: int = 1800

    def __post_init__(self) -> None:
        errors = []
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "deny":
                if not all(isinstance(p, str) and p for p in value):
                    errors.append("deny: patterns must be non-empty strings")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
                errors.append(f"{f.name}: must be a positive integer, got {value!r}")
        if not errors:
            if not 9 <= self.agent_write_priority <= 16:
                errors.append(f"agent_write_priority: must be 9..16, got {self.agent_write_priority}")
            if self.default_lease_s > self.max_lease_s:
                errors.append(f"default_lease_s {self.default_lease_s} exceeds max_lease_s {self.max_lease_s}")
        if errors:
            raise ValidationFailed("invalid site policy", errors)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> PolicySettings:
        raw = dict(raw or {})
        known = {f.name for f in fields(cls)}
        unknown = sorted(raw.keys() - known)
        if unknown:
            raise ValidationFailed("invalid site policy", [f"{k}: unknown setting" for k in unknown])
        if "deny" in raw:
            deny = raw["deny"]
            if not isinstance(deny, (list, tuple)):
                raise ValidationFailed("invalid site policy", ["deny: must be a list of patterns"])
            raw["deny"] = tuple(deny)
        return cls(**raw)

    def to_dict(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["deny"] = list(self.deny)
        return out


def role_rank(roles: Iterable[str]) -> int:
    """Rank of the strongest known role; -1 when there is none."""
    return max((_RANK[r] for r in roles if r in _RANK), default=-1)


class Policy:
    """The site's rules for the agent. Build with ``from_site`` from the site
    manifest (its ``policy`` and ``safety`` sections); ``configure`` swaps in a
    new manifest's rules."""

    def __init__(
        self,
        site: str,
        settings: PolicySettings | None = None,
        safety: Mapping[str, SafetyClass | str] | None = None,
    ) -> None:
        self.site = site
        self._writes: list[float] = []
        self.settings = PolicySettings()
        self._safety: dict[str, SafetyClass] = {}
        self.configure(settings or PolicySettings(), safety or {})

    @classmethod
    def from_site(cls, site: Mapping[str, Any] | Any) -> Policy:
        """From a site document (``site.yaml`` as a dict) or anything with
        ``to_dict()`` returning one, such as ``manifest.SiteManifest``."""
        doc = site if isinstance(site, Mapping) else site.to_dict()
        name = (doc.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name:
            raise ValidationFailed("invalid site", ["metadata.name: missing"])
        return cls(name, PolicySettings.from_dict(doc.get("policy")), doc.get("safety") or {})

    def configure(self, settings: PolicySettings, safety: Mapping[str, SafetyClass | str]) -> None:
        """Apply a (new) manifest's policy and safety map. Raises before
        changing anything when the safety map is invalid."""
        parsed: dict[str, SafetyClass] = {}
        errors = []
        for key, value in safety.items():
            try:
                parsed[str(PointRef.parse(key, default_site=self.site))] = SafetyClass(value)
            except ValueError as e:
                errors.append(f"safety.{key}: {e}")
        if errors:
            raise ValidationFailed("invalid safety map", errors)
        self.settings = settings
        self._safety = parsed

    # -- tools --------------------------------------------------------------------------
    def check_tool(self, tier: Tier, roles: Iterable[str], *, allowed_roles: Iterable[str] = ()) -> Decision:
        """Whether a user with ``roles`` may have the agent run a tool of
        ``tier`` now, only after an approval, or not at all. ``allowed_roles``
        is the tool's own restriction (``tools.registry.Tool.roles``)."""
        roles = set(roles)
        rank = role_rank(roles)
        if rank < 0:
            return Decision(Action.DENY, "the user has no role")
        restricted = set(allowed_roles)
        if restricted and not restricted & roles:
            return Decision(Action.DENY, f"this tool needs one of the roles {', '.join(sorted(restricted))}")
        if tier == "R":
            return Decision(Action.ALLOW, "tier R (read) runs automatically")
        if tier not in ("S", "L", "C"):
            return Decision(Action.DENY, f"unknown tier {tier!r}")
        if rank < _RANK["operator"]:
            return Decision(Action.DENY, "viewers may only run read tools (tier R)")
        if tier == "S":
            return Decision(Action.ALLOW, "tier S (draft, simulation, sandbox) runs automatically")
        if tier == "L":
            return Decision(Action.NEEDS_APPROVAL,
                            "tier L changes the live site; an operator, commissioner or admin must approve")
        return Decision(Action.NEEDS_APPROVAL, "tier C commits changes; a commissioner or admin must approve")

    def can_approve(self, tier: Tier, roles: Iterable[str], *, targets: int = 0) -> bool:
        """L: operator, commissioner or admin. C: commissioner or admin, and
        only admin for a plan touching more than ``max_devices_per_stage``
        devices (DESIGN.md section 10, rule 6). R and S need no approval."""
        least = APPROVER_ROLE.get(tier)
        if least is None:
            return False
        rank = role_rank(roles)
        if tier == "C" and targets > self.settings.max_devices_per_stage:
            return rank >= _RANK["admin"]
        return rank >= _RANK[least]

    def require_approver(self, tier: Tier, roles: Iterable[str], *, targets: int = 0) -> None:
        roles = set(roles)
        if not self.can_approve(tier, roles, targets=targets):
            if tier not in APPROVER_ROLE:
                raise PolicyDenied(f"tier {tier} calls need no approval")
            who = "an admin" if tier == "C" and targets > self.settings.max_devices_per_stage else (
                "an operator, commissioner or admin" if tier == "L" else "a commissioner or admin")
            raise PolicyDenied(f"only {who} may approve this tier {tier} call")

    # -- points -------------------------------------------------------------------------
    def safety_of(self, point: Point) -> SafetyClass:
        """The stricter of the point's own class and the manifest's safety map."""
        mapped = self._safety.get(str(point.ref), SafetyClass.NORMAL)
        order = (SafetyClass.NORMAL, SafetyClass.CRITICAL, SafetyClass.LIFE_SAFETY)
        return max(point.safety, mapped, key=order.index)

    def deny_match(self, ref: PointRef) -> str | None:
        """The first deny pattern matching the point's full or site-relative id."""
        full, relative = str(ref), f"{ref.device}/{ref.obj}"
        for pattern in self.settings.deny:
            if fnmatch.fnmatchcase(full, pattern) or (
                ref.site == self.site and fnmatch.fnmatchcase(relative, pattern)
            ):
                return pattern
        return None

    def check_point_write(
        self, point: Point, priority: int | None, value: Value, now: float | None = None
    ) -> int | None:
        """Admit an agent write of ``value`` to ``point`` or raise
        ``PolicyDenied`` (``InvalidRequest`` for a malformed priority or
        value). Returns the priority to write at: the requested one, the
        agent priority when none was given, None for non-commandable points.
        An admitted write counts toward the per-minute rate limit (``now``:
        monotonic seconds, see ``admit_write``)."""
        self._check_target(point)
        if not point.writable:
            raise PolicyDenied(f"{point.ref} is not writable")
        effective = self._priority(point, priority)
        _check_value(point, value)
        self.admit_write(now)
        return effective

    def check_force(self, point: Point | None, now: float | None = None) -> None:
        """Admit an IO force (``io_force``, live test steps) or raise
        ``PolicyDenied``. ``point`` is the point the forced channel feeds
        (None when no point uses the channel). Forcing a channel drives or
        simulates that point, so the life-safety and deny rules apply as for
        a write; an input need not be writable. The force counts toward the
        rate limit."""
        if point is not None:
            self._check_target(point)
        self.admit_write(now)

    def _check_target(self, point: Point) -> None:
        pid = str(point.ref)
        if self.safety_of(point) is SafetyClass.LIFE_SAFETY:
            raise PolicyDenied(f"{pid} is a life-safety point; the agent never writes life-safety points")
        pattern = self.deny_match(point.ref)
        if pattern is not None:
            raise PolicyDenied(f"{pid} matches the deny pattern {pattern!r} of the site policy; "
                               "it is read-only for the agent")

    def admit_write(self, now: float | None = None) -> None:
        """Count one live write against ``max_writes_per_minute`` over a
        sliding 60 s window (``check_point_write`` and ``check_force`` call
        it); raises ``PolicyDenied`` when it is full. ``now`` is monotonic
        seconds, like the default."""
        now = time.monotonic() if now is None else now
        # Writes more than a window ahead of now were counted before the wall clock stepped
        # back, or by a caller passing another clock; kept, they would fill the window until then.
        window = [t for t in self._writes if now - RATE_WINDOW_S < t <= now + RATE_WINDOW_S]
        self._writes = window
        limit = self.settings.max_writes_per_minute
        if len(window) >= limit:
            wait = max(0.0, min(window) + RATE_WINDOW_S - now)
            raise PolicyDenied(f"write rate limit reached ({limit} writes per minute); try again in {wait:.0f} s")
        window.append(now)

    def _priority(self, point: Point, priority: int | None) -> int | None:
        agent = self.settings.agent_write_priority
        if priority is None:
            return agent if point.commandable else None
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 16:
            raise InvalidRequest(f"priority must be an integer 1..16, got {priority!r}")
        if priority in RESERVED_PRIORITIES:
            raise PolicyDenied(f"priority {priority} is reserved for life safety, critical equipment and "
                               f"operators (1..8); the agent writes at {agent}..16")
        if priority < agent:
            raise PolicyDenied(f"priority {priority} would outrank the agent write priority {agent}; "
                               f"use {agent}..16")
        return priority if point.commandable else None

    # -- leases and approvals ----------------------------------------------------------------
    def clamp_lease(self, seconds: float | None) -> int:
        """Lease length in whole seconds: the default when none (or nonsense)
        is given, never more than ``max_lease_s``."""
        s = self.settings
        if seconds is None or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return s.default_lease_s
        if math.isnan(seconds) or seconds <= 0:
            return s.default_lease_s
        if math.isinf(seconds):
            return s.max_lease_s
        return max(1, min(math.ceil(seconds), s.max_lease_s))

    @property
    def approval_ttl_s(self) -> int:
        return self.settings.approval_ttl_s


def _check_value(point: Point, value: Value) -> None:
    pid = str(point.ref)
    if value is None:
        if not point.commandable:
            raise InvalidRequest(f"{pid} is not commandable; there is nothing to relinquish")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidRequest(f"{pid}: {value} is not a valid value")
    kind = point.datatype
    ok = {
        "real": isinstance(value, (int, float)) and not isinstance(value, bool),
        "bool": isinstance(value, bool) or (_integral(value) and value in (0, 1)),
        "int": _integral(value),
        "enum": _integral(value),
        "string": isinstance(value, str),
    }.get(kind, isinstance(value, (int, float, str)))
    if not ok:
        raise InvalidRequest(f"{pid} holds {kind} values; {value!r} does not fit")


def _integral(value: Value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
