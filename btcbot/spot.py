"""Spot price feeds.

Manages per-asset feeds (BTC, ETH, SOL) from Binance.
IMPORTANT: this feed is for *signals only*. It is almost certainly NOT the
source Kalshi settles against. Before risking real money, confirm which
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
            log.warning("spot price fetch failed (%s): %s", self.symbol, exc)
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
        return realized_vol_from_series(self._history, lookback_seconds)


class SpotFeedManager:
    """Manages a SpotFeed per asset family, lazy-created on first use."""

    def __init__(self, families: dict[str, Any], base_url: str) -> None:
        self._families = families
        self._base_url = base_url.rstrip("/")
        self._feeds: dict[str, SpotFeed] = {}

    def _feed(self, symbol: str) -> SpotFeed:
        if symbol not in self._feeds:
            self._feeds[symbol] = SpotFeed(base_url=self._base_url, symbol=symbol)
        return self._feeds[symbol]

    def price(self, family: str) -> Optional[float]:
        fam = self._families.get(family)
        if fam is None:
            return None
        return self._feed(fam.spot_symbol).price()

    def realized_vol(self, family: str, window: float) -> Optional[float]:
        fam = self._families.get(family)
        if fam is None:
            return None
        return self._feed(fam.spot_symbol).realized_vol(window)

    def klines(self, family: str, interval: str = "1m", limit: int = 1000) -> list[dict[str, float]]:
        fam = self._families.get(family)
        if fam is None:
            return []
        return self._feed(fam.spot_symbol).klines(interval, limit)

    def all_prices(self) -> dict[str, Optional[float]]:
        return {k: self.price(k) for k in self._families}

    def close(self) -> None:
        for feed in self._feeds.values():
            feed.close()

    # -- backward-compat helpers ----------------------------------------

    @property
    def first_feed(self) -> SpotFeed:
        for fam in self._families.values():
            return self._feed(fam.spot_symbol)
        raise RuntimeError("no families configured")

    @property
    def first_price(self) -> Optional[float]:
        for fam in self._families.values():
            return self._feed(fam.spot_symbol).price()
        return None
