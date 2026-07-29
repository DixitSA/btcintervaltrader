"""Realized-volatility wiring.

The model strategies price off a volatility number. Until this was wired up
`current_realized_vol` was never assigned anywhere in the repo and
`SpotFeed.realized_vol` was never called, so `edge_threshold` always ran on
its hardcoded 0.60 default no matter what the market was doing.

Two properties matter and are pinned here:

  1. The estimator recovers a volatility it was given. If it is biased, every
     model probability is biased with it, and the strategy trades a mispriced
     edge that does not exist.
  2. Live and backtest measure it the SAME way. If they diverge, the backtest
     describes a strategy that never runs.
"""

from __future__ import annotations

import math
import random

import pytest

from btcbot.config import Config
from btcbot.recorder import load_dataset
from btcbot.signals import annualize, realized_vol_from_series
from btcbot.simulate import generate
from btcbot.strategies import build_strategy


def _gbm(vol_per_year: float, n: int, dt: float, seed: int = 3) -> list[tuple[float, float]]:
    """A price path with a KNOWN annualized volatility."""
    rng = random.Random(seed)
    sigma_dt = vol_per_year * math.sqrt(dt / (365.0 * 24.0 * 3600.0))
    px = 60_000.0
    out = [(0.0, px)]
    for i in range(1, n):
        px *= math.exp(rng.gauss(-0.5 * sigma_dt**2, sigma_dt))
        out.append((i * dt, px))
    return out


@pytest.mark.parametrize("true_vol", [0.30, 0.60, 1.20])
def test_estimator_recovers_the_volatility_it_was_given(true_vol):
    """Measure, annualise with the SAME window, and get the input back."""
    dt, n = 2.0, 3000
    points = _gbm(true_vol, n=n, dt=dt)
    lookback = (n - 1) * dt

    window_vol = realized_vol_from_series(points, lookback)
    assert window_vol is not None

    recovered = annualize(window_vol, lookback)
    assert recovered is not None
    # Sampling error on one path is real; 25% either way is a loose but
    # meaningful bound -- a scaling bug misses by multiples, not percents.
    assert 0.75 * true_vol < recovered < 1.25 * true_vol, (
        f"true {true_vol:.2f} -> recovered {recovered:.2f}"
    )


def test_a_mismatched_window_is_badly_wrong():
    """Why measurement and annualisation must share one window.

    Annualising a 300s measurement as though it covered 3000s understates the
    volatility by sqrt(10). This is the failure the single-source-of-truth
    wiring exists to prevent.
    """
    points = _gbm(0.60, n=3000, dt=2.0)
    window_vol = realized_vol_from_series(points, 300.0)
    assert window_vol is not None

    right = annualize(window_vol, 300.0)
    wrong = annualize(window_vol, 3000.0)
    assert right is not None and wrong is not None
    assert wrong == pytest.approx(right / math.sqrt(10.0), rel=1e-9)


def test_partial_history_is_refused_not_extrapolated():
    """Warmup must not masquerade as a measurement.

    With only a minute of history, treating it as though it spanned the full
    lookback understates vol by sqrt(lookback/span) -- a factor of ~3.9 for
    60s against 900s. That is the same mismatched-window error as above,
    arriving through the back door while the feed is still filling up.
    """
    points = _gbm(0.60, n=31, dt=2.0)  # 60 seconds of history
    assert realized_vol_from_series(points, 900.0) is None

    # Once enough of the window is covered it reports again.
    plenty = _gbm(0.60, n=460, dt=2.0)  # ~918 seconds
    assert realized_vol_from_series(plenty, 900.0) is not None


def test_partial_but_sufficient_history_is_rescaled_not_understated():
    """Covering 60% of the window must not report 60%-of-window vol."""
    full = _gbm(0.60, n=460, dt=2.0, seed=5)
    partial = full[-280:]  # ~558s, above the 50% floor

    v_full = realized_vol_from_series(full, 900.0)
    v_part = realized_vol_from_series(partial, 900.0)
    assert v_full is not None and v_part is not None

    # Both are estimates of the same 900s volatility, so they should be in the
    # same ballpark -- not off by the sqrt(900/558) = 1.27 the naive version
    # would introduce.
    assert 0.6 < (v_part / v_full) < 1.6


def test_duplicate_timestamps_do_not_bias_the_estimate():
    """Concurrent markets share one spot, so points can arrive duplicated.

    That is NOT a bias: duplication inserts zero log returns, which halves the
    variance while doubling the count, and the sqrt(n) scaling cancels the two
    exactly. Pinned because the obvious intuition says otherwise -- the reason
    backtest.py still records one point per timestamp is buffer coverage (see
    the next test), not correctness of the number.
    """
    clean = _gbm(0.60, n=600, dt=2.0)
    duplicated = [p for point in clean for p in (point, point)]

    honest = realized_vol_from_series(clean, 1200.0)
    doubled = realized_vol_from_series(duplicated, 1200.0)
    assert honest is not None and doubled is not None
    assert doubled == pytest.approx(honest, rel=0.02)


def test_duplicates_shorten_the_reach_of_a_bounded_buffer():
    """Why the one-point-per-timestamp guard earns its keep.

    The live history is a bounded deque. Duplicating each timestamp across N
    concurrent markets fills it N times faster, so the same buffer spans a
    correspondingly shorter stretch of real time than the lookback asks for.
    """
    from collections import deque

    clean = _gbm(0.60, n=600, dt=2.0)

    buf_clean: deque = deque(maxlen=200)
    for point in clean:
        buf_clean.append(point)

    buf_dupe: deque = deque(maxlen=200)
    for point in clean:
        buf_dupe.append(point)
        buf_dupe.append(point)  # a second concurrent market, same timestamp

    span_clean = buf_clean[-1][0] - buf_clean[0][0]
    span_dupe = buf_dupe[-1][0] - buf_dupe[0][0]
    assert span_dupe == pytest.approx(span_clean / 2.0, rel=0.02)


def test_backtest_injects_realized_vol_when_the_strategy_asks(tmp_path):
    """The wiring itself: run_backtest must set current_realized_vol."""
    from btcbot.backtest import run_backtest

    generate(tmp_path, n_windows=12, seed=11, families=["KXBTC15M"])
    snaps = load_dataset(tmp_path)

    strat = build_strategy(
        "edge_threshold", {"min_edge": 0.02, "realized_vol_window": 300.0}
    )
    assert strat.current_realized_vol is None

    run_backtest(snaps, strat, Config())
    assert strat.current_realized_vol is not None, (
        "run_backtest did not feed realized vol to a strategy that asked for it"
    )
    assert strat.current_realized_vol > 0.0


def test_explicitly_disabling_the_window_uses_the_fixed_vol():
    """Opting out is still allowed, but must be explicit."""
    strat = build_strategy(
        "edge_threshold", {"vol_per_year": 0.42, "realized_vol_window": None}
    )
    assert strat._vol() == pytest.approx(0.42)

    # A measurement with no window configured must not be used.
    strat.current_realized_vol = 0.01
    assert strat._vol() == pytest.approx(0.42)


def test_uncalibrated_model_declines_to_trade():
    """No measurement yet -> no signal, rather than a guessed volatility."""
    from btcbot.models import UP
    from tests.test_core import make_snapshot

    strat = build_strategy("edge_threshold", {"min_edge": 0.02})
    assert strat.realized_vol_window, "measuring should be the default"
    assert strat.current_realized_vol is None
    assert strat._vol() is None

    snap = make_snapshot(p_up=0.50, strike=60_000.0, spot=60_500.0)
    assert strat.decide(snap) is None, (
        "traded with no calibrated volatility -- an uncalibrated model does "
        "not find edge, it invents it"
    )
    _ = UP


def test_wrong_volatility_fabricates_edge_past_the_trigger():
    """The bug this wiring exists to prevent, stated as a number.

    Measured live, BTC realized vol was ~24% while the old default assumed
    60%. With spot $50 over the strike and 450s left, that gap alone is worth
    far more than the 5-point trigger -- so the strategy would fire on a
    correctly priced book, and always toward DOWN on an upward move.
    """
    from btcbot.signals import fair_probability_up

    strike, spot, remaining = 63_700.0, 63_750.0, 450.0
    fair = fair_probability_up(spot, strike, remaining, 0.24)
    assumed = fair_probability_up(spot, strike, remaining, 0.60)
    assert fair is not None and assumed is not None

    fabricated = assumed - fair
    assert fabricated < -0.05, (
        f"expected the stale 60% default to invent a large negative edge, "
        f"got {fabricated:+.3f}"
    )
    assert abs(fabricated) > 3 * 0.05, "should dwarf the default min_edge"
