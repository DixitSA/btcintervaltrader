"""Model-vs-market strategy: trade only when the book disagrees with a
volatility model by more than fees can explain.

This is the shape a real edge in these markets has to take. It is not a money
printer -- it will mostly decline to trade, which is the point. When it does
fire it is because the quoted price implies a probability that a zero-drift
walk from current spot says is wrong by more than the round-trip cost.

The hard part is not this code. It is that your spot feed must match the
oracle Polymarket actually settles on, and your latency must be competitive
with everyone else running the same idea. See spot.py.
"""

from __future__ import annotations

from typing import Optional

from ..models import DOWN, UP, Snapshot
from ..signals import annualize, fair_probability_up, market_implied_up, microprice_up
from .base import Signal, Strategy

FAIR_VALUES = ("mid", "microprice")


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
        # How to read the market's own probability out of the two books.
        # "mid" is the default because it is what every recorded result in this
        # repo was produced with; switching it changes the measured edge on
        # every tick, so it is a deliberate choice, not a free upgrade.
        fair_value: str = "mid",
        microprice_depth: int = 1,
        **params,
    ):
        super().__init__(
            min_edge=min_edge,
            vol_per_year=vol_per_year,
            realized_vol_window=realized_vol_window,
            max_prob=max_prob,
            fair_value=fair_value,
            microprice_depth=microprice_depth,
            **params,
        )
        if fair_value not in FAIR_VALUES:
            raise ValueError(
                f"fair_value must be one of {FAIR_VALUES}, got {fair_value!r}"
            )
        self.min_edge = float(min_edge)
        self.vol_per_year = float(vol_per_year)
        self.realized_vol_window = realized_vol_window
        self.max_prob = float(max_prob)
        self.fair_value = fair_value
        self.microprice_depth = int(microprice_depth)
        # Optionally injected by the runner/backtester each tick.
        self.current_realized_vol: Optional[float] = None

    def _market_up(self, snap: Snapshot) -> Optional[float]:
        if self.fair_value == "microprice":
            return microprice_up(snap, depth=self.microprice_depth)
        return market_implied_up(snap)

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

        market_up = self._market_up(snap)
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
                f"[{self.fair_value}] (edge {edge_up:+.3f}), {remaining:.0f}s left"
            ),
        )
