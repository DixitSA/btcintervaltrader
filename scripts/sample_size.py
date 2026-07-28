#!/usr/bin/env python3
"""How long must the recorder run before a result can mean anything?

    python scripts/sample_size.py

A 15-minute binary is an extremely noisy instrument: the per-trade ROI
standard deviation is close to 1.0 because almost every trade returns roughly
-100% or +100%. Detecting a small mean inside that much noise takes a lot of
trades, and this prints how many.

    t = mean / (sd / sqrt(n))   ->   n = (2 * sd / mean)^2   for |t| = 2

The only quantity needing measurement is sd, taken here from the repo's own
simulator so the payoff shape is realistic rather than assumed.

Two things this table does NOT include, both of which make the real
requirement larger:

  * Fees. Kalshi charges the taker on entry and again on exit, about 3.5% of
    a 50c stake each way. An edge has to clear roughly 7% per round trip
    before it is worth anything at all, so read the small-edge rows as
    "detectable", not "profitable".
  * Selection. The table assumes you fixed the strategy in advance. `sweep`
    tries 20 cells, and picking the best of 20 needs a stricter bar than
    |t| > 2 -- which means still more data.
"""

from __future__ import annotations

import logging
import shutil
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.disable(logging.WARNING)

from btcbot.backtest import run_backtest
from btcbot.config import Config
from btcbot.recorder import load_dataset
from btcbot.simulate import generate
from btcbot.strategies import build_strategy

WINDOWS_PER_DAY = 24 * 4  # KXBTC15M: a window every 15 minutes


def research_cfg() -> Config:
    cfg = Config()
    cfg.risk.max_trades_per_hour = 10**9
    cfg.risk.daily_loss_limit_usd = 1e12
    cfg.risk.bankroll_usd = 100_000.0
    cfg.exits.max_drawdown_pct = None
    cfg.exits.max_drawdown_usd = None
    return cfg


def measure_sd(n_windows: int = 400, seed: int = 99) -> tuple[float, float, int]:
    tmp = Path(tempfile.mkdtemp(prefix="samplesize_"))
    try:
        generate(tmp, n_windows=n_windows, seed=seed, families=["KXBTC15M"])
        snaps = load_dataset(tmp)
        report = run_backtest(
            snaps,
            build_strategy(
                "volume_threshold",
                {"min_volume_usd": 0, "direction": "follow", "assumed_edge": 0.05},
            ),
            research_cfg(),
            use_exits=False,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    rets = report.trade_returns
    if len(rets) < 2:
        raise SystemExit("not enough simulated trades to measure dispersion")
    return statistics.mean(rets), statistics.stdev(rets), len(rets)


def main() -> int:
    mean, sd, n = measure_sd()
    print(f"measured on {n} simulated trades")
    print(f"  per-trade ROI mean   : {mean:+.4f}")
    print(f"  per-trade ROI stdev  : {sd:.4f}")
    print(f"\nKXBTC15M windows per day: {WINDOWS_PER_DAY}\n")

    print(f"{'true edge / trade':>18} {'trades needed':>15} {'days recording':>16}")
    print("-" * 52)
    for edge in (0.20, 0.10, 0.05, 0.03, 0.02, 0.01):
        needed = (2.0 * sd / edge) ** 2
        print(f"{edge:>17.0%} {needed:>15,.0f} {needed / WINDOWS_PER_DAY:>16,.1f}")

    print(
        "\nFees cost ~7% per round trip, so an edge below that loses money even "
        "when\nit is real. Treat the 1-3% rows as 'you will never resolve this', "
        "not as a plan."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
