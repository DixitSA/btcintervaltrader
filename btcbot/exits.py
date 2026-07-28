"""Exit policy: stop loss, take profit, trailing stop, time exit.

An honest note on stop losses in binary markets, because it changes how you
should read the backtest:

A stop loss here means selling your shares back into the book before expiry.
You crossed the spread to get in and you cross it again to get out. On a market
that is close to a coin flip, a 15-minute window generates a lot of noise, and a
tight stop converts positions that would have resolved in your favour into
realised losses.

That does not make stops useless -- they cap the tail and they cap correlated
bleed across many windows. But it does mean a stop is not free protection, and
the only way to know whether yours helps is to backtest with it on and off.
`backtest --no-exits` does exactly that comparison, and the report breaks P&L
out by exit reason so you can see what the stops actually cost or saved.

Thresholds are in probability points, not percent: a position entered at 0.60
with stop_loss_drop 0.15 exits when the bid reaches 0.45.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import ExitsConfig
from .portfolio import (
    EXIT_STOP_LOSS,
    EXIT_TAKE_PROFIT,
    EXIT_TIME,
    EXIT_TRAILING,
    OpenPosition,
)

log = logging.getLogger(__name__)

# Thresholds are computed by subtracting floats (0.60 - 0.15 = 0.4499...), so
# boundary comparisons need a tolerance or a mark sitting exactly on the stop
# fails to trigger.
EPS = 1e-9


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str = ""
    detail: str = ""


class ExitPolicy:
    def __init__(self, cfg: ExitsConfig):
        self.cfg = cfg

    def evaluate(
        self,
        pos: OpenPosition,
        mark: Optional[float],
        now: float,
        seconds_remaining: float,
    ) -> ExitDecision:
        """Decide whether to liquidate `pos` at the current `mark` (the bid)."""
        if not self.cfg.enabled:
            return ExitDecision(False)
        if mark is None:
            # No bid means no exit is possible at any price. Holding to expiry
            # is the only real option; claiming an exit here would be fiction.
            return ExitDecision(False, detail="no bid to sell into")

        held = now - pos.entry_ts
        if held < self.cfg.min_hold_seconds:
            return ExitDecision(False)

        # Very close to expiry the book widens and the outcome is nearly
        # decided; churning out there usually pays the spread for nothing.
        if seconds_remaining <= self.cfg.no_exit_within_seconds:
            return ExitDecision(False, detail="inside no-exit window")

        if self.cfg.stop_loss_drop is not None:
            trigger = pos.entry_price - self.cfg.stop_loss_drop
            if mark <= trigger + EPS:
                return ExitDecision(
                    True,
                    EXIT_STOP_LOSS,
                    f"mark {mark:.3f} <= stop {trigger:.3f} "
                    f"(entry {pos.entry_price:.3f})",
                )

        if self.cfg.take_profit_rise is not None:
            trigger = pos.entry_price + self.cfg.take_profit_rise
            if mark >= trigger - EPS:
                return ExitDecision(
                    True,
                    EXIT_TAKE_PROFIT,
                    f"mark {mark:.3f} >= target {trigger:.3f} "
                    f"(entry {pos.entry_price:.3f})",
                )

        if self.cfg.trailing_stop_drop is not None:
            trigger = pos.peak_mark - self.cfg.trailing_stop_drop
            # Only meaningful once the position has actually run up; otherwise
            # this duplicates the fixed stop at a worse level.
            if pos.peak_mark > pos.entry_price and mark <= trigger + EPS:
                return ExitDecision(
                    True,
                    EXIT_TRAILING,
                    f"mark {mark:.3f} <= trail {trigger:.3f} "
                    f"(peak {pos.peak_mark:.3f})",
                )

        if self.cfg.max_hold_seconds is not None and held >= self.cfg.max_hold_seconds:
            return ExitDecision(
                True, EXIT_TIME, f"held {held:.0f}s >= {self.cfg.max_hold_seconds:.0f}s"
            )

        return ExitDecision(False)


class DrawdownGuard:
    """Account-level kill switch keyed off equity, not just closed trades.

    The daily loss limit in risk.py only sees realised P&L. This one also sees
    positions currently under water, so it can stop new entries while a bad
    position is still open rather than after it resolves.
    """

    def __init__(self, max_drawdown_usd: Optional[float], max_drawdown_pct: Optional[float]):
        self.max_usd = max_drawdown_usd
        self.max_pct = max_drawdown_pct
        self.tripped_reason: Optional[str] = None

    def check(self, portfolio) -> Optional[str]:
        if self.tripped_reason:
            return self.tripped_reason

        dd = portfolio.max_drawdown
        if self.max_usd is not None and dd >= self.max_usd:
            self.tripped_reason = (
                f"drawdown ${dd:,.2f} hit limit ${self.max_usd:,.2f}"
            )
        elif self.max_pct is not None and portfolio.starting_cash > 0:
            pct = dd / portfolio.starting_cash
            if pct >= self.max_pct:
                self.tripped_reason = (
                    f"drawdown {pct:.1%} hit limit {self.max_pct:.1%}"
                )

        if self.tripped_reason:
            log.warning("drawdown guard tripped: %s", self.tripped_reason)
        return self.tripped_reason
