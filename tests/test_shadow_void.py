"""A window we cannot resolve must settle as a VOID, once, and stay resolved.

Regression: `unsettled_slugs()` keyed on `won is None`, and settling with
winning_side=None leaves `won` None. So once outcome inference started
abstaining (spot inside the noise band, or no reading near the close), those
records were re-settled and re-appended to the ledger on EVERY tick. The
recorder slowed from ~30 rows/min/family to under 10 as they accumulated, and
the ledger file grew without bound.
"""

from __future__ import annotations

import pytest

from btcbot.shadow import ShadowLedger


@pytest.fixture
def ledger(tmp_path):
    return ShadowLedger(ledger_path=str(tmp_path / "shadow.jsonl"), enabled=True)


def _record(ledger, slug="KXBTC15M-x"):
    from btcbot.shadow import ShadowRecord

    rec = ShadowRecord(
        slug=slug, rung=120, direction="follow", side="Up",
        fill_price=0.5, contracts=2.0, fee=0.01, strike=63_000.0,
    )
    ledger._records[f"{slug}::120::follow"] = rec
    ledger._dedup.add(f"{slug}::120::follow")
    return rec


def test_void_settlement_is_terminal(ledger):
    rec = _record(ledger)
    assert "KXBTC15M-x" in ledger.unsettled_slugs()

    first = ledger.settle("KXBTC15M-x", None, 63_000.4, settled_ts=1000.0)
    assert len(first) == 1
    assert rec.won is None, "a void has no verdict"
    assert rec.settled_ts == 1000.0, "but it IS resolved"

    # The whole point: it must not come back.
    assert "KXBTC15M-x" not in ledger.unsettled_slugs()
    again = ledger.settle("KXBTC15M-x", None, 63_000.4, settled_ts=1100.0)
    assert again == [], "a voided record must not be re-settled"
    assert rec.settled_ts == 1000.0, "and must not be restamped"


def test_repeated_settlement_attempts_do_not_accumulate(ledger):
    """What the recorder actually does: retry every tick after expiry."""
    _record(ledger)
    appended = 0
    for tick in range(50):
        for r in ledger.settle("KXBTC15M-x", None, 63_000.4, settled_ts=1000.0 + tick):
            appended += 1
    assert appended == 1, f"re-appended {appended} times; the ledger would grow forever"


def test_a_real_verdict_still_settles_normally(ledger):
    rec = _record(ledger)
    settled = ledger.settle("KXBTC15M-x", "Up", 63_500.0, settled_ts=1000.0)
    assert len(settled) == 1
    assert rec.won is True
    assert rec.settled_ts == 1000.0
    assert "KXBTC15M-x" not in ledger.unsettled_slugs()
    assert ledger.settle("KXBTC15M-x", "Up", 63_500.0, settled_ts=1100.0) == []


def test_unrelated_slug_stays_unsettled(ledger):
    _record(ledger, "KXBTC15M-a")
    _record(ledger, "KXETH15M-b")
    ledger.settle("KXBTC15M-a", None, 1.0, settled_ts=1000.0)
    assert ledger.unsettled_slugs() == {"KXETH15M-b"}
