# SPDX-License-Identifier: Apache-2.0
"""Locations of the BACnet-uc repository and of the harness state.

The repository root is the directory that holds ``west.yml`` and
``schemas/``. It is taken from the environment variable ``BACNET_UC_ROOT``
when set, otherwise found by walking up from this package (editable
install inside the repository) and then from the current directory.

Harness state (inventory, build cache, simulation work directories) lives in
``<home>/.bacnet-uc`` where ``<home>`` is ``BACNET_UC_HOME`` or the current
directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from bacnet_uc_harness.errors import HarnessError

ENV_ROOT = "BACNET_UC_ROOT"
ENV_HOME = "BACNET_UC_HOME"
STATE_DIR_NAME = ".bacnet-uc"


def _is_root(path: Path) -> bool:
    return (path / "west.yml").is_file() and (path / "schemas").is_dir()


def _walk_up(start: Path) -> Path | None:
    start = start.resolve()
    for cand in (start, *start.parents):
        if _is_root(cand):
            return cand
    return None


def repo_root(start: Path | str | None = None) -> Path:
    """The BACnet-uc repository root.

    Args:
        start: directory to search upwards from before the defaults.

    Raises:
        HarnessError: no repository found (set ``BACNET_UC_ROOT``).
    """
    env = os.environ.get(ENV_ROOT)
    if env:
        root = Path(env).expanduser().resolve()
        if not _is_root(root):
            raise HarnessError(
                f"{ENV_ROOT}={env} is not a BACnet-uc repository (needs west.yml and schemas/)"
            )
        return root
    candidates: list[Path] = []
    if start is not None:
        candidates.append(Path(start))
    candidates += [Path(__file__).parent, Path.cwd()]
    for cand in candidates:
        found = _walk_up(cand)
        if found is not None:
            return found
    raise HarnessError(
        "BACnet-uc repository not found: run inside the repository or set "
        f"{ENV_ROOT} to the directory containing west.yml and schemas/"
    )


def schemas_dir() -> Path:
    """``schemas/`` (JSON schemas of device.json, io.json, apps.json, system manifests)."""
    return repo_root() / "schemas"


def docs_dir() -> Path:
    return repo_root() / "docs"


def wasm_dir() -> Path:
    return repo_root() / "wasm"


def wasm_sdk_dir() -> Path:
    """``wasm/sdk`` (uc-cc, uc-aot, include/bacnet_uc.h)."""
    return repo_root() / "wasm" / "sdk"


def wasm_include_dir() -> Path:
    return wasm_sdk_dir() / "include"


def bacnet_uc_header() -> Path:
    """The guest ABI header ``wasm/sdk/include/bacnet_uc.h``."""
    return wasm_include_dir() / "bacnet_uc.h"


def wasm_examples_dir() -> Path:
    return repo_root() / "wasm" / "examples"


def firmware_dir() -> Path:
    """The Zephyr application ``firmware/``."""
    return repo_root() / "firmware"


def board_io_dir() -> Path:
    """Devicetree IO catalogs per board (``firmware/boards/io/<board>.dtsi``)."""
    return firmware_dir() / "boards" / "io"


def workspace_top() -> Path:
    """West workspace top directory (contains ``.west/``); the repository's
    parent when no ``.west`` directory is found."""
    root = repo_root()
    for cand in (root, *root.parents):
        if (cand / ".west").is_dir():
            return cand
    return root.parent


def home_dir() -> Path:
    """Directory that holds ``.bacnet-uc/`` (``BACNET_UC_HOME`` or the cwd)."""
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser().resolve() if env else Path.cwd().resolve()


def state_dir(create: bool = False) -> Path:
    """``<home>/.bacnet-uc``."""
    path = home_dir() / STATE_DIR_NAME
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(*parts: str, create: bool = True) -> Path:
    """``<home>/.bacnet-uc/cache/<parts...>`` (build artifacts)."""
    path = state_dir().joinpath("cache", *parts)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def board_key(board: str) -> str:
    """File-name form of a board target: ``frdm_mcxn947/mcxn947/cpu0`` ->
    ``frdm_mcxn947_mcxn947_cpu0``."""
    return board.replace("/", "_")
