"""Portfolio accounting and exit policy tests."""

from __future__ import annotations

import pytest

from btcbot.config import ExitsConfig
from btcbot.exits import DrawdownGuard, ExitPolicy
from btcbot.models import DOWN, UP, Book, Level
from btcbot.portfolio import (
    EXIT_EXPIRY,
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TIME,
    EXIT_TRAILING,
    OpenPosition,
    Portfolio,
    mark_for,
)

from .test_core import make_snapshot


# -- cash accounting -------------------------------------------------


def test_open_deducts_cash_and_tracks_basis():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0, fee=0.5)
    assert p.cash == pytest.approx(100.0 - 30.5)
    assert p.positions["m"].cost_basis == pytest.approx(30.5)
    assert p.open_count == 1


def test_open_refuses_to_exceed_cash():
    p = Portfolio(10.0)
    with pytest.raises(ValueError, match="insufficient cash"):
        p.open("m", UP, shares=100, price=0.50, ts=0.0)


def test_open_refuses_duplicate_position():
    p = Portfolio(100.0)
    p.open("m", UP, shares=10, price=0.50, ts=0.0)
    with pytest.raises(ValueError, match="already holding"):
        p.open("m", UP, shares=10, price=0.50, ts=1.0)


def test_settle_win_pays_one_per_share_less_fee():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0)
    trade = p.settle("m", UP, ts=900.0, winnings_fee_bps=200)

    # $50 gross, $20 profit, 2% of profit = $0.40 fee.
    assert p.cash == pytest.approx(70.0 + 49.60)
    assert trade.pnl == pytest.approx(19.60)
    assert trade.exit_reason == EXIT_EXPIRY
    assert trade.won


def test_settle_loss_is_a_total_loss_of_stake():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0)
    trade = p.settle("m", DOWN, ts=900.0)

    assert p.cash == pytest.approx(70.0)
    assert trade.pnl == pytest.approx(-30.0)
    assert not trade.won


def test_settle_void_returns_the_stake():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0)
    trade = p.settle("m", None, ts=900.0)
    assert p.cash == pytest.approx(100.0)
    assert trade.pnl == pytest.approx(0.0)


def test_close_early_realises_partial_loss():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0)
    trade = p.close("m", exit_price=0.45, ts=300.0, reason=EXIT_STOP_LOSS)

    assert trade.pnl == pytest.approx(-7.50)
    assert p.cash == pytest.approx(70.0 + 22.5)
    assert p.open_count == 0
    # The stop capped the loss well short of the full $30 stake.
    assert trade.pnl > -30.0


# -- valuation -------------------------------------------------------


def test_equity_tracks_marks_not_entry():
    p = Portfolio(100.0)
    p.open("m", UP, shares=50, price=0.60, ts=0.0)
    assert p.equity == pytest.approx(100.0)

    p.update_mark("m", 0.40)
    assert p.position_value == pytest.approx(20.0)
    assert p.equity == pytest.approx(90.0)
    assert p.unrealized_pnl == pytest.approx(-10.0)
    assert p.realized_pnl == pytest.approx(0.0)


def test_realized_and_unrealized_are_separate():
    p = Portfolio(100.0)
    p.open("a", UP, shares=20, price=0.50, ts=0.0)
    p.settle("a", UP, ts=10.0)
    p.open("b", UP, shares=20, price=0.50, ts=20.0)
    p.update_mark("b", 0.30)

    assert p.realized_pnl == pytest.approx(10.0)
    assert p.unrealized_pnl == pytest.approx(-4.0)
    assert p.total_pnl == pytest.approx(6.0)


def test_drawdown_uses_the_equity_curve():
    p = Portfolio(100.0)
    p.record_equity(0.0)
    p.open("a", UP, shares=100, price=0.50, ts=1.0)
    p.update_mark("a", 0.80)
    p.record_equity(2.0)          # equity 130
    p.update_mark("a", 0.20)
    p.record_equity(3.0)          # equity 70

    assert p.peak_equity == pytest.approx(130.0)
    assert p.max_drawdown == pytest.approx(60.0)


def test_mark_uses_the_bid_not_the_mid():
    snap = make_snapshot(p_up=0.60, spread=0.10)  # bid 0.55, ask 0.65
    assert mark_for(snap, UP) == pytest.approx(0.55)


def test_mark_falls_back_to_opposite_book():
    snap = make_snapshot(p_up=0.60)
    snap.up_book = Book(bids=[], asks=[Level(0.62, 100)])
    snap.down_book = Book(bids=[Level(0.30, 100)], asks=[Level(0.38, 100)])
    assert mark_for(snap, UP) == pytest.approx(1.0 - 0.38)


def test_mark_is_none_when_nothing_quoted():
    snap = make_snapshot()
    snap.up_book = Book()
    snap.down_book = Book()
    assert mark_for(snap, UP) is None


# -- exit policy -----------------------------------------------------


def make_pos(entry: float = 0.60, ts: float = 0.0) -> OpenPosition:
    return OpenPosition("m", UP, shares=10, entry_price=entry, entry_ts=ts)


def test_stop_loss_fires_at_threshold():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=0.15))
    d = policy.evaluate(make_pos(0.60), mark=0.45, now=100.0, seconds_remaining=500)
    assert d.should_exit and d.reason == EXIT_STOP_LOSS


def test_stop_loss_holds_above_threshold():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=0.15))
    assert not policy.evaluate(
        make_pos(0.60), mark=0.46, now=100.0, seconds_remaining=500
    ).should_exit


def test_take_profit_fires():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=None, take_profit_rise=0.20))
    d = policy.evaluate(make_pos(0.60), mark=0.81, now=100.0, seconds_remaining=500)
    assert d.should_exit and d.reason == EXIT_TAKE_PROFIT


def test_trailing_stop_needs_a_runup_first():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=None, trailing_stop_drop=0.10))
    pos = make_pos(0.60)

    # Never ran up: trailing stop must not fire off the entry price alone.
    assert not policy.evaluate(pos, mark=0.52, now=10.0, seconds_remaining=500).should_exit

    pos.update_mark(0.80)
    d = policy.evaluate(pos, mark=0.69, now=20.0, seconds_remaining=500)
    assert d.should_exit and d.reason == EXIT_TRAILING


def test_time_exit_fires_after_max_hold():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=None, max_hold_seconds=120))
    d = policy.evaluate(make_pos(0.60, ts=0.0), mark=0.60, now=130.0, seconds_remaining=500)
    assert d.should_exit and d.reason == EXIT_TIME


def test_no_exit_inside_the_expiry_window():
    """Near expiry the book widens and the outcome is nearly decided."""
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=0.15, no_exit_within_seconds=20))
    assert not policy.evaluate(
        make_pos(0.60), mark=0.10, now=100.0, seconds_remaining=10
    ).should_exit


def test_min_hold_blocks_immediate_exit():
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=0.15, min_hold_seconds=60))
    assert not policy.evaluate(
        make_pos(0.60, ts=0.0), mark=0.20, now=30.0, seconds_remaining=500
    ).should_exit


def test_no_exit_without_a_bid():
    """Cannot claim an exit at a price nobody is offering."""
    policy = ExitPolicy(ExitsConfig(stop_loss_drop=0.15))
    d = policy.evaluate(make_pos(0.60), mark=None, now=100.0, seconds_remaining=500)
    assert not d.should_exit
    assert "no bid" in d.detail


def test_disabled_policy_never_exits():
    policy = ExitPolicy(ExitsConfig(enabled=False, stop_loss_drop=0.01))
    assert not policy.evaluate(
        make_pos(0.60), mark=0.01, now=100.0, seconds_remaining=500
    ).should_exit


# -- drawdown guard --------------------------------------------------


def test_drawdown_guard_trips_on_usd_limit():
    p = Portfolio(100.0)
    p.record_equity(0.0)
    p.open("a", UP, shares=100, price=0.50, ts=1.0)
    p.update_mark("a", 0.20)
    p.record_equity(2.0)  # equity 70, drawdown 30

    guard = DrawdownGuard(max_drawdown_usd=25.0, max_drawdown_pct=None)
    assert guard.check(p) is not None


def test_drawdown_guard_trips_on_pct_limit():
    p = Portfolio(100.0)
    p.record_equity(0.0)
    p.open("a", UP, shares=100, price=0.50, ts=1.0)
    p.update_mark("a", 0.30)
    p.record_equity(2.0)  # equity 80, drawdown 20%

    guard = DrawdownGuard(max_drawdown_usd=None, max_drawdown_pct=0.15)
    assert guard.check(p) is not None


def test_drawdown_guard_stays_quiet_below_limit():
    p = Portfolio(100.0)
    p.record_equity(0.0)
    guard = DrawdownGuard(max_drawdown_usd=25.0, max_drawdown_pct=None)
    assert guard.check(p) is None


def test_drawdown_guard_latches_once_tripped():
    """A recovery must not silently re-arm trading."""
    p = Portfolio(100.0)
    p.record_equity(0.0)
    p.open("a", UP, shares=100, price=0.50, ts=1.0)
    p.update_mark("a", 0.10)
    p.record_equity(2.0)

    guard = DrawdownGuard(max_drawdown_usd=25.0, max_drawdown_pct=None)
    first = guard.check(p)
    assert first is not None

    p.update_mark("a", 0.90)
    p.record_equity(3.0)
    assert guard.check(p) == first


def test_portfolio_render_includes_key_lines():
    p = Portfolio(100.0)
    p.open("a", UP, shares=20, price=0.50, ts=0.0)
    p.settle("a", UP, ts=10.0)
    text = p.render()
    assert "equity" in text
    assert "realized P&L" in text
    assert EXIT_EXPIRY in text
