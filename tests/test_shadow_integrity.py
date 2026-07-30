"""Ledger durability: appending must never rewrite, and loading must dedupe.

append_settled() used to read the whole file, substitute one line, and write the
whole file back. On a live ledger that produced a file where 71% of lines were
interleaved fragments -- silently dropped on load, so the panel just looked
sparse. These pin the append-only contract that replaced it.
"""

from __future__ import annotations

import json

import pytest

from btcbot.shadow import ShadowLedger, ShadowRecord, _dedup_key


@pytest.fixture
def path(tmp_path):
    return tmp_path / "shadow.jsonl"


def make_ledger(path):
    return ShadowLedger(ledger_path=str(path), enabled=True)


def rec(slug="KXBTC15M-a", rung=0, direction="follow", **kw):
    return ShadowRecord(
        slug=slug, rung=rung, direction=direction, side="Up",
        fill_price=0.5, contracts=2.0, fee=0.01, strike=63_000.0, **kw
    )


def test_settling_appends_and_never_rewrites_existing_bytes(path):
    led = make_ledger(path)
    led.append(rec())
    after_entry = path.read_bytes()

    r = led._records[_dedup_key("KXBTC15M-a", 0, "follow")]
    r.won = True
    r.settled_ts = 1000.0
    led.append_settled(r)

    after_settle = path.read_bytes()
    assert after_settle.startswith(after_entry), (
        "the settled write must not touch bytes already on disk -- "
        "a truncate-and-rewrite is what corrupted the real ledger"
    )
    assert len(path.read_text().strip().splitlines()) == 2


def test_load_records_keeps_the_last_line_for_a_key(path):
    led = make_ledger(path)
    led.append(rec())
    r = led._records[_dedup_key("KXBTC15M-a", 0, "follow")]
    r.won = True
    r.settled_ts = 1000.0
    led.append_settled(r)

    loaded = make_ledger(path).load_records()
    assert len(loaded) == 1, "entry + settled for one key is ONE record, not two"
    assert loaded[0].won is True
    assert loaded[0].settled_ts == 1000.0


def test_duplicate_settled_lines_do_not_double_count(path):
    """The re-settle bug wrote the same settled record many times."""
    led = make_ledger(path)
    led.append(rec())
    r = led._records[_dedup_key("KXBTC15M-a", 0, "follow")]
    r.won = True
    r.settled_ts = 1000.0
    for _ in range(20):
        led.append_settled(r)

    loaded = make_ledger(path).load_records()
    assert len(loaded) == 1
    assert len(path.read_text().strip().splitlines()) == 21  # history is kept


def test_distinct_keys_are_all_kept(path):
    led = make_ledger(path)
    led.append(rec(rung=0))
    led.append(rec(rung=1))
    led.append(rec(slug="KXETH15M-b", rung=0))
    loaded = make_ledger(path).load_records()
    assert len(loaded) == 3


def test_corrupt_lines_are_counted_not_silently_dropped(path):
    good = json.dumps({"slug": "KXBTC15M-a", "rung": 0, "direction": "follow"})
    path.write_text(
        good + "\n"
        + '"side": "Down", "best_ask": 0.97{"schema_ve{"schema_version"\n'   # real shape
        + "not json at all\n"
        + '{"slug": "KXETH15M-b", "rung": 1, "direction": "fade"}\n',
        encoding="utf-8",
    )
    led = make_ledger(path)
    loaded = led.load_records()
    assert len(loaded) == 2
    assert led.load_errors == 2
    assert led.load_lines == 4


def test_a_clean_ledger_reports_no_errors(path):
    led = make_ledger(path)
    led.append(rec())
    fresh = make_ledger(path)
    fresh.load_records()
    assert fresh.load_errors == 0


def test_appending_to_a_missing_file_creates_it(path):
    led = make_ledger(path)
    assert not path.exists()
    r = rec(won=True, settled_ts=1.0)
    led.append_settled(r)
    assert path.exists()
    assert len(make_ledger(path).load_records()) == 1
