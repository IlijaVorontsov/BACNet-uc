"""Render engine (DESIGN.md §5.4 steps 3 and 5, §19.2)."""

import os

import pytest

from uc_controller import render
from uc_controller.render import RenderError, Templates, render_roles, write_tree
from uc_controller.roles.base import RenderedFile
from uc_controller.testing import context

from dummy import DummyRole


def test_template_global_P_applies_the_prefix(tmp_path):
    (tmp_path / "t.j2").write_text("include {{ P('/etc/x.conf') }}\nvalue={{ v }}\n")
    install = Templates("", str(tmp_path))
    staged = install.with_prefix("/var/lib/uc-controller/staging/tx")
    assert install.render("t.j2", v=1) == "include /etc/x.conf\nvalue=1\n"
    assert staged.render("t.j2", v=1) == "include /var/lib/uc-controller/staging/tx/etc/x.conf\nvalue=1\n"


def test_strict_undefined_and_trailing_newline(tmp_path):
    (tmp_path / "t.j2").write_text("{{ missing }}")
    (tmp_path / "n.j2").write_text("a\n")
    tpl = Templates("", str(tmp_path))
    with pytest.raises(RenderError):
        tpl.render("t.j2")
    assert tpl.render("n.j2") == "a\n"
    with pytest.raises(RenderError):
        tpl.P("relative/path")


def test_context_prefix_reaches_templates():
    ctx = context("trunk", prefix="/stage")
    assert ctx.P("/etc/a") == "/stage/etc/a"
    assert ctx.tpl.P("/etc/a") == "/stage/etc/a"
    assert ctx.with_prefix("").tpl.P("/etc/a") == "/etc/a"


def test_duplicate_path_is_an_error():
    ctx = context("trunk")
    a = DummyRole("a", 10, {"/etc/same.conf": "a\n"})
    b = DummyRole("b", 20, {"/etc/same.conf": "b\n"})
    with pytest.raises(RenderError, match="both role a and role b"):
        render_roles([a, b], ctx)


def test_bad_paths_are_errors():
    ctx = context("trunk")
    for bad in ("etc/relative", "/etc/../shadow", "/etc/dir/"):
        with pytest.raises(RenderError):
            render_roles([DummyRole("x", 1, {bad: "x"})], ctx)
    role = DummyRole("x", 1, {"/etc/a": RenderedFile("/etc/a", "text-not-bytes")})
    with pytest.raises(RenderError):
        render_roles([role], ctx)


def test_render_is_sorted_and_skips_disabled_roles():
    ctx = context("trunk")
    a = DummyRole("a", 10, {"/etc/z": "z", "/etc/a": "a"})
    b = DummyRole("b", 20, {"/etc/m": "m"}, enabled=False)
    files = render_roles([a, b], ctx)
    assert list(files) == ["/etc/a", "/etc/z"]
    assert {o.role for o in files.values()} == {"a"}


def test_render_is_deterministic():
    one = render_roles([DummyRole("a", 1, {"/b": "1", "/a": "2"})], context("flat"))
    two = render_roles([DummyRole("a", 1, {"/a": "2", "/b": "1"})], context("flat"))
    assert [(k, v.file.content) for k, v in one.items()] == [(k, v.file.content) for k, v in two.items()]


def test_write_tree_prefixes_and_modes(tmp_path):
    ctx = context("trunk", prefix=str(tmp_path))
    role = DummyRole("a", 1, {"/etc/x.conf": "path=@P@/etc/y\n",
                              "/etc/secret": RenderedFile("/etc/secret", b"s", mode=0o600, secret=True)})
    files = render_roles([role], ctx)
    write_tree(files, str(tmp_path))
    assert (tmp_path / "etc/x.conf").read_text() == f"path={tmp_path}/etc/y\n"
    assert os.stat(tmp_path / "etc/secret").st_mode & 0o777 == 0o600


def test_templates_dir_comes_from_the_source_tree():
    assert render.Templates().directory.endswith("controller/templates")
