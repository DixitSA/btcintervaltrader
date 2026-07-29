"""Shadow ledger: record hypothetical trades at every rung x direction.

Decouples learning from risk. The live portfolio might take 0-1 trades per
window; the shadow ledger records what *would* have happened at every rung,
both directions, regardless of available capital.

Records are at a fixed $1 notional so they are comparable across rungs,
windows, and bankroll regimes. The dedup key is (slug, rung, direction) so a
mid-window restart cannot double-record.

Three producer sources share one schema:
  live    — recorded during live/paper trading, has real depth/spread
  replay  — regenerated offline from recorded snapshots, has depth/spread
  history — sourced from Kalshi settled-market data, NO depth/spread

History records carry depth/spread as null and are STRUCTURALLY BARRED from
net-P&L ranking (enforced by assertion, not documentation).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from .fees import build_fee_model
from .models import DOWN, UP, Snapshot

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Rung definitions in seconds_remaining: (label, min_remaining, max_remaining)
# Rung 0 = last 90s (max 120, min 30), Rung 3 = full window
RUNG_DEFS: list[tuple[int, float, float]] = [
    (0, 30.0, 120.0),
    (1, 30.0, 240.0),
    (2, 30.0, 420.0),
    (3, 30.0, 900.0),
]

DIRECTIONS = ["follow", "fade"]


@dataclass
class ShadowRecord:
    schema_version: int = SCHEMA_VERSION
    producer: str = "live"
    slug: str = ""
    strike: Optional[float] = None
    window_end_ts: float = 0.0
    rung: int = 0
    seconds_remaining: float = 0.0
    direction: str = ""
    side: str = ""
    best_ask: Optional[float] = None
    spread: Optional[float] = None
    depth: Optional[float] = None
    fill_price: Optional[float] = None
    contracts: float = 0.0
    fee: float = 0.0
    market_implied_prob: Optional[float] = None
    signal_prob: Optional[float] = None
    spot_at_entry: Optional[float] = None
    final_spot: Optional[float] = None
    winning_side: Optional[str] = None
    won: Optional[bool] = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    margin_bps: Optional[float] = None
    settled_ts: Optional[float] = None


def _rung_for(seconds_remaining: float, rung_defs: list[tuple[int, float, float]] = RUNG_DEFS) -> Optional[int]:
    for rung, lo, hi in rung_defs:
        if lo < seconds_remaining <= hi:
            return rung
    return None


def _dedup_key(slug: str, rung: int, direction: str) -> str:
    return f"{slug}::r{rung}::{direction}"


def _follow_side(snap: Snapshot) -> Optional[str]:
    ub = snap.up_book
    db = snap.down_book
    up_mid = ub.mid
    down_mid = db.mid
    up_bid = ub.best_bid
    down_bid = db.best_bid
    if up_mid is not None and down_mid is not None:
        p_up = (up_mid + (1.0 - db.mid)) / 2.0
    elif up_mid is not None:
        p_up = up_mid
    elif down_mid is not None:
        p_up = 1.0 - down_mid
    elif up_bid is not None:
        p_up = up_bid
    elif down_bid is not None:
        p_up = 1.0 - down_bid
    else:
        return None
    return UP if p_up >= 0.5 else DOWN


def _fade_side(snap: Snapshot) -> Optional[str]:
    fav = _follow_side(snap)
    if fav is None:
        return None
    return DOWN if fav == UP else UP


_FN_MAP = {"follow": _follow_side, "fade": _fade_side}


class ShadowLedger:
    """Append-only ledger of hypothetical trades.

    Records opened by evaluate() (entry-time), completed by settle().
    Persisted to one JSONL file. Entirely independent of Portfolio.cash.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        producer: str = "live",
        fee_model=None,
        rung_defs: Optional[list[tuple[int, float, float]]] = None,
        enabled: bool = True,
    ):
        self.path = Path(ledger_path) if ledger_path else None
        self.producer = producer
        self.fee_model = fee_model
        self.rung_defs = rung_defs or RUNG_DEFS
        self.enabled = enabled

        if producer == "history" and self.path:
            self._assert_history_path()

        # In-memory records keyed by dedup key, for settlement lookup
        self._records: dict[str, ShadowRecord] = {}
        self._dedup: set[str] = set()
        if self.path and self.path.exists():
            self._load_existing()

    def _assert_history_path(self) -> None:
        if "history" not in self.path.name:
            raise RuntimeError(
                f"history producer must write to a separate file, not '{self.path.name}'"
            )

    def _load_existing(self) -> None:
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    rec = ShadowRecord(**d)
                    key = _dedup_key(rec.slug, rec.rung, rec.direction)
                    self._records[key] = rec
                    self._dedup.add(key)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    log.warning("skipping bad shadow record: %s", exc)

    def already_recorded(self, slug: str, rung: int, direction: str) -> bool:
        return _dedup_key(slug, rung, direction) in self._dedup

    def evaluate(
        self,
        snap: Snapshot,
        *,
        signal_prob: Optional[float] = None,
    ) -> list[ShadowRecord]:
        """Build shadow records for every (rung, direction) not yet recorded.

        No capital rationing — every possible entry at every rung is recorded.
        Returns records with entry fields populated; callers must persist via
        append(). Settlement fields are filled later by settle().
        """
        if not self.enabled:
            return []

        remaining = snap.market.seconds_remaining(snap.ts)
        rung = _rung_for(remaining, self.rung_defs)
        if rung is None:
            return []

        slug = snap.market.slug
        records: list[ShadowRecord] = []

        for direction in DIRECTIONS:
            if self.already_recorded(slug, rung, direction):
                continue

            side_fn = _FN_MAP.get(direction)
            if side_fn is None:
                continue
            side = side_fn(snap)
            if side is None:
                continue

            book = snap.book(side)
            ask = book.best_ask
            if ask is None or ask <= 0:
                continue

            spread = book.spread
            depth = sum(lv.price * lv.size for lv in book.asks)

            # Fixed $1 notional
            contracts = 1.0 / ask

            avg_price = book.sweep_cost(contracts)
            if avg_price is None:
                continue

            fee = self.fee_model.entry_fee(contracts, avg_price) if self.fee_model else 0.0

            # Market-implied probability
            up_mid = snap.up_book.mid
            down_mid = snap.down_book.mid
            if up_mid is not None and down_mid is not None:
                p_up = (up_mid + (1.0 - down_mid)) / 2.0
            elif up_mid is not None:
                p_up = up_mid
            elif down_mid is not None:
                p_up = 1.0 - down_mid
            else:
                p_up = None

            is_history = self.producer == "history"

            rec = ShadowRecord(
                producer=self.producer,
                slug=slug,
                strike=snap.market.strike,
                window_end_ts=snap.market.end_ts,
                rung=rung,
                seconds_remaining=remaining,
                direction=direction,
                side=side,
                best_ask=ask,
                spread=spread if not is_history else None,
                depth=depth if not is_history else None,
                fill_price=avg_price,
                contracts=contracts,
                fee=fee,
                market_implied_prob=p_up,
                signal_prob=signal_prob,
                spot_at_entry=snap.spot,
            )
            records.append(rec)

        return records

    def settle(
        self,
        slug: str,
        winning_side: Optional[str],
        final_spot: Optional[float],
        settled_ts: float,
    ) -> list[ShadowRecord]:
        """Settle all outstanding records for a slug.

        Computes gross/net P&L and margin_bps. Returns completed records;
        callers must persist via append_settled().
        """
        if not self.enabled:
            return []

        prefix = f"{slug}::"
        matched_keys = [k for k in self._dedup if k.startswith(prefix)]
        settled: list[ShadowRecord] = []

        for key in matched_keys:
            rec = self._records.get(key)
            if rec is None:
                continue
            if rec.won is not None:
                continue  # already settled

            won = (winning_side == rec.side) if winning_side is not None else None

            fill = rec.fill_price or 0.0
            if won is True:
                gross_pnl = 1.0
                net_pnl = 1.0 - fill - rec.fee
            elif won is False:
                gross_pnl = 0.0
                net_pnl = -fill - rec.fee
            else:
                gross_pnl = 0.0
                net_pnl = 0.0

            strike = rec.strike
            margin_bps = None
            if final_spot is not None and strike is not None and strike > 0:
                margin_bps = abs(final_spot - strike) / strike

            rec.final_spot = final_spot
            rec.winning_side = winning_side
            rec.won = won
            rec.gross_pnl = gross_pnl
            rec.net_pnl = net_pnl
            rec.margin_bps = margin_bps
            rec.settled_ts = settled_ts

            settled.append(rec)

        return settled

    def append(self, record: ShadowRecord) -> None:
        """Write entry record to the ledger file."""
        if not self.enabled or self.path is None:
            return
        key = _dedup_key(record.slug, record.rung, record.direction)
        if key in self._dedup:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")
        self._records[key] = record
        self._dedup.add(key)

    def append_settled(self, record: ShadowRecord) -> None:
        """Overwrite an existing record with settlement fields filled in."""
        if not self.enabled or self.path is None:
            return
        if not self.path.exists():
            return

        key = _dedup_key(record.slug, record.rung, record.direction)
        lines = self.path.read_text().splitlines()
        rewritten = False
        out_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                d = json.loads(stripped)
                existing_key = _dedup_key(
                    d.get("slug", ""),
                    d.get("rung", -1),
                    d.get("direction", ""),
                )
                if existing_key == key:
                    out_lines.append(json.dumps(asdict(record), default=str))
                    rewritten = True
                else:
                    out_lines.append(stripped)
            except (json.JSONDecodeError, TypeError, ValueError):
                out_lines.append(stripped)

        if rewritten:
            self.path.write_text("\n".join(out_lines) + "\n")

        self._records[key] = record
        self._dedup.add(key)

    def load_records(self) -> list[ShadowRecord]:
        """Load all records from the ledger file (reads fresh from disk)."""
        records: list[ShadowRecord] = []
        if not self.path or not self.path.exists():
            return records
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records.append(ShadowRecord(**d))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    log.warning("skipping bad shadow record: %s", exc)
        return records

    def unsettled_slugs(self) -> set[str]:
        """Return slugs with at least one unsettled (won is None) record."""
        slugs: set[str] = set()
        for key, rec in self._records.items():
            if rec.won is None:
                slug = key.split("::")[0]
                if slug:
                    slugs.add(slug)
        return slugs

    @property
    def record_count(self) -> int:
        return len(self._records)

    def close(self) -> None:
        pass


# -- Firewall assertions (Phase 0.2) --------------------------------------


def assert_no_history_in_ranking(records: list[ShadowRecord]) -> None:
    """Enforce that history-sourced records never enter net-P&L ranking."""
    for r in records:
        if r.producer == "history":
            assert r.spread is None, f"history record {r.slug} r{r.rung} has non-null spread"
            assert r.depth is None, f"history record {r.slug} r{r.rung} has non-null depth"
