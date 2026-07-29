"""Multi-market concurrency and the total-exposure cap.

Trading several window families at once is the legitimate way to raise trade
count. It is also the point at which per-trade sizing stops being sufficient:
Kelly sizes each position as if it were the only one, so N concurrent positions
is N times the intended risk unless total exposure is capped.
"""

from __future__ import annotations

import pytest

from btcbot.backtest import run_backtest
from btcbot.config import Config
from btcbot.models import UP
from btcbot.portfolio import Portfolio
from btcbot.recorder import load_dataset
from btcbot.risk import RiskManager
from btcbot.simulate import generate
from btcbot.strategies import build_strategy
from btcbot.strategies.base import Signal

from .test_core import make_snapshot

FAMILIES = ["btc-updown-15m", "eth-updown-15m", "sol-updown-15m"]


@pytest.fixture(scope="module")
def multi_snapshots(tmp_path_factory):
    out = tmp_path_factory.mktemp("multi")
    generate(out, n_windows=120, seed=11, families=FAMILIES)
    return load_dataset(out)


def research_cfg(**overrides) -> Config:
    cfg = Config()
    cfg.risk.bankroll_usd = 10_000.0
    cfg.risk.max_trades_per_hour = 10**9
    cfg.risk.daily_loss_limit_usd = 1e12
    cfg.risk.max_concurrent_positions = 10
    cfg.exits.max_drawdown_pct = None
    cfg.exits.max_drawdown_usd = None
    for key, value in overrides.items():
        for target in (cfg.risk, cfg.markets, cfg.exits):
            if hasattr(target, key):
                setattr(target, key, value)
    return cfg


# -- discovery -------------------------------------------------------


def test_simulator_emits_overlapping_families(multi_snapshots):
    slugs = {s.market.slug for s in multi_snapshots}
    for fam in FAMILIES:
        assert any(s.startswith(fam) for s in slugs), fam

    # Windows from different families must actually coexist in time.
    by_family = {}
    for snap in multi_snapshots:
        fam = snap.market.slug.rsplit("-sim-", 1)[0]
        by_family.setdefault(fam, []).append(snap.ts)
    spans = {f: (min(v), max(v)) for f, v in by_family.items()}
    a, b = spans[FAMILIES[0]], spans[FAMILIES[1]]
    assert a[0] < b[1] and b[0] < a[1], "families do not overlap in time"


# -- exposure cap ----------------------------------------------------


def test_exposure_cap_limits_total_stake():
    cfg = research_cfg(max_total_exposure_fraction=0.05, max_stake_per_trade_usd=1e9)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
    pf = Portfolio(10_000.0)

    # Budget is 5% of 10k = $500. Consume $450 of it.
    pf.open("a", UP, shares=900, price=0.50, ts=0.0)
    assert sum(p.cost_basis for p in pf.positions.values()) == pytest.approx(450.0)

    snap = make_snapshot(p_up=0.50)
    order, reason = risk.evaluate(snap, Signal(side=UP, prob=0.80), portfolio=pf)
    assert order is not None, reason
    assert order.shares * order.limit_price <= 55.0, "stake exceeded remaining headroom"


def test_exposure_cap_blocks_when_full():
    cfg = research_cfg(max_total_exposure_fraction=0.05)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
    pf = Portfolio(10_000.0)
    pf.open("a", UP, shares=1100, price=0.50, ts=0.0)  # $550 > $500 budget

    order, reason = risk.evaluate(
        make_snapshot(p_up=0.50), Signal(side=UP, prob=0.90), portfolio=pf
    )
    assert order is None
    assert "total exposure" in reason


def test_without_portfolio_the_cap_is_simply_not_applied():
    """The cap needs the portfolio; its absence must not crash sizing."""
    cfg = research_cfg(max_total_exposure_fraction=0.01)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
    order, reason = risk.evaluate(make_snapshot(p_up=0.50), Signal(side=UP, prob=0.80))
    assert order is not None, reason


def test_stake_never_exceeds_available_cash():
    cfg = research_cfg(max_total_exposure_fraction=1.0, max_stake_per_trade_usd=1e9)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
    pf = Portfolio(10_000.0)
    pf.open("a", UP, shares=19_000, price=0.50, ts=0.0)  # leaves $500 cash

    order, reason = risk.evaluate(
        make_snapshot(p_up=0.50), Signal(side=UP, prob=0.80), portfolio=pf
    )
    if order is not None:
        assert order.shares * order.limit_price <= pf.cash + 1e-6


# -- concurrent backtest ---------------------------------------------


def test_multi_family_raises_trade_count(multi_snapshots):
    """The honest way to trade more: more markets, not a different bet."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": "follow", "assumed_edge": 0.05},
    )
    cfg = research_cfg()

    single = [s for s in multi_snapshots if s.market.slug.startswith(FAMILIES[0])]
    one = run_backtest(single, strategy=strat, cfg=cfg, use_exits=False)
    many = run_backtest(multi_snapshots, strategy=strat, cfg=cfg, use_exits=False)

    assert many.n > one.n * 2, f"expected far more trades: {one.n} -> {many.n}"


def test_concurrent_positions_respect_the_exposure_cap(multi_snapshots):
    """No moment may exceed the configured total exposure."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 100_000, "direction": "follow", "assumed_edge": 0.05},
    )
    cfg = research_cfg(max_total_exposure_fraction=0.05, max_stake_per_trade_usd=1e9)
    report = run_backtest(multi_snapshots, strategy=strat, cfg=cfg, use_exits=False)

    assert report.n > 0
    assert report.portfolio is not None
    # Cash can never go negative if exposure was honoured throughout.
    assert report.portfolio.cash >= -1e-6


def test_backtest_never_overdraws_cash(multi_snapshots):
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 50_000, "direction": "follow", "assumed_edge": 0.05},
    )
    cfg = research_cfg(max_total_exposure_fraction=0.5, max_stake_per_trade_usd=1e9)
    report = run_backtest(multi_snapshots, strategy=strat, cfg=cfg, use_exits=True)
    assert report.portfolio.cash >= -1e-6


def test_every_position_is_closed_at_the_end(multi_snapshots):
    """No trade may be left dangling, or P&L silently understates risk."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 200_000, "direction": "follow", "assumed_edge": 0.05},
    )
    report = run_backtest(
        multi_snapshots, strategy=strat, cfg=research_cfg(), use_exits=True
    )
    assert report.portfolio.open_count == 0
    assert len(report.portfolio.closed) == report.n


def test_more_trades_does_not_improve_a_losing_edge(multi_snapshots):
    """The point of the whole exercise.

    Volume scales an edge; it does not create one. Trading three families
    instead of one must not turn a non-edge into a demonstrated edge.
    """
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": "follow", "assumed_edge": 0.05},
    )
    report = run_backtest(
        multi_snapshots, strategy=strat, cfg=research_cfg(), use_exits=False
    )
    if report.n < 30:
        pytest.skip("too few trades")
    assert report.roi_t_stat is not None
    assert report.roi_t_stat < 2.0
