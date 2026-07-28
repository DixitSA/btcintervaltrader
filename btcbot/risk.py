"""Risk and position sizing.

This layer has veto power over every strategy. It exists because the failure
mode of a 15-minute binary bot is not "one bad trade" -- it is placing 96 bets
a day at a small negative edge and calling the drawdown bad luck.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import FeeConfig, MarketsConfig, RiskConfig
from .models import Order, Snapshot
from .strategies.base import Signal

log = logging.getLogger(__name__)


@dataclass
class RiskState:
    bankroll: float
    realized_pnl_today: float = 0.0
    open_positions: int = 0
    trade_times: list[float] = field(default_factory=list)
    day_start: float = field(default_factory=lambda: time.time())
    halted_reason: Optional[str] = None


def kelly_fraction(prob: float, price: float) -> float:
    """Kelly stake for a binary contract bought at `price` paying $1.

    f* = (p - price) / (1 - price)
    """
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return (prob - price) / (1.0 - price)


class RiskManager:
    def __init__(self, risk: RiskConfig, fees: FeeConfig, markets: MarketsConfig):
        self.cfg = risk
        self.fees = fees
        self.markets = markets
        self.state = RiskState(bankroll=risk.bankroll_usd)

    # -- gates ---------------------------------------------------------

    def _roll_day(self, now: float) -> None:
        if now - self.state.day_start >= 86_400:
            self.state.day_start = now
            self.state.realized_pnl_today = 0.0
            if self.state.halted_reason == "daily loss limit":
                self.state.halted_reason = None

    def _recent_trades(self, now: float) -> int:
        cutoff = now - 3600.0
        self.state.trade_times = [t for t in self.state.trade_times if t >= cutoff]
        return len(self.state.trade_times)

    def check_market(self, snap: Snapshot) -> Optional[str]:
        """Structural reasons to skip this market entirely."""
        remaining = snap.market.seconds_remaining(snap.ts)
        if remaining < self.markets.min_seconds_remaining:
            return f"only {remaining:.0f}s left"
        if remaining > self.markets.max_seconds_remaining:
            return "window not open yet"
        return None

    def evaluate(self, snap: Snapshot, signal: Signal) -> tuple[Optional[Order], Optional[str]]:
        """Turn a signal into a sized order, or explain why not."""
        now = snap.ts
        self._roll_day(now)

        if self.state.halted_reason:
            return None, f"halted: {self.state.halted_reason}"

        market_reason = self.check_market(snap)
        if market_reason:
            return None, market_reason

        if self.state.open_positions >= self.cfg.max_concurrent_positions:
            return None, "max concurrent positions"

        if self._recent_trades(now) >= self.cfg.max_trades_per_hour:
            return None, "hourly trade cap"

        if self.state.realized_pnl_today <= -abs(self.cfg.daily_loss_limit_usd):
            self.state.halted_reason = "daily loss limit"
            return None, "halted: daily loss limit"

        book = snap.book(signal.side)
        ask = book.best_ask
        if ask is None:
            return None, "no ask"

        spread = book.spread
        if spread is not None and spread > self.markets.max_spread:
            return None, f"spread {spread:.3f} too wide"

        entry = ask + self.fees.slippage
        if entry > self.cfg.max_entry_price:
            return None, f"entry {entry:.3f} above max_entry_price"
        if entry < self.cfg.min_entry_price:
            return None, f"entry {entry:.3f} below min_entry_price"

        # Edge must survive fees. Fee on winnings applies to profit only.
        gross_edge = signal.prob - entry
        expected_profit = signal.prob * (1.0 - entry)
        fee_drag = expected_profit * (self.fees.winnings_fee_bps / 10_000.0)
        fee_drag += entry * (self.fees.taker_fee_bps / 10_000.0)
        net_edge = gross_edge - fee_drag

        if net_edge <= 0:
            return None, (
                f"no edge after fees (gross {gross_edge:+.4f}, net {net_edge:+.4f})"
            )

        f = kelly_fraction(signal.prob, entry) * self.cfg.kelly_fraction
        if f <= 0:
            return None, "non-positive kelly"

        stake = min(
            self.state.bankroll * f,
            self.state.bankroll * self.cfg.max_stake_fraction,
            self.cfg.max_stake_per_trade_usd,
        )
        if stake <= 0.5:
            return None, f"stake ${stake:.2f} below minimum"

        shares = stake / entry

        # Refuse orders the visible book cannot actually fill.
        avg_fill = book.sweep_cost(shares)
        if avg_fill is None:
            return None, "book too thin to fill"
        if avg_fill > self.cfg.max_entry_price:
            return None, f"sweep price {avg_fill:.3f} above max_entry_price"

        depth_usd = sum(lv.price * lv.size for lv in book.asks)
        if depth_usd < self.markets.min_book_depth_usd:
            return None, f"book depth ${depth_usd:.0f} too thin"

        return (
            Order(
                side=signal.side,
                shares=round(shares, 2),
                limit_price=round(min(avg_fill + self.fees.slippage, self.cfg.max_entry_price), 3),
                reason=f"{signal.reason} | net_edge={net_edge:+.4f} stake=${stake:.2f}",
            ),
            None,
        )

    # -- bookkeeping ---------------------------------------------------

    def on_trade(self, ts: float) -> None:
        self.state.trade_times.append(ts)
        self.state.open_positions += 1

    def on_settlement(self, pnl: float) -> None:
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.bankroll += pnl
        self.state.realized_pnl_today += pnl
        if self.state.realized_pnl_today <= -abs(self.cfg.daily_loss_limit_usd):
            self.state.halted_reason = "daily loss limit"
            log.warning("daily loss limit hit; halting new entries")
