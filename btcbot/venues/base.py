"""Venue abstraction.

Everything above this line is venue-neutral: strategies, risk sizing, the
portfolio, exits and the backtester all operate on `Market`, `Book` and
`Snapshot`. A venue's job is only to produce those and to place orders.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from ..models import Book, Market, Order


class Venue(Protocol):
    name: str

    def discover_markets(
        self,
        prefixes: list[str],
        strike_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> list[Market]:
        """Open windows whose identifier starts with any given prefix.

        `strike_bounds` maps prefix -> (min, max) plausible strike so each asset
        is parsed against its own range rather than BTC's.
        """
        ...

    def get_books(self, market: Market) -> Optional[tuple[Book, Book]]:
        """(up_book, down_book) for this market, or None if unavailable."""
        ...

    def place_order(self, market: Market, order: Order, selling: bool) -> dict[str, Any]:
        """Submit a real order. Raises if the venue is not configured for it."""
        ...

    def close(self) -> None: ...
