"""Portfolio accounting: cash, open positions, mark-to-market, P&L ledger.

This is the module that makes the risk limits real. Before it existed, live
mode released a position slot at expiry and booked $0 P&L, which meant the
daily loss limit could never fire on an actual loss.

Two distinct numbers matter and are tracked separately:

  realized P&L   -- closed trades only. What you have actually made or lost.
  unrealized P&L -- open positions marked at what the book would pay you *now*
                    (the bid, not the mid -- the mid is a price nobody offers).

Equity is cash + what the open positions would liquidate for. The stop loss
and the drawdown kill switch both key off equity, so they respond to positions
going against you rather than waiting for expiry to find out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .models import DOWN, UP

log = logging.getLogger(__name__)

# Exit reasons, also used by the reporting layer to group trades.
EXIT_STOP_LOSS = "stop_loss"
EXIT_TAKE_PROFIT = "take_profit"
EXIT_TRAILING = "trailing_stop"
EXIT_TIME = "time_exit"
EXIT_EXPIRY = "expiry"
EXIT_VOID = "void"


@dataclass
class OpenPosition:
    slug: str
    side: str
    shares: float
    entry_price: float
    entry_ts: float
    fees_paid: float = 0.0
    # Best mark seen while held, for trailing stops.
    peak_mark: float = 0.0
    last_mark: Optional[float] = None

    def __post_init__(self) -> None:
        if self.peak_mark <= 0.0:
            self.peak_mark = self.entry_price
        if self.last_mark is None:
            self.last_mark = self.entry_price

    @property
    def cost_basis(self) -> float:
        return self.shares * self.entry_price + self.fees_paid

    def mark_value(self, mark: Optional[float] = None) -> float:
        """Liquidation value at `mark` (defaults to the last mark seen)."""
        px = mark if mark is not None else (self.last_mark or self.entry_price)
        return self.shares * px

    def unrealized(self, mark: Optional[float] = None) -> float:
        return self.mark_value(mark) - self.cost_basis

    def update_mark(self, mark: float) -> None:
        self.last_mark = mark
        if mark > self.peak_mark:
            self.peak_mark = mark


@dataclass
class ClosedTrade:
    slug: str
    side: str
    shares: float
    entry_price: float
    exit_price: float
    entry_ts: float
    exit_ts: float
    pnl: float
    exit_reason: str
    outcome: Optional[str] = None
    fees_paid: float = 0.0

    @property
    def won(self) -> bool:
        return self.pnl > 0

    @property
    def held_seconds(self) -> float:
        return self.exit_ts - self.entry_ts


class Portfolio:
    """Tracks cash, open positions and a full trade ledger."""

    def __init__(self, starting_cash: float):
        self.starting_cash = float(starting_cash)
        self.cash = float(starting_cash)
        self.positions: dict[str, OpenPosition] = {}
        self.closed: list[ClosedTrade] = []
        self.equity_curve: list[tuple[float, float]] = []
        self._peak_equity = float(starting_cash)

    # -- opening / closing ---------------------------------------------

    def open(
        self,
        slug: str,
        side: str,
        shares: float,
        price: float,
        ts: float,
        fee: float = 0.0,
    ) -> OpenPosition:
        if slug in self.positions:
            raise ValueError(f"already holding a position in {slug}")

        cost = shares * price + fee
        if cost > self.cash + 1e-9:
            raise ValueError(
                f"insufficient cash: need ${cost:.2f}, have ${self.cash:.2f}"
            )

        self.cash -= cost
        pos = OpenPosition(
            slug=slug,
            side=side,
            shares=shares,
            entry_price=price,
            entry_ts=ts,
            fees_paid=fee,
        )
        self.positions[slug] = pos
        return pos

    def close(
        self,
        slug: str,
        exit_price: float,
        ts: float,
        reason: str,
        fee: float = 0.0,
        outcome: Optional[str] = None,
    ) -> ClosedTrade:
        """Close by selling back into the book at `exit_price`."""
        pos = self.positions.pop(slug)
        proceeds = pos.shares * exit_price - fee
        self.cash += proceeds

        trade = ClosedTrade(
            slug=slug,
            side=pos.side,
            shares=pos.shares,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_ts=pos.entry_ts,
            exit_ts=ts,
            pnl=proceeds - pos.cost_basis,
            exit_reason=reason,
            outcome=outcome,
            fees_paid=pos.fees_paid + fee,
        )
        self.closed.append(trade)
        return trade

    def settle(
        self,
        slug: str,
        winning_side: Optional[str],
        ts: float,
        fee_model=None,
    ) -> ClosedTrade:
        """Resolve at expiry: winning shares pay $1, losing shares pay $0."""
        pos = self.positions[slug]

        if winning_side is None:
            # Undecidable window: return the stake rather than invent a result.
            return self.close(
                slug, pos.entry_price, ts, EXIT_VOID, fee=0.0, outcome=None
            )

        won = winning_side == pos.side
        if won:
            gross = pos.shares
            exit_price = 1.0
        else:
            gross = 0.0
            exit_price = 0.0

        # Kalshi charges its taker fee at trade time, so settlement_fee is zero
        # and this stays zero without a model. Routed through the fee model
        # anyway so a venue that charges at resolution cannot be missed here.
        fee = (
            fee_model.settlement_fee(pos.shares, pos.entry_price, won)
            if fee_model is not None
            else 0.0
        )

        pos_obj = self.positions.pop(slug)
        self.cash += gross - fee
        trade = ClosedTrade(
            slug=slug,
            side=pos_obj.side,
            shares=pos_obj.shares,
            entry_price=pos_obj.entry_price,
            exit_price=exit_price,
            entry_ts=pos_obj.entry_ts,
            exit_ts=ts,
            pnl=(gross - fee) - pos_obj.cost_basis,
            exit_reason=EXIT_EXPIRY,
            outcome=winning_side,
            fees_paid=pos_obj.fees_paid + fee,
        )
        self.closed.append(trade)
        return trade

    # -- marking -------------------------------------------------------

    def update_mark(self, slug: str, mark: float) -> None:
        pos = self.positions.get(slug)
        if pos is not None:
            pos.update_mark(mark)

    def record_equity(self, ts: float) -> float:
        eq = self.equity
        self.equity_curve.append((ts, eq))
        self._peak_equity = max(self._peak_equity, eq)
        return eq

    # -- values --------------------------------------------------------

    @property
    def position_value(self) -> float:
        return sum(p.mark_value() for p in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + self.position_value

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized() for p in self.positions.values())

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_cash

    @property
    def peak_equity(self) -> float:
        return max(self._peak_equity, self.equity)

    @property
    def drawdown(self) -> float:
        """Current drop from peak equity, as a positive number."""
        return max(0.0, self.peak_equity - self.equity)

    @property
    def max_drawdown(self) -> float:
        peak = self.starting_cash
        worst = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            worst = max(worst, peak - eq)
        return max(worst, self.drawdown)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def has_position(self, slug: str) -> bool:
        return slug in self.positions

    # -- reporting -----------------------------------------------------

    def exit_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in self.closed:
            counts[t.exit_reason] = counts.get(t.exit_reason, 0) + 1
        return counts

    def pnl_by_exit_reason(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for t in self.closed:
            out[t.exit_reason] = out.get(t.exit_reason, 0.0) + t.pnl
        return out

    def log_trades(self) -> str:
        """Print a compact trade log — side, entry→exit, P&L, outcome."""
        if not self.closed:
            return "(no closed trades)"
        lines = ["", f"  TRADE LOG ({len(self.closed)} trades)"] + [
            f"  {t.side:<4} {t.entry_price:.3f} -> {t.exit_price:.3f}  "
            f"${t.pnl:+,.2f}  {'WON ' if t.won else 'LOST'} "
            f"({t.exit_reason})  held {t.held_seconds:.0f}s"
            for t in self.closed
        ]
        return "\n".join(lines)

    def render(self) -> str:
        wins = sum(1 for t in self.closed if t.won)
        losses = len(self.closed) - wins
        lines = [
            "-" * 58,
            "PORTFOLIO",
            "-" * 58,
            f"cash              : ${self.cash:,.2f}",
            f"open positions    : {self.open_count} (${self.position_value:,.2f})",
            f"equity            : ${self.equity:,.2f}",
            f"realized P&L      : ${self.realized_pnl:+,.2f}",
            f"unrealized P&L    : ${self.unrealized_pnl:+,.2f}",
            f"total P&L         : ${self.total_pnl:+,.2f} "
            f"({self.total_pnl / self.starting_cash:+.2%})",
            f"wins/losses       : {wins}W / {losses}L",
            f"max drawdown      : ${self.max_drawdown:,.2f}",
            f"closed trades     : {len(self.closed)}",
        ]
        counts = self.exit_reason_counts()
        if counts:
            pnl = self.pnl_by_exit_reason()
            lines.append("exits by reason   :")
            for reason in sorted(counts, key=lambda r: -counts[r]):
                lines.append(
                    f"  {reason:<14} {counts[reason]:>4}  ${pnl[reason]:+,.2f}"
                )
        for pos in self.positions.values():
            lines.append(
                f"  HOLDING {pos.slug} {pos.side} {pos.shares:.2f} @ "
                f"{pos.entry_price:.3f} mark {(pos.last_mark or 0):.3f} "
                f"({pos.unrealized():+.2f})"
            )
        lines.append("-" * 58)
        return "\n".join(lines)


def mark_for(snap, side: str) -> Optional[float]:
    """What a position on `side` would liquidate for right now.

    Uses the best bid: that is the price someone will actually pay you. Marking
    at the mid overstates equity and would let a stop loss believe it exited at
    a price it could not get.
    """
    book = snap.book(side)
    bid = book.best_bid
    if bid is not None:
        return bid
    # Fall back to the opposite book's ask complement if this side has no bids.
    other = snap.book(DOWN if side == UP else UP)
    ask = other.best_ask
    return (1.0 - ask) if ask is not None else None
