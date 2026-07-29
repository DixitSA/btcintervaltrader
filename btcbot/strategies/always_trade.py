"""Always trade: place one bet per window, every window.

Removes every gate except the basic market eligibility checks (remaining time,
book depth, spread). The volume filter, edge threshold, and volatility
requirement are all bypassed — this strategy exists to guarantee a full
observation set so the calibrator has data to work with.

Direction is configurable just like volume_threshold's:
  follow  = buy the market's favourite
  fade    = buy the market's underdog
  up      = always buy Up
  down    = always buy Down

Without a claim to any edge (assumed_edge=0), every trade will be the minimum
Kelly size at the gate — tiny bets that build a track record without
meaningful risk.
"""

from __future__ import annotations

from typing import Optional

from ..models import DOWN, UP, Snapshot
from ..signals import favored_side, market_implied_up
from .base import Signal, Strategy


class AlwaysTradeStrategy(Strategy):
    name = "always_trade"

    def __init__(
        self,
        direction: str = "follow",
        assumed_edge: float = 0.06,
        **params,
    ):
        super().__init__(direction=direction, assumed_edge=assumed_edge, **params)
        if direction not in ("follow", "fade", "up", "down"):
            raise ValueError(f"unknown direction: {direction}")
        self.direction = direction
        self.assumed_edge = float(assumed_edge)

    def decide(self, snap: Snapshot) -> Optional[Signal]:
        p_up = market_implied_up(snap)
        if p_up is None:
            return None

        if self.direction == "up":
            side = UP
        elif self.direction == "down":
            side = DOWN
        else:
            fav = favored_side(snap)
            if fav is None:
                return None
            side = fav if self.direction == "follow" else (DOWN if fav == UP else UP)

        market_prob = p_up if side == UP else 1.0 - p_up
        prob = min(0.99, max(0.01, market_prob + self.assumed_edge))

        return Signal(
            side=side,
            prob=prob,
            reason=(
                f"always_trade direction={self.direction}, "
                f"market_p={market_prob:.3f}, assumed_edge={self.assumed_edge:.3f}"
            ),
        )
