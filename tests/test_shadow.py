"""Tests for the shadow ledger."""

from __future__ import annotations

import os
import tempfile

import pytest

from btcbot.shadow import (
    DIRECTIONS,
    RUNG_DEFS,
    SCHEMA_VERSION,
    ShadowLedger,
    ShadowRecord,
    _dedup_key,
    _fade_side,
    _follow_side,
    _rung_for,
    assert_no_history_in_ranking,
)


# -- rung logic -----------------------------------------------------------


def test_rung_for_correct_range():
    assert _rung_for(50.0) == 0
    assert _rung_for(200.0) == 1
    assert _rung_for(400.0) == 2
    assert _rung_for(800.0) == 3


def test_rung_for_boundaries():
    assert _rung_for(120.0) == 0
    assert _rung_for(240.0) == 1
    assert _rung_for(420.0) == 2
    assert _rung_for(900.0) == 3


def test_rung_for_out_of_range():
    assert _rung_for(10.0) is None
    assert _rung_for(30.0) is None
    assert _rung_for(901.0) is None


def test_dedup_key_format():
    assert _dedup_key("slug-1", 0, "follow") == "slug-1::r0::follow"
    assert _dedup_key("slug-1", 2, "fade") == "slug-1::r2::fade"


# -- side selection -------------------------------------------------------


def make_snapshot(p_up: float = 0.55):
    from btcbot.models import DOWN, UP, Book, Level, Market, Snapshot

    spread = 0.02
    market = Market(
        condition_id="0xtest",
        slug="test-window",
        question="Test",
        up_token_id="up",
        down_token_id="down",
        start_ts=1000.0,
        end_ts=1900.0,
        strike=100_000.0,
    )
    return Snapshot(
        ts=1500.0,
        market=market,
        up_book=Book(
            bids=[Level(p_up - spread / 2, 100)],
            asks=[Level(p_up + spread / 2, 100)],
        ),
        down_book=Book(
            bids=[Level(1 - p_up - spread / 2, 100)],
            asks=[Level(1 - p_up + spread / 2, 100)],
        ),
        spot=100_000.0,
    )


def test_follow_side_favours_high_prob():
    snap = make_snapshot(p_up=0.55)
    assert _follow_side(snap) == "Up"


def test_follow_side_favours_down():
    snap = make_snapshot(p_up=0.45)
    assert _follow_side(snap) == "Down"


def test_fade_side_inverts_follow():
    snap = make_snapshot(p_up=0.55)
    assert _fade_side(snap) == "Down"


def test_side_selection_on_thin_book():
    """Even with no Up book, the Down book alone should yield a side."""
    from btcbot.models import DOWN, UP, Book, Level, Market, Snapshot

    market = Market(
        condition_id="0xtest", slug="t", question="?", up_token_id="u",
        down_token_id="d", start_ts=0, end_ts=2000, strike=100_000,
    )
    snap = Snapshot(
        ts=1000.0, market=market,
        up_book=Book(bids=[], asks=[]),
        # Down bid at 0.40 -> best_bid=0.40, no asks -> we use down best_bid (1-0.40=0.60 P(Up))
        down_book=Book(bids=[Level(0.40, 100)], asks=[]),
        spot=100_000.0,
    )
    side = _follow_side(snap)
    assert side is not None
    # Down bid 0.40 implies market prices Up at ~0.60, so follow should pick Up
    assert side == "Up"


# -- ShadowLedger lifecycle -----------------------------------------------


@pytest.fixture
def ledger_path():
    path = os.path.join(tempfile.gettempdir(), "test_shadow_ledger.jsonl")
    if os.path.exists(path):
        os.remove(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


def test_empty_ledger(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    assert ledger.record_count == 0
    assert ledger.load_records() == []


def test_append_and_count(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up")
    ledger.append(rec)
    assert ledger.record_count == 1
    assert ledger.already_recorded("w1", 0, "follow")
    assert not ledger.already_recorded("w1", 1, "follow")


def test_dedup_prevents_double_append(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec1 = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up", fill_price=0.90)
    rec2 = ShadowRecord(slug="w1", rung=0, direction="follow", side="Down", fill_price=0.10)
    ledger.append(rec1)
    ledger.append(rec2)  # same dedup key -> rejected
    assert ledger.record_count == 1


def test_settle_marks_won(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(
        slug="w1", rung=0, direction="follow", side="Up",
        fill_price=0.90, fee=0.01, contracts=1.111,
        strike=100_000.0,
    )
    ledger.append(rec)
    settled = ledger.settle("w1", "Up", 105_000.0, 2000.0)
    assert len(settled) == 1
    assert settled[0].won is True
    assert settled[0].gross_pnl == 1.0
    assert settled[0].net_pnl == pytest.approx(1.0 - 0.90 - 0.01)
    assert settled[0].margin_bps == pytest.approx(0.05)


def test_settle_marks_loss(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(slug="w2", rung=1, direction="fade", side="Down", fill_price=0.40, fee=0.02, contracts=2.5)
    ledger.append(rec)
    settled = ledger.settle("w2", "Up", 102_000.0, 3000.0)
    assert len(settled) == 1
    assert settled[0].won is False
    assert settled[0].gross_pnl == 0.0
    assert settled[0].net_pnl == pytest.approx(-0.40 - 0.02)


def test_settle_void_on_no_winner(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(slug="w3", rung=0, direction="follow", side="Up", fill_price=0.50, fee=0.02)
    ledger.append(rec)
    settled = ledger.settle("w3", None, None, 2000.0)
    assert len(settled) == 1
    assert settled[0].won is None
    assert settled[0].net_pnl == 0.0


def test_persist_and_reload(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up", fill_price=0.90, fee=0.01)
    ledger.append(rec)
    settled = ledger.settle("w1", "Up", 105_000.0, 2000.0)
    for s in settled:
        ledger.append_settled(s)

    ledger2 = ShadowLedger(ledger_path=ledger_path, enabled=True)
    assert ledger2.record_count == 1
    recs = ledger2.load_records()
    assert len(recs) == 1
    assert recs[0].won is True
    assert recs[0].net_pnl == pytest.approx(1.0 - 0.90 - 0.01)


def test_settle_only_unsettled(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up")
    ledger.append(rec)
    ledger.settle("w1", "Up", 100_000.0, 2000.0)
    # Second settle should be a no-op (already settled)
    settled2 = ledger.settle("w1", "Down", 100_000.0, 2000.0)
    assert len(settled2) == 0  # already settled, skipped


def test_unsettled_slugs(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    rec1 = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up")
    rec2 = ShadowRecord(slug="w2", rung=1, direction="fade", side="Down")
    ledger.append(rec1)
    ledger.append(rec2)
    unsettled = ledger.unsettled_slugs()
    assert "w1" in unsettled
    assert "w2" in unsettled
    ledger.settle("w1", "Up", 100_000.0, 2000.0)
    unsettled2 = ledger.unsettled_slugs()
    assert "w1" not in unsettled2
    assert "w2" in unsettled2


def test_disabled_ledger(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=False)
    rec = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up")
    ledger.append(rec)
    assert ledger.record_count == 0


# -- evaluate() with snapshot ---------------------------------------------


def test_evaluate_creates_records(ledger_path):
    from btcbot.fees import KalshiFees

    fee_model = KalshiFees()
    ledger = ShadowLedger(
        ledger_path=ledger_path, enabled=True, fee_model=fee_model,
    )
    snap = make_snapshot(p_up=0.55)
    records = ledger.evaluate(snap)
    # At p_up=0.55, seconds_remaining = 1900 - 1500 = 400, which is rung 2
    assert len(records) == 2  # follow + fade
    assert records[0].direction in ("follow", "fade")
    assert records[1].direction in ("follow", "fade")
    assert records[0].rung == 2
    assert records[1].rung == 2
    for rec in records:
        assert rec.fill_price is not None
        assert rec.contracts > 0
        assert rec.best_ask is not None


def test_evaluate_dedup(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    snap = make_snapshot(p_up=0.55)
    first = ledger.evaluate(snap)
    for r in first:
        ledger.append(r)
    second = ledger.evaluate(snap)
    assert len(second) == 0  # all deduped


def test_evaluate_outside_rung(ledger_path):
    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    from btcbot.models import Book, Level, Market, Snapshot

    market = Market(
        condition_id="0xtest", slug="t", question="?", up_token_id="u",
        down_token_id="d", start_ts=0, end_ts=100, strike=100_000,
    )
    snap = Snapshot(
        ts=96.0, market=market,  # 4s remaining -> below min (30)
        up_book=Book(bids=[Level(0.50, 100)], asks=[Level(0.52, 100)]),
        down_book=Book(bids=[Level(0.48, 100)], asks=[Level(0.50, 100)]),
        spot=100_000.0,
    )
    records = ledger.evaluate(snap)
    assert len(records) == 0  # seconds_remaining=4, below all rung mins (30)


# -- firewall -------------------------------------------------------------


def test_history_records_have_null_depth_spread(ledger_path):
    rec = ShadowRecord(slug="w1", rung=0, direction="follow", side="Up", producer="history")
    assert rec.spread is None
    assert rec.depth is None


def test_no_history_in_ranking_passes_clean():
    records = [
        ShadowRecord(slug="w1", rung=0, direction="follow", producer="live", spread=0.02, depth=100.0),
        ShadowRecord(slug="w2", rung=1, direction="fade", producer="replay", spread=0.03, depth=200.0),
    ]
    assert_no_history_in_ranking(records)


def test_no_history_in_ranking_raises_on_violation():
    records = [
        ShadowRecord(slug="w1", rung=0, direction="follow", producer="history", spread=0.02, depth=100.0),
    ]
    with pytest.raises(AssertionError):
        assert_no_history_in_ranking(records)
