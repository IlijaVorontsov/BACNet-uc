"""Test helpers shared by every work package (DESIGN.md §19.4).

    from uc_controller.testing import FakeFacts, FakeSecrets, context, render_role, assert_golden

    ctx = context("trunk")                      # site/examples/trunk.yaml, fake facts and secrets
    files = render_role("network", "flat")      # {path: bytes}, install pass
    assert_golden(files, "controller/tests/core/golden/flat")

``UC_UPDATE_GOLDEN=1`` makes assert_golden rewrite the golden directory.
Nothing here touches the real system.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config as _config
from . import paths as _paths
from .apply import build_context
from .model import Facts
from .render import render_roles
from .roles import load_roles
from .roles.base import RenderContext
from .secrets import SecretError, check_name


@dataclass(frozen=True)
class FakeFacts(Facts):
    """Facts of a Raspberry Pi 4 with an RTC; nothing optional exists on disk."""
    multiarch: str = "aarch64-linux-gnu"
    mosquitto: str | None = "2.1.2"
    pg_major: int | None = 18
    rtc: bool = True
    container: bool = False
    root_partuuid: str | None = "8a4d0bd1-02"
    paths_present: frozenset | None = frozenset()


FAKE_SSH_KEY = """-----BEGIN OPENSSH PRIVATE KEY-----
ZmFrZSBrZXkgZm9yIHRlc3RzIG9ubHkgKHVjLWNvbnRyb2xsZXIgdGVzdGluZy5weSkK
-----END OPENSSH PRIVATE KEY-----"""


def fake_value(name: str, kind: str) -> str:
    """Deterministic stand-in for a generated secret."""
    digest = hashlib.sha256(f"uc-fake:{name}".encode()).hexdigest()
    if kind == "password":
        return ("fake" + digest)[:32]
    if kind == "token":
        return ("faketoken" + digest)[:43]
    if kind == "hex32":
        return digest
    if kind == "ssh-ed25519":
        return FAKE_SSH_KEY
    raise SecretError(f"unknown secret kind {kind!r}")


def guess_kind(name: str) -> str:
    if name.startswith("hub-token-"):
        return "token"
    if name.endswith("ssh-key"):
        return "ssh-ed25519"
    if name.endswith("-cipher"):
        return "hex32"
    return "password"


class FakeSecrets:
    """In-memory SecretStore with deterministic values.

    ``get`` of a secret that was never set or ensured returns a fake value of
    the guessed kind, so golden renders never depend on randomness.
    """

    def __init__(self, values: dict[str, str] | None = None):
        self.values: dict[str, str] = dict(values or {})
        self.ensured: list[tuple[str, str]] = []

    def path(self, name: str) -> str:
        return _paths.SECRETS_DIR + "/" + check_name(name)

    system_path = path

    def exists(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str) -> str:
        check_name(name)
        if name not in self.values:
            self.values[name] = fake_value(name, guess_kind(name))
        return self.values[name]

    def set(self, name: str, value) -> None:
        check_name(name)
        self.values[name] = value.decode() if isinstance(value, bytes) else value

    def names(self) -> list[str]:
        return sorted(self.values)

    def ensure(self, name: str, kind: str) -> bool:
        check_name(name)
        self.ensured.append((name, kind))
        if name in self.values:
            return False
        self.values[name] = fake_value(name, kind)
        return True

    def ensure_standard(self, site) -> list[str]:
        from .secrets import standard_secrets
        return [n for n, k in standard_secrets(site) if self.ensure(n, k)]


def example_path(example: str) -> Path:
    return _paths.examples_dir() / f"{example}.yaml"


def context(example: str = "trunk", prefix: str = "", facts: Facts | None = None,
            secrets=None, devices=None, hold: bool = False, first_boot: bool = False,
            board: dict | None = None) -> RenderContext:
    """A RenderContext built from site/examples/<example>.yaml (or a path)."""
    path = example if str(example).endswith((".yaml", ".yml")) else example_path(example)
    site = _config.load(str(path))
    devices = list(devices or [])
    derived = _config.derive(site, devices, board or {})
    return build_context(site, devices, derived, secrets if secrets is not None else FakeSecrets(),
                         facts or FakeFacts(), _paths.current(), first_boot=first_boot,
                         hold=hold, prefix=prefix)


def render_role(name: str, example: str = "trunk", **kw) -> dict[str, bytes]:
    """Install-pass render of one role: {path: content}."""
    ctx = context(example, **kw)
    roles = load_roles([name])
    return {path: owned.file.content for path, owned in render_roles(roles, ctx).items()}


def render_roles_files(names: list[str], example: str = "trunk", **kw):
    """Like render_role for several roles; returns {path: Owned} (with modes and owners)."""
    ctx = context(example, **kw)
    return render_roles(load_roles(names), ctx)


def assert_golden(files: dict[str, bytes], golden_dir: str | os.PathLike) -> None:
    """Compare {abs path: bytes} with golden_dir/<abs path>.

    Missing, extra and differing files fail with a unified diff. With
    UC_UPDATE_GOLDEN=1 the directory is rewritten instead.
    """
    golden = Path(golden_dir)
    if os.environ.get("UC_UPDATE_GOLDEN") == "1":
        if golden.exists():
            shutil.rmtree(golden)
        for path, data in files.items():
            target = golden / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return
    have = {"/" + str(f.relative_to(golden)): f.read_bytes() for f in golden.rglob("*") if f.is_file()} \
        if golden.exists() else {}
    problems = []
    for path in sorted(set(files) | set(have)):
        if path not in have:
            problems.append(f"{path}: not in golden dir {golden} (run with UC_UPDATE_GOLDEN=1)")
        elif path not in files:
            problems.append(f"{path}: in golden dir but no longer rendered")
        elif have[path] != files[path]:
            import difflib
            diff = "\n".join(difflib.unified_diff(
                have[path].decode(errors="replace").splitlines(),
                files[path].decode(errors="replace").splitlines(),
                f"golden{path}", f"rendered{path}", lineterm=""))
            problems.append(f"{path} differs:\n{diff}")
    if problems:
        raise AssertionError("golden mismatch:\n" + "\n".join(problems))
