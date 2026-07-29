"""Audit data providers — read-only views into the system's audit trail.

Every function in this module reads persisted data and returns plain dicts
for JSON serialisation.  No writing, no side effects.

Sources:
  - outcomes.jsonl    — every settled trade with signal probability
  - shadow.jsonl      — hypothetical trades at every rung × direction
  - snapshots-*.jsonl — recorded market snapshots
  - RiskManager state — live from the runner
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


def load_calibration(data_dir: str, alpha_prior: float = 1.0, beta_prior: float = 1.0) -> dict[str, Any]:
    """Read outcomes.jsonl and build the calibration curve.

    Returns buckets + aggregate stats, or an error dict if no data.
    """
    outcomes = _load_outcomes(data_dir)
    if not outcomes:
        return {"ok": False, "error": "no outcome records found"}

    buckets: dict[float, list[bool]] = {}
    for rec in outcomes:
        bucket = round(rec.get("signal_prob", 0.5) * 10) / 10
        buckets.setdefault(bucket, []).append(rec.get("won", False))

    rows = []
    for bucket in sorted(buckets):
        outcomes_list = buckets[bucket]
        n = len(outcomes_list)
        wins = sum(1 for w in outcomes_list if w)
        raw_mid = bucket
        cal = (alpha_prior + wins) / (alpha_prior + beta_prior + n)
        rows.append({
            "bucket": bucket,
            "n": n,
            "wins": wins,
            "losses": n - wins,
            "raw_mid": raw_mid,
            "calibrated": round(cal, 4),
            "delta": round(cal - raw_mid, 4),
        })

    total_wins = sum(r["wins"] for r in rows)
    total = sum(r["n"] for r in rows)
    return {
        "ok": True,
        "buckets": rows,
        "total": total,
        "total_wins": total_wins,
        "total_losses": total - total_wins,
        "win_rate": round(total_wins / total, 4) if total else 0,
        "alpha_prior": alpha_prior,
        "beta_prior": beta_prior,
    }


def load_outcomes(data_dir: str, limit: int = 500) -> dict[str, Any]:
    """Return the most recent outcome records."""
    records = _load_outcomes(data_dir)
    if not records:
        return {"ok": False, "error": "no outcome records found", "records": []}
    records.sort(key=lambda r: r.get("exit_ts", 0), reverse=True)
    return {
        "ok": True,
        "records": records[:limit],
        "total": len(records),
        "shown": min(limit, len(records)),
    }


def _load_outcomes(data_dir: str) -> list[dict[str, Any]]:
    path = Path(data_dir) / "outcomes.jsonl"
    if not path.exists():
        return []
    records = []
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return records


def data_quality(data_dir: str) -> dict[str, Any]:
    """Scan snapshot files for completeness, gaps, and window coverage."""
    snap_dir = Path(data_dir)
    if not snap_dir.exists():
        return {"ok": False, "error": f"'{data_dir}' does not exist"}

    files = sorted(snap_dir.glob("snapshots-*.jsonl"))
    if not files:
        return {"ok": False, "error": "no snapshot files found"}

    total_lines = 0
    file_stats = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
            lines = [l for l in text.splitlines() if l.strip()]
            total_lines += len(lines)
            file_stats.append({
                "name": f.name,
                "size_bytes": f.stat().st_size,
                "lines": len(lines),
            })
        except OSError:
            continue

    # Read timestamps and slugs from the latest file for gap analysis
    gaps: list[dict[str, Any]] = []
    slug_counts: dict[str, int] = defaultdict(int)
    prev_ts: Optional[float] = None
    max_gap = 0.0

    try:
        newest = files[-1]
        for line in newest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = row.get("ts")
            slug = row.get("market", {}).get("slug") if isinstance(row.get("market"), dict) else None
            if ts is not None:
                ts_f = float(ts)
                if prev_ts is not None:
                    gap = ts_f - prev_ts
                    max_gap = max(max_gap, gap)
                    if gap > 10.0:
                        gaps.append({
                            "from": prev_ts,
                            "to": ts_f,
                            "gap_seconds": round(gap, 1),
                        })
                prev_ts = ts_f
            if slug:
                slug_counts[slug] += 1
    except OSError:
        pass

    return {
        "ok": True,
        "files": file_stats,
        "total_snapshots": total_lines,
        "latest_file": file_stats[-1]["name"] if file_stats else None,
        "gaps_over_10s": gaps[-50:] if gaps else [],
        "total_gaps": len(gaps),
        "max_gap_seconds": round(max_gap, 1),
        "window_snapshot_counts": [
            {"slug": slug, "snapshots": cnt}
            for slug, cnt in sorted(slug_counts.items(), key=lambda x: -x[1])
        ],
        "windows_tracked": len(slug_counts),
    }


def risk_state(runner) -> dict[str, Any]:
    """Snapshot of the RiskManager's internal state, or an error if not running."""
    if runner is None:
        return {"ok": False, "error": "no active runner"}

    risk = getattr(runner, "risk", None)
    if risk is None:
        return {"ok": False, "error": "risk manager not initialised"}

    state = risk.state
    now = __import__("time").time()
    recent = state._recent_trades(now) if hasattr(state, "_recent_trades") else len(state.trade_times)

    return {
        "ok": True,
        "bankroll": round(state.bankroll, 2),
        "realized_pnl_today": round(state.realized_pnl_today, 2),
        "open_positions": state.open_positions,
        "trades_last_hour": recent,
        "max_trades_per_hour": risk.cfg.max_trades_per_hour,
        "halted_reason": state.halted_reason,
        "daily_loss_limit_usd": risk.cfg.daily_loss_limit_usd,
        "max_concurrent": risk.cfg.max_concurrent_positions,
        "kelly_fraction": risk.cfg.kelly_fraction,
        "max_stake_per_trade": risk.cfg.max_stake_per_trade_usd,
        "max_stake_fraction": risk.cfg.max_stake_fraction,
        "max_total_exposure": risk.cfg.max_total_exposure_fraction,
        "max_entry_price": risk.cfg.max_entry_price,
        "min_entry_price": risk.cfg.min_entry_price,
    }
