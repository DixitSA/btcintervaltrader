"""Polymarket venue adapter, wrapping the existing Gamma + CLOB clients."""

from __future__ import annotations

import logging
from typing import Any, Optional

from ..clob import ClobClient, LiveOrderClient
from ..markets import GammaClient
from ..models import Book, Market, Order

log = logging.getLogger(__name__)


class PolymarketVenue:
    name = "polymarket"

    def __init__(
        self,
        gamma_url: str = "https://gamma-api.polymarket.com",
        clob_url: str = "https://clob.polymarket.com",
        timeout: float = 10.0,
    ):
        self.gamma = GammaClient(gamma_url, timeout=timeout)
        self.clob = ClobClient(clob_url, timeout=timeout)
        self.clob_url = clob_url
        self._orders: Optional[LiveOrderClient] = None

    def close(self) -> None:
        self.gamma.close()
        self.clob.close()

    def discover_markets(
        self,
        prefixes: list[str],
        strike_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> list[Market]:
        return self.gamma.fetch_open_markets(prefixes, strike_bounds=strike_bounds)

    def get_books(self, market: Market) -> Optional[tuple[Book, Book]]:
        books = self.clob.get_books([market.up_token_id, market.down_token_id])
        up = books.get(market.up_token_id)
        down = books.get(market.down_token_id)
        if up is None or down is None:
            return None
        return up, down

    def place_order(self, market: Market, order: Order, selling: bool) -> dict[str, Any]:
        if self._orders is None:
            self._orders = LiveOrderClient(host=self.clob_url)
        return self._orders.submit(market.token_id(order.side), order, selling=selling)
