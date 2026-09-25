"""Clock correlation between a device timebase (stimulus or LA) and host CLOCK_REALTIME.

The stimulus board timestamps each SYNC pulse on its cycle counter, the logic analyzer sees the
same pulse on its SYNC channel, and the host brackets every ``stim pulse`` command with
``time.time_ns()``. A least-squares line (offset plus skew; crystals differ by tens of ppm, so
skew must be fitted) maps one timebase onto the other. The residuals measure how well the
mapping holds; above 250 us cross-domain timing results are inconclusive rather than failed
(v1 design section 6.3, test R-05).

Times are integer nanoseconds: epoch values (~1.8e18 ns) do not survive a float intact, so the
fit is done on offsets from the sample means.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

INCONCLUSIVE_ABOVE_S = 250e-6
MIN_POINTS = 3  # two points always fit exactly and give no residual


class Verdict(enum.StrEnum):
    OK = "ok"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class ClockFit:
    """reference = y0 + slope * (source - x0), fitted by least squares."""

    x0_ns: int  # mean source time
    y0_ns: int  # mean reference time
    slope: float
    residuals_ns: tuple[float, ...]  # reference minus fitted value, per input pair

    def map(self, source_ns: int) -> int:
        """Reference time (ns) of a source timestamp (ns)."""
        return self.y0_ns + round(self.slope * (source_ns - self.x0_ns))

    def map_s(self, source_s: float) -> int:
        """Reference time (ns) of a source time in seconds, e.g. an LA edge."""
        return self.map(round(source_s * 1e9))

    @property
    def offset_ns(self) -> int:
        """Reference time at source time zero."""
        return self.map(0)

    @property
    def skew_ppm(self) -> float:
        """How much faster the reference clock runs than the source clock."""
        return (self.slope - 1.0) * 1e6

    @property
    def max_residual_s(self) -> float:
        return max(abs(r) for r in self.residuals_ns) / 1e9

    @property
    def verdict(self) -> Verdict:
        return Verdict.OK if self.max_residual_s <= INCONCLUSIVE_ABOVE_S else Verdict.INCONCLUSIVE


def fit(source_ns: Sequence[int], reference_ns: Sequence[int]) -> ClockFit:
    """Least-squares fit of reference = offset + slope * source over paired timestamps."""
    n = len(source_ns)
    if n != len(reference_ns):
        raise ValueError(f"{n} source but {len(reference_ns)} reference timestamps")
    if n < MIN_POINTS:
        raise ValueError(f"need at least {MIN_POINTS} pairs, got {n}")
    x0, y0 = sum(source_ns) // n, sum(reference_ns) // n
    dx = [float(x - x0) for x in source_ns]
    dy = [float(y - y0) for y in reference_ns]
    mx, my = sum(dx) / n, sum(dy) / n
    sxx = sum((x - mx) ** 2 for x in dx)
    if sxx == 0:
        raise ValueError("all source timestamps are equal")
    slope = sum((x - mx) * (y - my) for x, y in zip(dx, dy, strict=True)) / sxx
    intercept = my - slope * mx  # at dx == 0, i.e. source x0
    y0_fit = y0 + round(intercept)
    residuals = tuple(y - (y0_fit - y0) - slope * x for x, y in zip(dx, dy, strict=True))
    return ClockFit(x0, y0_fit, slope, residuals)


@dataclass(frozen=True)
class SyncSample:
    """One SYNC pulse: its stimulus timestamp and the host clock just before and after."""

    stim_ns: int
    host_before_ns: int
    host_after_ns: int

    @property
    def host_ns(self) -> int:
        """Best host estimate of the pulse: the middle of the command round trip."""
        return (self.host_before_ns + self.host_after_ns) // 2


def collect(
    pulse: Callable[[], int], count: int, spacing_s: float, clock: Callable[[], int] = time.time_ns
) -> list[SyncSample]:
    """Fire ``count`` SYNC pulses ``spacing_s`` apart; ``pulse()`` returns the stimulus time (ns).

    For example ``collect(lambda: stim.pulse("sync", 100).t0_ns, 20, 0.05)``. The constant part
    of the command latency ends up in the fitted offset; its jitter ends up in the residuals.
    """
    samples = []
    for i in range(count):
        if i:
            time.sleep(spacing_s)
        before = clock()
        stim_ns = pulse()
        samples.append(SyncSample(stim_ns, before, clock()))
    return samples


def fit_host(samples: Sequence[SyncSample]) -> ClockFit:
    """Stimulus timebase -> host CLOCK_REALTIME from :func:`collect` samples."""
    return fit([s.stim_ns for s in samples], [s.host_ns for s in samples])
