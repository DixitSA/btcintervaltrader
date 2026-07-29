"""End-to-end backtest tests, including the control experiment.

The key test here is `test_volume_rule_shows_no_edge_in_a_no_edge_world`: on
synthetic data where volume is generated independently of the price path, the
volume rule must NOT show a significant edge. If it ever does, the harness has
a bug and every result it produces is worthless.
"""

from __future__ import annotations

import pytest

from btcbot.backtest import BacktestReport, WindowResult, infer_outcome, run_backtest
from btcbot.config import Config
from btcbot.models import DOWN, UP
from btcbot.recorder import load_dataset, snapshot_from_dict, snapshot_to_dict
from btcbot.simulate import generate
from btcbot.strategies import build_strategy

from .test_core import make_snapshot


@pytest.fixture(scope="module")
def sim_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("sim")
    generate(out, n_windows=300, seed=7)
    return out


@pytest.fixture(scope="module")
def sim_snapshots(sim_dir):
    return load_dataset(sim_dir)


def test_simulator_produces_a_dataset(sim_snapshots):
    assert len(sim_snapshots) > 1000
    slugs = {s.market.slug for s in sim_snapshots}
    assert len(slugs) == 300


def test_snapshot_roundtrips_through_serialisation():
    snap = make_snapshot(p_up=0.42, volume=123.0)
    restored = snapshot_from_dict(snapshot_to_dict(snap))
    assert restored.ts == snap.ts
    assert restored.market.slug == snap.market.slug
    assert restored.market.strike == snap.market.strike
    assert restored.window_volume == snap.window_volume
    assert restored.up_book.best_ask == snap.up_book.best_ask
    assert restored.down_book.best_bid == snap.down_book.best_bid


def test_infer_outcome_prefers_spot_versus_strike():
    up = make_snapshot(spot=100_500.0, strike=100_000.0)
    down = make_snapshot(spot=99_500.0, strike=100_000.0)
    assert infer_outcome([up]) == UP
    assert infer_outcome([down]) == DOWN


def test_infer_outcome_falls_back_to_terminal_price():
    snap = make_snapshot(p_up=0.98, strike=None, spot=None)
    assert infer_outcome([snap]) == UP


def test_infer_outcome_none_when_undecidable():
    snap = make_snapshot(p_up=0.5, strike=None, spot=None)
    assert infer_outcome([snap]) is None


def test_default_volume_rule_takes_no_trades(sim_snapshots):
    """With assumed_edge=0 the risk layer should refuse every trade,
    because a signal that equals the market price has no edge to size."""
    cfg = Config()
    strat = build_strategy(
        "volume_threshold", {"min_volume_usd": 500_000, "assumed_edge": 0.0}
    )
    report = run_backtest(sim_snapshots, strat, cfg)
    assert report.windows_with_signal > 0
    assert report.n == 0


def research_config() -> Config:
    """Config with throughput caps and kill switches relaxed, so a measurement
    reflects the rule rather than the risk limits."""
    cfg = Config()
    cfg.risk.max_trades_per_hour = 10**9
    cfg.risk.daily_loss_limit_usd = 1e12
    cfg.risk.bankroll_usd = 100_000.0
    cfg.exits.max_drawdown_pct = None
    cfg.exits.max_drawdown_usd = None
    return cfg


@pytest.mark.parametrize("direction", ["follow", "fade", "up", "down"])
def test_volume_rule_shows_no_edge_in_a_no_edge_world(sim_snapshots, direction):
    """The control experiment, run to expiry with no stops.

    Volume in the simulator is drawn independently of the price path, so no
    setting of the volume rule can have a real edge. A failure here means the
    backtester is leaking future information.

    Exits are disabled so payoffs stay binary and the win-rate z-score is a
    valid statistic. The exits-enabled case is covered below with the
    t-statistic instead.
    """
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": direction, "assumed_edge": 0.05},
    )
    report = run_backtest(sim_snapshots, strategy=strat, cfg=research_config(), use_exits=False)

    if report.n < 30:
        pytest.skip(f"only {report.n} trades for direction={direction}")

    z = report.z_score
    assert z is not None
    assert abs(z) < 3.5, (
        f"direction={direction} showed z={z:+.2f} in a world with no edge by "
        "construction -- the harness is leaking information"
    )


@pytest.mark.parametrize("direction", ["follow", "fade", "up", "down"])
def test_no_positive_edge_with_stops_either(sim_snapshots, direction):
    """Same control, with stops on, judged by the t-statistic.

    Stops must not manufacture a positive edge out of a no-edge world. They are
    allowed to LOSE (crossing the spread twice costs real money) -- so this only
    asserts the absence of a significant *gain*.
    """
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": direction, "assumed_edge": 0.05},
    )
    report = run_backtest(sim_snapshots, strategy=strat, cfg=research_config(), use_exits=True)

    if report.n < 30:
        pytest.skip(f"only {report.n} trades for direction={direction}")

    t = report.roi_t_stat
    assert t is not None
    assert t < 2.0, (
        f"direction={direction} showed a significant POSITIVE t={t:+.2f} with "
        "stops in a world with no edge -- stops cannot create edge"
    )


def test_breakeven_includes_the_entry_fee():
    """Break-even is entry price PLUS the taker fee, not the price alone.

    Omitting the fee understates the bar you have to clear and so flatters the
    z-score computed against it -- an optimistic bias, in the direction that
    costs money.
    """
    from btcbot.fees import KalshiFees

    report = BacktestReport(starting_bankroll=100.0)
    report.trades = [
        WindowResult(slug="w", side=UP, shares=10, entry_price=0.50, outcome=UP, pnl=1.0)
    ]

    # No fee model attached: falls back to the fee-free break-even.
    assert report.breakeven_win_rate == pytest.approx(0.50)

    report.fee_model = KalshiFees()
    # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> rounded up to the cent = 0.02
    assert report.avg_entry_fee_per_contract == pytest.approx(0.02)
    assert report.breakeven_win_rate == pytest.approx(0.52)


@pytest.mark.parametrize("pnl,label", [(1.0, "all wins"), (-1.0, "all losses")])
def test_degenerate_win_rate_reports_no_z_score_rather_than_a_huge_one(pnl, label):
    """A 0% or 100% win rate must read as "cannot tell", not as certainty.

    The binomial standard error is exactly zero at both boundaries. Flooring it
    to keep the division alive made a 2-trade, 2-win sample print z = +530330 --
    infinite confidence from the least informative sample there is, in the one
    statistic the CLI tells the user to sanity-check against zero.
    """
    report = BacktestReport(starting_bankroll=100.0)
    report.trades = [
        WindowResult(slug=f"w{i}", side=UP, shares=10, entry_price=0.50,
                     outcome=UP, pnl=pnl)
        for i in range(2)
    ]

    assert report.win_rate == pytest.approx(1.0 if pnl > 0 else 0.0)
    assert report.win_rate_stderr is None, f"{label}: se is 0, not merely small"
    assert report.z_score is None, f"{label}: z must be None, not a huge number"

    rendered = report.render()
    assert "win-rate z-score      : -" in rendered
    assert ("every trade won" if pnl > 0 else "every trade lost") in rendered


def test_z_score_still_computed_off_the_boundary():
    """The refusal is confined to p = 0 and p = 1; ordinary samples still report."""
    report = BacktestReport(starting_bankroll=100.0)
    report.trades = [
        WindowResult(slug=f"w{i}", side=UP, shares=10, entry_price=0.50,
                     outcome=UP, pnl=1.0 if i < 3 else -1.0)
        for i in range(4)
    ]
    # 3/4 wins, break-even 0.50 (no fee model attached).
    assert report.win_rate_stderr == pytest.approx((0.75 * 0.25 / 4) ** 0.5)
    assert report.z_score == pytest.approx(0.25 / report.win_rate_stderr)


def test_breakeven_with_fees_makes_the_z_score_stricter(sim_snapshots):
    """The fee-inclusive bar must never be easier than the fee-free one."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": "follow", "assumed_edge": 0.05},
    )
    report = run_backtest(
        sim_snapshots, strategy=strat, cfg=research_config(), use_exits=False
    )
    if report.n < 30:
        pytest.skip(f"only {report.n} trades")

    assert report.fee_model is not None, "run_backtest must attach the fee model"
    assert report.avg_entry_fee_per_contract > 0.0
    assert report.breakeven_win_rate > report.avg_entry_price


def test_sweep_keeps_payoffs_binary_so_its_z_column_is_valid(sim_dir, monkeypatch):
    """`sweep` must run with stops OFF.

    The win-rate z-score compares a win RATE against an entry PRICE. That is
    only like-for-like while payoffs are binary. With stops on, `won` becomes
    "P&L > 0" while break-even stays a price, and this same no-edge dataset
    reports z around -3 to -6 in EVERY cell -- an artefact of the mismatched
    null, not a finding. The t-statistic is unaffected either way, which is
    why it is the column the docs tell you to read.
    """
    import argparse as _argparse

    from btcbot import cli

    seen: list = []
    real = cli.run_backtest

    def spy(snapshots, strategy, cfg, *args, **kwargs):
        seen.append(kwargs.get("use_exits", args[0] if args else None))
        return real(snapshots, strategy, cfg, *args, **kwargs)

    monkeypatch.setattr(cli, "run_backtest", spy)

    rc = cli.cmd_sweep(
        _argparse.Namespace(
            config=None,
            data_dir=str(sim_dir),
            thresholds=[300_000.0],
            assumed_edge=0.05,
        )
    )

    assert rc == 0
    assert seen, "sweep ran no backtests"
    assert all(v is False for v in seen), (
        f"sweep ran with use_exits={sorted(set(map(str, seen)))}; the z column "
        "is only a valid statistic while payoffs are binary"
    )


def test_stops_are_actually_exercised(sim_snapshots):
    """Guards against the stop silently never firing."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": "follow", "assumed_edge": 0.05},
    )
    cfg = research_config()
    cfg.exits.stop_loss_drop = 0.10

    report = run_backtest(sim_snapshots, strategy=strat, cfg=cfg, use_exits=True)
    assert report.exits.get("stop_loss", 0) > 0
    assert report.exits.get("expiry", 0) > 0


def test_stops_change_the_outcome_distribution(sim_snapshots):
    """Stops must measurably alter results, and the harness must expose both."""
    strat = build_strategy(
        "volume_threshold",
        {"min_volume_usd": 300_000, "direction": "follow", "assumed_edge": 0.05},
    )
    cfg = research_config()

    without = run_backtest(sim_snapshots, strategy=strat, cfg=cfg, use_exits=False)
    with_stops = run_backtest(sim_snapshots, strategy=strat, cfg=cfg, use_exits=True)

    assert without.n == with_stops.n
    assert set(without.exits) == {"expiry"}
    assert "stop_loss" in with_stops.exits
    # Crossing the spread twice on a coin flip should not raise the share of
    # profitable trades.
    assert with_stops.win_rate < without.win_rate


def test_report_metrics_are_consistent():
    report = BacktestReport(starting_bankroll=100.0)
    report.trades = [
        WindowResult("a", UP, shares=10, entry_price=0.50, outcome=UP, pnl=5.0),
        WindowResult("b", UP, shares=10, entry_price=0.50, outcome=DOWN, pnl=-5.0),
        WindowResult("c", UP, shares=10, entry_price=0.50, outcome=UP, pnl=5.0),
    ]
    report.ending_bankroll = 105.0

    assert report.n == 3
    assert report.wins == 2
    assert report.win_rate == pytest.approx(2 / 3)
    assert report.total_staked == pytest.approx(15.0)
    assert report.total_pnl == pytest.approx(5.0)
    assert report.roi == pytest.approx(1 / 3)
    assert report.breakeven_win_rate == pytest.approx(0.50)
    assert report.max_drawdown == pytest.approx(5.0)


def test_report_renders_without_trades():
    report = BacktestReport(starting_bankroll=100.0)
    report.rejections["no edge after fees"] = 12
    text = report.render()
    assert "No trades were taken" in text
    assert "no edge after fees" in text


def test_report_flags_insignificant_results():
    report = BacktestReport(starting_bankroll=100.0)
    # 11 wins out of 20 at $0.50 -- a coin flip.
    report.trades = [
        WindowResult(f"w{i}", UP, 10, 0.50, UP if i < 11 else DOWN, 5.0 if i < 11 else -5.0)
        for i in range(20)
    ]
    text = report.render()
    assert "NOT statistically distinguishable" in text


def test_paper_fill_accepted_at_exactly_the_limit():
    """Regression: float noise must not reject a fill sitting exactly at the
    rounded limit price."""
    from btcbot.execution import PaperExecutor
    from btcbot.models import Order

    cfg = Config()
    snap = make_snapshot(p_up=0.30, volume=600_000)
    ask = snap.up_book.best_ask
    limit = round(ask + cfg.fees.slippage, 3)

    fill = PaperExecutor(cfg).buy(snap, Order(side=UP, shares=10.0, limit_price=limit))
    assert fill is not None
    assert fill.price == pytest.approx(limit, abs=1e-6)
