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


def test_fixture_has_no_error():
    data = load_fixture()
    assert "error" not in data, f"capture failed: {data.get('error')}"


def test_every_market_parses():
    data = load_fixture()
    seen = 0
    for entry in data.get("markets_raw", []):
        for raw in entry["response"].get("markets", []) or []:
            market = parse_market(raw)
            assert market is not None, f"failed to parse market: {raw.get('ticker')}"
            assert market.slug
            assert market.end_ts > market.start_ts
            seen += 1
    assert seen > 0, "fixture contained no markets"


def test_window_durations_look_like_the_series():
    """A 15-minute series should produce ~900s windows."""
    data = load_fixture()
    for entry in data.get("markets_raw", []):
        if "15M" not in entry["series"].upper():
            continue
        for raw in entry["response"].get("markets", []) or []:
            market = parse_market(raw)
            if market is None:
                continue
            span = market.end_ts - market.start_ts
            assert 60 <= span <= 3600, f"{market.slug} span {span}s is not window-like"


def test_strikes_parse_from_real_market_text():
    """If this fails, model-based strategies will skip every market."""
    data = load_fixture()
    markets, with_strike = 0, 0
    for entry in data.get("markets_raw", []):
        for raw in entry["response"].get("markets", []) or []:
            market = parse_market(raw)
            if market is None:
                continue
            markets += 1
            if market.strike is not None:
                with_strike += 1
    if markets:
        assert with_strike > 0, (
            "no strike parsed from any real market -- parse_strike needs the "
            "actual field or text format from the fixture"
        )


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
