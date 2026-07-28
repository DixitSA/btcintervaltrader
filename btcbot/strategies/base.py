"""Strategy interface.

A strategy answers one question: given what is visible right now, which side
would you buy and what do you believe its true probability is? It does NOT
decide size -- that is the risk layer's job, so that a strategy bug cannot
blow up the bankroll.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..models import Snapshot


@dataclass(frozen=True)
class Signal:
    side: str
    # The strategy's estimate of P(this side wins). Used for sizing and for
    # edge computation against the market price.
    prob: float
    reason: str = ""


class Strategy:
    name = "base"

    def __init__(self, **params: Any):
        self.params = params

    def decide(self, snap: Snapshot) -> Optional[Signal]:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}({self.params})"
