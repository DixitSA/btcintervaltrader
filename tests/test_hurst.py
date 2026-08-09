"""Hurst by R/S, and the control that makes the number readable.

The central test here is `test_random_walk_null_is_not_one_half`: it pins the
estimator's small-sample bias, which is the whole reason `null_hurst` exists.
"""

import math
import random

import pytest

from btcbot.hurst import (
    DEFAULT_LAGS,
    hurst_exponent,
    log_returns,
    null_hurst,
    rescaled_range,
    spot_segments,
)
from btcbot.models import Book, Level, Market, Snapshot


def walk(n, rng, memory=0.0):
    """`memory` > 0 makes each step lean the way the last one went."""
    out = []
    prev = 0.0
    for _ in range(n):
        step = rng.gauss(0.0, 1.0) + memory * prev
        out.append(step)
        prev = step
    return out


def test_rescaled_range_is_scale_free():
    """R/S must not change when the series is multiplied by a constant."""
    rng = random.Random(3)
    series = walk(200, rng)
    base = rescaled_range(series)
    assert base == pytest.approx(rescaled_range([v * 1000.0 for v in series]))


def test_rescaled_range_refuses_degenerate_input():
    assert rescaled_range([]) is None
    assert rescaled_range([0.5]) is None
    assert rescaled_range([0.2, 0.2, 0.2]) is None  # flat: no information


def test_log_returns_skips_bad_prices():
    assert log_returns([100.0, 0.0, 110.0]) == pytest.approx(
        [math.log(110.0 / 100.0)]
    )
    assert log_returns([100.0]) == []


def test_trending_series_scores_above_a_random_walk():
    rng = random.Random(5)
    flat = hurst_exponent([walk(600, rng) for _ in range(20)])
    trend = hurst_exponent([walk(600, rng, memory=0.35) for _ in range(20)])
    assert flat is not None and trend is not None
    assert trend.exponent > flat.exponent


def test_mean_reverting_series_scores_below_a_random_walk():
    rng = random.Random(5)
    flat = hurst_exponent([walk(600, rng) for _ in range(20)])
    revert = hurst_exponent([walk(600, rng, memory=-0.45) for _ in range(20)])
    assert flat is not None and revert is not None
    assert revert.exponent < flat.exponent


def test_random_walk_null_is_not_one_half():
    """The bias this module exists to expose.

    R/S on short samples reads meaningfully above 0.5 on data with no memory at
    all. Comparing a measured exponent to 0.5 would call that a trend.
    """
    null = null_hurst([300] * 20, trials=60, seed=1)
    assert null is not None
    mean, sd = null
    assert sd > 0
    assert mean > 0.5
    # Wide bounds on purpose: this pins that the bias is real and sizeable,
    # not a specific value that a lag-set change would falsify.
    assert 0.5 < mean < 0.75


def test_a_random_walk_is_not_significant_against_its_own_null():
    """The control experiment: no memory in, no finding out."""
    rng = random.Random(17)
    lengths = [300] * 20
    segments = [walk(n, rng) for n in lengths]
    result = hurst_exponent(segments)
    null = null_hurst(lengths, trials=120, seed=99)
    assert result is not None and null is not None
    mean, sd = null
    z = (result.exponent - mean) / sd
    assert abs(z) < 3.0


def test_null_shrinks_toward_one_half_as_samples_grow():
    short = null_hurst([100] * 12, lags=(8, 16, 32), trials=40, seed=2)
    long = null_hurst([4000] * 12, lags=(8, 16, 32), trials=40, seed=2)
    assert short is not None and long is not None
    assert abs(long[0] - 0.5) < abs(short[0] - 0.5)


def test_result_reports_what_it_actually_used():
    rng = random.Random(4)
    res = hurst_exponent([walk(500, rng) for _ in range(6)], lags=(8, 16, 32))
    assert res is not None
    assert res.lags == (8, 16, 32)
    assert len(res.log_rs) == len(res.chunks) == 3
    # Bigger lags cut into fewer chunks.
    assert res.chunks[0] > res.chunks[-1]
    assert res.n_chunks == sum(res.chunks)


def test_too_little_data_returns_nothing_rather_than_a_number():
    assert hurst_exponent([[0.1, 0.2, 0.3]], lags=DEFAULT_LAGS) is None
    assert hurst_exponent([], lags=DEFAULT_LAGS) is None
    # A single usable lag cannot support a slope.
    rng = random.Random(6)
    assert hurst_exponent([walk(12, rng)], lags=(8, 64)) is None


def test_a_stuck_feed_produces_no_measurement():
    """A frozen price is not a random walk with H=0.5; it is no data at all."""
    assert hurst_exponent([[0.0] * 300 for _ in range(10)]) is None
    # And the same when the constant is not exactly representable, so the
    # deviations arrive as rounding dust rather than zeros.
    assert hurst_exponent([[0.2] * 300 for _ in range(10)]) is None


def test_null_refuses_impossible_requests():
    assert null_hurst([], trials=50) is None
    assert null_hurst([300], trials=1) is None


def _snap(ts, spot, slug):
    m = Market(
        condition_id=slug,
        slug=slug,
        question="?",
        up_token_id="u",
        down_token_id="d",
        start_ts=0.0,
        end_ts=900.0,
        strike=100_000.0,
    )
    b = Book(bids=[Level(0.5, 10)], asks=[Level(0.5, 10)])
    return Snapshot(ts=ts, market=m, up_book=b, down_book=b, spot=spot)


def test_spot_segments_deduplicates_repeated_timestamps():
    """Concurrent markets share a spot price; counting it twice fakes flat ticks."""
    windows = {
        "w1": [
            _snap(0.0, 100.0, "w1"),
            _snap(0.0, 100.0, "w1"),  # same tick, second market
            _snap(1.0, 101.0, "w1"),
            _snap(2.0, 102.0, "w1"),
        ]
    }
    segments = spot_segments(windows)
    assert len(segments) == 1
    assert len(segments[0]) == 2  # three prices -> two returns, not three


def test_spot_segments_orders_by_timestamp():
    windows = {
        "w1": [_snap(2.0, 102.0, "w1"), _snap(0.0, 100.0, "w1"), _snap(1.0, 101.0, "w1")]
    }
    segments = spot_segments(windows)
    assert segments[0] == pytest.approx(
        [math.log(101.0 / 100.0), math.log(102.0 / 101.0)]
    )


def test_spot_segments_keeps_windows_separate():
    """One segment per window: joins between windows are not real returns."""
    windows = {
        "w1": [_snap(float(i), 100.0 + i, "w1") for i in range(5)],
        "w2": [_snap(float(i), 500.0 + i, "w2") for i in range(5)],
    }
    assert len(spot_segments(windows)) == 2


def test_spot_segments_drops_windows_without_usable_spot():
    m = Market(
        condition_id="w",
        slug="w",
        question="?",
        up_token_id="u",
        down_token_id="d",
        start_ts=0.0,
        end_ts=900.0,
    )
    b = Book()
    windows = {
        "w1": [Snapshot(ts=float(i), market=m, up_book=b, down_book=b) for i in range(5)],
        "w2": [_snap(0.0, 100.0, "w2"), _snap(1.0, 101.0, "w2")],
    }
    assert spot_segments(windows) == []
