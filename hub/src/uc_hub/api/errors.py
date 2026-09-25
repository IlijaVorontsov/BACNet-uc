"""Errors in the API's shape: ``{"error": {"code", "message", "details"?}}``
with the status of the code (docs/ai-harness/API.md)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from ..core.errors import HubError, ValidationFailed
from ..store.db import dumps

logger = logging.getLogger(__name__)

STATUS: dict[str, int] = {
    "invalid": 400,
    "validation": 400,
    "unauthorized": 401,
    "denied": 403,
    "not_found": 404,
    "conflict": 409,
    "unsupported": 501,
    "device_error": 502,
    "timeout": 504,
}


class HubJSONResponse(JSONResponse):
    """JSON as the store writes it: strict (NaN from a faulty sensor becomes
    null) and ASCII-only, so any text can be sent."""

    def render(self, content: Any) -> bytes:
        return dumps(content).encode()


def error_response(status: int, code: str, message: str, details: Any = None,
                   headers: dict[str, str] | None = None) -> HubJSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return HubJSONResponse({"error": body}, status_code=status, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(HubError)
    async def hub_error(request: Request, exc: HubError) -> HubJSONResponse:
        status = STATUS.get(exc.code, 500)
        details = exc.errors if isinstance(exc, ValidationFailed) and exc.errors else None
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        if status >= 500:
            logger.warning("%s %s: %s (%s)", request.method, request.url.path, exc, exc.code)
        return error_response(status, exc.code, str(exc) or exc.code, details, headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> HubJSONResponse:
        errors = [f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg', 'invalid')}"
                  for err in exc.errors()]
        return error_response(400, "invalid", "invalid request: " + "; ".join(errors), errors)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> HubJSONResponse:
        code = {404: "not_found", 405: "invalid"}.get(exc.status_code, f"http_{exc.status_code}")
        message = f"no route {request.url.path}" if exc.status_code == 404 else str(exc.detail)
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception) -> HubJSONResponse:
        logger.exception("%s %s failed", request.method, request.url.path)
        return error_response(500, "error", "internal error; the hub log has the details")
