"""Validate parsers against REAL captured API responses.

The machine this code was written on cannot reach Kalshi (network policy), so
every parser here was built from published documentation. This module closes
that gap without needing network access at build time:

    1. On a machine that can reach the venue:
           python -m btcbot verify-venue --dump fixtures/kalshi.json
    2. Commit fixtures/kalshi.json (public market data only).
    3. These tests then run against genuine payloads instead of assumptions.

They skip cleanly until a fixture exists, so the suite stays green either way.
A skipped test here is a REMINDER, not a pass -- the wire format is unconfirmed
for as long as this is skipping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcbot.config import strike_bounds_for_prefix
from btcbot.venues.kalshi import parse_market, parse_orderbook

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
KALSHI_FIXTURE = FIXTURE_DIR / "kalshi.json"


def load_fixture():
    if not KALSHI_FIXTURE.exists():
        pytest.skip(
            "no captured fixture. Run `python -m btcbot verify-venue "
            f"--dump {KALSHI_FIXTURE}` where the venue is reachable, and commit "
            "it. Until then the Kalshi wire format is UNCONFIRMED."
        )
    return json.loads(KALSHI_FIXTURE.read_text())


def iter_fixture_markets(data):
    """Yield (series, raw, parsed) using the same per-asset strike bounds that
    discovery uses in production.

    Parsing the fixture with default (BTC) bounds instead is what let a
    SOL-strike regression sit green: $72 falls under BTC's $1,000 floor.
    """
    for entry in data.get("markets_raw", []):
        series = entry["series"]
        lo, hi = strike_bounds_for_prefix(series)
        for raw in entry["response"].get("markets", []) or []:
            yield series, raw, parse_market(raw, strike_min=lo, strike_max=hi)


def test_fixture_has_no_error():
    data = load_fixture()
    assert "error" not in data, f"capture failed: {data.get('error')}"


def test_every_market_parses():
    data = load_fixture()
    seen = 0
    for _series, raw, market in iter_fixture_markets(data):
        assert market is not None, f"failed to parse market: {raw.get('ticker')}"
        assert market.slug
        assert market.end_ts > market.start_ts
        seen += 1
    assert seen > 0, "fixture contained no markets"


def test_window_durations_look_like_the_series():
    """A 15-minute series should produce ~900s windows."""
    data = load_fixture()
    for series, _raw, market in iter_fixture_markets(data):
        if "15M" not in series.upper() or market is None:
            continue
        span = market.end_ts - market.start_ts
        assert 60 <= span <= 3600, f"{market.slug} span {span}s is not window-like"


def test_every_market_has_a_strike():
    """EVERY market must yield a strike, not just one of them.

    A market with strike=None cannot be settled (runner._settle_expired needs
    spot vs strike) and every model-based signal on it is dead. Asserting only
    that *some* strike parsed hid a total SOL failure behind a working BTC one.
    """
    data = load_fixture()
    missing = [
        raw.get("ticker")
        for _series, raw, market in iter_fixture_markets(data)
        if market is not None and market.strike is None
    ]
    assert not missing, (
        f"no strike parsed for {missing} -- check the strike bounds for that "
        "asset in markets.families (BTC's $1k floor rejects SOL strikes)"
    )


def test_strike_is_in_the_right_decade_for_the_asset():
    """Guards against an asset being parsed against another's bounds.

    A strike outside the asset's own plausible range means either the bounds
    are wrong or the wrong field was read -- both silently invert signals.
    """
    data = load_fixture()
    checked = 0
    for series, raw, market in iter_fixture_markets(data):
        if market is None or market.strike is None:
            continue
        lo, hi = strike_bounds_for_prefix(series)
        assert lo <= market.strike <= hi, (
            f"{raw.get('ticker')} strike {market.strike} outside {series} "
            f"bounds ({lo}, {hi})"
        )
        checked += 1
    assert checked > 0, "fixture produced no strikes to check"


def test_btc_bounds_reject_small_asset_strikes():
    """Locks in WHY bounds must be per-asset rather than global.

    Re-parsing every fixture market with BTC's bounds must lose any strike that
    sits below BTC's floor. If this ever stops failing, the per-asset bounds
    have quietly stopped mattering and the SOL regression can return.
    """
    data = load_fixture()
    btc_lo, btc_hi = strike_bounds_for_prefix("BTC")
    small = []
    for series, raw, market in iter_fixture_markets(data):
        if market is None or market.strike is None or market.strike >= btc_lo:
            continue
        small.append(series)
        under_btc = parse_market(raw, strike_min=btc_lo, strike_max=btc_hi)
        assert under_btc is not None and under_btc.strike is None, (
            f"{raw.get('ticker')} strike {market.strike} is below BTC's floor "
            f"{btc_lo} yet parsed under BTC bounds -- the bounds are not being applied"
        )
    if not small:
        pytest.skip("fixture has no sub-$1000 strikes (capture SOL to cover this)")


def test_orderbooks_parse_and_are_complementary():
    """The bid-only invariant, checked against real books.

    up_bid + down_ask must equal 1.0 exactly, because down_ask is DERIVED from
    up_bid. If this fails on real data, the ask synthesis is wrong and every
    price the bot computes is wrong with it.
    """
    data = load_fixture()
    books = data.get("orderbooks_raw", {})
    if not books:
        pytest.skip("fixture contained no orderbooks")

    for ticker, raw in books.items():
        up, down = parse_orderbook(raw)

        if up.best_bid is not None and down.best_ask is not None:
            assert up.best_bid + down.best_ask == pytest.approx(1.0), ticker
        if down.best_bid is not None and up.best_ask is not None:
            assert down.best_bid + up.best_ask == pytest.approx(1.0), ticker

        for book, name in ((up, "up"), (down, "down")):
            for level in book.bids + book.asks:
                assert 0.0 <= level.price <= 1.0, f"{ticker} {name} price out of range"
                assert level.size > 0, f"{ticker} {name} non-positive size"


def test_books_are_not_crossed():
    """Best bid above best ask would mean the sides are swapped."""
    data = load_fixture()
    for ticker, raw in (data.get("orderbooks_raw") or {}).items():
        for book, name in zip(parse_orderbook(raw), ("up", "down")):
            if book.best_bid is not None and book.best_ask is not None:
                assert book.best_bid <= book.best_ask, (
                    f"{ticker} {name} book is crossed: bid {book.best_bid} > "
                    f"ask {book.best_ask} -- bids and asks are likely swapped"
                )
