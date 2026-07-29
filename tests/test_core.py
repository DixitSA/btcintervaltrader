from __future__ import annotations

import math

import pytest

from btcbot.config import Config
from btcbot.markets import parse_market, parse_strike
from btcbot.models import DOWN, UP, Book, Level, Market, Position, Snapshot
from btcbot.risk import RiskManager, kelly_fraction
from btcbot.signals import fair_probability_up, market_implied_up
from btcbot.strategies import build_strategy


def make_book(bid: float, ask: float, size: float = 500.0) -> Book:
    return Book(
        bids=[Level(bid, size), Level(bid - 0.01, size)],
        asks=[Level(ask, size), Level(ask + 0.01, size)],
    )


def make_snapshot(p_up: float = 0.5, volume: float = 0.0, spread: float = 0.02, **kw) -> Snapshot:
    market = Market(
        condition_id="0x1",
        slug="btc-updown-15m-test",
        question="Bitcoin Up or Down?",
        up_token_id="up",
        down_token_id="down",
        start_ts=1000.0,
        end_ts=1900.0,
        volume=volume,
        strike=kw.pop("strike", 100_000.0),
    )
    return Snapshot(
        ts=kw.pop("ts", 1300.0),
        market=market,
        up_book=make_book(p_up - spread / 2, p_up + spread / 2, kw.pop("size", 500.0)),
        down_book=make_book((1 - p_up) - spread / 2, (1 - p_up) + spread / 2),
        spot=kw.pop("spot", 100_000.0),
        window_volume=volume,
    )


# -- books -----------------------------------------------------------


def test_sweep_cost_walks_levels():
    book = Book(asks=[Level(0.50, 100), Level(0.52, 100)])
    assert book.sweep_cost(100) == pytest.approx(0.50)
    assert book.sweep_cost(200) == pytest.approx(0.51)


def test_sweep_cost_returns_none_when_book_too_thin():
    book = Book(asks=[Level(0.50, 10)])
    assert book.sweep_cost(100) is None


def test_book_mid_and_spread():
    book = make_book(0.49, 0.51)
    assert book.mid == pytest.approx(0.50)
    assert book.spread == pytest.approx(0.02)


# -- model -----------------------------------------------------------


def test_fair_probability_is_half_at_the_money():
    p = fair_probability_up(100_000, 100_000, 900, 0.60)
    assert p == pytest.approx(0.5, abs=0.01)


def test_fair_probability_rises_when_spot_above_strike():
    below = fair_probability_up(99_500, 100_000, 900, 0.60)
    above = fair_probability_up(100_500, 100_000, 900, 0.60)
    assert below < 0.5 < above


def test_fair_probability_collapses_at_expiry():
    assert fair_probability_up(100_500, 100_000, 0, 0.60) == 1.0
    assert fair_probability_up(99_500, 100_000, 0, 0.60) == 0.0


def test_market_implied_up_averages_both_books():
    snap = make_snapshot(p_up=0.60)
    assert market_implied_up(snap) == pytest.approx(0.60, abs=0.01)


# -- sizing ----------------------------------------------------------


def test_kelly_is_zero_at_fair_price():
    assert kelly_fraction(0.60, 0.60) == pytest.approx(0.0)


def test_kelly_positive_only_with_edge():
    assert kelly_fraction(0.65, 0.60) > 0
    assert kelly_fraction(0.55, 0.60) < 0


def test_position_payout_applies_fee_to_profit_only():
    pos = Position("m", UP, shares=100, avg_price=0.50)
    # Wins: $100 gross, $50 profit, 2% of profit = $1 fee.
    assert pos.payout(UP, winnings_fee_bps=200) == pytest.approx(99.0)
    assert pos.payout(DOWN) == 0.0


# -- the volume rule -------------------------------------------------


def test_volume_strategy_silent_below_threshold():
    strat = build_strategy("volume_threshold", {"min_volume_usd": 500_000})
    assert strat.decide(make_snapshot(volume=100_000)) is None


def test_volume_strategy_fires_above_threshold():
    strat = build_strategy("volume_threshold", {"min_volume_usd": 500_000})
    sig = strat.decide(make_snapshot(p_up=0.60, volume=600_000))
    assert sig is not None
    assert sig.side == UP  # "follow" buys the favoured side


def test_volume_strategy_fade_inverts():
    strat = build_strategy(
        "volume_threshold", {"min_volume_usd": 500_000, "direction": "fade"}
    )
    sig = strat.decide(make_snapshot(p_up=0.60, volume=600_000))
    assert sig is not None and sig.side == DOWN


def test_volume_strategy_default_claims_no_edge_over_market():
    """The default must not invent an edge: prob should equal the market price."""
    strat = build_strategy("volume_threshold", {"min_volume_usd": 500_000})
    snap = make_snapshot(p_up=0.60, volume=600_000)
    sig = strat.decide(snap)
    assert sig is not None
    assert sig.prob == pytest.approx(market_implied_up(snap), abs=1e-6)


def test_volume_strategy_rejects_bad_direction():
    with pytest.raises(ValueError):
        build_strategy("volume_threshold", {"direction": "sideways"})


# -- risk gates ------------------------------------------------------


def make_risk(**overrides) -> RiskManager:
    cfg = Config()
    for key, value in overrides.items():
        for target in (cfg.risk, cfg.fees, cfg.markets):
            if hasattr(target, key):
                setattr(target, key, value)
    return RiskManager(cfg.risk, cfg.fees, cfg.markets)


def test_risk_declines_signal_with_no_edge():
    """A signal that merely echoes the market price must never be sized."""
    risk = make_risk()
    strat = build_strategy("volume_threshold", {"min_volume_usd": 500_000})
    snap = make_snapshot(p_up=0.60, volume=600_000)
    sig = strat.decide(snap)
    order, reason = risk.evaluate(snap, sig)
    assert order is None
    assert "edge" in reason


def test_risk_sizes_a_genuine_edge():
    risk = make_risk()
    snap = make_snapshot(p_up=0.50, volume=600_000)
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(snap, Signal(side=UP, prob=0.70))
    assert order is not None, reason
    assert order.shares > 0
    assert order.limit_price <= risk.cfg.max_entry_price


def test_risk_blocks_expensive_entries():
    risk = make_risk()
    snap = make_snapshot(p_up=0.95, volume=600_000)
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(snap, Signal(side=UP, prob=0.99))
    assert order is None
    assert "max_entry_price" in reason


def test_risk_blocks_thin_books():
    risk = make_risk()
    snap = make_snapshot(p_up=0.50, volume=600_000, size=1.0)
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(snap, Signal(side=UP, prob=0.70))
    assert order is None
    assert "thin" in reason


def test_risk_halts_after_daily_loss_limit():
    risk = make_risk(daily_loss_limit_usd=10.0)
    risk.on_trade(1300.0)
    risk.on_settlement(-11.0)
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(make_snapshot(volume=600_000), Signal(side=UP, prob=0.70))
    assert order is None
    assert "halted" in reason


def test_risk_enforces_concurrent_position_cap():
    risk = make_risk(max_concurrent_positions=1)
    risk.on_trade(1300.0)
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(make_snapshot(volume=600_000), Signal(side=UP, prob=0.90))
    assert order is None
    assert "concurrent" in reason


def test_risk_refuses_stale_window():
    risk = make_risk(min_seconds_remaining=30)
    snap = make_snapshot(p_up=0.50, volume=600_000, ts=1890.0)  # 10s left
    from btcbot.strategies.base import Signal

    order, reason = risk.evaluate(snap, Signal(side=UP, prob=0.70))
    assert order is None
    assert "left" in reason


# -- market parsing --------------------------------------------------


# -- always_trade strategy -------------------------------------------

def test_always_trade_fires_on_every_window():
    from btcbot.strategies.always_trade import AlwaysTradeStrategy

    strat = AlwaysTradeStrategy(direction="up", assumed_edge=0.06)
    snap = make_snapshot(p_up=0.55, volume=0)
    sig = strat.decide(snap)
    assert sig is not None
    assert sig.side == UP
    assert sig.prob == pytest.approx(min(0.99, 0.55 + 0.06))


def test_always_trade_follow_favoured():
    from btcbot.strategies.always_trade import AlwaysTradeStrategy

    strat = AlwaysTradeStrategy(direction="follow", assumed_edge=0.06)
    snap = make_snapshot(p_up=0.55, volume=0)  # market favours Up
    sig = strat.decide(snap)
    assert sig is not None
    assert sig.side == UP

    snap2 = make_snapshot(p_up=0.45, volume=0)  # market favours Down
    sig2 = strat.decide(snap2)
    assert sig2 is not None
    assert sig2.side == DOWN


def test_always_trade_fade():
    from btcbot.strategies.always_trade import AlwaysTradeStrategy

    strat = AlwaysTradeStrategy(direction="fade", assumed_edge=0.06)
    snap = make_snapshot(p_up=0.55, volume=0)
    sig = strat.decide(snap)
    assert sig is not None
    assert sig.side == DOWN  # fades the favourite


def test_always_trade_registered():
    from btcbot.strategies import REGISTRY, build_strategy

    assert "always_trade" in REGISTRY
    s = build_strategy("always_trade", {"direction": "up", "assumed_edge": 0.06})
    assert s is not None
    assert s.decide(make_snapshot(volume=0)) is not None


def test_always_trade_no_market_price_returns_none():
    from btcbot.strategies.always_trade import AlwaysTradeStrategy

    strat = AlwaysTradeStrategy()
    # No bids or asks -> market_implied_up returns None
    empty_book = Book(bids=[], asks=[])
    snap = Snapshot(
        ts=1000.0,
        market=Market(
            condition_id="x", slug="t", question="?", up_token_id="u",
            down_token_id="d", start_ts=0, end_ts=2000, strike=100_000,
        ),
        up_book=empty_book,
        down_book=empty_book,
        spot=100_000.0,
    )
    assert strat.decide(snap) is None


# -- market parsing --------------------------------------------------


def test_parse_strike_reads_price_to_beat():
    assert parse_strike("Will BTC be above $109,412.55 at 3pm?") == pytest.approx(109_412.55)


def test_parse_strike_ignores_payout_language():
    assert parse_strike("Each share pays $1. Price to beat $95,000.00") == pytest.approx(95_000.0)


def test_parse_strike_returns_none_when_ambiguous():
    assert parse_strike("above $95,000 or below $97,000") is None
    assert parse_strike("no numbers here") is None


def test_parse_market_maps_up_token_by_outcome_order():
    raw = {
        "conditionId": "0xabc",
        "slug": "btc-updown-15m-123",
        "question": "Bitcoin Up or Down?",
        "clobTokenIds": '["tokenDOWN","tokenUP"]',
        "outcomes": '["Down","Up"]',
        "endDate": "2026-07-28T12:15:00Z",
        "startDate": "2026-07-28T12:00:00Z",
        "volumeNum": 1234.5,
        "description": "Price to beat $100,000.00",
    }
    m = parse_market(raw)
    assert m is not None
    assert m.up_token_id == "tokenUP"
    assert m.down_token_id == "tokenDOWN"
    assert m.strike == pytest.approx(100_000.0)
    assert m.end_ts - m.start_ts == pytest.approx(900.0)


def test_parse_market_rejects_missing_tokens():
    assert parse_market({"slug": "x", "endDate": "2026-07-28T12:15:00Z"}) is None
