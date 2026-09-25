# SPDX-License-Identifier: Apache-2.0
"""WAMR memory pool budget of the applications on a node.

Every module image, instance, linear memory (incl. the app heap) and
execution environment of a node's WebAssembly apps is allocated from one pool
of ``CONFIG_UC_APP_POOL_SIZE`` bytes. :func:`board_pool_size` reads the value
the firmware is built with for a board (``firmware/boards/<board>.conf``, else
``firmware/prj.conf``, else the Kconfig default); :func:`app_pool_estimate`
predicts what one app takes:

``linear``
    the linear memory WAMR allocates for the instance, as predicted by
    ``wasm/sdk/uc-wasm-info`` (``linear_memory_bounds_bytes``: shrunk to
    ``__heap_base``, plus ``heap_kb``, rounded to the 4 KiB page the firmware
    allocates; modules that grow their memory: the declared size);
``stack``
    ``stack_kb`` * 1024 (the WAMR operand/aux stack of the exec env);
``module``
    fast interpreter: ``CODE_FACTOR`` * code section bytes (the translated
    code and the module structures); AOT: ``AOT_FACTOR`` * the ``.aot`` file
    size + ``AOT_BASE`` (file buffer, text/data sections and relocation
    tables in the pool);
``overhead``
    ``APP_OVERHEAD`` bytes per app (instance and exec env structures, native
    symbol table, event queue).

The constants are fitted to the pool use of the four stock examples measured
on native_sim (64-bit WAMR structures, integration build, ``node_info``
``pool_free`` before/after each deploy): thermostat 54920 B, alarm 50376 B,
blinky 38776 B (heap_kb 8), uc-link 39496 B (heap_kb 0); the estimates are
0.6-2.1 % above. As x86-64 AOT files (``CONFIG_WAMR_AOT=y`` build) the same
apps took 78208 B (thermostat, 23328 B .aot), 74360 B (alarm, 20040 B) and
61280 B (blinky, 11788 B); a fourth did not fit (``NO_MEM``). The Cortex-M
boards need somewhat less (32-bit pointers; thumb AOT code is not
calibrated, the x86-64 factors are used).
The check is a warning: the firmware reports rc ``NO_MEM`` when an app does
not fit.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from bacnet_uc_harness import paths
from bacnet_uc_harness.errors import HarnessError

#: firmware/Kconfig default of CONFIG_UC_APP_POOL_SIZE
KCONFIG_POOL_DEFAULT = 131072
#: pool bytes the allocator and the runtime use without apps (native_sim:
#: pool_total 261760 of 262144, 392 B used)
POOL_RESERVED = 1024
CODE_FACTOR = 5.3
AOT_FACTOR = 1.5
AOT_BASE = 17408
APP_OVERHEAD = 6144
#: share of the pool above which the budget is reported as tight
TIGHT = 0.9

_POOL_RE = re.compile(r"^\s*CONFIG_UC_APP_POOL_SIZE\s*=\s*(\d+)\s*$", re.M)


@lru_cache(maxsize=16)
def _pool_from_files(board_key: str, firmware: str) -> tuple[int, str]:
    fw = Path(firmware)
    for conf in (fw / "boards" / f"{board_key}.conf", fw / "prj.conf"):
        try:
            m = _POOL_RE.search(conf.read_text(encoding="utf-8"))
        except OSError:
            continue
        if m:
            return int(m.group(1)), str(conf.relative_to(fw.parent))
    return KCONFIG_POOL_DEFAULT, "firmware/Kconfig default"


def board_pool_size(board: str) -> tuple[int, str]:
    """``(CONFIG_UC_APP_POOL_SIZE, source file)`` of a board's firmware build."""
    key = paths.board_key(board)
    if key == "native_sim":
        key = "native_sim_native_64"
    return _pool_from_files(key, str(paths.firmware_dir()))


@lru_cache(maxsize=256)
def _wasm_info(path: str, mtime_ns: int, heap_kb: int) -> dict[str, Any]:
    tool = paths.wasm_sdk_dir() / "uc-wasm-info"
    if not tool.is_file():
        raise HarnessError(f"{tool} not found")
    res = subprocess.run([sys.executable, str(tool), "--json", "--allow-grow", "--heap-kb",
                          str(heap_kb), path], capture_output=True, text=True, check=False,
                         timeout=60)
    try:
        info = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise HarnessError(f"uc-wasm-info {path}: {(res.stderr or res.stdout).strip()}") from None
    if not isinstance(info, dict):
        raise HarnessError(f"uc-wasm-info {path}: unexpected output")
    return info


def wasm_info(wasm: Path | str, heap_kb: int = 8) -> dict[str, Any]:
    """``uc-wasm-info --json`` of a module (cached by path, mtime and heap)."""
    p = Path(wasm).resolve()
    return _wasm_info(str(p), p.stat().st_mtime_ns, int(heap_kb))


@dataclass
class AppEstimate:
    app: str
    module: int
    linear: int
    stack: int
    overhead: int = APP_OVERHEAD
    aot: bool = False
    note: str | None = None

    @property
    def total(self) -> int:
        return self.module + self.linear + self.stack + self.overhead

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"app": self.app, "total": self.total, "module": self.module,
                               "linear_memory": self.linear, "stack": self.stack,
                               "overhead": self.overhead, "aot": self.aot}
        if self.note:
            out["note"] = self.note
        return out


def app_pool_estimate(name: str, wasm: Path | str, *, heap_kb: int = 8, stack_kb: int = 4,
                      aot_file: Path | str | None = None) -> AppEstimate:
    """Predicted pool use of one app (see the module docstring)."""
    info = wasm_info(wasm, heap_kb)
    linear = info.get("linear_memory_bounds_bytes")
    note = None
    if not isinstance(linear, int):
        linear = int(info.get("memory_declared_bytes") or 65536) + heap_kb * 1024
        note = "linear memory not shrinkable (memory.grow or missing exports): declared size"
    if aot_file is not None:
        module = int(Path(aot_file).stat().st_size * AOT_FACTOR) + AOT_BASE
    else:
        module = int(int(info.get("code_bytes") or 0) * CODE_FACTOR)
    return AppEstimate(name, module, linear, stack_kb * 1024, aot=aot_file is not None,
                       note=note)


@dataclass
class NodeBudget:
    node: str
    board: str
    pool: int
    pool_source: str
    apps: list[AppEstimate] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def available(self) -> int:
        return max(0, self.pool - POOL_RESERVED)

    @property
    def total(self) -> int:
        return sum(a.total for a in self.apps)

    @property
    def status(self) -> str:
        if self.total > self.available:
            return "exceeded"
        if self.total > TIGHT * self.available:
            return "tight"
        return "ok"

    def message(self) -> str | None:
        """Warning text, ``None`` when the apps fit with headroom."""
        if self.status == "ok":
            return None
        parts = ", ".join(f"{a.app} ~{a.total // 1024} KiB" for a in self.apps)
        head = (f"WAMR pool budget of node {self.node!r} ({self.board}): the apps need about "
                f"{self.total} B ({parts}), CONFIG_UC_APP_POOL_SIZE is {self.pool} B "
                f"({self.pool_source})")
        if self.status == "exceeded":
            return head + ": apps will fail to start with NO_MEM; use fewer apps, smaller " \
                          "heap_kb/stack_kb or a board with a larger pool"
        return head + f": more than {int(TIGHT * 100)} % used, little headroom"

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "board": self.board, "pool": self.pool,
                "pool_source": self.pool_source, "available": self.available,
                "estimate": self.total, "status": self.status,
                "apps": [a.to_dict() for a in self.apps], "not_estimated": self.unknown}


def node_budget(node: str, board: str, apps: Sequence[Mapping[str, Any]]) -> NodeBudget:
    """Budget of one node. ``apps``: ``{"name", "wasm", "heap_kb", "stack_kb",
    "aot_file"?}``; apps without a readable ``wasm`` are listed in
    ``unknown``."""
    pool, source = board_pool_size(board)
    b = NodeBudget(node, board, pool, source)
    for a in apps:
        wasm = a.get("wasm")
        try:
            if wasm is None:
                raise HarnessError("no module")
            b.apps.append(app_pool_estimate(str(a["name"]), wasm,
                                            heap_kb=int(a.get("heap_kb", 8)),
                                            stack_kb=int(a.get("stack_kb", 4)),
                                            aot_file=a.get("aot_file")))
        except (HarnessError, OSError, subprocess.SubprocessError) as exc:
            b.unknown.append(f"{a.get('name')}: {exc}")
    return b


def system_budgets(system: Any, wasm_for: Any,
                   artifacts: Mapping[tuple[str, str], Any] | None = None
                   ) -> dict[str, NodeBudget]:
    """Budgets of every node of a :class:`~bacnet_uc_harness.manifest.System`
    that runs apps (generated uc-link instances included).

    Args:
        wasm_for: ``(AppSpec) -> Path`` of the app's ``.wasm`` (e.g.
            :meth:`bacnet_uc_harness.planner.ArtifactBuilder.wasm_for`).
        artifacts: built artifacts keyed ``(node, app)``; an AOT artifact's
            file size is used for its module.
    """
    from bacnet_uc_harness.render import node_apps

    out: dict[str, NodeBudget] = {}
    for n in system.nodes:
        specs = node_apps(system, n.name)
        if not specs:
            continue
        apps: list[dict[str, Any]] = []
        for app in specs:
            entry: dict[str, Any] = {"name": app.name, "heap_kb": app.heap_kb,
                                     "stack_kb": app.stack_kb}
            try:
                entry["wasm"] = wasm_for(app)
            except HarnessError:
                entry["wasm"] = None
            art = (artifacts or {}).get((n.name, app.name))
            if art is not None and getattr(art, "aot", False):
                entry["aot_file"] = art.path
            apps.append(entry)
        out[n.name] = node_budget(n.name, n.board, apps)
    return out
