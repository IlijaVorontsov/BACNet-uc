"""R-01 stimulus link (rig gate): one ``stim info``, 1000 x ``stim din do3``, line limits, reset.

Catalogue R-01: one stim info: proto=0, board=nucleo_f767zi, profile matches bench, chans
include every bench stim.chans entry. 1000 x 'stim din do3': 0 ERR, 0 protocol errors, round
trip p99 < 20 ms at 115200. A 511-char line is accepted and a longer one is refused by the
host. A forced stimulus reset raises StimRebooted.

Runs against the real stimulus board when the bench has one; otherwise against the pty fake
(fake_stim.py), which checks the host side of the link only: the p99 bound and the OpenOCD
reset apply to hardware.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from fake_stim import FakeStim
from hilrig import flash as flash_mod
from hilrig.bench import Bench
from hilrig.stim import DEFAULT_PROFILE, MAX_LINE, PROTO, Stim, StimRebooted

pytestmark = pytest.mark.rig(gate=True)
P99_LIMIT_S = 0.020
BOARD = "nucleo_f767zi"


@dataclass
class Link:
    stim: Stim
    kind: str  # "hardware" | "fake"
    profile: str
    chans: tuple[str, ...]
    fake: FakeStim | None = None
    probe: str | None = None


@pytest.fixture
def stim_link(
    request: pytest.FixtureRequest, selected_bench: Bench | None, artifacts: Path
) -> Iterator[Link]:
    if selected_bench is not None and selected_bench.stim is not None:
        s = selected_bench.stim
        yield Link(
            request.getfixturevalue("stim"), "hardware", selected_bench.dut.profile, s.chans, probe=s.probe
        )
        return
    with (
        FakeStim(board=BOARD, profile="P1") as fake,
        Stim(fake.port, log=artifacts / "rig" / "R-01-fake-stim.log") as driver,
    ):
        yield Link(driver, "fake", "P1", fake.chans, fake=fake)


@pytest.fixture(autouse=True)
def rig_dir(artifacts: Path) -> Path:
    path = artifacts / "rig"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_r01_stim_info_fields(stim_link: Link) -> None:
    info = stim_link.stim.info()
    assert info.proto == PROTO == 0
    assert info.board == BOARD
    assert (info.profile or DEFAULT_PROFILE) == stim_link.profile  # It1 firmware: no key = P1 map
    missing = sorted(set(stim_link.chans) - set(info.chans))
    assert not missing, f"stimulus lacks bench channels {missing}"


def test_r01_din_round_trip(stim_link: Link, rig_dir: Path) -> None:
    stim = stim_link.stim
    count = 1000 if stim_link.kind == "hardware" else 200
    rtts = []
    for _ in range(count):
        start = time.perf_counter()
        level = stim.din("do3")  # raises on any ERR (StimError) or protocol error (StimLinkError)
        rtts.append(time.perf_counter() - start)
        assert level in (0, 1)
    rtts.sort()
    p99 = rtts[int(0.99 * (count - 1))]
    rtt_ms = {"min": rtts[0], "p50": statistics.median(rtts), "p99": p99, "max": rtts[-1]}
    rtt_ms = {k: v * 1e3 for k, v in rtt_ms.items()}
    info = stim.info()
    report = {"link": stim_link.kind, "command": "stim din do3", "commands": count, "errors": 0}
    report |= {"proto": info.proto, "fw": info.fw, "board": info.board, "rtt_ms": rtt_ms}
    (rig_dir / "R-01.json").write_text(json.dumps(report, indent=2) + "\n")
    p50, p99_ms = rtt_ms["p50"], rtt_ms["p99"]
    print(f"R-01 ({stim_link.kind}): {count} x stim din do3, p50 {p50:.2f} ms, p99 {p99_ms:.2f} ms")
    if stim_link.kind == "hardware":
        assert p99 < P99_LIMIT_S, f"stim din round trip p99 {p99 * 1e3:.2f} ms (limit 20 ms)"


def test_r01_line_limits(stim_link: Link) -> None:
    line = "stim din do3"
    assert "v" in stim_link.stim.command(line + " " * (MAX_LINE - len(line)), read_only=True)
    with pytest.raises(ValueError, match="at most 511"):
        stim_link.stim.command(line + " " * (MAX_LINE + 1 - len(line)))
    if stim_link.fake is not None:
        assert not [c for c in stim_link.fake.commands if len(c) > MAX_LINE]  # never sent


def poll_until_rebooted(stim: Stim, limit_s: float = 10.0) -> None:
    """Keep reading until the board's reboot shows (StimRebooted) or ``limit_s`` passes."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        stim.din("do3")
        time.sleep(0.05)


def test_r01_forced_reset_raises_stim_rebooted(stim_link: Link) -> None:
    stim = stim_link.stim
    if stim_link.fake is not None:
        stim_link.fake.reboot()
    else:
        if not stim_link.probe:
            pytest.skip("bench.yml stim.probe is not set: the stimulus cannot be reset over SWD")
        if flash_mod.find_openocd() is None:
            pytest.skip(f"OpenOCD not found (set {flash_mod.OPENOCD_ENV} or ZEPHYR_SDK_INSTALL_DIR)")
        flash_mod.reset_target(stim_link.probe)
    with pytest.raises(StimRebooted):
        poll_until_rebooted(stim)
    assert stim.banner is not None and stim.banner.startswith(f"STIM READY proto={PROTO}")
    stim.safe()  # the link resynchronises after the reboot
