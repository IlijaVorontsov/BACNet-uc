"""R-01 stimulus link self-test: protocol v0 on every reply, round-trip time measured.

Runs against the real stimulus board when the bench has one; otherwise against the pty
fake (fake_stim.py), which checks the host side of the link only. The p99 < 20 ms criterion
(test catalogue R-01, 115200 baud) applies to hardware only.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from fake_stim import FakeStim
from hilrig.bench import Bench
from hilrig.stim import PROTO, Stim

P99_LIMIT_S = 0.020


@pytest.fixture
def stim_link(
    request: pytest.FixtureRequest, selected_bench: Bench | None, artifacts: Path
) -> Iterator[tuple[Stim, str]]:
    """(driver, "hardware" | "fake")."""
    if selected_bench is not None and selected_bench.stim is not None:
        yield request.getfixturevalue("stim"), "hardware"
        return
    with FakeStim() as fake, Stim(fake.port, log=artifacts / "rig" / "R-01-fake-stim.log") as driver:
        yield driver, "fake"


@pytest.fixture(autouse=True)
def rig_dir(artifacts: Path) -> Path:
    path = artifacts / "rig"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_r01_stimulus_link(
    stim_link: tuple[Stim, str], record_property: Callable[[str, object], None], rig_dir: Path
) -> None:
    stim, kind = stim_link
    count = 1000 if kind == "hardware" else 200
    rtts = []
    for _ in range(count):
        start = time.perf_counter()
        info = stim.info()  # raises on any ERR or link error
        rtts.append(time.perf_counter() - start)
        assert info.proto == PROTO
    rtts.sort()
    p99 = rtts[int(0.99 * (count - 1))]
    rtt_ms = {
        "min": rtts[0] * 1e3,
        "p50": statistics.median(rtts) * 1e3,
        "p99": p99 * 1e3,
        "max": rtts[-1] * 1e3,
    }
    report = {
        "link": kind,
        "commands": count,
        "proto": PROTO,
        "fw": info.fw,
        "board": info.board,
        "rtt_ms": rtt_ms,
    }
    (rig_dir / "R-01.json").write_text(json.dumps(report, indent=2) + "\n")
    for key, value in rtt_ms.items():
        record_property(f"stim_rtt_{key}_ms", round(value, 3))
    print(
        f"R-01 ({kind}): {count} x stim info, rtt p50 {rtt_ms['p50']:.2f} ms, "
        f"p99 {rtt_ms['p99']:.2f} ms, max {rtt_ms['max']:.2f} ms"
    )
    if kind == "hardware":
        assert p99 < P99_LIMIT_S, f"stim info round trip p99 {p99 * 1e3:.2f} ms"
