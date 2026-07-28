"""Feature computation and a baseline fair-value model."""

from __future__ import annotations

import math
from typing import Optional

from .models import DOWN, UP, Snapshot


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def fair_probability_up(
    spot: float,
    strike: float,
    seconds_remaining: float,
    vol_per_year: float,
) -> Optional[float]:
    """P(spot at expiry > strike) under a zero-drift lognormal walk.

    Zero drift is the right default here: over 15 minutes any realistic BTC
    drift is swamped by volatility, and assuming otherwise is how a model
    talks itself into a directional edge it does not have.
    """
    if spot <= 0 or strike <= 0 or vol_per_year <= 0:
        return None
    if seconds_remaining <= 0:
        return 1.0 if spot > strike else 0.0

    t = seconds_remaining / (365.0 * 24.0 * 3600.0)
    sigma_t = vol_per_year * math.sqrt(t)
    if sigma_t <= 1e-12:
        return 1.0 if spot > strike else 0.0

    d2 = (math.log(spot / strike) - 0.5 * sigma_t**2) / sigma_t
    return normal_cdf(d2)


def annualize(window_vol: float, window_seconds: float) -> Optional[float]:
    """Scale a realized vol measured over `window_seconds` to annual terms."""
    if window_vol <= 0 or window_seconds <= 0:
        return None
    periods_per_year = (365.0 * 24.0 * 3600.0) / window_seconds
    return window_vol * math.sqrt(periods_per_year)


def market_implied_up(snap: Snapshot) -> Optional[float]:
    """Market's implied P(Up), de-noised across both books.

    The Up ask and the Down bid describe the same probability from two sides;
    averaging them is more robust than trusting either book alone when one is
    thin.
    """
    up_mid = snap.up_book.mid
    down_mid = snap.down_book.mid
    if up_mid is None and down_mid is None:
        return None
    if up_mid is None:
        return 1.0 - float(down_mid)
    if down_mid is None:
        return float(up_mid)
    return (up_mid + (1.0 - down_mid)) / 2.0


def book_imbalance(snap: Snapshot, side: str, depth: int = 5) -> Optional[float]:
    """(bid size - ask size) / total, over the top `depth` levels. In [-1, 1]."""
    book = snap.book(side)
    bid_sz = sum(lv.size for lv in book.bids[:depth])
    ask_sz = sum(lv.size for lv in book.asks[:depth])
    total = bid_sz + ask_sz
    if total <= 0:
        return None
    return (bid_sz - ask_sz) / total


def favored_side(snap: Snapshot) -> Optional[str]:
    """Whichever side the market currently prices above 50%."""
    p_up = market_implied_up(snap)
    if p_up is None:
        return None
    if abs(p_up - 0.5) < 1e-9:
        return None
    return UP if p_up > 0.5 else DOWN
