"""Outcome determination -- the ground truth every P&L number rests on.

Validated against Kalshi's own `result` field for 45 recorded windows in data/.
The rules here were wrong on 16.7% of them before these fixes, in two distinct
ways that both looked like ordinary noise:

  1. the window was never recorded to its close, so a mid-window snapshot got
     treated as terminal (wrong on 4 of 4 such windows);
  2. spot sat within one 60-second move of the strike, where an instantaneous
     Binance print cannot resolve what a 60-second CF Benchmarks average decided
     (right 31/31 beyond 0.10% of the strike, 1/3 within 0.02%).
"""

from __future__ import annotations

import pytest

from btcbot.config import load_config
from btcbot.models import DOWN, UP
from btcbot.signals import SETTLEMENT_NOISE_PCT, settlement_side


# -- the shared primitive --------------------------------------------


def test_clear_distance_from_strike_resolves():
    assert settlement_side(100_500.0, 100_000.0) == UP
    assert settlement_side(99_500.0, 100_000.0) == DOWN


def test_inside_the_noise_band_abstains():
    """0.04% of 100k is $40. Kalshi averages 60s; spot moves ~$39 in 60s."""
    assert settlement_side(100_010.0, 100_000.0) is None
    assert settlement_side(99_990.0, 100_000.0) is None
    assert settlement_side(100_000.0, 100_000.0) is None


def test_band_edges():
    band = SETTLEMENT_NOISE_PCT
    just_inside = 100_000.0 * (1 + band * 0.9)
    just_outside = 100_000.0 * (1 + band * 1.1)
    assert settlement_side(just_inside, 100_000.0) is None
    assert settlement_side(just_outside, 100_000.0) == UP


def test_band_scales_with_price_so_sol_is_not_held_to_btc_precision():
    """The band is relative, so it means the same thing at $72 and at $63,000."""
    # SOL: 0.04% of 72.85 is under 3 cents.
    assert settlement_side(72.90, 72.85) == UP
    assert settlement_side(72.851, 72.85) is None


def test_missing_or_nonsense_inputs_abstain():
    assert settlement_side(None, 100_000.0) is None
    assert settlement_side(100_000.0, None) is None
    assert settlement_side(None, None) is None
    assert settlement_side(100_000.0, 0.0) is None
    assert settlement_side(-1.0, 100_000.0) is None


def test_a_wider_band_can_be_requested():
    assert settlement_side(100_050.0, 100_000.0) == UP
    assert settlement_side(100_050.0, 100_000.0, noise_pct=0.01) is None


# -- the runner's close-adjacent spot --------------------------------


@pytest.fixture
def runner():
    from btcbot.runner import Runner

    r = Runner(load_config(), strategy=None, record=False, trade=False)
    yield r
    r.close()


def test_close_spot_uses_the_last_reading_taken_before_the_close(runner):
    end_ts = 2_000.0
    runner._last_spot["s"] = (end_ts - 2.0, 63_500.0)
    assert runner._close_spot("s", end_ts) == 63_500.0


def test_close_spot_refuses_a_reading_long_before_the_close(runner):
    """The bug this guards: settlement ran 60s AFTER the close and fetched spot
    then, reading a price the window never saw."""
    end_ts = 2_000.0
    runner._last_spot["s"] = (end_ts - 600.0, 63_500.0)
    assert runner._close_spot("s", end_ts) is None


def test_close_spot_is_none_when_the_window_was_never_seen(runner):
    assert runner._close_spot("never-recorded", 2_000.0) is None


def test_close_spot_honours_its_max_age(runner):
    end_ts = 2_000.0
    runner._last_spot["s"] = (end_ts - 45.0, 63_500.0)
    assert runner._close_spot("s", end_ts) is None
    assert runner._close_spot("s", end_ts, max_age=60.0) == 63_500.0
