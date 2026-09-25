"""The JSON endpoints of docs/ai-harness/API.md."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .. import __version__
from ..core.errors import HubError, NotFound, error_for
from ..core.types import DeviceDescription, ProtocolName
from ..policy.policy import ranked_roles
from ..runtime.config import Identity
from ..runtime.services import Services
from . import bodies
from .auth import Auth, require

#: How long ``GET /api/devices/{name}`` waits for a device that was never described.
DESCRIBE_TIMEOUT_S = 15.0


def json_routes(services: Services, auth: Auth) -> APIRouter:
    router = APIRouter(prefix="/api")
    caller = Depends(auth.caller)

    # -- site and points ------------------------------------------------------------------
    @router.get("/health")
    async def health() -> dict[str, Any]:
        llm = services.llm
        return {
            "ok": True,
            "version": __version__,
            "site": services.site.name,
            "llm": {"provider": llm.name, "model": llm.model, "configured": bool(getattr(llm, "configured", True))},
            "dev_mode": auth.dev_mode,
        }

    @router.get("/me")
    async def me(who: Identity = caller) -> dict[str, Any]:
        return {"user": who.user, "roles": ranked_roles(who.roles)}

    @router.get("/site")
    async def site(who: Identity = caller) -> dict[str, Any]:
        return services.site.site_json()

    @router.get("/devices/{name}")
    async def device(name: str, refresh: bool = False, who: Identity = caller) -> dict[str, Any]:
        """The last description; ``refresh`` describes an online device again,
        for what changes while it runs (its apps' state, ticks and errors)."""
        site = services.site
        record = site.device(name)
        description = site.description(name)
        if description is None or (refresh and record.online):
            try:
                async with asyncio.timeout(DESCRIBE_TIMEOUT_S):
                    description = await site.describe(name)
            except (HubError, TimeoutError, OSError) as e:
                why = str(e) or type(e).__name__
                if description is None:
                    # Never described: the record, and why its points are unknown.
                    return DeviceDescription(record, extra={"error": f"not described yet: {why}"}).to_json()
                out = description.to_json()
                out["extra"] = {**out["extra"], "error": f"not described again: {why}"}
                return out
        return description.to_json()

    @router.get("/points")
    async def points(q: str = "", device: str | None = None, space: str | None = None, tag: str | None = None,
                     limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                     who: Identity = caller) -> dict[str, Any]:
        site = services.site
        total, found = site.search(q, space=space or None, device=device or None, tag=tag or None, limit=limit,
                                   offset=offset)
        out = []
        for point in found:
            item = point.to_json()
            reading = site.latest(point.ref)
            if reading is not None:
                item["reading"] = reading.to_json()
            out.append(item)
        return {"total": total, "points": out}

    @router.post("/points/read")
    async def read_points(request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.ReadPoints)
        site = services.site
        refs = [site.point_ref(text) for text in body.ids]
        return {"readings": [r.to_json() for r in await site.read(refs)]}

    @router.post("/discover")
    async def discover(request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.Discover)
        protocol = ProtocolName(body.protocol) if body.protocol else None
        return await services.site.discover(protocol, body.timeout_s)

    @router.post("/devices/{name}/identify")
    async def identify(name: str, request: Request, who: Identity = caller) -> dict[str, Any]:
        """The person asking is the approver: the call goes through the runner
        (policy, audit) as a tier L call they approve themselves."""
        require(who, "operator")
        body = await bodies.parse(request, bodies.Identify)
        services.site.device(name)
        runner = services.runner
        ctx = services.context(who.user, who.roles)
        prepared = await runner.prepare(ctx, "device_identify", {"device": name, "seconds": body.seconds})
        if prepared.action == "refused":
            assert prepared.result is not None
            raise error_for(prepared.result.error_code or "error", prepared.result.summary)
        result = await runner.execute(prepared, approved_by=who.user, approver_roles=who.roles)
        if not result.ok:
            raise error_for(result.error_code or "error", result.summary)
        return {}

    # -- manifest, plan, tests ------------------------------------------------------------------
    @router.get("/manifest")
    async def manifest(who: Identity = caller) -> dict[str, Any]:
        return await services.store.manifest_state()

    @router.get("/manifest/revisions")
    async def revisions(who: Identity = caller) -> dict[str, Any]:
        return {"revisions": await services.store.list_revisions()}

    @router.get("/plan")
    async def plan(who: Identity = caller) -> dict[str, Any]:
        current = services.manifests.current_plan()
        return {"plan": current.to_json() if current is not None else None}

    @router.get("/tests")
    async def tests(who: Identity = caller) -> dict[str, Any]:
        return await services.store.test_results()

    # -- runs --------------------------------------------------------------------------------
    @router.get("/runs")
    async def runs(limit: int = Query(20, ge=1, le=200), who: Identity = caller) -> dict[str, Any]:
        return {"runs": await services.store.list_runs(limit)}

    @router.post("/runs", status_code=201)
    async def create_run(request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.CreateRun)
        return await services.runs.create_run(user=who.user, roles=who.roles, message=body.message,
                                              playbook=body.playbook)

    @router.get("/runs/{run_id}")
    async def run(run_id: str, who: Identity = caller) -> dict[str, Any]:
        row = await services.store.get_run(run_id)
        if row is None:
            raise NotFound(f"no run {run_id}")
        return row

    @router.post("/runs/{run_id}/messages", status_code=202)
    async def message(run_id: str, request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.Message)
        await services.runs.post_message(run_id, user=who.user, roles=who.roles, message=body.message)
        return {}

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, who: Identity = caller) -> dict[str, Any]:
        await services.runs.cancel(run_id, user=who.user, roles=who.roles)
        return {}

    @router.post("/runs/{run_id}/answer")
    async def answer(run_id: str, request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.Answer)
        await services.runs.answer(run_id, body.question_id, body.answer, user=who.user, roles=who.roles)
        return {}

    # -- approvals and audit ---------------------------------------------------------------------
    @router.get("/approvals")
    async def approvals(state: str | None = None, who: Identity = caller) -> dict[str, Any]:
        return {"approvals": await services.store.list_approvals(state=state or None)}

    @router.post("/approvals/{approval_id}")
    async def decide(approval_id: str, request: Request, who: Identity = caller) -> dict[str, Any]:
        body = await bodies.parse(request, bodies.Decide)
        return await services.approvals.decide(approval_id, body.decision, user=who.user, roles=who.roles,
                                               comment=body.comment, scope=body.scope)

    @router.get("/audit")
    async def audit(limit: int = Query(100, ge=1, le=1000), who: Identity = caller) -> dict[str, Any]:
        require(who, "commissioner")
        return {"entries": await services.store.list_audit(limit)}

    return router
