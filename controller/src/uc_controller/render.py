"""Rendering (DESIGN.md §5.4 steps 3 and 5, §19.2).

Jinja2 with StrictUndefined and keep_trailing_newline; templates load from
paths.templates_dir(). The global ``P(path)`` returns ctx.prefix + path:

- validation pass: prefix = staging root; files are written under it and
  the validators read them there;
- install pass: prefix = ''; the result is diffed and installed.

Rendering is deterministic: roles in install order, files sorted by path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import jinja2

from . import paths as _paths
from .roles.base import RenderContext, RenderedFile, Role

log = logging.getLogger(__name__)


class RenderError(Exception):
    """A role rendered something invalid (duplicate or relative path, bad type)."""


def _to_list(value):
    return list(value) if value is not None else []


class Templates:
    """Template access bound to one prefix: ``tpl.render(name, **vars) -> str``."""

    def __init__(self, prefix: str = "", directory: str | None = None):
        self.prefix = prefix
        self.directory = str(directory or _paths.templates_dir())
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(self.directory),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False,
        )
        self.env.globals["P"] = self.P
        self.env.filters["to_list"] = _to_list

    def P(self, path: str) -> str:  # noqa: N802
        if not str(path).startswith("/"):
            raise RenderError(f"P() needs an absolute path, got {path!r}")
        return self.prefix + str(path)

    def with_prefix(self, prefix: str) -> "Templates":
        return Templates(prefix, self.directory)

    def render(self, name: str, /, **variables) -> str:
        try:
            return self.env.get_template(name).render(**variables)
        except jinja2.UndefinedError as exc:
            raise RenderError(f"template {name}: {exc}") from exc
        except jinja2.TemplateNotFound as exc:
            raise RenderError(f"template {name} not found in {self.directory}") from exc


@dataclass(frozen=True)
class Owned:
    """A rendered file and the role that produced it."""
    role: str
    file: RenderedFile


def render_roles(roles: list[Role], ctx: RenderContext) -> dict[str, Owned]:
    """Render every enabled role; returns {path: Owned} sorted by path.

    A disabled role renders nothing (its old files become stale and are
    removed by apply). Two roles rendering the same path is an error.
    """
    out: dict[str, Owned] = {}
    for role in roles:
        if not role.enabled(ctx):
            continue
        for f in role.render(ctx):
            if not isinstance(f, RenderedFile):
                raise RenderError(f"role {role.name} returned {type(f).__name__}, not RenderedFile")
            if not f.path.startswith("/") or "/../" in f.path or f.path.endswith("/"):
                raise RenderError(f"role {role.name}: bad target path {f.path!r}")
            if not isinstance(f.content, bytes):
                raise RenderError(f"role {role.name}: content of {f.path} is not bytes")
            if f.path in out:
                other = out[f.path].role
                raise RenderError(f"{f.path} is rendered by both role {other} and role {role.name}")
            out[f.path] = Owned(role.name, f)
    return dict(sorted(out.items()))


def write_tree(files: dict[str, Owned], root: str) -> None:
    """Write rendered files below root (staging, or `uc-ctl render --out`).

    Modes are applied; owners are not (the tree belongs to the caller).
    """
    for path, owned in files.items():
        target = root.rstrip("/") + path
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(owned.file.content)
        os.chmod(target, owned.file.mode)
