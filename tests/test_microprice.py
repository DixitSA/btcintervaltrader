"""Size-weighted mid: leans toward the thin side, and never invents edge."""

import pytest

from btcbot.models import DOWN, UP, Book, Level, Market, Snapshot
from btcbot.signals import market_implied_up, microprice_up, weighted_mid
from btcbot.strategies import build_strategy


def book(bid, bid_sz, ask, ask_sz):
    return Book(bids=[Level(bid, bid_sz)], asks=[Level(ask, ask_sz)])


def market(strike=100_000.0, start=0.0, end=900.0):
    return Market(
        condition_id="c",
        slug="btc-updown-15m-x",
        question="?",
        up_token_id="u",
        down_token_id="d",
        start_ts=start,
        end_ts=end,
        strike=strike,
    )


def snapshot(up: Book, down: Book, ts=0.0, spot=100_000.0):
    return Snapshot(ts=ts, market=market(), up_book=up, down_book=down, spot=spot)


def test_balanced_book_reduces_to_the_mid():
    b = book(0.40, 100, 0.60, 100)
    assert weighted_mid(b) == pytest.approx(b.mid)


def test_heavy_bid_pushes_the_estimate_toward_the_ask():
    """500 bid against 20 offered: the next trade almost certainly lifts."""
    b = book(0.40, 500, 0.60, 20)
    wm = weighted_mid(b)
    assert wm > b.mid
    assert wm < b.best_ask
    # I = 500/520 = 0.9615 -> 0.40 + 0.9615 * 0.20
    assert wm == pytest.approx(0.40 + (500 / 520) * 0.20)


def test_heavy_ask_pushes_the_estimate_toward_the_bid():
    b = book(0.40, 20, 0.60, 500)
    wm = weighted_mid(b)
    assert wm < b.mid
    assert wm > b.best_bid


def test_estimate_stays_inside_the_spread_for_any_imbalance():
    for bid_sz, ask_sz in ((1, 10_000), (10_000, 1), (7, 7), (1, 1)):
        b = book(0.33, bid_sz, 0.67, ask_sz)
        wm = weighted_mid(b)
        assert b.best_bid <= wm <= b.best_ask


def test_zero_sizes_fall_back_to_the_mid_rather_than_guessing():
    b = book(0.40, 0, 0.60, 0)
    assert weighted_mid(b) == pytest.approx(0.50)


def test_one_sided_book_has_no_estimate():
    assert weighted_mid(Book(bids=[Level(0.4, 10)], asks=[])) is None
    assert weighted_mid(Book(bids=[], asks=[Level(0.6, 10)])) is None


def test_depth_aggregates_more_levels():
    b = Book(
        bids=[Level(0.40, 10), Level(0.39, 990)],
        asks=[Level(0.60, 10), Level(0.61, 990)],
    )
    # Top of book is balanced; so is the aggregate. Both give the mid.
    assert weighted_mid(b, depth=1) == pytest.approx(0.50)
    assert weighted_mid(b, depth=2) == pytest.approx(0.50)

    lopsided = Book(
        bids=[Level(0.40, 10), Level(0.39, 990)],
        asks=[Level(0.60, 10), Level(0.61, 5)],
    )
    assert weighted_mid(lopsided, depth=1) == pytest.approx(0.50)
    assert weighted_mid(lopsided, depth=2) > 0.55


def test_depth_must_be_positive():
    with pytest.raises(ValueError):
        weighted_mid(book(0.4, 10, 0.6, 10), depth=0)


def test_symmetric_books_agree_with_the_mid_based_estimate():
    snap = snapshot(book(0.55, 100, 0.60, 100), book(0.40, 100, 0.45, 100))
    assert microprice_up(snap) == pytest.approx(market_implied_up(snap))


def test_size_skew_moves_the_implied_probability():
    """Same prices, different resting size -> a different read on P(Up)."""
    balanced = snapshot(book(0.55, 100, 0.60, 100), book(0.40, 100, 0.45, 100))
    up_heavy = snapshot(book(0.55, 900, 0.60, 20), book(0.40, 20, 0.45, 900))
    assert microprice_up(up_heavy) > microprice_up(balanced)
    # The mid cannot see this at all -- that is the whole point.
    assert market_implied_up(up_heavy) == pytest.approx(market_implied_up(balanced))


def test_missing_book_falls_back_to_the_other_side():
    snap = snapshot(Book(), book(0.40, 50, 0.45, 10))
    got = microprice_up(snap)
    assert got is not None
    assert got == pytest.approx(1.0 - weighted_mid(snap.down_book))


def test_no_books_at_all_gives_nothing():
    assert microprice_up(snapshot(Book(), Book())) is None


def test_probability_stays_in_range_on_wide_thin_books():
    snap = snapshot(book(0.01, 1, 0.99, 5000), book(0.01, 5000, 0.99, 1))
    p = microprice_up(snap)
    assert 0.0 <= p <= 1.0


def test_edge_threshold_defaults_to_the_mid():
    """Every recorded result in this repo used the mid; the default must not move."""
    strat = build_strategy("edge_threshold", {})
    assert strat.fair_value == "mid"


def test_edge_threshold_accepts_microprice_and_rejects_typos():
    strat = build_strategy("edge_threshold", {"fair_value": "microprice"})
    assert strat.fair_value == "microprice"
    with pytest.raises(ValueError):
        build_strategy("edge_threshold", {"fair_value": "midprice"})


def test_fair_value_choice_changes_the_measured_edge():
    """A skewed book is priced differently by the two estimators."""
    snap = snapshot(book(0.55, 900, 0.60, 20), book(0.40, 20, 0.45, 900))
    mid_strat = build_strategy("edge_threshold", {"fair_value": "mid"})
    mp_strat = build_strategy("edge_threshold", {"fair_value": "microprice"})
    assert mp_strat._market_up(snap) > mid_strat._market_up(snap)


def test_microprice_does_not_manufacture_a_signal_on_a_balanced_book():
    """Switching estimator on a symmetric book must not change the decision."""
    snap = snapshot(book(0.55, 100, 0.60, 100), book(0.40, 100, 0.45, 100))
    params = {"min_edge": 0.05, "realized_vol_window": None, "vol_per_year": 0.5}
    mid_strat = build_strategy("edge_threshold", dict(params, fair_value="mid"))
    mp_strat = build_strategy("edge_threshold", dict(params, fair_value="microprice"))

    mid_sig = mid_strat.decide(snap)
    mp_sig = mp_strat.decide(snap)
    if mid_sig is None:
        assert mp_sig is None
    else:
        assert mp_sig is not None
        assert mid_sig.side == mp_sig.side
        assert mp_sig.prob == pytest.approx(mid_sig.prob)


def test_side_helper_still_reads_the_right_book():
    """Guards the UP/DOWN wiring the estimator depends on."""
    snap = snapshot(book(0.55, 100, 0.60, 100), book(0.40, 100, 0.45, 100))
    assert snap.book(UP) is snap.up_book
    assert snap.book(DOWN) is snap.down_book
