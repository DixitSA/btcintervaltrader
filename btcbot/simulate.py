"""Synthetic dataset generator.

Generates recorded-format snapshots from a zero-drift random walk, with market
prices set to the model-fair probability plus a spread and some noise.

Two uses:

1. It lets you exercise the whole harness end to end before you have collected
   any real data.
2. It is a control. In this world there is *by construction* no edge in volume,
   because volume is generated independently of the price path. If a strategy
   shows a large positive z-score here, the bug is in the strategy or the
   backtester -- not a discovery.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Optional

from .models import Book, Level, Market, Snapshot
from .recorder import SnapshotWriter
from .signals import fair_probability_up


def _book_around(prob: float, spread: float, depth_shares: float, levels: int = 5) -> Book:
    """Build a plausible two-sided book centred on `prob`."""
    prob = min(max(prob, 0.02), 0.98)
    bid = max(0.01, prob - spread / 2.0)
    ask = min(0.99, prob + spread / 2.0)
    return Book(
        bids=[
            Level(price=round(max(0.01, bid - i * 0.01), 3), size=depth_shares * (1 + i * 0.5))
            for i in range(levels)
        ],
        asks=[
            Level(price=round(min(0.99, ask + i * 0.01), 3), size=depth_shares * (1 + i * 0.5))
            for i in range(levels)
        ],
    )


def generate(
    out_dir: str | Path,
    n_windows: int = 400,
    window_seconds: int = 900,
    tick_seconds: int = 15,
    start_spot: float = 100_000.0,
    vol_per_year: float = 0.60,
    spread: float = 0.02,
    depth_shares: float = 200.0,
    volume_low: float = 5_000.0,
    volume_high: float = 900_000.0,
    seed: Optional[int] = 42,
    start_ts: float = 1_750_000_000.0,
    families: Optional[list[str]] = None,
) -> int:
    """Write `n_windows` synthetic windows per family. Returns snapshots written.

    Passing more than one family produces markets whose windows OVERLAP in
    time, which is what exercises concurrent positions and the total-exposure
    cap. Each family walks its own independent price path.
    """
    if families and len(families) > 1:
        total = 0
        for i, fam in enumerate(families):
            total += generate(
                out_dir,
                n_windows=n_windows,
                window_seconds=window_seconds,
                tick_seconds=tick_seconds,
                start_spot=start_spot,
                vol_per_year=vol_per_year,
                spread=spread,
                depth_shares=depth_shares,
                volume_low=volume_low,
                volume_high=volume_high,
                seed=(seed + i * 977) if seed is not None else None,
                start_ts=start_ts,
                families=[fam],
            )
        return total

    prefix = families[0] if families else "btc-updown-15m"
    rng = random.Random(seed)
    written = 0
    spot = start_spot

    with SnapshotWriter(out_dir) as writer:
        for w in range(n_windows):
            w_start = start_ts + w * window_seconds
            w_end = w_start + window_seconds
            strike = spot

            # Volume for this window is drawn independently of the price path:
            # that independence is exactly the null hypothesis being tested.
            total_volume = rng.uniform(volume_low, volume_high)

            sigma_per_tick = vol_per_year * math.sqrt(tick_seconds / (365.0 * 24.0 * 3600.0))

            n_ticks = window_seconds // tick_seconds
            for i in range(n_ticks):
                ts = w_start + i * tick_seconds
                remaining = w_end - ts

                spot *= math.exp(rng.gauss(-0.5 * sigma_per_tick**2, sigma_per_tick))

                p_up = fair_probability_up(spot, strike, remaining, vol_per_year)
                if p_up is None:
                    continue
                # Market is fair plus noise, i.e. no systematic mispricing.
                noisy = min(0.98, max(0.02, p_up + rng.gauss(0.0, 0.01)))

                cumulative = total_volume * ((i + 1) / n_ticks)

                market = Market(
                    condition_id=f"0xsim{abs(hash(prefix))%9999:04d}{w:06d}",
                    slug=f"{prefix}-sim-{int(w_start)}",
                    question=f"Bitcoin Up or Down? Price to beat ${strike:,.2f}",
                    up_token_id=f"{prefix}-up-{w}",
                    down_token_id=f"{prefix}-down-{w}",
                    start_ts=w_start,
                    end_ts=w_end,
                    volume=cumulative,
                    liquidity=depth_shares * 2,
                    strike=strike,
                )

                writer.write(
                    Snapshot(
                        ts=ts,
                        market=market,
                        up_book=_book_around(noisy, spread, depth_shares),
                        down_book=_book_around(1.0 - noisy, spread, depth_shares),
                        spot=spot,
                        window_volume=cumulative,
                    )
                )
                written += 1

            # Final snapshot at expiry so the backtester can read the outcome.
            final_up = 1.0 if spot > strike else 0.0
            market_final = Market(
                condition_id=f"0xsim{abs(hash(prefix))%9999:04d}{w:06d}",
                slug=f"{prefix}-sim-{int(w_start)}",
                question=f"Bitcoin Up or Down? Price to beat ${strike:,.2f}",
                up_token_id=f"{prefix}-up-{w}",
                down_token_id=f"{prefix}-down-{w}",
                start_ts=w_start,
                end_ts=w_end,
                volume=total_volume,
                liquidity=depth_shares * 2,
                strike=strike,
            )
            writer.write(
                Snapshot(
                    ts=w_end,
                    market=market_final,
                    up_book=_book_around(final_up, 0.01, depth_shares),
                    down_book=_book_around(1.0 - final_up, 0.01, depth_shares),
                    spot=spot,
                    window_volume=total_volume,
                )
            )
            written += 1

    return written
