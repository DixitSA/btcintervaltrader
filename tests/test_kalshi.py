"""Kalshi adapter and fee model tests.

The orderbook test is the important one. Kalshi publishes only BIDS on both
sides; each side's ask must be synthesised as (1 - opposite bid). Reading the
`no` array as the yes-ask inverts every price the bot computes, and would do so
silently.
"""

from __future__ import annotations

import math

import pytest

from btcbot.config import Config, FeeConfig
from btcbot.fees import KalshiFees, PolymarketFees, build_fee_model
from btcbot.models import DOWN, UP
from btcbot.venues.kalshi import (
    KalshiVenue,
    cents_to_prob,
    parse_market,
    parse_orderbook,
    parse_strike,
    parse_ticker,
)


# -- orderbook -------------------------------------------------------


def test_orderbook_synthesises_asks_from_the_opposite_side():
    """yes_ask = 1 - best_no_bid, and vice versa."""
    raw = {
        "orderbook": {
            # Kalshi sends cents, ascending (highest bid last).
            "yes": [[40, 100], [42, 50]],
            "no": [[54, 70], [56, 30]],
        }
    }
    yes, no = parse_orderbook(raw)

    assert yes.best_bid == pytest.approx(0.42)
    # Best no bid is 56c -> yes ask is 44c.
    assert yes.best_ask == pytest.approx(0.44)
    assert no.best_bid == pytest.approx(0.56)
    # Best yes bid is 42c -> no ask is 58c.
    assert no.best_ask == pytest.approx(0.58)


def test_orderbook_yes_and_no_are_complementary():
    raw = {"orderbook": {"yes": [[30, 10]], "no": [[65, 10]]}}
    yes, no = parse_orderbook(raw)
    assert yes.best_bid + no.best_ask == pytest.approx(1.0)
    assert no.best_bid + yes.best_ask == pytest.approx(1.0)


def test_orderbook_bids_are_sorted_best_first():
    raw = {"orderbook": {"yes": [[10, 1], [40, 1], [25, 1]], "no": [[50, 1]]}}
    yes, _ = parse_orderbook(raw)
    assert [lv.price for lv in yes.bids] == pytest.approx([0.40, 0.25, 0.10])


def test_orderbook_asks_are_sorted_cheapest_first():
    raw = {"orderbook": {"yes": [[10, 1]], "no": [[50, 1], [30, 1], [70, 1]]}}
    yes, _ = parse_orderbook(raw)
    # no bids 70/50/30 -> yes asks 30/50/70
    assert [lv.price for lv in yes.asks] == pytest.approx([0.30, 0.50, 0.70])


def test_orderbook_handles_dollar_string_format():
    """Newer responses use *_dollars string pairs instead of cent integers."""
    raw = {
        "orderbook": {
            "yes_dollars": [["0.42", "50"]],
            "no_dollars": [["0.56", "30"]],
        }
    }
    yes, no = parse_orderbook(raw)
    assert yes.best_bid == pytest.approx(0.42)
    assert yes.best_ask == pytest.approx(0.44)


def test_orderbook_drops_zero_size_levels():
    raw = {"orderbook": {"yes": [[40, 0], [38, 5]], "no": [[55, 5]]}}
    yes, _ = parse_orderbook(raw)
    assert yes.best_bid == pytest.approx(0.38)


def test_orderbook_survives_an_empty_side():
    raw = {"orderbook": {"yes": [], "no": [[55, 5]]}}
    yes, no = parse_orderbook(raw)
    assert yes.best_bid is None
    assert yes.best_ask == pytest.approx(0.45)
    assert no.asks == []


def test_cents_to_prob():
    assert cents_to_prob(42) == pytest.approx(0.42)


# -- market parsing --------------------------------------------------


def test_parse_market_from_kalshi_fields():
    raw = {
        "ticker": "KXBTC15M-26JUL281745",
        "title": "Bitcoin price up or down?",
        "subtitle": "Above $118,240.55 at 5:45pm",
        "open_time": "2026-07-28T17:30:00Z",
        "close_time": "2026-07-28T17:45:00Z",
        "dollar_volume": 12345.0,
        "open_interest": 500,
    }
    m = parse_market(raw)
    assert m is not None
    assert m.slug == "KXBTC15M-26JUL281745"
    assert m.end_ts - m.start_ts == pytest.approx(900.0)
    assert m.strike == pytest.approx(118_240.55)
    assert m.volume == pytest.approx(12345.0)
    # Kalshi is one market with two sides, so both token ids are the ticker.
    assert m.token_id(UP) == m.token_id(DOWN) == "KXBTC15M-26JUL281745"


def test_parse_market_requires_a_close_time():
    assert parse_market({"ticker": "KXBTC15M-1"}) is None


def test_parse_market_requires_a_ticker():
    assert parse_market({"close_time": "2026-07-28T17:45:00Z"}) is None


def test_parse_strike_prefers_numeric_field():
    assert parse_strike({"floor_strike": 118_000.0}) == pytest.approx(118_000.0)


def test_parse_strike_falls_back_to_text():
    assert parse_strike({"subtitle": "Above $95,000.00"}) == pytest.approx(95_000.0)


def test_parse_strike_none_when_ambiguous():
    assert parse_strike({"subtitle": "between $90,000 and $95,000"}) is None
    assert parse_strike({"subtitle": "no numbers"}) is None


def test_parse_ticker_splits_series_and_time():
    parts = parse_ticker("KXBTC15M-26JUL281745")
    assert parts["series"] == "KXBTC15M"
    assert parts["date"] == "26JUL28"
    assert parts["time"] == "1745"


def test_parse_ticker_tolerates_unknown_shapes():
    assert parse_ticker("WEIRD")["series"] == "WEIRD"


# -- discovery -------------------------------------------------------


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_discover_filters_to_requested_series():
    markets = {
        "markets": [
            {"ticker": "KXBTC15M-26JUL281745", "close_time": "2026-07-28T17:45:00Z"},
            {"ticker": "KXETHD-26JUL28", "close_time": "2026-07-28T17:45:00Z"},
        ]
    }
    venue = KalshiVenue()
    venue._http = type("H", (), {"request": lambda self, *a, **k: FakeResp(markets)})()

    found = venue.discover_markets(["KXBTC15M"])
    assert len(found) == 1
    assert found[0].slug == "KXBTC15M-26JUL281745"


def test_discover_deduplicates_across_series():
    payload = {
        "markets": [
            {"ticker": "KXBTC15M-1", "close_time": "2026-07-28T17:45:00Z"},
        ]
    }
    venue = KalshiVenue()
    venue._http = type("H", (), {"request": lambda self, *a, **k: FakeResp(payload)})()
    found = venue.discover_markets(["KXBTC15M", "KXBTC15M"])
    assert len(found) == 1


# -- order construction ----------------------------------------------


def test_place_order_maps_up_to_yes_and_uses_cents():
    captured = {}

    class Recorder:
        def request(self, method, url, headers=None, **kwargs):
            captured["method"] = method
            captured["json"] = kwargs.get("json")
            return FakeResp({"order": {"order_id": "x"}})

    venue = KalshiVenue()
    venue._http = Recorder()
    venue.auth = type("A", (), {"headers": lambda self, m, p: {}})()

    from btcbot.models import Market, Order

    market = Market(
        condition_id="KXBTC15M-1",
        slug="KXBTC15M-1",
        question="q",
        up_token_id="KXBTC15M-1",
        down_token_id="KXBTC15M-1",
        start_ts=0,
        end_ts=900,
    )
    venue.place_order(market, Order(side=UP, shares=7, limit_price=0.42), selling=False)

    body = captured["json"]
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["yes_price"] == 42     # cents, not dollars
    assert body["count"] == 7
    assert body["time_in_force"] == "immediate_or_cancel"


def test_place_order_maps_down_to_no():
    captured = {}

    class Recorder:
        def request(self, method, url, headers=None, **kwargs):
            captured["json"] = kwargs.get("json")
            return FakeResp({})

    venue = KalshiVenue()
    venue._http = Recorder()
    venue.auth = type("A", (), {"headers": lambda self, m, p: {}})()

    from btcbot.models import Market, Order

    market = Market("t", "t", "q", "t", "t", 0, 900)
    venue.place_order(market, Order(side=DOWN, shares=3, limit_price=0.58), selling=True)

    body = captured["json"]
    assert body["side"] == "no"
    assert body["action"] == "sell"
    assert body["no_price"] == 58


def test_authed_request_without_credentials_is_a_clear_error():
    venue = KalshiVenue(auth=None)
    from btcbot.models import Market, Order

    market = Market("t", "t", "q", "t", "t", 0, 900)
    with pytest.raises(RuntimeError, match="credentials"):
        venue.place_order(market, Order(side=UP, shares=1, limit_price=0.5), selling=False)


# -- fees ------------------------------------------------------------


def test_kalshi_fee_matches_published_formula():
    """fee = ceil(0.07 * C * P * (1-P) * 100) / 100"""
    fees = KalshiFees()
    for shares, price in ((100, 0.50), (37, 0.23), (1000, 0.91)):
        expected = math.ceil(0.07 * shares * price * (1 - price) * 100) / 100
        assert fees.entry_fee(shares, price) == pytest.approx(expected)


def test_kalshi_fee_peaks_at_fifty_cents():
    fees = KalshiFees()
    per_contract = lambda p: fees.entry_fee(10_000, p) / 10_000
    assert per_contract(0.50) == pytest.approx(0.0175, abs=1e-4)
    assert per_contract(0.50) > per_contract(0.20)
    assert per_contract(0.50) > per_contract(0.80)


def test_kalshi_fee_rounds_up_to_the_cent():
    """Small orders pay the rounding, which is a real cost."""
    fees = KalshiFees()
    assert fees.entry_fee(1, 0.50) == pytest.approx(0.02)  # 0.0175 -> 2c


def test_kalshi_charges_nothing_extra_at_settlement():
    fees = KalshiFees()
    assert fees.settlement_fee(100, 0.50, won=True) == 0.0
    assert fees.settlement_fee(100, 0.50, won=False) == 0.0


def test_kalshi_fee_is_zero_for_no_shares():
    assert KalshiFees().entry_fee(0, 0.5) == 0.0


def test_polymarket_charges_on_profit_not_entry():
    fees = PolymarketFees(taker_fee_bps=0.0, winnings_fee_bps=200.0)
    assert fees.entry_fee(100, 0.50) == 0.0
    # Win: $50 profit, 2% = $1.
    assert fees.settlement_fee(100, 0.50, won=True) == pytest.approx(1.0)
    assert fees.settlement_fee(100, 0.50, won=False) == 0.0


def test_kalshi_entry_cost_dwarfs_polymarket_at_the_money():
    """The reason a strategy tuned on one venue can be nonsense on the other."""
    shares, price = 100, 0.50
    kalshi = KalshiFees().entry_fee(shares, price)
    poly = PolymarketFees().entry_fee(shares, price)
    assert kalshi > poly
    # 1.75c/contract on a 50c stake is 3.5% of notional, paid up front.
    assert kalshi / (shares * price) == pytest.approx(0.035, abs=1e-3)


def test_build_fee_model_selects_by_venue():
    cfg = FeeConfig()
    assert isinstance(build_fee_model("kalshi", cfg), KalshiFees)
    assert isinstance(build_fee_model("polymarket", cfg), PolymarketFees)


def test_default_config_venue_is_kalshi():
    assert Config().venue == "kalshi"
    assert isinstance(build_fee_model(Config().venue, FeeConfig()), KalshiFees)
