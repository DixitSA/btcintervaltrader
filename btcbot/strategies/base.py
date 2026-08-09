"""Strategy interface.

A strategy answers one question: given what is visible right now, which side
would you buy and what do you believe its true probability is? It does NOT
decide size -- that is the risk layer's job, so that a strategy bug cannot
blow up the bankroll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..models import DOWN, UP, Snapshot
from ..signals import market_implied_up, microprice_up

# How a strategy reads the market's own probability off the two books.
#   mid        -- midpoint of each book. Assumes the next trade is equally
#                 likely to hit either side.
#   microprice -- size-weighted mid, which leans toward the thin side. Reduces
#                 to the mid exactly when the book is balanced.
# `mid` is the default because every recorded result in this repo was produced
# with it; switching changes the measured probability on every tick.
FAIR_VALUES = ("mid", "microprice")


@dataclass(frozen=True)
class Signal:
    side: str
    # The strategy's estimate of P(this side wins). Used for sizing and for
    # edge computation against the market price.
    prob: float
    reason: str = ""


class Strategy:
    name = "base"

    def __init__(
        self,
        fair_value: str = "mid",
        microprice_depth: int = 1,
        **params: Any,
    ):
        if fair_value not in FAIR_VALUES:
            raise ValueError(
                f"fair_value must be one of {FAIR_VALUES}, got {fair_value!r}"
            )
        self.fair_value = fair_value
        self.microprice_depth = int(microprice_depth)
        # Recorded in params too, so `describe()` and the shadow ledger both
        # say which estimator produced a result.
        self.params = dict(
            params, fair_value=fair_value, microprice_depth=self.microprice_depth
        )

    def market_up(self, snap: Snapshot) -> Optional[float]:
        """Market's implied P(Up) under THIS strategy's chosen estimator."""
        if self.fair_value == "microprice":
            return microprice_up(snap, depth=self.microprice_depth)
        return market_implied_up(snap)

    def favored_side(self, snap: Snapshot) -> Optional[str]:
        """Whichever side this strategy's own estimator prices above 50%.

        Deliberately not `signals.favored_side`, which always reads the mid. A
        strategy that prices with the microprice but picks its side from the mid
        would, on a skewed book, occasionally buy the side it had just decided
        was the underdog -- and nothing would crash to tell you. The side and
        the price must come from the same estimator.
        """
        p_up = self.market_up(snap)
        if p_up is None or abs(p_up - 0.5) < 1e-9:
            return None
        return UP if p_up > 0.5 else DOWN

    def decide(self, snap: Snapshot) -> Optional[Signal]:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}({self.params})"
