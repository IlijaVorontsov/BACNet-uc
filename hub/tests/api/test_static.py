"""The built web app at /: files, the SPA fallback, no way out of web_dir,
and cache headers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from support.site import Hub

from uc_hub.api import create_app


@pytest.fixture
def web(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>uc-hub</title>")
    (root / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    (root / "assets" / "app-1234.js").write_text("console.log('app');")
    (tmp_path / "secret.txt").write_text("not for the web")
    (root / "escape.txt").symlink_to(tmp_path / "secret.txt")
    return root


@pytest.fixture
async def site(hub: Hub, web: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(hub.services, web_dir=web)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
        yield client


async def test_files_and_the_app_fallback(site: httpx.AsyncClient) -> None:
    index = await site.get("/")
    assert index.status_code == 200 and "<title>uc-hub</title>" in index.text
    assert index.headers["cache-control"] == "no-cache"
    for route in ("/devices/r204-ctl", "/runs/r_1/approvals", "/changes"):
        response = await site.get(route)
        assert response.status_code == 200 and response.text == index.text
    asset = await site.get("/assets/app-1234.js")
    assert asset.text == "console.log('app');"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert (await site.get("/sw.js")).headers["cache-control"] == "no-cache"
    assert (await site.head("/")).status_code == 200


@pytest.mark.parametrize("path", ["/assets/missing.js", "/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt",
                                  "/%2Fetc%2Fpasswd", "/escape.txt", "/index.html%00.js"])
async def test_nothing_outside_web_dir(site: httpx.AsyncClient, path: str) -> None:
    response = await site.get(path)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert "not for the web" not in response.text


async def test_api_paths_never_fall_back(site: httpx.AsyncClient) -> None:
    for path in ("/api", "/api/nope", "/api/runs/x/nope"):
        response = await site.get(path)
        assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert (await site.get("/api/health")).json()["ok"] is True


async def test_a_web_dir_without_an_index_is_not_served(hub: Hub, tmp_path: Path) -> None:
    app = create_app(hub.services, web_dir=tmp_path / "empty")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as client:
        assert (await client.get("/")).status_code == 404
