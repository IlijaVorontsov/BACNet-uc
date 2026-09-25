"""The hub's HTTP API (docs/ai-harness/API.md) and the browser app.

``create_app(services)`` builds the FastAPI application over started
``Services``: the JSON endpoints, the event streams, the error shape, and,
when ``web_dir`` is configured, the built web app with its SPA fallback.
API responses are never cached (``Cache-Control: no-store``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .. import __version__
from ..runtime.services import Services
from . import errors
from .auth import Auth
from .routes import json_routes
from .static import mount_web
from .streams import stream_routes

logger = logging.getLogger(__name__)

#: Seconds between keep-alive comments on the event streams.
KEEPALIVE_S = 15.0


def create_app(services: Services, *, web_dir: Path | None = None, keepalive_s: float = KEEPALIVE_S) -> FastAPI:
    """``web_dir`` defaults to the config's; the services must be started."""
    auth = Auth(services.config)
    app = FastAPI(title="uc-hub", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  default_response_class=errors.HubJSONResponse)
    errors.install(app)
    app.include_router(json_routes(services, auth))
    app.include_router(stream_routes(services, auth, keepalive_s=keepalive_s))
    folder = web_dir if web_dir is not None else services.config.web_dir
    if folder is not None:
        mount_web(app, folder)
    app.add_middleware(_Headers)
    return app


class _Headers:
    """``no-store`` on every API response, ``nosniff`` on everything (plain
    ASGI middleware, so the event streams pass through untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        api = scope["path"] == "/api" or scope["path"].startswith("/api/")

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if not (api and k.lower() == b"cache-control")]
                if api:
                    headers.append((b"cache-control", b"no-store"))
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, with_headers)
