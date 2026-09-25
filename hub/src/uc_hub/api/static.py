"""The built browser app (``web_dir``) at ``/``.

A path that names a file below ``web_dir`` serves it; any other path
without a file extension serves ``index.html``, so the app's own routes
survive a reload. Paths that leave ``web_dir`` (``..``, symlinks pointing
outside) and missing assets are 404. Hashed assets (``/assets/``) may be
cached for good; ``index.html`` and the service worker are revalidated
on every load, so a new build reaches the browser.
"""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..core.errors import NotFound

logger = logging.getLogger(__name__)

_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "no-cache"


def mount_web(app: FastAPI, web_dir: Path) -> bool:
    """Serve the app from ``web_dir``; False (and a warning) when it has no index.html."""
    root = web_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning("web_dir %s has no index.html; the browser app is not served (build it with pnpm build)",
                       web_dir)
        return False

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def web(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise NotFound(f"no route /{path}")
        try:
            target = (root / path).resolve() if path else index
        except (OSError, ValueError):  # a NUL byte, a symlink loop
            raise NotFound(f"no file /{path}") from None
        if not target.is_relative_to(root):
            raise NotFound(f"no file /{path}")
        if target.is_file():
            return FileResponse(target, headers={"Cache-Control": _cache(target.relative_to(root))})
        if PurePosixPath(path).suffix:
            raise NotFound(f"no file /{path}")
        return FileResponse(index, headers={"Cache-Control": _REVALIDATE})

    return True


def _cache(relative: Path) -> str:
    return _IMMUTABLE if relative.parts[:1] == ("assets",) else _REVALIDATE
