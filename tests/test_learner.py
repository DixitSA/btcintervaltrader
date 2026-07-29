from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcbot.learner import Calibrator, OutcomeRecord, OutcomeStore


# -- Calibrator -------------------------------------------------------


def test_calibrator_no_data_returns_prob_unchanged():
    c = Calibrator()
    assert c.calibrate(0.55) == (0.55, 0)
    assert c.calibrate(0.80) == (0.80, 0)
    assert c.calibrate(0.95) == (0.95, 0)


def test_calibrator_single_bucket():
    c = Calibrator(alpha_prior=1.0, beta_prior=1.0)
    # 3 wins, 1 loss at prob 0.60 bucket
    c.observe(0.61, True)
    c.observe(0.60, True)
    c.observe(0.60, True)
    c.observe(0.59, False)

    cal, n = c.calibrate(0.60)
    assert n == 4
    # posterior mean = (1 + 3) / (2 + 4) = 4/6 ≈ 0.6667
    assert cal == pytest.approx(4.0 / 6.0)


def test_calibrator_multiple_buckets():
    c = Calibrator(alpha_prior=1.0, beta_prior=1.0)
    # Bucket 0.60: 10 wins, 0 losses
    for _ in range(10):
        c.observe(0.60, True)
    # Bucket 0.70: 8 wins, 2 losses
    for _ in range(8):
        c.observe(0.70, True)
    for _ in range(2):
        c.observe(0.70, False)

    cal_60, n_60 = c.calibrate(0.60)
    assert n_60 == 10
    assert cal_60 == pytest.approx((1 + 10) / (2 + 10))

    cal_70, n_70 = c.calibrate(0.70)
    assert n_70 == 10
    assert cal_70 == pytest.approx((1 + 8) / (2 + 10))


def test_calibrator_custom_prior():
    c = Calibrator(alpha_prior=2.0, beta_prior=5.0)
    c.observe(0.60, True)

    cal, n = c.calibrate(0.60)
    assert n == 1
    # posterior mean = (2 + 1) / (7 + 1) = 3/8
    assert cal == pytest.approx(3.0 / 8.0)


def test_calibrator_table():
    c = Calibrator()
    c.observe(0.60, True)
    c.observe(0.60, True)
    c.observe(0.70, False)

    table = c.table
    assert len(table) == 2
    rows_by_bucket = {r["bucket"]: r for r in table}
    assert 0.6 in rows_by_bucket
    assert 0.7 in rows_by_bucket
    assert rows_by_bucket[0.6]["n"] == 2
    assert rows_by_bucket[0.6]["wins"] == 2
    assert rows_by_bucket[0.7]["n"] == 1
    assert rows_by_bucket[0.7]["wins"] == 0


def test_calibrator_empty_table():
    c = Calibrator()
    assert c.table == []


def test_calibrator_bucket_rounding():
    """Verify bucket boundaries: round(prob * 10) / 10."""
    c = Calibrator()
    c.observe(0.54, True)
    c.observe(0.55, True)  # 0.55 → 0.6 (banker's rounding: round-half-to-even)
    c.observe(0.56, True)

    cal_5, n_5 = c.calibrate(0.50)
    assert n_5 == 1  # only 0.54 rounds to 0.5

    cal_6, n_6 = c.calibrate(0.60)
    assert n_6 == 2  # 0.55 and 0.56 both round to 0.6


# -- OutcomeStore -----------------------------------------------------


def test_outcome_store_roundtrip(tmp_path: Path):
    path = tmp_path / "outcomes.jsonl"
    store = OutcomeStore(path)

    records = [
        OutcomeRecord(slug="s1", side="Up", signal_prob=0.60, entry_price=0.55,
                      entry_ts=100.0, exit_ts=1100.0, outcome="Up", pnl=10.0,
                      exit_reason="expiry"),
        OutcomeRecord(slug="s2", side="Down", signal_prob=0.55, entry_price=0.45,
                      entry_ts=200.0, exit_ts=1200.0, outcome="Down", pnl=5.0,
                      exit_reason="expiry"),
    ]
    for rec in records:
        store.append(rec)

    loaded = store.load()
    assert len(loaded) == 2
    assert loaded[0].slug == "s1"
    assert loaded[0].signal_prob == 0.60
    assert loaded[0].pnl == 10.0
    assert loaded[1].slug == "s2"
    assert loaded[1].pnl == 5.0


def test_outcome_store_load_empty(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    store = OutcomeStore(path)
    assert store.load() == []


def test_outcome_store_load_missing(tmp_path: Path):
    path = tmp_path / "nonexistent.jsonl"
    store = OutcomeStore(path)
    assert store.load() == []


def test_outcome_store_skip_bad_lines(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"slug": "good", "side": "Up", "signal_prob": 0.5, "entry_price": 0.5, "entry_ts": 0, "exit_ts": 1, "outcome": "Up", "pnl": 1.0, "exit_reason": "expiry"}\nnot json\n')
    store = OutcomeStore(path)
    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].slug == "good"


def test_outcome_store_feed(tmp_path: Path):
    path = tmp_path / "outcomes.jsonl"
    store = OutcomeStore(path)
    calibrator = Calibrator()

    # No records yet — feed is a no-op
    assert store.feed(calibrator) == 0

    # Add one record and feed
    store.append(OutcomeRecord(slug="s1", side="Up", signal_prob=0.60, entry_price=0.55,
                                entry_ts=100.0, exit_ts=1100.0, outcome="Up", pnl=10.0,
                                exit_reason="expiry"))

    assert store.feed(calibrator) == 1
    cal, n = calibrator.calibrate(0.60)
    assert n == 1
    assert cal == pytest.approx((1 + 1) / (2 + 1))  # one win


# -- Round-trip: calibrator + store ----------------------------------


def test_calibrator_from_stored_outcomes(tmp_path: Path):
    """Feed multiple outcomes through the store into a fresh calibrator."""
    store = OutcomeStore(tmp_path / "outcomes.jsonl")

    # 8 wins at 0.60, 2 losses at 0.60
    for _ in range(8):
        store.append(OutcomeRecord("s", "Up", 0.60, 0.55, 0, 100, "Up", 1.0, "expiry"))
    for _ in range(2):
        store.append(OutcomeRecord("s", "Up", 0.60, 0.55, 0, 100, "Down", -1.0, "expiry"))

    c = Calibrator()
    n = store.feed(c)
    assert n == 10
    cal, _ = c.calibrate(0.60)
    # posterior mean = (1 + 8) / (2 + 10) = 9/12 = 0.75
    assert cal == pytest.approx(9.0 / 12.0)
