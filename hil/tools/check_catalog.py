#!/usr/bin/env python3
"""Check a DUT build's devicetree against the rig's pin tables and bench file (docs/HIL.md 5.4).

The rig is hand-wired from docs/hil/pin-tables.md and addresses DUT channels by their catalog
names and ids (D22). The ids are the devicetree order of the ``uc,io-channels`` catalog, so a
channel inserted or moved in the firmware silently rewires the rig (FW-01). The build job runs
this tool on every DUT build (``build/zephyr/edt.pickle``) and fails when:

- the catalog order differs from ``hilrig.bench.CATALOGS`` (what the host code assumes);
- on P1 (nucleo_f767zi), a catalog channel differs from pin tables 1.1 in id, name, kind, MCU pin,
  polarity, pull or ADC/timer channel; extra channels differ from the hil-io rows of 1.2;
  the markers (``/zephyr,user`` ``hil-marker-gpios``) differ from the marker row of 1.2;
- ``--bench``: the bench's DUT board is not the build's board, or a DUT channel wired in
  ``stim.chans`` is missing from the build's catalog.

Usage: check_catalog.py BUILD [--bench FILE] [--pin-tables FILE]
  BUILD is a DUT build directory (with zephyr/edt.pickle) or the edt.pickle itself. Loading the
  pickle needs Zephyr's python-devicetree: $ZEPHYR_BASE, or the build's CMakeCache ZEPHYR_BASE.
Exit status: 0 consistent, 1 mismatch, 2 usage or load error.
"""

from __future__ import annotations

import argparse
import pickle
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from os import environ
from pathlib import Path
from typing import Any

try:
    from hilrig.bench import CATALOGS, PROFILES, RIG_CHANNELS, Bench, BenchError
except ImportError:  # run from a checkout without 'pip install -e hil'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from hilrig.bench import CATALOGS, PROFILES, RIG_CHANNELS, Bench, BenchError

PIN_TABLES = Path(__file__).resolve().parents[2] / "docs" / "hil" / "pin-tables.md"
P1_BOARD = "nucleo_f767zi"
# Stimulus-only channels (rig self-test and senses): they have no DUT catalog entry.
STIM_ONLY = frozenset(RIG_CHANNELS) | {"lb", "v3v3", "v5"}
# zephyr/dt-bindings/gpio/gpio.h
GPIO_ACTIVE_LOW, GPIO_PULL_UP, GPIO_PULL_DOWN = 0x1, 0x10, 0x20

_PIN = re.compile(r"\bP([A-K])(\d{1,2})\b")
_HW = re.compile(r"\b(ADC\d_IN\d+|TIM\d+_CH\d)\b")


class CheckError(Exception):
    """The input cannot be checked (missing file, unreadable table or pickle)."""


@dataclass(frozen=True)
class Channel:
    """One DUT channel as the pin tables or the devicetree describe it."""

    id: int
    name: str
    kind: str  # di, do, ai, ao; "marker" for hil-marker-gpios
    pin: str | None = None  # MCU pin, e.g. PC13
    active_low: bool = False
    pull: str | None = None  # "up", "down" or None
    hw: str | None = None  # ADC1_IN3 or TIM1_CH1 for ai/ao


@dataclass(frozen=True)
class PinTable:
    """The P1 rows of pin tables 1.1 (catalog) and 1.2 (hil-io extras, markers)."""

    catalog: list[Channel]
    extras: list[Channel]
    markers: list[Channel]


@dataclass
class Report:
    """Problems (fail the check) and notes (informational) of one run."""

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------------ pin tables


def _section(text: str, heading: str) -> list[list[str]]:
    """Table rows (cells without the outer pipes) of the '### <heading>' section."""
    m = re.search(rf"^### {re.escape(heading)}\b.*?$(.*?)(?=^#{{2,3}} |\Z)", text, re.M | re.S)
    if m is None:
        raise CheckError(f"pin tables: no section '### {heading}'")
    rows = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows[1:]  # without the header row


def _flags(text: str) -> tuple[bool, str | None]:
    pull = "up" if "PULL_UP" in text else "down" if "PULL_DOWN" in text else None
    return "ACTIVE_LOW" in text, pull


def _pin(text: str) -> str | None:
    m = _PIN.search(text)
    return f"P{m.group(1)}{m.group(2)}" if m else None


def parse_pin_tables(text: str) -> PinTable:
    """Read the P1 catalog (1.1), the hil-io extras and the markers (1.2) from pin-tables.md."""
    catalog = []
    for cells in _section(text, "1.1"):
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        low, pull = _flags(cells[2])
        hw = _HW.search(cells[2])
        catalog.append(
            Channel(
                id=int(cells[0]),
                name=cells[1],
                kind=re.split(r"[\s,(]", cells[2], maxsplit=1)[0],
                pin=_pin(cells[3]),
                active_low=low,
                pull=pull,
                hw=hw.group(1) if hw else None,
            )
        )
    extras: list[Channel] = []
    markers: list[Channel] = []
    for cells in _section(text, "1.2"):
        if len(cells) < 4:
            continue
        pins = [f"P{p}{n}" for p, n in _PIN.findall(cells[1])]
        if cells[0].startswith("Marker"):
            markers = [Channel(i, f"m{i}", "marker", pin) for i, pin in enumerate(pins)]
        elif "(hil-io)" in cells[0]:
            names = re.findall(r"\b(d[io]\d+)\b", cells[0])
            ids = [int(i) for i in re.findall(r"\d+", re.sub(r"^.*?\bids?\b", "", cells[3]))]
            low, pull = _flags(cells[3])
            if not (len(names) == len(pins) == len(ids[: len(names)]) > 0):
                raise CheckError(f"pin tables 1.2: cannot read the row {cells[0]!r}")
            extras += [Channel(i, n, n[:2], p, low, pull) for n, p, i in zip(names, pins, ids, strict=False)]
    if not catalog or not markers:
        raise CheckError("pin tables: no catalog rows in 1.1 or no marker row in 1.2")
    return PinTable(catalog, sorted(extras, key=lambda c: c.id), markers)


# ------------------------------------------------------------------------------ devicetree


def load_edt(build: Path) -> tuple[Any, str | None]:
    """Unpickle build/zephyr/edt.pickle; also return the build's BOARD from CMakeCache.txt."""
    pickle_path = build if build.is_file() else build / "zephyr" / "edt.pickle"
    build_dir = pickle_path.parent.parent
    cache: dict[str, str] = {}
    cache_file = build_dir / "CMakeCache.txt"
    if cache_file.is_file():
        for line in cache_file.read_text(errors="replace").splitlines():
            m = re.match(r"^(\w+):[A-Z]+=(.*)$", line)
            if m:
                cache[m.group(1)] = m.group(2)
    base = environ.get("ZEPHYR_BASE") or cache.get("ZEPHYR_BASE")
    if base:
        sys.path.insert(0, str(Path(base) / "scripts" / "dts" / "python-devicetree" / "src"))
    try:
        with pickle_path.open("rb") as f:
            edt = pickle.load(f)  # the rig's own build output, never downloaded
    except FileNotFoundError as e:
        raise CheckError(f"{pickle_path}: no such file (not a Zephyr build directory?)") from e
    except ModuleNotFoundError as e:
        raise CheckError(f"{pickle_path}: needs Zephyr's python-devicetree (set ZEPHYR_BASE): {e}") from e
    board = cache.get("BOARD")
    return edt, canonical_board(board) if board else None


def canonical_board(board: str) -> str:
    """The rig's name of a board target: sysbuild caches the full target (nucleo_f767zi/stm32f767xx)."""
    known = set(PROFILES.values()) | set(CATALOGS)
    parts = board.split("/")
    for n in range(len(parts), 0, -1):
        if "/".join(parts[:n]) in known:
            return "/".join(parts[:n])
    return board


def _gpio_pin(entry: Any) -> str:
    """'PC13' for a gpios entry on an STM32 gpioc controller (label gpio<port>)."""
    for label in entry.controller.labels:
        m = re.fullmatch(r"gpio([a-k])", label)
        if m:
            return f"P{m.group(1).upper()}{entry.data['pin']}"
    return f"{entry.controller.path}:{entry.data['pin']}"


def _pinctrl_pin(controller: Any, prefix: str) -> str | None:
    """Pin of the controller's default pinctrl node named '<prefix>_p<port><n>' (hal_stm32)."""
    for pinctrl in controller.pinctrls:
        for conf in pinctrl.conf_nodes:
            m = re.fullmatch(rf"{prefix}_p([a-k])(\d+)", conf.name)
            if m:
                return f"P{m.group(1).upper()}{m.group(2)}"
    return None


def _label(node: Any, pattern: str) -> str | None:
    for label in node.labels:
        m = re.fullmatch(pattern, label)
        if m:
            return m.group(1)
    return None


def _channel(i: int, name: str, node: Any) -> Channel:
    kind = node.props["kind"].val
    if "gpios" in node.props:
        entry = node.props["gpios"].val[0]
        flags = entry.data.get("flags", 0)
        pull = "up" if flags & GPIO_PULL_UP else "down" if flags & GPIO_PULL_DOWN else None
        return Channel(i, name, kind, _gpio_pin(entry), bool(flags & GPIO_ACTIVE_LOW), pull)
    if "io-channels" in node.props:
        entry = node.props["io-channels"].val[0]
        adc, ch = _label(entry.controller, r"adc(\d)"), entry.data["input"]
        pin = _pinctrl_pin(entry.controller, f"adc{adc}_in{ch}") if adc else None
        return Channel(i, name, kind, pin, hw=f"ADC{adc}_IN{ch}" if adc else None)
    if "pwms" in node.props:
        entry = node.props["pwms"].val[0]
        tim, ch = _label(entry.controller.parent, r"timers(\d+)"), entry.data["channel"]
        pin = _pinctrl_pin(entry.controller, f"tim{tim}_ch{ch}") if tim else None
        return Channel(i, name, kind, pin, hw=f"TIM{tim}_CH{ch}" if tim else None)
    return Channel(i, name, kind)  # simulated channel


def dut_catalog(edt: Any) -> list[Channel] | None:
    """The uc,io-channels catalog in devicetree (= id) order; None without a catalog (MQTT)."""
    nodes = edt.compat2nodes.get("uc,io-channels", [])
    if not nodes:
        return None
    if len(nodes) > 1:
        raise CheckError(f"{len(nodes)} uc,io-channels nodes; the firmware expects one")
    return [_channel(i, name, node) for i, (name, node) in enumerate(nodes[0].children.items())]


def dut_markers(edt: Any) -> list[Channel]:
    """hil-marker-gpios of /zephyr,user (snippet hil); empty in release images."""
    user = next((n for n in edt.nodes if n.path == "/zephyr,user"), None)
    if user is None or "hil-marker-gpios" not in user.props:
        return []
    out = []
    for i, entry in enumerate(user.props["hil-marker-gpios"].val):
        flags = entry.data.get("flags", 0)
        out.append(Channel(i, f"m{i}", "marker", _gpio_pin(entry), bool(flags & GPIO_ACTIVE_LOW)))
    return out


# ------------------------------------------------------------------------------ checks


def compare(what: str, expected: Sequence[Channel], actual: Sequence[Channel]) -> list[str]:
    """Field-by-field differences, one line per channel and field."""
    problems = []
    if len(expected) != len(actual):
        problems.append(f"{what}: {len(expected)} channels expected, the build has {len(actual)}")
    for e, a in zip(expected, actual, strict=False):
        for key in ("id", "name", "kind", "pin", "active_low", "pull", "hw"):
            want, got = getattr(e, key), getattr(a, key)
            if want != got and not (key == "hw" and want is None):
                problems.append(f"{what}: {e.name} (id {e.id}): {key} {want!r} expected, build has {got!r}")
    return problems


def check(edt: Any, board: str | None, table: PinTable | None, bench: Bench | None) -> Report:
    """All checks of one DUT build."""
    report = Report()
    catalog = dut_catalog(edt)
    markers = dut_markers(edt)
    names = [c.name for c in catalog or []]
    if catalog is None:
        report.notes.append("no uc,io-channels catalog (apps/mqtt_tls): catalog checks skipped")
    elif board is not None and board in CATALOGS:
        base = list(CATALOGS[board])
        if names[: len(base)] != base:
            report.problems.append(f"catalog order {names} differs from hilrig.bench.CATALOGS {base}")
    else:
        report.notes.append(f"board {board!r} has no catalog in hilrig.bench.CATALOGS")

    if table is not None and board == P1_BOARD:
        if catalog is not None:
            n = len(table.catalog)
            report.problems += compare("pin tables 1.1", table.catalog, catalog[:n])
            if len(catalog) == n:
                report.notes.append("no hil-io extras (release catalog)")
            else:
                report.problems += compare("pin tables 1.2 (hil-io)", table.extras, catalog[n:])
        if markers:
            report.problems += compare("pin tables 1.2 (markers)", table.markers, markers)
        else:
            report.notes.append("no hil-marker-gpios (no hil snippet): marker check skipped")
    elif table is not None:
        report.notes.append(f"pin tables list P1 ({P1_BOARD}) only: pin check skipped for {board!r}")

    if bench is not None:
        if board is not None and bench.dut.board != board:
            report.problems.append(f"bench {bench.name}: dut.board {bench.dut.board!r}, build is {board!r}")
        wired = [c for c in (bench.stim.chans if bench.stim else ()) if c not in STIM_ONLY]
        missing = [c for c in wired if c not in names]
        if catalog is not None and missing:
            report.problems.append(f"bench {bench.name}: stim.chans {missing} are not in the build's catalog")
        wired_markers = [c for c in (bench.stim.chans if bench.stim else ()) if re.fullmatch(r"m\d", c)]
        if markers and len(markers) < len(wired_markers):
            report.problems.append(f"bench {bench.name}: {wired_markers} wired, the build has {len(markers)}")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("build", type=Path, help="DUT build directory or its zephyr/edt.pickle")
    parser.add_argument("--bench", type=Path, help="bench.yml to check against the build")
    parser.add_argument("--pin-tables", type=Path, default=PIN_TABLES, help="docs/hil/pin-tables.md")
    args = parser.parse_args(argv)
    try:
        table = parse_pin_tables(args.pin_tables.read_text())
        bench = Bench.load(args.bench) if args.bench else None
        edt, board = load_edt(args.build)
        report = check(edt, board, table, bench)
    except (CheckError, BenchError, OSError) as e:
        print(f"check_catalog: {e}", file=sys.stderr)
        return 2
    for note in report.notes:
        print(f"note: {note}")
    for problem in report.problems:
        print(f"FAIL: {problem}")
    catalog = dut_catalog(edt)
    print(
        f"check_catalog: {args.build} (board {board}): {len(catalog or [])} catalog channels, "
        f"{len(dut_markers(edt))} markers, {len(report.problems)} problems"
    )
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
