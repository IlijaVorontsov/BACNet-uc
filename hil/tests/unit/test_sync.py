"""Unit tests of hilrig.sync: least-squares clock fit, residuals, verdict, SYNC collection."""

from __future__ import annotations

import random

import pytest

from hilrig import sync

EPOCH_NS = 1_790_000_000 * 10**9  # host CLOCK_REALTIME in late 2026


def stim_times(n: int = 20, period_ns: int = 50_000_000, start_ns: int = 7_000_000_123) -> list[int]:
    return [start_ns + i * period_ns for i in range(n)]


def test_fit_recovers_offset_and_skew_at_epoch_scale() -> None:
    x = stim_times()
    slope = 1 + 50e-6  # host clock 50 ppm fast relative to the stimulus crystal
    offset = EPOCH_NS + 3_000
    y = [offset + round(slope * xi) for xi in x]
    fit = sync.fit(x, y)
    assert fit.skew_ppm == pytest.approx(50.0, abs=1e-3)
    assert abs(fit.offset_ns - offset) <= 1
    assert all(abs(fit.map(xi) - yi) <= 1 for xi, yi in zip(x, y, strict=True))
    assert fit.max_residual_s < 1e-9 and fit.verdict is sync.Verdict.OK
    # an event 10 s after the last pulse is still placed to the ns
    assert abs(fit.map(x[-1] + 10**10) - (offset + round(slope * (x[-1] + 10**10)))) <= 2
    assert fit.map_s(x[0] / 1e9) == pytest.approx(y[0], abs=2)


def test_residuals_and_verdict_threshold() -> None:
    x = stim_times()
    rng = random.Random(5)
    jitter = [rng.uniform(-100_000, 100_000) for _ in x]  # +-100 us of host-side jitter
    fit = sync.fit(x, [EPOCH_NS + xi + round(j) for xi, j in zip(x, jitter, strict=True)])
    assert len(fit.residuals_ns) == len(x)
    assert abs(sum(fit.residuals_ns)) <= 0.5 * len(x)  # zero-sum up to the ns rounding of the offset
    assert fit.max_residual_s <= 200e-6 and fit.verdict is sync.Verdict.OK
    y = [EPOCH_NS + xi for xi in x]
    y[7] += 600_000  # one 600 us outlier (USB hiccup)
    bad = sync.fit(x, y)
    assert bad.max_residual_s > sync.INCONCLUSIVE_ABOVE_S and bad.verdict is sync.Verdict.INCONCLUSIVE


@pytest.mark.parametrize(
    ("x", "y", "message"),
    [([1, 2], [1, 2], "at least 3"), ([1, 2, 3], [1, 2], "reference"), ([5, 5, 5], [1, 2, 3], "equal")],
)
def test_fit_rejects_bad_input(x: list[int], y: list[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        sync.fit(x, y)


def test_collect_and_fit_host() -> None:
    """collect() brackets each pulse with the host clock; fit_host() uses the midpoints."""
    host = iter(range(EPOCH_NS, EPOCH_NS + 10**12, 1_000_000))  # each clock read advances 1 ms
    stim = iter(stim_times())
    samples = sync.collect(lambda: next(stim), 5, 0.0, clock=lambda: next(host))
    assert [s.host_after_ns - s.host_before_ns for s in samples] == [1_000_000] * 5
    assert samples[0].host_ns == EPOCH_NS + 500_000
    fit = sync.fit_host(samples)
    assert fit.map(samples[0].stim_ns) == pytest.approx(samples[0].host_ns, abs=1)
    assert fit.skew_ppm == pytest.approx((2_000_000 / 50_000_000 - 1) * 1e6)
