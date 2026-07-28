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
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .config import Config
from .execution import PaperExecutor
from .models import DOWN, UP, Fill, Snapshot
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

    @property
    def won(self) -> bool:
        return self.outcome is not None and self.outcome == self.side


@dataclass
class BacktestReport:
    trades: list[WindowResult] = field(default_factory=list)
    windows_seen: int = 0
    windows_with_signal: int = 0
    rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    starting_bankroll: float = 0.0
    ending_bankroll: float = 0.0

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.won)

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
        lines += [
            f"win rate              : {wr:.1%} ({self.wins}/{self.n})",
            f"break-even win rate   : {be:.1%}  (avg entry ${be:.3f})",
            f"total staked          : ${self.total_staked:,.2f}",
            f"total P&L             : ${self.total_pnl:,.2f}",
            f"ROI on stake          : {(self.roi or 0.0):+.2%}",
            f"bankroll              : ${self.starting_bankroll:,.2f} -> ${self.ending_bankroll:,.2f}",
            f"max drawdown          : ${self.max_drawdown:,.2f}",
        ]
        z = self.z_score
        if z is not None:
            lines.append(f"edge z-score          : {z:+.2f}")
            if abs(z) < 2.0:
                lines.append(
                    "  -> NOT statistically distinguishable from no edge. "
                    "Do not trade this live."
                )
            elif z > 0:
                lines.append(
                    "  -> Positive and significant on THIS sample. Confirm it holds "
                    "out-of-sample before believing it."
                )
            else:
                lines.append("  -> Significantly WORSE than break-even.")
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
) -> BacktestReport:
    windows = group_windows(snapshots)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
    executor = PaperExecutor(cfg)

    report = BacktestReport(starting_bankroll=risk.state.bankroll)

    for slug in sorted(windows, key=lambda s: windows[s][0].ts):
        window = windows[slug]
        report.windows_seen += 1
        outcome = infer_outcome(window)

        signalled = False
        fill: Optional[Fill] = None
        reason_text = ""

        for snap in window:
            signal = strategy.decide(snap)
            if signal is None:
                continue
            signalled = True

            order, rejection = risk.evaluate(snap, signal)
            if order is None:
                if rejection:
                    report.rejections[rejection] += 1
                continue

            fill = executor.buy(snap, order)
            if fill is None:
                report.rejections["paper fill failed"] += 1
                continue

            reason_text = order.reason
            risk.on_trade(snap.ts)
            if one_trade_per_window:
                break

        if signalled:
            report.windows_with_signal += 1

        if fill is None:
            continue

        # Settle: winning shares pay $1, fee applies to profit only.
        if outcome is None:
            pnl = -fill.fee  # treat as voided stake return
        elif outcome == fill.side:
            profit = fill.shares * (1.0 - fill.price)
            pnl = profit * (1.0 - cfg.fees.winnings_fee_bps / 10_000.0) - fill.fee
        else:
            pnl = -(fill.shares * fill.price) - fill.fee

        risk.on_settlement(pnl)
        report.trades.append(
            WindowResult(
                slug=slug,
                side=fill.side,
                shares=fill.shares,
                entry_price=fill.price,
                outcome=outcome,
                pnl=pnl,
                reason=reason_text,
            )
        )

    report.ending_bankroll = risk.state.bankroll
    return report
