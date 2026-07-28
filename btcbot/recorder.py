"""Snapshot recording and (de)serialisation.

Recording is step one of this whole project. You cannot evaluate any rule --
the $500k volume rule included -- without a dataset of what the books actually
looked like tick by tick and how each window resolved. Run `record` for a few
days before you run anything else.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .models import Book, Level, Market, Snapshot

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def book_to_dict(book: Book) -> dict[str, Any]:
    return {
        "bids": [[lv.price, lv.size] for lv in book.bids],
        "asks": [[lv.price, lv.size] for lv in book.asks],
    }


def book_from_dict(raw: dict[str, Any]) -> Book:
    return Book(
        bids=[Level(price=float(p), size=float(s)) for p, s in raw.get("bids", [])],
        asks=[Level(price=float(p), size=float(s)) for p, s in raw.get("asks", [])],
    )


def snapshot_to_dict(snap: Snapshot) -> dict[str, Any]:
    m = snap.market
    return {
        "v": SCHEMA_VERSION,
        "ts": snap.ts,
        "market": {
            "condition_id": m.condition_id,
            "slug": m.slug,
            "question": m.question,
            "up_token_id": m.up_token_id,
            "down_token_id": m.down_token_id,
            "start_ts": m.start_ts,
            "end_ts": m.end_ts,
            "volume": m.volume,
            "liquidity": m.liquidity,
            "strike": m.strike,
        },
        "up_book": book_to_dict(snap.up_book),
        "down_book": book_to_dict(snap.down_book),
        "spot": snap.spot,
        "window_volume": snap.window_volume,
    }


def snapshot_from_dict(raw: dict[str, Any]) -> Snapshot:
    m = raw["market"]
    market = Market(
        condition_id=m.get("condition_id", ""),
        slug=m.get("slug", ""),
        question=m.get("question", ""),
        up_token_id=m.get("up_token_id", ""),
        down_token_id=m.get("down_token_id", ""),
        start_ts=float(m.get("start_ts", 0.0)),
        end_ts=float(m.get("end_ts", 0.0)),
        volume=float(m.get("volume", 0.0)),
        liquidity=float(m.get("liquidity", 0.0)),
        strike=(float(m["strike"]) if m.get("strike") is not None else None),
    )
    return Snapshot(
        ts=float(raw["ts"]),
        market=market,
        up_book=book_from_dict(raw.get("up_book", {})),
        down_book=book_from_dict(raw.get("down_book", {})),
        spot=(float(raw["spot"]) if raw.get("spot") is not None else None),
        window_volume=float(raw.get("window_volume", 0.0)),
    )


class SnapshotWriter:
    """Appends snapshots to a JSONL file, one file per UTC day."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path: Optional[Path] = None
        self._fh = None

    def _file_for(self, ts: float) -> Path:
        import datetime as _dt

        day = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        return self.data_dir / f"snapshots-{day}.jsonl"

    def write(self, snap: Snapshot) -> None:
        path = self._file_for(snap.ts)
        if path != self._path:
            self.close()
            self._path = path
            self._fh = path.open("a", encoding="utf-8")
        assert self._fh is not None
        self._fh.write(json.dumps(snapshot_to_dict(snap), separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SnapshotWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def read_snapshots(paths: Iterable[str | Path]) -> Iterator[Snapshot]:
    for path in paths:
        p = Path(path)
        if not p.exists():
            log.warning("skipping missing file: %s", p)
            continue
        with p.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield snapshot_from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    log.warning("bad record %s:%d: %s", p, lineno, exc)


def load_dataset(data_dir: str | Path) -> list[Snapshot]:
    files = sorted(Path(data_dir).glob("snapshots-*.jsonl"))
    return list(read_snapshots(files))
