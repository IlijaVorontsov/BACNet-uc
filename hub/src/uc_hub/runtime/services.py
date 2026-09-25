"""Everything the hub runs, built from ``hub.yaml``, started and stopped in order.

``Services(config)`` only builds what needs no site: the store, the run
event bus, the live value hub, the tool registry and runner, the question
broker, the approval broker, the agent's run manager and the LLM provider.
``start()`` opens the database, loads the live manifest (importing
``site_file`` as revision 1 on the first start), and then builds and starts
the site-dependent parts in dependency order: policy, site runtime
(drivers), leases (whose first act is to release what a previous run left
active, which needs the drivers), bridges, and last the agent runs (those
a restart interrupted continue or end). ``stop()`` reverses that: the run
tasks stop, bridges relinquish, the agent's leases are released (what does
not answer in time stays in the store for the next start), the lease loop
stops, drivers stop, the event streams end, the store closes. Both are
idempotent; a stopped instance is not restarted.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO, Any, TypeVar

from ..agent import ApprovalBroker, RunManager
from ..core.errors import Conflict
from ..core.types import Plan, ProtocolName, TestResult
from ..llm import make_provider
from ..llm.base import LlmProvider
from ..policy import LeaseManager, Policy
from ..store import EventBus, LiveHub, Store
from ..tools import ToolCallContext, ToolRunner, build_registry
from .bridges import Bridges, Gateway
from .config import HubConfig
from .live import LeaseReleaser, LiveControl
from .manifest import ManifestService, load_live_manifest
from .questions import Questions
from .site import DriverFactory, SiteRuntime
from .testing import run_live_tests

logger = logging.getLogger(__name__)

T = TypeVar("T")
#: How long ``stop`` waits for the agent's leases to be released.
STOP_RELEASE_TIMEOUT_S = 20.0


class Services:
    def __init__(
        self,
        config: HubConfig,
        *,
        llm: LlmProvider | None = None,
        factories: Mapping[ProtocolName, DriverFactory] | None = None,
        site_options: Mapping[str, Any] | None = None,
        sweep_interval_s: float = 5.0,
        passive: bool = False,
    ) -> None:
        """``llm``, ``factories`` (driver classes), ``site_options``
        (``SiteRuntime`` keyword arguments) and ``sweep_interval_s`` (how
        often expired approvals are closed) replace the defaults in tests.
        ``passive`` services only look and plan (``uc-hub plan``): the
        gateway bridges and the agent runs do not start."""
        self.config = config
        self.store = Store(config.db_path)
        self.events = EventBus(self.store)
        self.live = LiveHub()
        self.registry = build_registry()
        self.runner = ToolRunner(self.registry, self.store, inline_limit=config.agent.result_inline_limit)
        self.questions = Questions(self.store, self.events)
        self.approvals = ApprovalBroker(self)
        self.runs = RunManager(self, sweep_interval_s=sweep_interval_s)
        self.llm = llm if llm is not None else make_provider(config.llm_settings(), base_dir=config.base_dir)
        self._factories = factories
        self._site_options = dict(site_options or {})
        self._passive = passive
        self._lock: IO[str] | None = None
        self._state = "new"
        self._policy: Policy | None = None
        self._site: SiteRuntime | None = None
        self._leases: LeaseManager | None = None
        self._bridges: Bridges | None = None
        self._manifests: ManifestService | None = None
        self._control: LiveControl | None = None

    # -- parts that exist once started ------------------------------------------------------
    @property
    def policy(self) -> Policy:
        return _started(self._policy)

    @property
    def site(self) -> SiteRuntime:
        return _started(self._site)

    @property
    def leases(self) -> LeaseManager:
        return _started(self._leases)

    @property
    def bridges(self) -> Bridges:
        return _started(self._bridges)

    @property
    def manifests(self) -> ManifestService:
        return _started(self._manifests)

    @property
    def control(self) -> LiveControl:
        return _started(self._control)

    @property
    def running(self) -> bool:
        return self._state == "started"

    # -- life cycle ---------------------------------------------------------------------------
    async def start(self) -> None:
        if self._state == "started":
            return
        if self._state != "new":
            raise RuntimeError(f"services are {self._state}; build a new instance to start again")
        self._state = "starting"
        try:
            await self._start()
        except BaseException:
            await self._shutdown()
            self._state = "stopped"
            raise
        self._state = "started"

    async def _start(self) -> None:
        config = self.config
        self._lock = _lock_data_dir(config.data_dir)
        await self.store.open()
        manifest, revision = await load_live_manifest(self.store, config.site_file)
        self.live.site = manifest.name
        policy = self._policy = Policy.from_site(manifest)
        options: dict[str, Any] = {
            "driver_settings": {p: config.driver_settings(p) for p in ProtocolName},
            # BACnet-uc is this hub's own device family: its driver also serves
            # drafts that add the first node.
            "configured": config.configured_protocols() | {ProtocolName.BACNET_UC},
            **self._site_options,
        }
        if self._factories is not None:
            options["factories"] = self._factories
        site = self._site = SiteRuntime(manifest, self.live, **options)
        releaser = LeaseReleaser(site, self.store)
        leases = self._leases = LeaseManager(self.store, releaser, on_event=releaser.on_event)
        bridges = self._bridges = Bridges(site, policy)
        self._control = LiveControl(site, policy, leases)
        manifests = self._manifests = ManifestService(
            self.store, site, Gateway(site, policy, bridges), policy, live=manifest, live_revision=revision,
            base_dir=config.site_file.parent, uc_link_wasm=config.uc_link_wasm, on_plan=self._plan_updated,
        )
        site.pending_changes = manifests.pending_changes
        await manifests.open()
        await site.start()
        await leases.start()
        if not self._passive:
            await bridges.start(manifest.bridges)
            await self.runs.start()
        logger.info("site %s is running manifest revision %d (%d devices)", manifest.name, revision,
                    len(site.devices()))

    async def stop(self) -> None:
        if self._state in ("stopped", "stopping"):
            return
        self._state = "stopping"
        await self._shutdown()
        self._state = "stopped"

    async def _shutdown(self) -> None:
        try:
            await self.runs.stop()
        except Exception:
            logger.exception("stopping the agent runs failed")
        if self._bridges is not None:
            try:
                await self._bridges.stop()
            except Exception:
                logger.exception("stopping the bridges failed")
        await self._release_leases()
        parts: list[tuple[str, Any]] = [("leases", self._leases), ("site", self._site)]
        for name, part in parts:
            if part is None:
                continue
            try:
                await part.stop()
            except Exception:
                logger.exception("stopping the %s failed", name)
        try:
            await self.llm.aclose()
        except Exception:
            logger.exception("closing the LLM provider failed")
        self.events.close()
        self.live.close()
        await self.store.close()
        if self._lock is not None:
            self._lock.close()
            self._lock = None

    async def _release_leases(self) -> None:
        """A stopped hub cannot end a lease, so the agent's writes and forces
        are undone now. What cannot be undone in time stays active in the
        store and is released by the next start."""
        leases = self._leases
        if leases is None or not leases.running or not leases.active():
            return
        active = leases.active()
        try:
            async with asyncio.timeout(STOP_RELEASE_TIMEOUT_S):
                await asyncio.gather(*(leases.release(lease.id, reason="the hub stopped") for lease in active),
                                     return_exceptions=True)
        except TimeoutError:
            logger.warning("releasing %d lease(s) at stop timed out; the next start releases them", len(active))

    # -- for tools, the agent loop and the API ---------------------------------------------------
    def context(self, user: str, roles: Iterable[str], *, run_id: str | None = None,
                call_id: str | None = None) -> ToolCallContext:
        return ToolCallContext(user=user, roles=set(roles), run_id=run_id, call_id=call_id, site=self.site,
                               services=self)

    async def run_live_tests(self, names: list[str] | None, *, user: str,
                             run_id: str | None = None) -> list[TestResult]:
        """Run the live manifest's tests (all, or ``names``), store the
        results and announce them (``tests.updated``)."""
        manifests = self.manifests
        results = await run_live_tests(self.control, manifests.live.tests, user=user, run_id=run_id, names=names)
        await self.store.save_test_results(results, revision=manifests.live_revision, replace=names is None)
        if run_id is not None:
            stored = (await self.store.test_results("live"))["results"]
            await self.events.append(run_id, "tests.updated", results=stored)
        return results

    async def _plan_updated(self, run_id: str | None, plan: Plan | None) -> None:
        if run_id is None:
            return
        await self.events.append(run_id, "plan.updated", plan_id=plan.id if plan else None,
                                 revision=plan.revision if plan else None, changes=len(plan.changes) if plan else 0)


def _lock_data_dir(data_dir: Path) -> IO[str]:
    """One hub per ``data_dir``: two would drive the same devices and release
    each other's leases. The lock ends with the process."""
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(data_dir / "uc-hub.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise Conflict(f"another uc-hub process uses {data_dir}; stop it first (one hub per data_dir)") from None
    return handle


def _started(part: T | None) -> T:
    if part is None:
        raise RuntimeError("the hub services are not started")
    return part
