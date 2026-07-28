"""Replay recorded snapshots through the live strategy + risk code.

The point of this module is to answer one question honestly: does the rule
make money net of fees, and is the result distinguishable from luck?

That second half is what separates this from a video's backtest. A 15-minute
binary is close to a coin flip, so a 55% win rate over 40 trades is entirely
consistent with having no edge at all. The report prints the break-even win
rate and a standard error so you can see whether your sample says anything.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from .config import Config
from .execution import PaperExecutor
from .fees import build_fee_model
from .exits import DrawdownGuard, ExitPolicy
from .models import DOWN, UP, Fill, Order, Snapshot
from .portfolio import EXIT_EXPIRY, Portfolio, mark_for
from .risk import RiskManager
from .signals import market_implied_up
from .strategies.base import Strategy


@dataclass
class WindowResult:
    slug: str
    side: str
    shares: float
    entry_price: float
    outcome: Optional[str]
    pnl: float
    reason: str = ""
    exit_reason: str = EXIT_EXPIRY
    exit_price: float = 0.0

    @property
    def won(self) -> bool:
        """Profitable, not merely "picked the right side".

        A stopped-out position can hold the winning side and still lose money,
        so P&L is the honest criterion once exits are in play.
        """
        return self.pnl > 0

    @property
    def picked_correctly(self) -> bool:
        return self.outcome is not None and self.outcome == self.side


@dataclass
class BacktestReport:
    trades: list[WindowResult] = field(default_factory=list)
    windows_seen: int = 0
    windows_with_signal: int = 0
    rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    exits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    starting_bankroll: float = 0.0
    ending_bankroll: float = 0.0
    portfolio: Optional[Portfolio] = None

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

    @property
    def trade_returns(self) -> list[float]:
        """Per-trade return on stake. The basis for the significance test."""
        out = []
        for t in self.trades:
            stake = t.shares * t.entry_price
            if stake > 0:
                out.append(t.pnl / stake)
        return out

    @property
    def roi_t_stat(self) -> Optional[float]:
        """t-statistic of mean per-trade return against zero.

        Preferred over the win-rate z-score once stops are enabled: with early
        exits the payoff is no longer binary, so comparing a win rate to the
        entry price stops being the right null.
        """
        rets = self.trade_returns
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var <= 0:
            return None
        return mean / math.sqrt(var / len(rets))

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.n if self.n else None

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_staked(self) -> float:
        return sum(t.shares * t.entry_price for t in self.trades)

    @property
    def roi(self) -> Optional[float]:
        staked = self.total_staked
        return self.total_pnl / staked if staked > 0 else None

    @property
    def avg_entry_price(self) -> Optional[float]:
        if not self.n:
            return None
        return sum(t.entry_price for t in self.trades) / self.n

    @property
    def breakeven_win_rate(self) -> Optional[float]:
        """At average entry price c, you must win c of the time to break even."""
        return self.avg_entry_price

    @property
    def win_rate_stderr(self) -> Optional[float]:
        """Standard error of the observed win rate."""
        if self.n < 2:
            return None
        p = self.wins / self.n
        return math.sqrt(max(p * (1.0 - p), 1e-12) / self.n)

    @property
    def z_score(self) -> Optional[float]:
        """How many standard errors the win rate sits above break-even.

        Below ~2, the sample does not support a claim of edge.
        """
        be = self.breakeven_win_rate
        se = self.win_rate_stderr
        wr = self.win_rate
        if be is None or se in (None, 0) or wr is None:
            return None
        return (wr - be) / se

    @property
    def max_drawdown(self) -> float:
        peak = self.starting_bankroll
        equity = self.starting_bankroll
        worst = 0.0
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    def render(self) -> str:
        lines = [
            "=" * 62,
            "BACKTEST REPORT",
            "=" * 62,
            f"windows seen          : {self.windows_seen}",
            f"windows with signal   : {self.windows_with_signal}",
            f"trades taken          : {self.n}",
        ]
        if not self.n:
            lines += [
                "",
                "No trades were taken. Most common reasons for declining:",
            ]
            for reason, count in sorted(
                self.rejections.items(), key=lambda kv: -kv[1]
            )[:8]:
                lines.append(f"  {count:6d}  {reason}")
            lines.append("=" * 62)
            return "\n".join(lines)

        wr = self.win_rate or 0.0
        be = self.breakeven_win_rate or 0.0
        dd = self.portfolio.max_drawdown if self.portfolio else self.max_drawdown
        lines += [
            f"profitable trades     : {wr:.1%} ({self.wins}/{self.n})",
            f"break-even win rate   : {be:.1%}  (avg entry ${be:.3f})",
            f"total staked          : ${self.total_staked:,.2f}",
            f"total P&L             : ${self.total_pnl:,.2f}",
            f"ROI on stake          : {(self.roi or 0.0):+.2%}",
            f"equity                : ${self.starting_bankroll:,.2f} -> ${self.ending_bankroll:,.2f}",
            f"max drawdown          : ${dd:,.2f}",
        ]

        if self.exits:
            lines.append("exits                 :")
            pnl_by = {}
            for t in self.trades:
                pnl_by[t.exit_reason] = pnl_by.get(t.exit_reason, 0.0) + t.pnl
            for reason in sorted(self.exits, key=lambda r: -self.exits[r]):
                lines.append(
                    f"  {reason:<16} {self.exits[reason]:>5}  "
                    f"${pnl_by.get(reason, 0.0):+,.2f}"
                )

        t_stat = self.roi_t_stat
        if t_stat is not None:
            lines.append(f"ROI t-statistic       : {t_stat:+.2f}")
            if abs(t_stat) < 2.0:
                lines.append(
                    "  -> NOT statistically distinguishable from no edge. "
                    "Do not trade this live."
                )
            elif t_stat > 0:
                lines.append(
                    "  -> Positive and significant on THIS sample. Confirm it holds "
                    "out-of-sample before believing it."
                )
            else:
                lines.append("  -> Significantly LOSING.")

        z = self.z_score
        if z is not None and not self.exits.keys() - {EXIT_EXPIRY}:
            # Only meaningful when every trade ran to expiry (binary payoff).
            lines.append(f"win-rate z-score      : {z:+.2f}")

        if self.n < 100:
            lines.append(
                f"  note: {self.n} trades is a small sample for a near-coin-flip market."
            )
        lines.append("=" * 62)
        return "\n".join(lines)


def infer_outcome(window: list[Snapshot]) -> Optional[str]:
    """Determine which side won a recorded window.

    Prefers spot vs strike at the final snapshot. Falls back to the terminal
    market price, which converges to 0 or 1 at resolution.
    """
    if not window:
        return None
    last = window[-1]

    if last.spot is not None and last.market.strike is not None:
        return UP if last.spot > last.market.strike else DOWN

    p_up = market_implied_up(last)
    if p_up is None:
        return None
    if p_up >= 0.9:
        return UP
    if p_up <= 0.1:
        return DOWN
    return None


def group_windows(snaps: Iterable[Snapshot]) -> dict[str, list[Snapshot]]:
    windows: dict[str, list[Snapshot]] = defaultdict(list)
    for snap in snaps:
        windows[snap.market.slug].append(snap)
    for slug in windows:
        windows[slug].sort(key=lambda s: s.ts)
    return windows


def run_backtest(
    snapshots: Iterable[Snapshot],
    strategy: Strategy,
    cfg: Config,
    one_trade_per_window: bool = True,
    use_exits: Optional[bool] = None,
) -> BacktestReport:
    """Replay windows through strategy -> risk -> execution -> portfolio.

    `use_exits` overrides cfg.exits.enabled, so the same dataset can be run
    with and without stops to measure what they actually cost or saved.
    """
    windows = group_windows(snapshots)
    fee_model = build_fee_model(cfg.venue, cfg.fees)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets, fee_model=fee_model)
    executor = PaperExecutor(cfg, fee_model=fee_model)
    portfolio = Portfolio(cfg.risk.bankroll_usd)

    exits_cfg = replace(cfg.exits, enabled=use_exits) if use_exits is not None else cfg.exits
    exit_policy = ExitPolicy(exits_cfg)
    guard = DrawdownGuard(exits_cfg.max_drawdown_usd, exits_cfg.max_drawdown_pct)

    report = BacktestReport(starting_bankroll=portfolio.starting_cash)
    report.portfolio = portfolio
    report.windows_seen = len(windows)

    outcomes = {slug: infer_outcome(w) for slug, w in windows.items()}
    final_ts = {slug: w[-1].ts for slug, w in windows.items()}

    # Replay in TIME order, not window order, so overlapping markets compete
    # for the same capital exactly as they would live. Iterating window by
    # window would let every market spend the full bankroll independently.
    ordered = sorted(
        (s for w in windows.values() for s in w), key=lambda s: (s.ts, s.market.slug)
    )

    entered: set[str] = set()
    signalled: set[str] = set()
    entry_reason: dict[str, str] = {}

    def record(slug: str) -> None:
        last = portfolio.closed[-1]
        report.exits[last.exit_reason] += 1
        report.trades.append(
            WindowResult(
                slug=slug,
                side=last.side,
                shares=last.shares,
                entry_price=last.entry_price,
                outcome=outcomes.get(slug),
                pnl=last.pnl,
                reason=entry_reason.get(slug, ""),
                exit_reason=last.exit_reason,
                exit_price=last.exit_price,
            )
        )

    for snap in ordered:
        slug = snap.market.slug

        # -- manage an open position -----------------------------------
        if portfolio.has_position(slug):
            pos = portfolio.positions[slug]
            mark = mark_for(snap, pos.side)
            if mark is not None:
                portfolio.update_mark(slug, mark)

            decision = exit_policy.evaluate(
                pos, mark, snap.ts, snap.market.seconds_remaining(snap.ts)
            )
            if decision.should_exit:
                exit_fill = executor.sell(
                    snap,
                    Order(
                        side=pos.side,
                        shares=pos.shares,
                        limit_price=0.0,  # take whatever the bid gives
                        reason=decision.detail,
                    ),
                )
                if exit_fill is not None:
                    trade = portfolio.close(
                        slug,
                        exit_fill.price,
                        snap.ts,
                        decision.reason,
                        fee=exit_fill.fee,
                        outcome=outcomes.get(slug),
                    )
                    risk.on_settlement(trade.pnl)
                    record(slug)
                else:
                    report.rejections["exit fill failed"] += 1

            # Window is closing and we still hold: settle at the outcome.
            if snap.ts >= final_ts[slug] and portfolio.has_position(slug):
                trade = portfolio.settle(
                    slug, outcomes.get(slug), snap.ts, fee_model=fee_model
                )
                risk.on_settlement(trade.pnl)
                record(slug)

            portfolio.record_equity(snap.ts)
            continue

        # -- otherwise consider an entry -------------------------------
        if slug in entered:
            continue

        signal = strategy.decide(snap)
        if signal is None:
            continue
        signalled.add(slug)

        if guard.check(portfolio):
            report.rejections[f"halted: {guard.tripped_reason}"] += 1
            continue

        order, rejection = risk.evaluate(snap, signal, portfolio=portfolio)
        if order is None:
            if rejection:
                report.rejections[rejection] += 1
            continue

        fill = executor.buy(snap, order)
        if fill is None:
            report.rejections["paper fill failed"] += 1
            continue

        try:
            portfolio.open(slug, fill.side, fill.shares, fill.price, snap.ts, fee=fill.fee)
        except ValueError as exc:
            report.rejections[str(exc).split(":")[0]] += 1
            continue

        entered.add(slug)
        entry_reason[slug] = order.reason
        risk.on_trade(snap.ts)
        portfolio.record_equity(snap.ts)

    # Anything still open (dataset ended mid-window) settles at its outcome.
    for slug in list(portfolio.positions):
        trade = portfolio.settle(
            slug, outcomes.get(slug), final_ts[slug], fee_model=fee_model
        )
        risk.on_settlement(trade.pnl)
        record(slug)

    report.windows_with_signal = len(signalled)
    report.ending_bankroll = portfolio.equity
    return report
