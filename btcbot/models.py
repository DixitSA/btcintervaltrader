"""Core data types shared across the bot.

Kept dependency-free so the strategy/risk/backtest layers can be unit tested
without any network access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

UP = "Up"
DOWN = "Down"


@dataclass(frozen=True)
class Market:
    """A single Polymarket binary Up/Down window."""

    condition_id: str
    slug: str
    question: str
    up_token_id: str
    down_token_id: str
    start_ts: float
    end_ts: float
    volume: float = 0.0
    liquidity: float = 0.0
    # The "price to beat". Polymarket publishes this in the market text; we parse
    # it best-effort, so it can be None. Never trade on a None strike.
    strike: Optional[float] = None

    def token_id(self, side: str) -> str:
        return self.up_token_id if side == UP else self.down_token_id

    def seconds_remaining(self, now: float) -> float:
        return self.end_ts - now


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Book:
    """One side's order book. Bids descending, asks ascending."""

    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    def sweep_cost(self, shares: float) -> Optional[float]:
        """Average fill price to buy `shares` by taking the ask side.

        Returns None if the book cannot fill the whole order -- callers must
        treat that as "do not trade", not as "fill at the last price".
        """
        if shares <= 0:
            return 0.0
        remaining = shares
        notional = 0.0
        for lvl in self.asks:
            take = min(remaining, lvl.size)
            notional += take * lvl.price
            remaining -= take
            if remaining <= 1e-9:
                return notional / shares
        return None


@dataclass
class Snapshot:
    """Everything the strategy layer is allowed to see at one instant.

    The backtester replays recorded Snapshots through the exact same strategy
    code the live bot runs, so a backtest cannot accidentally use information
    the live bot would not have had.
    """

    ts: float
    market: Market
    up_book: Book
    down_book: Book
    spot: Optional[float] = None
    # Cumulative matched volume in THIS window, in USD.
    window_volume: float = 0.0

    def book(self, side: str) -> Book:
        return self.up_book if side == UP else self.down_book


@dataclass(frozen=True)
class Order:
    side: str  # UP or DOWN
    shares: float
    limit_price: float
    reason: str = ""


@dataclass
class Fill:
    ts: float
    market_slug: str
    side: str
    shares: float
    price: float
    fee: float = 0.0

    @property
    def cost(self) -> float:
        return self.shares * self.price + self.fee


@dataclass
class Position:
    market_slug: str
    side: str
    shares: float
    avg_price: float
    fees_paid: float = 0.0

    def payout(self, winning_side: Optional[str], winnings_fee_bps: float = 0.0) -> float:
        """Gross USD returned at resolution. Each winning share pays $1."""
        if winning_side is None:
            # Void / unresolvable window: assume stake returned.
            return self.shares * self.avg_price
        if winning_side != self.side:
            return 0.0
        gross = self.shares * 1.0
        profit = max(0.0, gross - self.shares * self.avg_price)
        return gross - profit * (winnings_fee_bps / 10_000.0)
