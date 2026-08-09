"""Hurst exponent by rescaled-range (R/S) analysis.

This measures whether the BTC path INSIDE a 15-minute window trends, mean
reverts, or is a random walk:

    H ~ 0.5   random walk -- past moves say nothing about future moves
    H < 0.5   mean reverting -- moves tend to be given back
    H > 0.5   trending -- moves tend to continue

It matters here because it tests the claim the whole repo rests on. If the path
is a random walk, no rule that reads only the price history can produce a
directional edge, no matter how the backtest looks; whatever the sweep finds is
selection. If H is genuinely away from 0.5 there is at least something to
explain.

**Read `null_hurst` before reading any number this produces.** R/S is badly
biased upward on short samples: fed a few hundred points of pure random walk it
routinely returns 0.55-0.60, which is exactly the "mild trending" reading a
hopeful person wants to see. The measured H is only interesting relative to what
the estimator returns on a random walk of the SAME shape -- same window count,
same length, same lags. That control is the point of this module, in the same
way `simulate.py` is the point of the backtester.

Reference: the hurst-calculator / TimeSeries Analysis entries in
awesome-systematic-trading -- see docs/systematic-trading.md.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

DEFAULT_LAGS = (8, 16, 32, 64)


@dataclass(frozen=True)
class HurstResult:
    exponent: float
    lags: tuple[int, ...]
    # Mean log2(R/S) actually observed at each lag, in the same order.
    log_rs: tuple[float, ...]
    # How many independent chunks contributed at each lag.
    chunks: tuple[int, ...]

    @property
    def n_chunks(self) -> int:
        return sum(self.chunks)


def log_returns(prices: Sequence[float]) -> list[float]:
    """Log returns of a price series, skipping non-positive prices."""
    clean = [p for p in prices if p and p > 0]
    return [math.log(b / a) for a, b in zip(clean, clean[1:])]


def rescaled_range(series: Sequence[float]) -> Optional[float]:
    """R/S of one contiguous chunk of returns.

    R is the range of the cumulative deviation from the mean; S is the standard
    deviation. Their ratio is dimensionless, which is what lets values from
    chunks of different volatility be pooled.
    """
    n = len(series)
    if n < 2:
        return None
    mean = sum(series) / n
    devs = [v - mean for v in series]

    cum = 0.0
    lo = hi = 0.0
    for d in devs:
        cum += d
        lo = min(lo, cum)
        hi = max(hi, cum)
    rng = hi - lo

    sd = math.sqrt(sum(d * d for d in devs) / n)

    # A flat chunk carries no information about persistence, and dropping it is
    # correct. The tolerance has to be RELATIVE, though: on a genuinely constant
    # series the deviations come out as rounding dust rather than exact zeros,
    # and R and S are then both dust in the same proportion -- which divides out
    # to a perfectly respectable-looking R/S. A stuck spot feed would otherwise
    # feed pure floating-point noise into the fit as if it were data.
    tol = 1e-12 * max(max(abs(v) for v in series), 1.0)
    if sd <= tol or rng <= tol:
        return None
    return rng / sd


def hurst_exponent(
    segments: Iterable[Sequence[float]],
    lags: Sequence[int] = DEFAULT_LAGS,
) -> Optional[HurstResult]:
    """Fit H from pooled R/S across `segments` of returns.

    Each segment must be CONTIGUOUS in time -- pass one per recorded window
    rather than one concatenated series, or the joins between windows enter the
    statistic as if they were ordinary ticks.

    Segments are cut into non-overlapping chunks of each lag, R/S is averaged
    per lag, and H is the slope of log(R/S) against log(lag).
    """
    seg_list = [list(s) for s in segments]
    usable_lags: list[int] = []
    mean_log_rs: list[float] = []
    counts: list[int] = []

    for lag in sorted(set(int(x) for x in lags)):
        if lag < 2:
            continue
        values: list[float] = []
        for seg in seg_list:
            for start in range(0, len(seg) - lag + 1, lag):
                rs = rescaled_range(seg[start : start + lag])
                if rs is not None and rs > 0:
                    values.append(math.log(rs))
        if len(values) < 2:
            continue
        usable_lags.append(lag)
        mean_log_rs.append(sum(values) / len(values))
        counts.append(len(values))

    if len(usable_lags) < 2:
        return None

    xs = [math.log(l) for l in usable_lags]
    x_mean = sum(xs) / len(xs)
    y_mean = sum(mean_log_rs) / len(mean_log_rs)
    sxx = sum((x - x_mean) ** 2 for x in xs)
    if sxx <= 0:
        return None
    sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, mean_log_rs))

    return HurstResult(
        exponent=sxy / sxx,
        lags=tuple(usable_lags),
        log_rs=tuple(mean_log_rs),
        chunks=tuple(counts),
    )


def null_hurst(
    segment_lengths: Sequence[int],
    lags: Sequence[int] = DEFAULT_LAGS,
    trials: int = 200,
    seed: int = 42,
) -> Optional[tuple[float, float]]:
    """(mean, stdev) of H measured on random walks shaped like your data.

    THE control. Generates `trials` synthetic datasets with the same segment
    count and lengths as the real one, each a pure Gaussian random walk with no
    memory whatsoever, and measures H on every one.

    The mean it returns is the estimator's bias at this sample size -- typically
    well above 0.5 -- and the stdev is how far a real measurement has to sit
    from that mean before it means anything. Compare against these, never
    against 0.5.
    """
    lengths = [int(n) for n in segment_lengths if int(n) >= 2]
    if not lengths or trials < 2:
        return None

    rng = random.Random(seed)
    measured: list[float] = []
    for _ in range(trials):
        segments = [[rng.gauss(0.0, 1.0) for _ in range(n)] for n in lengths]
        res = hurst_exponent(segments, lags=lags)
        if res is not None:
            measured.append(res.exponent)

    if len(measured) < 2:
        return None
    mean = sum(measured) / len(measured)
    var = sum((m - mean) ** 2 for m in measured) / (len(measured) - 1)
    return mean, math.sqrt(var)


def spot_segments(windows: dict[str, list]) -> list[list[float]]:
    """Per-window spot return series from grouped snapshots.

    One segment per market window. Snapshots are deduplicated by timestamp
    first: overlapping markets are polled in the same tick and carry the SAME
    spot price, so leaving the duplicates in would inject a run of zero returns
    and drag H toward mean reversion.
    """
    segments: list[list[float]] = []
    for snaps in windows.values():
        seen: dict[float, float] = {}
        for snap in snaps:
            if snap.spot is not None and snap.spot > 0 and snap.ts not in seen:
                seen[snap.ts] = float(snap.spot)
        if len(seen) < 3:
            continue
        prices = [px for _, px in sorted(seen.items())]
        rets = log_returns(prices)
        if len(rets) >= 2:
            segments.append(rets)
    return segments
