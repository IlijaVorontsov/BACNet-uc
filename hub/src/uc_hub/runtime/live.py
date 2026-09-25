"""Live writes and IO forces of the agent, and how their leases end.

``LiveControl`` is the only path by which the agent changes live values
(``point_write``, ``io_force`` and live acceptance tests): the policy admits
the write (life-safety, deny patterns, priorities, rate limit), a lease is
recorded before the device is touched, and BACnet-uc nodes also get the
lease length (``lease_ms``) so they undo it themselves if the gateway dies.

``LeaseReleaser`` undoes a lease when it ends: a write to a commandable
point is relinquished at its priority; a write to a point without a priority
array writes back the value read before the first write (kept in the lease
target as ``restore``); a force is released. Release attempts that fail are
audited; the lease manager retries them.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from ..core.errors import DeviceError, HubError, InvalidRequest, NotFound
from ..core.ids import PointRef
from ..core.types import Point, Quality, Value, WriteResult
from ..policy import Lease, LeaseManager, Policy
from ..store import Store
from .site import SiteRuntime

logger = logging.getLogger(__name__)


class LiveControl:
    def __init__(self, site: SiteRuntime, policy: Policy, leases: LeaseManager) -> None:
        self.site = site
        self.policy = policy
        self.leases = leases

    # -- previews (approval cards): the rules, without counting or touching anything --------
    async def check_write(self, ref: PointRef, value: Value, priority: int | None = None) -> tuple[Point, int | None]:
        """The point and the priority a write would use; raises what the write would."""
        point = await self.site.point(ref)
        return point, self.policy.check_point_write(point, priority, value, admit=False)

    async def check_force(self, node: str, channel: str) -> Point | None:
        point = await self.site.channel_point(node, channel)
        self.policy.check_force(point, admit=False)
        return point

    # -- writes -----------------------------------------------------------------------------
    async def write(
        self, ref: PointRef, value: Value, *, priority: int | None = None, lease_s: float | None = None,
        user: str, run_id: str | None = None,
    ) -> tuple[WriteResult, Lease]:
        """Write ``value`` under a lease (the site's default length when
        ``lease_s`` is None, never more than its maximum)."""
        point = await self.site.point(ref)
        effective = self.policy.check_point_write(point, priority, value)
        seconds = self.policy.clamp_lease(lease_s)
        target: dict[str, Any] = {"point": str(ref)}
        if effective is None:
            target["restore"] = await self._restore_value(ref)
        lease = await self.leases.grant("write", target, effective, seconds, user, run_id)
        try:
            result = await self.site.write(ref, value, effective,
                                           lease_ms=seconds * 1000 if effective is not None else None)
        except HubError:
            await self.leases.release(lease.id, reason="the write failed")
            raise
        if not result.ok:
            await self.leases.release(lease.id, reason="the write failed")
            raise DeviceError(f"{ref}: {result.error or 'the write failed'}")
        return result, lease

    async def _restore_value(self, ref: PointRef) -> Value:
        """What to write back when the lease of a point without a priority
        array ends: the value before the agent's first write of it."""
        held = self._lease("write", {"point": str(ref)}, None)
        if held is not None:
            return held.target["restore"]  # type: ignore[no-any-return]
        (reading,) = await self.site.read([ref])
        if reading.quality is not Quality.GOOD or reading.value is None:
            raise DeviceError(f"{ref} has no priority array and its current value cannot be read "
                              f"({reading.error or reading.quality.value}), so it could not be restored "
                              "when the lease ends; the write was not made")
        return reading.value

    async def relinquish(self, ref: PointRef, priority: int | None, *, user: str,
                         run_id: str | None = None) -> None:
        """Undo the agent's write of ``ref`` at ``priority`` (None: the write
        of a point without a priority array): release its lease, or, when
        there is none, relinquish the slot directly (policy checked)."""
        held = self._lease("write", {"point": str(ref)}, priority)
        if held is not None:
            await self._release(held, "relinquished on request")
            return
        point = await self.site.point(ref)
        if priority is None:
            raise InvalidRequest(f"{ref}: the agent holds no write of it to undo")
        effective = self.policy.check_point_write(point, priority, None)
        assert effective is not None
        result = await self.site.relinquish(ref, effective)
        if not result.ok:
            raise DeviceError(f"{ref}: {result.error or 'relinquish failed'}")

    # -- forces -----------------------------------------------------------------------------
    async def force(
        self, node: str, channel: str, value: float, *, lease_s: float | None = None, user: str,
        run_id: str | None = None,
    ) -> tuple[Lease, Point | None]:
        """Force an IO channel of a bacnet-uc node under a lease."""
        if not math.isfinite(value):
            raise InvalidRequest(f"{node} {channel}: a force value must be a finite number, not {value}")
        point = await self.site.channel_point(node, channel)
        self.policy.check_force(point)
        seconds = self.policy.clamp_lease(lease_s)
        lease = await self.leases.grant("force", {"node": node, "channel": channel}, None, seconds, user, run_id)
        try:
            await self.site.force(node, channel, value, lease_ms=seconds * 1000)
        except HubError:
            await self.leases.release(lease.id, reason="the force failed")
            raise
        return lease, point

    async def release_force(self, node: str, channel: str) -> Lease | None:
        """Release a force: through its lease when the agent holds one, else
        directly (the channel's point still may not be life-safety or denied)."""
        held = self._lease("force", {"node": node, "channel": channel}, None)
        if held is not None:
            return await self._release(held, "released on request")
        await self.check_force(node, channel)
        await self.site.force(node, channel, None)
        return None

    # -- leases -----------------------------------------------------------------------------
    def _lease(self, kind: str, match: dict[str, Any], priority: int | None) -> Lease | None:
        for lease in self.leases.active():
            if lease.kind == kind and lease.priority == priority and all(
                    lease.target.get(k) == v for k, v in match.items()):
                return lease
        return None

    async def _release(self, lease: Lease, reason: str) -> Lease:
        after = await self.leases.release(lease.id, reason=reason)
        if after.state != "released":
            raise DeviceError(f"releasing lease {lease.id} failed: {after.last_error}; it is retried")
        return after


class LeaseReleaser:
    """``LeaseManager`` releaser and event handler over the running site."""

    def __init__(self, site: SiteRuntime, store: Store) -> None:
        self.site = site
        self.store = store

    async def __call__(self, lease: Lease) -> None:
        target = lease.target
        if lease.kind == "force":
            node, channel = str(target["node"]), str(target["channel"])
            self.site.device(node)
            try:
                await self.site.force(node, channel, None)
            except NotFound:
                logger.info("lease %s: %s has no channel %s any more; nothing to release", lease.id, node, channel)
            return
        ref = self.site.point_ref(str(target["point"]))
        if lease.priority is not None:
            result = await self.site.relinquish(ref, lease.priority)
        elif "restore" in target:
            result = await self.site.write(ref, target["restore"], None)
        else:
            raise InvalidRequest(f"lease {lease.id}: {ref} has no priority and no value to restore")
        if not result.ok:
            raise DeviceError(result.error or f"undoing the write of {ref} failed")

    async def on_event(self, event: str, lease: Lease, detail: str) -> None:
        """Audit how leases end; failed attempts are what an operator must see."""
        outcome = {"released": "ok", "release_failed": "error", "failed": "failed"}.get(event)
        if outcome is None:
            return
        if outcome != "ok":
            logger.warning("lease %s (%s %s): %s", lease.id, lease.kind, lease.target, detail)
        await self.store.add_audit(
            user=lease.created_by, run_id=lease.run_id, action=f"lease.{event}", outcome=outcome,
            args={"lease": lease.id, "kind": lease.kind, "target": lease.target, "priority": lease.priority},
            detail=detail,
        )
