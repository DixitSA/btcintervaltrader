"""Feature computation and a baseline fair-value model."""

from __future__ import annotations

import math
from typing import Optional

from .models import DOWN, UP, Book, Snapshot


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


def realized_vol_from_series(
    points, lookback_seconds: float
) -> Optional[float]:
    """Stdev of log returns over the lookback, scaled to that whole window.

    `points` is (timestamp, price), oldest first. "Now" is the last point's
    timestamp rather than the wall clock, so this works unchanged when
    replaying recorded history.

    The result is the volatility OVER `lookback_seconds`; pass it and the SAME
    lookback to `annualize`. One implementation shared by the live runner and
    the backtester is deliberate -- if those two measured vol differently the
    backtest would silently be describing a strategy that never runs.

    Log returns, not simple returns, because fair_probability_up prices a
    geometric walk in log space.
    """
    pts_all = list(points)
    if not pts_all or lookback_seconds <= 0:
        return None

    cutoff = pts_all[-1][0] - lookback_seconds
    kept = [(ts, px) for ts, px in pts_all if ts >= cutoff and px > 0]
    if len(kept) < 3:
        return None

    # The points may not actually SPAN the lookback -- during warmup there are
    # only a few seconds of history. Scaling that to the full window as though
    # it did understates vol by sqrt(lookback/span), which for 60s of history
    # against a 900s window is a factor of ~3.9. Refuse rather than return a
    # number that is wrong by multiples.
    span = kept[-1][0] - kept[0][0]
    if span < 0.5 * lookback_seconds:
        return None

    prices = [px for _, px in kept]
    rets = [math.log(cur / prev) for prev, cur in zip(prices, prices[1:])]
    if len(rets) < 2:
        return None

    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None

    # Volatility over the span actually covered, then rescaled to the window
    # that was asked for. sqrt-of-time, so any residual gap is corrected
    # rather than silently absorbed.
    vol_over_span = var**0.5 * (len(rets) ** 0.5)
    return vol_over_span * math.sqrt(lookback_seconds / span)


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


def weighted_mid(book: Book, depth: int = 1) -> Optional[float]:
    """Size-weighted mid ("microprice"), the first-order Stoikov estimator.

        I = bid_size / (bid_size + ask_size)
        weighted_mid = I * ask + (1 - I) * bid

    The mid assumes the next trade is equally likely to hit either side. When
    resting size is lopsided it is not: a book showing 500 contracts bid and 20
    offered is far more likely to trade up than down, and the mid systematically
    understates where it will go. The weight leans the estimate toward the THIN
    side, which is the side about to be consumed.

    Reduces to the mid exactly when the two sides are balanced, so it is never
    worse than the mid on a symmetric book -- only different on a skewed one.

    `depth` levels are aggregated per side. The estimator is defined at
    top-of-book (depth=1) and that is the default; deeper aggregation trades
    responsiveness for stability on thin books.

    Reference: Stoikov, "The Micro-Price" (2018), via the Orderbook section of
    awesome-systematic-trading -- see docs/systematic-trading.md.
    """
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None:
        return None
    if depth < 1:
        raise ValueError("depth must be >= 1")

    bid_sz = sum(lv.size for lv in book.bids[:depth])
    ask_sz = sum(lv.size for lv in book.asks[:depth])
    total = bid_sz + ask_sz
    if total <= 0:
        # Sizes missing or zero -- no imbalance information, so the mid is the
        # honest answer rather than an arbitrary lean.
        return (bid + ask) / 2.0

    imbalance = bid_sz / total
    return imbalance * ask + (1.0 - imbalance) * bid


def microprice_up(snap: Snapshot, depth: int = 1) -> Optional[float]:
    """Market's implied P(Up), estimated by microprice instead of mid.

    Same both-books averaging as `market_implied_up`, with each mid replaced by
    the size-weighted mid.

    On Kalshi this is better founded than it first looks. The book is bid-only:
    the Up ask is DERIVED from the best Down bid, so "Up ask size" literally IS
    the resting Down interest. The imbalance being measured is therefore
    genuinely "how much size wants Up versus how much wants Down", not an
    artifact of one venue's quoting convention.

    Two things it does not do. It does not create edge -- it sharpens the
    estimate of what the market thinks, which usually SHRINKS the gap a
    model-vs-market strategy sees, and shrinking a spurious gap is the point.
    And it is computed from resting size, the cheapest quantity on a book to
    fake; on a thin 15-minute binary a single large resting order moves it
    several points. Prefer it to the mid, do not trust it as a signal on its own.
    """
    up_wm = weighted_mid(snap.up_book, depth)
    down_wm = weighted_mid(snap.down_book, depth)
    if up_wm is None and down_wm is None:
        return None
    if up_wm is None:
        return 1.0 - float(down_wm)
    if down_wm is None:
        return float(up_wm)
    return (up_wm + (1.0 - down_wm)) / 2.0


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
