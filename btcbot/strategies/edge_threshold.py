"""Model-vs-market strategy: trade only when the book disagrees with a
volatility model by more than fees can explain.

This is the shape a real edge in these markets has to take. It is not a money
printer -- it will mostly decline to trade, which is the point. When it does
fire it is because the quoted price implies a probability that a zero-drift
walk from current spot says is wrong by more than the round-trip cost.

The hard part is not this code. It is that your spot feed must match the oracle
Kalshi actually settles on -- the CF Benchmarks real-time index for the asset,
averaged over the last sixty seconds, not an instantaneous Binance print -- and
your latency must be competitive with everyone else running the same idea.
See spot.py.
"""

from __future__ import annotations

from typing import Optional

from ..models import DOWN, UP, Snapshot
from ..signals import annualize, fair_probability_up, market_implied_up
from .base import Signal, Strategy


class EdgeThresholdStrategy(Strategy):
    name = "edge_threshold"

    def __init__(
        self,
        min_edge: float = 0.05,
        vol_per_year: float = 0.60,
        # Measure vol over the last window by default rather than assume it.
        # Set to None to fall back to the fixed `vol_per_year`, but read the
        # warning on _vol() before you do.
        realized_vol_window: Optional[float] = 900.0,
        max_prob: float = 0.95,
        **params,
    ):
        super().__init__(
            min_edge=min_edge,
            vol_per_year=vol_per_year,
            realized_vol_window=realized_vol_window,
            max_prob=max_prob,
            **params,
        )
        self.min_edge = float(min_edge)
        self.vol_per_year = float(vol_per_year)
        self.realized_vol_window = realized_vol_window
        self.max_prob = float(max_prob)
        # Optionally injected by the runner/backtester each tick.
        self.current_realized_vol: Optional[float] = None

    def _vol(self) -> Optional[float]:
        """Annualized vol to price with, or None if it cannot be measured.

        WHY THIS REFUSES TO GUESS. The whole signal here is model-vs-market, so
        the volatility is not a tuning knob -- it IS the edge. Getting it wrong
        does not weaken the signal, it fabricates one.

        Measured against live KXBTC15M spot, BTC realized vol ran ~24% while
        the old hardcoded default assumed 60%. With spot $50 above the strike
        and 450s left, that gap alone puts the model 17 POINTS below the fair
        price -- more than three times the 5-point trigger. The sign is
        negative on every upward move, so the strategy would have faded every
        rally into a correctly priced book, systematically, and paid ~7%
        round-trip fees for the privilege.

        So when a measurement window is configured but no measurement has
        arrived yet, this returns None and the strategy declines to trade,
        rather than falling back to a constant that has no claim to being right.
        """
        if self.realized_vol_window:
            if not self.current_realized_vol:
                return None
            ann = annualize(self.current_realized_vol, float(self.realized_vol_window))
            return ann if ann and ann > 0 else None
        return self.vol_per_year

    def decide(self, snap: Snapshot) -> Optional[Signal]:
        if snap.spot is None or snap.market.strike is None:
            # No strike parsed means we cannot know which way is "up".
            return None

        remaining = snap.market.seconds_remaining(snap.ts)
        if remaining <= 0:
            return None

        vol = self._vol()
        if vol is None:
            # No calibrated volatility yet -- an uncalibrated model would
            # invent edge rather than find it. See _vol().
            return None

        model_up = fair_probability_up(
            spot=snap.spot,
            strike=snap.market.strike,
            seconds_remaining=remaining,
            vol_per_year=vol,
        )
        if model_up is None:
            return None

        market_up = market_implied_up(snap)
        if market_up is None:
            return None

        edge_up = model_up - market_up
        if abs(edge_up) < self.min_edge:
            return None

        if edge_up > 0:
            side, prob = UP, model_up
        else:
            side, prob = DOWN, 1.0 - model_up

        if prob > self.max_prob:
            return None

        return Signal(
            side=side,
            prob=prob,
            reason=(
                f"model_p_up={model_up:.3f} vs market_p_up={market_up:.3f} "
                f"(edge {edge_up:+.3f}), {remaining:.0f}s left"
            ),
        )
