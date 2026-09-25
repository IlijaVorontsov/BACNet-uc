"""Manifest revisions, the draft, plans and apply, over the store.

The live revision is what the site runs. The first edit creates a draft from
it; every ``edit`` is a JSON Patch that is validated at once (a patch that
does not apply or leaves an invalid document is refused and the draft stays
as it was) and stored as a new draft revision. Only a site admin may add or
remove a life-safety mark (DESIGN.md section 10, rule 1).

``plan`` diffs the draft (or, without a draft, the live revision: drift
repair) against the nodes and the live manifest. Plans are kept with their
change payloads, in memory and in the store, so an approved plan can be
applied after a restart. ``apply`` runs a plan in stages of
``policy.max_devices_per_stage`` targets; each attempt keeps its own
backups, and ``rollback`` restores the targets of a failed plan from the
first one. When every change succeeded the planned revision goes live: the
site runtime, the bridges, the tags and the policy follow it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..core.errors import Conflict, InvalidRequest, NotFound, PolicyDenied, ValidationFailed
from ..core.types import Change, ChangeResult, Plan, SafetyClass
from ..drivers.bacnet_uc.api import NodeApi
from ..manifest import (
    SiteManifest,
    TargetBackup,
    apply_patch,
    apply_plan,
    compute_plan,
    directory_resolver,
    get_pointer,
    rollback_target,
    yaml_diff,
)
from ..manifest.apply import ProgressCallback
from ..manifest.nodedocs import changed_paths
from ..manifest.plan import GATEWAY, NodeResolver
from ..policy import Policy, PolicySettings
from ..store import PlanBackupStore, Store
from .bridges import Gateway
from .site import SiteRuntime

logger = logging.getLogger(__name__)

#: ``(run_id, plan)`` after a plan was computed, applied or made stale by an
#: edit (plan None); the services turn it into a ``plan.updated`` run event.
PlanCallback = Callable[[str | None, Plan | None], Awaitable[None]]
_KEEP_PLANS = 20
_LIFE_SAFETY = SafetyClass.LIFE_SAFETY


async def load_live_manifest(store: Store, site_file: Path) -> tuple[SiteManifest, int]:
    """The live manifest and its revision. On first start (no live revision)
    ``site_file`` is imported as the first revision and goes live; later
    the store is authoritative and a different ``site_file`` only gets a
    warning."""
    base_dir = site_file.parent
    live = await store.live_revision()
    if live is None:
        manifest = SiteManifest.load(site_file)
        text = site_file.read_text(encoding="utf-8")
        rev = await store.add_revision(text, author="hub", message=f"imported from {site_file.name}", head="live")
        logger.info("imported %s as manifest revision %d", site_file, rev)
        return manifest, rev
    stored = await store.revision_yaml(live)
    assert stored is not None
    manifest = SiteManifest.from_yaml(stored, base_dir, source=f"revision {live}")
    if site_file.exists():
        try:
            same = SiteManifest.load(site_file) == manifest
        except ValidationFailed as e:
            same, why = False, f"it is invalid: {e}"
        else:
            why = "it differs"
        if not same:
            logger.warning("%s is not the live manifest (%s); the hub runs revision %d from its database. "
                           "Change the site through a draft, plan and apply.", site_file, why, live)
    return manifest, live


def plan_record(plan: Plan) -> dict[str, Any]:
    """A plan with its change payloads, as stored."""
    return {**plan.to_json(), "changes": [{**c.to_json(), "payload": c.payload} for c in plan.changes]}


def plan_from_record(data: dict[str, Any]) -> Plan:
    return Plan(
        id=data["id"], revision=int(data["revision"]), base_revision=int(data["base_revision"]),
        changes=[Change(id=c["id"], target=c["target"], kind=c["kind"], summary=c["summary"], diff=c["diff"],
                        payload=c["payload"], tier=c["tier"]) for c in data["changes"]],
        created_at=float(data["created_at"]), warnings=list(data["warnings"]), blocked=dict(data["blocked"]),
    )


@dataclass(slots=True)
class EditResult:
    draft_revision: int
    #: Top-level sections of the draft that differ from the live revision.
    sections: list[str]
    #: site.yaml diff from the live revision to the draft.
    diff: str
    #: False when the patch left the document as it was (no new revision).
    changed: bool


@dataclass(slots=True)
class ApplyOutcome:
    plan: Plan
    results: list[ChangeResult]
    attempt: int
    live_revision: int

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)


class ManifestService:
    def __init__(
        self,
        store: Store,
        site: SiteRuntime,
        gateway: Gateway,
        policy: Policy,
        *,
        live: SiteManifest,
        live_revision: int,
        base_dir: Path,
        uc_link_wasm: Path | None = None,
        on_plan: PlanCallback | None = None,
        plan_node_timeout_s: float = 60.0,
        apply_verify_timeout_s: float = 10.0,
    ) -> None:
        self.store = store
        self.site = site
        self.gateway = gateway
        self.policy = policy
        self.live = live
        self.live_revision = live_revision
        self.draft: SiteManifest | None = None
        self.draft_revision: int | None = None
        self.base_dir = base_dir
        self.uc_link_wasm = uc_link_wasm
        self.on_plan = on_plan
        self.plan_node_timeout_s = plan_node_timeout_s
        self.apply_verify_timeout_s = apply_verify_timeout_s
        self._plans: dict[str, Plan] = {}
        self._current: Plan | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Load the draft and its latest plan (unless it was applied) from the store."""
        draft = await self.store.draft_revision()
        if draft is not None:
            self.draft, self.draft_revision = await self._revision(draft), draft
        row = await self.store.latest_plan(self.revision)
        if row is not None and row["state"] in ("planned", "failed"):
            self._remember(plan_from_record(row["data"]))

    @property
    def revision(self) -> int:
        """The revision edits apply to: the draft's, else the live one."""
        return self.draft_revision if self.draft_revision is not None else self.live_revision

    @property
    def current(self) -> SiteManifest:
        return self.draft if self.draft is not None else self.live

    async def _revision(self, rev: int) -> SiteManifest:
        text = await self.store.revision_yaml(rev)
        if text is None:
            raise NotFound(f"manifest revision {rev} not found")
        return SiteManifest.from_yaml(text, self.base_dir, source=f"revision {rev}")

    # -- reading and editing ------------------------------------------------------------------
    def get(self, which: Literal["draft", "live"] | None = None, path: str = "") -> tuple[str, int, Any]:
        """``(which, revision, value at the JSON pointer)``; ``which`` None
        is the draft when there is one."""
        if which == "draft" and self.draft is None:
            raise NotFound("there is no draft; manifest_edit creates one from the live revision")
        use_draft = self.draft is not None and which != "live"
        manifest = self.draft if use_draft and self.draft is not None else self.live
        revision = self.revision if use_draft else self.live_revision
        return ("draft" if use_draft else "live"), revision, get_pointer(manifest.to_dict(), path)

    async def edit(self, patch: Any, *, user: str, roles: set[str] | frozenset[str], message: str = "",
                   run_id: str | None = None) -> EditResult:
        """Apply a JSON Patch to the draft (created from the live revision
        when there is none). Raises ``InvalidRequest`` (the patch does not
        apply), ``ValidationFailed`` (the result is not a valid site) or
        ``PolicyDenied`` (a non-admin adds or removes a life-safety mark);
        the draft is unchanged then."""
        async with self._lock:
            base = self.current
            manifest = SiteManifest.from_dict(apply_patch(base.to_dict(), patch), self.base_dir)
            if manifest.name != self.live.name:
                raise InvalidRequest(f"metadata.name is the site id in every point id; it stays {self.live.name!r}")
            _check_life_safety_marks(base, manifest, roles)
            if manifest == base:
                return self._edit_result(changed=False)
            author = f"agent ({user})" if run_id else user
            rev = await self.store.save_draft(manifest.to_yaml(), author=author,
                                              message=message or _patch_summary(patch), run_id=run_id)
            self.draft, self.draft_revision = manifest, rev
            self._current = None
        await self._notify(run_id, None)
        return self._edit_result(changed=True)

    def _edit_result(self, *, changed: bool) -> EditResult:
        return EditResult(self.revision, self.changed_sections(), self.diff(), changed)

    def changed_sections(self) -> list[str]:
        """Top-level sections in which the draft differs from the live revision."""
        live, want = self.live.to_dict(), self.current.to_dict()
        return [k for k in dict.fromkeys([*want, *live]) if want.get(k) != live.get(k)]

    def diff(self) -> str:
        """site.yaml diff from the live revision to the draft ("" without a draft)."""
        return yaml_diff(self.live.to_yaml(), self.current.to_yaml())

    def pending_changes(self) -> int:
        """Changes waiting to be applied: the current plan's, or before
        planning the manifest paths the draft changes."""
        if self._current is not None and self._current.changes:
            return len(self._current.changes)
        if self.draft is None:
            return 0
        return len(changed_paths(self.live.to_dict(), self.draft.to_dict()))

    # -- planning -------------------------------------------------------------------------------
    async def plan(self, *, run_id: str | None = None) -> Plan:
        """Plan the draft (or the live revision when there is no draft)."""
        async with self._lock:
            desired, revision = self.current, self.revision
            count = await self.store.plan_count(revision)
            plan_id = f"p{revision}" if count == 0 else f"p{revision}-{count + 1}"
            async with self._nodes(desired) as nodes:
                plan = await compute_plan(
                    desired, self.live, nodes, directory_resolver(self.base_dir), self.uc_link_wasm, revision,
                    self.live_revision, plan_id=plan_id, node_timeout_s=self.plan_node_timeout_s,
                )
            await self.store.save_plan(plan.id, revision=plan.revision, base_revision=plan.base_revision,
                                       data=plan_record(plan))
            self._remember(plan)
        await self._notify(run_id, plan)
        return plan

    def current_plan(self) -> Plan | None:
        """The latest plan of the current draft (``GET /api/plan``)."""
        return self._current

    async def get_plan(self, plan_id: str) -> Plan:
        plan = self._plans.get(plan_id)
        if plan is None:
            row = await self.store.get_plan(plan_id)
            if row is None:
                raise NotFound(f"no plan {plan_id!r}; compute one with plan")
            plan = plan_from_record(row["data"])
        return plan

    def check_applicable(self, plan: Plan) -> None:
        """Raise unless ``plan`` can be applied now: it is complete and was
        computed against the live revision for the current draft."""
        if plan.blocked:
            raise InvalidRequest(f"plan {plan.id} cannot be applied; unreachable while planning: "
                                 + ", ".join(f"{t} ({r})" for t, r in plan.blocked.items()))
        if plan.base_revision != self.live_revision:
            raise Conflict(f"plan {plan.id} was computed against revision {plan.base_revision}, but revision "
                           f"{self.live_revision} is live now; plan again")
        if plan.revision != self.revision:
            raise Conflict(f"plan {plan.id} is for revision {plan.revision}, but the draft is revision "
                           f"{self.revision} now; plan again")

    def _remember(self, plan: Plan) -> None:
        self._plans[plan.id] = plan
        while len(self._plans) > _KEEP_PLANS:
            del self._plans[next(iter(self._plans))]
        if plan.revision == self.revision:
            self._current = plan

    @contextlib.asynccontextmanager
    async def _nodes(self, desired: SiteManifest) -> AsyncIterator[NodeResolver]:
        """Node resolver for ``desired``: the driver's connection for nodes
        the site runs at the same address, a temporary client for nodes the
        draft adds or moves."""
        running = self.site.manifest.nodes
        extra: dict[str, NodeApi] = {}

        def resolve(name: str) -> NodeApi:
            want = desired.nodes[name]
            have = running.get(name)
            if have is not None and have.get("transport") == want.get("transport"):
                return self.site.node(name)
            if name not in extra:
                extra[name] = self.site.bacnet_uc.connect(name, want)
            return extra[name]

        try:
            yield resolve
        finally:
            for client in extra.values():
                await client.close()

    # -- apply and rollback -------------------------------------------------------------------
    async def apply(self, plan_id: str, *, run_id: str | None = None,
                    on_progress: ProgressCallback | None = None) -> ApplyOutcome:
        async with self._lock:
            plan = await self.get_plan(plan_id)
            self.check_applicable(plan)
            desired = self.current
            row = await self.store.get_plan(plan.id)
            attempt = (row["attempts"] if row is not None else 0) + 1
            await self.store.update_plan(plan.id, state="applying", attempts=attempt)
            logger.info("applying plan %s (revision %d, attempt %d)", plan.id, plan.revision, attempt)
            try:
                async with self._nodes(desired) as nodes:
                    results = await apply_plan(
                        plan, nodes, self.gateway, PlanBackupStore(self.store, attempt), on_progress,
                        self.policy.settings.max_devices_per_stage, verify_timeout_s=self.apply_verify_timeout_s,
                    )
            except BaseException:
                # Interrupted (the run was cancelled) or broken half-way: the attempt failed, and
                # the plan stays the draft's plan to retry or roll back, also after a restart.
                await self.store.update_plan(plan.id, state="failed")
                raise
            outcome = ApplyOutcome(plan, results, attempt, self.live_revision)
            if outcome.ok:
                await self.store.set_live(plan.revision)
                await self._go_live(desired, plan.revision)
                outcome.live_revision = plan.revision
                self.site.refresh(t for t in plan.targets if t != GATEWAY)
            await self.store.update_plan(plan.id, state="applied" if outcome.ok else "failed")
        await self._notify(run_id, plan if not outcome.ok else None)
        return outcome

    async def _go_live(self, manifest: SiteManifest, revision: int) -> None:
        self.live, self.live_revision = manifest, revision
        self.draft, self.draft_revision, self._current = None, None, None
        doc = manifest.to_dict()
        self.policy.configure(PolicySettings.from_dict(doc.get("policy")), doc.get("safety") or {})
        await self.site.reload(manifest)
        await self.gateway.bridges.replace(manifest.bridges)
        logger.info("manifest revision %d is live", revision)

    async def rollback(self, plan_id: str) -> list[ChangeResult]:
        """Restore every target of a failed plan from the backup taken before
        the plan's first apply attempt (targets never backed up are skipped).
        A plan that went live is refused (``Conflict``): restoring its
        targets would leave the devices, the tags and the policy's safety map
        behind the live manifest; going back takes a new plan."""
        async with self._lock:
            plan = await self.get_plan(plan_id)
            row = await self.store.get_plan(plan.id)
            if row is not None and row["state"] == "applied":
                raise Conflict(f"plan {plan.id} was applied and revision {plan.revision} is live; to go back, "
                               f"change the draft and apply a new plan")
            results = []
            async with self._nodes(await self._revision(plan.revision)) as nodes:
                for target in plan.targets:
                    row = await self.store.get_plan_backup(plan.id, target, first=True)
                    if row is None:
                        continue
                    backup = TargetBackup.from_json(row["documents"])
                    results.append(await rollback_target(backup, nodes, self.gateway))
            self.site.refresh(t for t in plan.targets if t != GATEWAY)
        return results

    async def _notify(self, run_id: str | None, plan: Plan | None) -> None:
        if self.on_plan is None:
            return
        try:
            await self.on_plan(run_id, plan)
        except Exception:
            logger.exception("plan callback failed")


def _check_life_safety_marks(before: SiteManifest, after: SiteManifest, roles: set[str] | frozenset[str]) -> None:
    was = {k for k, v in before.safety.items() if v is _LIFE_SAFETY}
    now = {k for k, v in after.safety.items() if v is _LIFE_SAFETY}
    changed = sorted(was ^ now)
    if changed and "admin" not in roles:
        raise PolicyDenied("only a site admin may add or remove life-safety marks: " + ", ".join(changed))


def _patch_summary(patch: Any) -> str:
    ops = [f"{op.get('op')} {op.get('path')}" for op in patch if isinstance(op, dict)][:5]
    more = len(patch) - len(ops) if isinstance(patch, list) else 0
    return "; ".join(ops) + (f" and {more} more" if more > 0 else "")
