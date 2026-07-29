"""BTC spot price feed.

IMPORTANT: this feed is for *signals only*. It is almost certainly NOT the
source Polymarket settles against. Before risking real money, confirm which
oracle and which exact timestamp resolves these windows -- a feed that differs
by even a few dollars near expiry will flip the outcome of exactly the trades
you thought were safest.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Optional

import httpx

from .signals import realized_vol_from_series

log = logging.getLogger(__name__)


class SpotFeed:
    """Polls a spot price and keeps a short rolling history for signals."""

    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        symbol: str = "BTCUSDT",
        history: int = 600,
        timeout: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.symbol = symbol
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "btcintervaltrader/0.1"})
        self._history: deque[tuple[float, float]] = deque(maxlen=history)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "SpotFeed":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def price(self) -> Optional[float]:
        try:
            resp = self._http.get(
                f"{self.base_url}/api/v3/ticker/price", params={"symbol": self.symbol}
            )
            resp.raise_for_status()
            px = float(resp.json()["price"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.warning("spot price fetch failed: %s", exc)
            return None
        self._history.append((time.time(), px))
        return px

    def klines(self, interval: str = "1m", limit: int = 1000) -> list[dict[str, float]]:
        """Historical candles, used to build backtest datasets."""
        resp = self._http.get(
            f"{self.base_url}/api/v3/klines",
            params={"symbol": self.symbol, "interval": interval, "limit": limit},
        )
        resp.raise_for_status()
        return [
            {
                "open_ts": row[0] / 1000.0,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_ts": row[6] / 1000.0,
            }
            for row in resp.json()
        ]

    def realized_vol(self, lookback_seconds: float = 300.0) -> Optional[float]:
        """Stdev of log returns over the lookback, scaled to that window.

        Delegates to the shared implementation so the live path and the
        backtester cannot drift apart.
        """
        return realized_vol_from_series(self._history, lookback_seconds)
