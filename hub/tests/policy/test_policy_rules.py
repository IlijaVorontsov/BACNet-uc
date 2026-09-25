from __future__ import annotations

from typing import Any

import pytest

from uc_hub.core.errors import InvalidRequest, PolicyDenied, ValidationFailed
from uc_hub.core.ids import PointRef
from uc_hub.core.types import Point, PointKind, SafetyClass
from uc_hub.policy import ROLES, Action, Policy, PolicySettings

SITE: dict[str, Any] = {
    "apiVersion": "bacnet-uc/v1",
    "kind": "Site",
    "metadata": {"name": "hq"},
    "safety": {
        "ahu1-ctl/binary-output:9": "life-safety",
        "hq/ahu1-ctl/binary-output:3": "critical",
    },
    "policy": {
        "agent_write_priority": 12,
        "default_lease_s": 300,
        "max_lease_s": 3600,
        "deny": ["ahu1-ctl/binary-output:*", "hq/boiler-*/*", "*/analog-value:666"],
        "max_writes_per_minute": 3,
        "max_devices_per_stage": 5,
        "approval_ttl_s": 1800,
    },
}


def point(text: str, *, writable: bool = True, commandable: bool = True, datatype: str = "real",
          safety: SafetyClass = SafetyClass.NORMAL) -> Point:
    return Point(PointRef.parse(text, default_site="hq"), "p", PointKind.OUTPUT, datatype=datatype,  # type: ignore[arg-type]
                 writable=writable, commandable=commandable, safety=safety)


@pytest.fixture
def policy() -> Policy:
    return Policy.from_site(SITE)


# -- tools ----------------------------------------------------------------------------------
EXPECTED = {
    #            R        S        L                 C
    "viewer": ("allow", "deny", "deny", "deny"),
    "operator": ("allow", "allow", "needs_approval", "needs_approval"),
    "commissioner": ("allow", "allow", "needs_approval", "needs_approval"),
    "admin": ("allow", "allow", "needs_approval", "needs_approval"),
}
APPROVE = {
    "viewer": (False, False, False, False),
    "operator": (False, False, True, False),
    "commissioner": (False, False, True, True),
    "admin": (False, False, True, True),
}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("index,tier", list(enumerate("RSLC")))
def test_tool_matrix(policy: Policy, role: str, index: int, tier: str) -> None:
    decision = policy.check_tool(tier, {role})  # type: ignore[arg-type]
    assert decision.action == Action(EXPECTED[role][index])
    assert decision.reason
    assert policy.can_approve(tier, [role]) is APPROVE[role][index]  # type: ignore[arg-type]


def test_tool_roles_edge_cases(policy: Policy) -> None:
    assert policy.check_tool("R", set()).denied
    assert policy.check_tool("R", {"guest"}).denied
    assert policy.check_tool("S", {"viewer", "operator"}).allowed  # the strongest role counts
    assert policy.check_tool("X", {"admin"}).denied  # type: ignore[arg-type]
    decision = policy.check_tool("R", {"operator"}, allowed_roles={"admin", "commissioner"})
    assert decision.denied and "admin" in decision.reason
    assert policy.check_tool("R", {"admin"}, allowed_roles={"admin"}).allowed
    assert policy.check_tool("C", {"operator"}).needs_approval
    assert not policy.can_approve("C", set())


def test_large_plans_need_an_admin(policy: Policy) -> None:
    assert policy.can_approve("C", {"commissioner"}, targets=5)
    assert not policy.can_approve("C", {"commissioner"}, targets=6)
    assert policy.can_approve("C", {"admin"}, targets=60)
    with pytest.raises(PolicyDenied, match="admin"):
        policy.require_approver("C", {"commissioner"}, targets=6)
    with pytest.raises(PolicyDenied, match="operator"):
        policy.require_approver("L", {"viewer"})
    with pytest.raises(PolicyDenied, match="no approval"):
        policy.require_approver("R", {"admin"})
    policy.require_approver("L", {"operator"})


# -- point writes -----------------------------------------------------------------------------
def test_life_safety_is_never_writable(policy: Policy) -> None:
    with pytest.raises(PolicyDenied, match="life-safety"):
        policy.check_point_write(point("ahu1-ctl/binary-output:9"), 12, 1, now=0)
    with pytest.raises(PolicyDenied, match="life-safety"):
        policy.check_point_write(point("r204-ctl/binary-value:1", safety=SafetyClass.LIFE_SAFETY), None, 1, now=0)
    # The stricter of the point's own class and the manifest map wins.
    assert policy.safety_of(point("ahu1-ctl/binary-output:9", safety=SafetyClass.CRITICAL)) is \
        SafetyClass.LIFE_SAFETY
    assert policy.safety_of(point("ahu1-ctl/binary-output:3")) is SafetyClass.CRITICAL
    # Critical points are not blocked by their class (the deny list covers this one).
    critical = point("r204-ctl/analog-output:1", safety=SafetyClass.CRITICAL)
    assert policy.check_point_write(critical, None, 50.0, now=0) == 12


@pytest.mark.parametrize("text,pattern", [
    ("hq/ahu1-ctl/binary-output:4", "ahu1-ctl/binary-output:*"),
    ("hq/boiler-2/analog-value:1", "hq/boiler-*/*"),
    ("hq/r204-ctl/analog-value:666", "*/analog-value:666"),
])
def test_deny_patterns(policy: Policy, text: str, pattern: str) -> None:
    ref = PointRef.parse(text)
    assert policy.deny_match(ref) == pattern
    with pytest.raises(PolicyDenied, match="deny pattern"):
        policy.check_point_write(point(text), None, 1.0, now=0)


def test_deny_patterns_do_not_overmatch(policy: Policy) -> None:
    for text in ("hq/ahu1-ctl/analog-output:4", "hq/ahu10-ctl/binary-output:4", "hq/boiler/analog-value:1",
                 "other/ahu1-ctl/binary-output:4"):
        assert policy.deny_match(PointRef.parse(text)) is None, text


def test_non_writable(policy: Policy) -> None:
    with pytest.raises(PolicyDenied, match="not writable"):
        policy.check_point_write(point("r204-ctl/analog-input:1", writable=False, commandable=False), None, 1.0,
                                 now=0)


@pytest.mark.parametrize("priority", [1, 5, 8])
def test_reserved_priorities(policy: Policy, priority: int) -> None:
    with pytest.raises(PolicyDenied, match="reserved"):
        policy.check_point_write(point("r204-ctl/analog-output:1"), priority, 1.0, now=0)


@pytest.mark.parametrize("priority", [9, 10, 11])
def test_priorities_above_the_agent(policy: Policy, priority: int) -> None:
    with pytest.raises(PolicyDenied, match="outrank"):
        policy.check_point_write(point("r204-ctl/analog-output:1"), priority, 1.0, now=0)


@pytest.mark.parametrize("priority", [0, 17, True, 12.0, "12"])
def test_malformed_priorities(policy: Policy, priority: Any) -> None:
    with pytest.raises(InvalidRequest):
        policy.check_point_write(point("r204-ctl/analog-output:1"), priority, 1.0, now=0)


def test_effective_priority(policy: Policy) -> None:
    cmd = point("r204-ctl/analog-output:1")
    assert policy.check_point_write(cmd, None, 1.0, now=0) == 12
    assert policy.check_point_write(cmd, 16, None, now=1) == 16  # relinquish at 16
    plain = point("r204-node/led", commandable=False, datatype="bool")
    assert policy.check_point_write(plain, None, True, now=2) is None
    permissive = Policy("hq", PolicySettings(agent_write_priority=9, max_writes_per_minute=100))
    assert permissive.check_point_write(cmd, 9, 1.0, now=0) == 9
    with pytest.raises(PolicyDenied):
        permissive.check_point_write(cmd, 8, 1.0, now=0)


@pytest.mark.parametrize("datatype,value", [
    ("real", float("nan")), ("real", float("inf")), ("real", "hot"), ("real", True),
    ("bool", 2), ("bool", "on"), ("int", 1.5), ("enum", True), ("string", 3),
])
def test_bad_values(datatype: str, value: Any) -> None:
    policy = Policy("hq", PolicySettings(max_writes_per_minute=100))
    with pytest.raises(InvalidRequest):
        policy.check_point_write(point("r204-ctl/analog-value:1", datatype=datatype), None, value, now=0)


@pytest.mark.parametrize("datatype,value", [
    ("real", 21), ("real", 21.5), ("bool", 1), ("bool", False), ("bool", 1.0), ("int", 3), ("enum", 2.0),
    ("string", "auto"),
])
def test_good_values(datatype: str, value: Any) -> None:
    policy = Policy("hq", PolicySettings(max_writes_per_minute=100))
    policy.check_point_write(point("r204-ctl/analog-value:1", datatype=datatype), None, value, now=0)


def test_relinquish_needs_a_priority_array() -> None:
    policy = Policy("hq")
    with pytest.raises(InvalidRequest, match="relinquish"):
        policy.check_point_write(point("r204-node/led", commandable=False), None, None, now=0)


def test_rate_limit_sliding_window(policy: Policy) -> None:
    cmd = point("r204-ctl/analog-output:1")
    for t in (0.0, 10.0, 20.0):
        policy.check_point_write(cmd, None, 1.0, now=t)
    with pytest.raises(PolicyDenied, match="rate limit") as err:
        policy.check_point_write(cmd, None, 1.0, now=59.0)
    assert "1 s" in str(err.value)
    # Denied writes do not count; the first write leaves the window at t=60.
    policy.check_point_write(cmd, None, 1.0, now=60.0)
    with pytest.raises(PolicyDenied, match="rate limit"):
        policy.check_point_write(cmd, None, 1.0, now=69.0)
    policy.check_point_write(cmd, None, 1.0, now=70.0)
    # Rejections before the rate check do not consume the budget either.
    with pytest.raises(PolicyDenied, match="life-safety"):
        policy.check_point_write(point("ahu1-ctl/binary-output:9"), None, 1, now=200.0)
    for t in (200.0, 200.1, 200.2):
        policy.admit_write(t)


def test_reconfigure_keeps_the_rate_window(policy: Policy) -> None:
    cmd = point("r204-ctl/analog-output:1")
    for t in (0.0, 1.0, 2.0):
        policy.check_point_write(cmd, None, 1.0, now=t)
    policy.configure(PolicySettings(max_writes_per_minute=3), {})
    with pytest.raises(PolicyDenied, match="rate limit"):
        policy.check_point_write(cmd, None, 1.0, now=3.0)
    assert policy.safety_of(point("ahu1-ctl/binary-output:9")) is SafetyClass.NORMAL
    with pytest.raises(ValidationFailed):
        policy.configure(PolicySettings(), {"not a point": "life-safety"})
    with pytest.raises(ValidationFailed):
        policy.configure(PolicySettings(), {"a/b": "radioactive"})


# -- leases and settings ------------------------------------------------------------------------
@pytest.mark.parametrize("seconds,expected", [
    (None, 300), (0, 300), (-5, 300), (float("nan"), 300), (True, 300),
    (0.2, 1), (60, 60), (59.5, 60), (3600, 3600), (100_000, 3600), (float("inf"), 3600),
])
def test_clamp_lease(policy: Policy, seconds: Any, expected: int) -> None:
    assert policy.clamp_lease(seconds) == expected


def test_settings_defaults_and_validation() -> None:
    policy = Policy.from_site({"metadata": {"name": "hq"}})
    assert policy.settings == PolicySettings()
    assert policy.settings.to_dict()["deny"] == []
    assert policy.approval_ttl_s == 1800
    assert PolicySettings.from_dict(SITE["policy"]).deny == tuple(SITE["policy"]["deny"])
    for bad in ({"agent_write_priority": 8}, {"agent_write_priority": 17}, {"default_lease_s": 7200},
                {"max_writes_per_minute": 0}, {"deny": "x"}, {"deny": [""]}, {"surprise": 1},
                {"approval_ttl_s": True}):
        with pytest.raises(ValidationFailed):
            PolicySettings.from_dict(bad)
    with pytest.raises(ValidationFailed):
        Policy.from_site({"metadata": {}})


def test_from_site_accepts_a_manifest_object() -> None:
    class Manifest:
        def to_dict(self) -> dict[str, Any]:
            return SITE

    policy = Policy.from_site(Manifest())
    assert policy.site == "hq"
    assert policy.settings.max_writes_per_minute == 3


def test_rate_limit_survives_clock_steps_and_mixed_clocks() -> None:
    """Entries far in the future of ``now`` (the wall clock stepped back, or a
    caller passed time.time() where others use the monotonic default) must not
    keep the window full until that future time."""
    policy = Policy("hq", PolicySettings(max_writes_per_minute=2))
    wall = 1_790_290_000.0
    policy.admit_write(wall)
    policy.admit_write(wall + 1)
    with pytest.raises(PolicyDenied):
        policy.admit_write(wall + 2)
    policy.admit_write(5_000.0)
    policy.admit_write(5_001.0)
    with pytest.raises(PolicyDenied):
        policy.admit_write(5_002.0)
    # A step back within the window stays conservative: those writes still count.
    with pytest.raises(PolicyDenied):
        policy.admit_write(4_990.0)
    policy.admit_write(5_061.0)


def test_io_force_obeys_safety_and_deny_rules(policy: Policy) -> None:
    """Forcing the channel of a life-safety point (simulating a smoke detector,
    driving a smoke damper) is writing that point; rule 1 applies."""
    smoke = point("ahu1-ctl/binary-output:9", writable=False, commandable=False)
    with pytest.raises(PolicyDenied, match="life-safety"):
        policy.check_force(smoke, now=0)
    with pytest.raises(PolicyDenied, match="deny pattern"):
        policy.check_force(point("ahu1-ctl/binary-output:4"), now=0)
    sensor = point("r204-ctl/analog-input:1", writable=False, commandable=False)
    policy.check_force(sensor, now=0)  # inputs are forced to simulate them
    policy.check_force(None, now=1)  # a channel without a point
    policy.check_force(sensor, now=2)
    with pytest.raises(PolicyDenied, match="rate limit"):
        policy.check_force(sensor, now=3)
