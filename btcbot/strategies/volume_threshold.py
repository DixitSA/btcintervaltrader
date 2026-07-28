"""The "just bet when volume is over $500k" rule, implemented faithfully.

A note on what this rule actually is, because it matters for reading the
backtest output:

Volume is a *magnitude*, not a *direction*. Knowing that $500k has traded in a
window tells you how much interest there is; it says nothing about whether the
window resolves Up or Down. So the rule as usually stated is incomplete -- it
has no way to pick a side.

Rather than guess which half the original claim left out, this implementation
makes the direction an explicit parameter:

  direction="follow"    buy whichever side the market already favours
  direction="fade"      buy the side the market disfavours
  direction="up"        always buy Up
  direction="down"      always buy Down

Backtest all four. If the rule carries a real edge, one of them will beat
break-even net of fees on a decent sample. If none does -- which is what the
structure of these markets predicts -- you have your answer from your own data
instead of from a video.
"""

from __future__ import annotations

from typing import Optional

from ..models import DOWN, UP, Snapshot
from ..signals import favored_side, market_implied_up
from .base import Signal, Strategy


class VolumeThresholdStrategy(Strategy):
    name = "volume_threshold"

    def __init__(
        self,
        min_volume_usd: float = 500_000.0,
        direction: str = "follow",
        # How confident to claim the pick is. The honest default is "no better
        # than the market", which makes the edge calculation report ~zero and
        # correctly refuses to size up.
        assumed_edge: float = 0.0,
        **params,
    ):
        super().__init__(
            min_volume_usd=min_volume_usd,
            direction=direction,
            assumed_edge=assumed_edge,
            **params,
        )
        if direction not in ("follow", "fade", "up", "down"):
            raise ValueError(f"unknown direction: {direction}")
        self.min_volume_usd = float(min_volume_usd)
        self.direction = direction
        self.assumed_edge = float(assumed_edge)

    def decide(self, snap: Snapshot) -> Optional[Signal]:
        if snap.window_volume < self.min_volume_usd:
            return None

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
            if self.direction == "follow":
                side = fav
            else:
                side = DOWN if fav == UP else UP

        market_prob = p_up if side == UP else 1.0 - p_up
        # Claimed probability = market price plus whatever edge the user
        # asserts this rule has. With the default of 0.0 this is exactly the
        # market price, so the risk layer will find no edge and decline.
        prob = min(0.99, max(0.01, market_prob + self.assumed_edge))

        return Signal(
            side=side,
            prob=prob,
            reason=(
                f"volume ${snap.window_volume:,.0f} >= ${self.min_volume_usd:,.0f}, "
                f"direction={self.direction}, market_p={market_prob:.3f}"
            ),
        )
