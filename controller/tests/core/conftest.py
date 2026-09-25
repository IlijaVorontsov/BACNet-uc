"""Shared fixtures of the WP1 core tests (tier T1)."""

import os
import sys
from pathlib import Path

import pytest

CONTROLLER = Path(__file__).resolve().parents[2]
os.environ.setdefault("UC_CONTROLLER_SRC", str(CONTROLLER))
if str(CONTROLLER / "src") not in sys.path:
    sys.path.insert(0, str(CONTROLLER / "src"))

EXAMPLES = ("trunk", "dual", "flat")
GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture
def uc_root(tmp_path, monkeypatch):
    """A private UC_ROOT for one test."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("UC_ROOT", str(root))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    return root


@pytest.fixture
def example_path():
    def _path(name):
        return str(CONTROLLER / "site" / "examples" / f"{name}.yaml")
    return _path
