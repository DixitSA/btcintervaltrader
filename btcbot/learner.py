"""Online calibration from observed trade outcomes.

Beta-Binomial per probability bucket. With zero observations the calibrator
is a no-op (returns the input probability unchanged). Every settled trade
appends a record to ``data/outcomes.jsonl`` and updates the posterior.

The interface is deliberately minimal: observe → calibrate. The runner owns
the Calibrator, loads it from disk at startup, feeds it on settlement, and
applies the calibrated probability before handing a signal to the risk layer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutcomeRecord:
    slug: str
    side: str
    signal_prob: float
    entry_price: float
    entry_ts: float
    exit_ts: float
    outcome: Optional[str]
    pnl: float
    exit_reason: str


class OutcomeStore:
    """Append-only JSONL store of settled trade outcomes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, record: OutcomeRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")

    def load(self) -> list[OutcomeRecord]:
        if not self.path.exists():
            return []
        records: list[OutcomeRecord] = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records.append(OutcomeRecord(**d))
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    log.warning("skipping bad outcome record: %s", exc)
        return records

    def feed(self, calibrator: Calibrator) -> int:
        """Feed all stored outcomes into *calibrator*. Returns count."""
        records = self.load()
        for r in records:
            calibrator.observe(r.signal_prob, r.pnl > 0)
        log.info("calibrator loaded %d past outcomes from %s", len(records), self.path)
        return len(records)


class Calibrator:
    """Beta-Binomial calibration bucketed by signal probability.

    Prior is Beta(α, β), default uniform (1, 1). After *w* wins out of *n*
    observations in a bucket, the posterior mean is (α+w) / (α+β+n).
    """

    def __init__(self, alpha_prior: float = 1.0, beta_prior: float = 1.0):
        self.alpha_prior = alpha_prior
        self.beta_prior = beta_prior
        self._buckets: dict[float, list[bool]] = {}

    def observe(self, signal_prob: float, won: bool) -> None:
        bucket = self._bucket(signal_prob)
        self._buckets.setdefault(bucket, []).append(won)

    def calibrate(self, prob: float) -> tuple[float, int]:
        """Return (calibrated_probability, n_observations_in_bucket).

        Returns (prob, 0) unchanged when the bucket has no data yet.
        """
        bucket = self._bucket(prob)
        outcomes = self._buckets.get(bucket)
        if not outcomes:
            return prob, 0
        n = len(outcomes)
        wins = sum(1 for w in outcomes if w)
        calibrated = (self.alpha_prior + wins) / (self.alpha_prior + self.beta_prior + n)
        return calibrated, n

    @property
    def table(self) -> list[dict]:
        """Rows for display: bucket, calibrated prob, raw mid, n, wins."""
        rows = []
        for bucket in sorted(self._buckets):
            outcomes = self._buckets[bucket]
            n = len(outcomes)
            wins = sum(1 for w in outcomes if w)
            cal = (self.alpha_prior + wins) / (self.alpha_prior + self.beta_prior + n)
            rows.append({
                "bucket": bucket,
                "calibrated": round(cal, 4),
                "raw_mid": bucket,
                "n": n,
                "wins": wins,
            })
        return rows

    @staticmethod
    def _bucket(prob: float) -> float:
        return round(prob * 10) / 10
