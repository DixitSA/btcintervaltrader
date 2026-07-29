"""Fee model.

Fees are not a detail here -- on a near-coin-flip they are most of the reason
the game is negative sum.

Kalshi charges a taker fee UP FRONT on every fill, following

    fee = ceil(0.07 * C * P * (1 - P) * 100) / 100

where C is contracts and P the price in dollars. That peaks at 1.75c per
contract at P = 0.50 -- i.e. 3.5% of a 50c stake, paid on entry, and again on
exit if you stop out. It falls away toward the extremes, which is why cheap
longshots look deceptively cheap to trade.

The FeeModel protocol is kept so the risk layer depends on an interface rather
than on Kalshi's schedule directly: these are venue policy and they change.
"""

from __future__ import annotations

import math
from typing import Protocol


class FeeModel(Protocol):
    name: str

    def entry_fee(self, shares: float, price: float) -> float: ...

    def exit_fee(self, shares: float, price: float) -> float: ...

    def settlement_fee(self, shares: float, entry_price: float, won: bool) -> float: ...

    def round_trip_cost(self, shares: float, price: float) -> float:
        """Total fee burden assumed when deciding whether an edge survives."""
        ...


class KalshiFees:
    """Kalshi's published taker formula, charged per fill.

    Rounded UP to the next cent, per Kalshi's schedule -- so on small orders
    the rounding itself is a real cost. A 1-contract fill at any price pays at
    least 1 cent.
    """

    name = "kalshi"

    def __init__(self, taker_coefficient: float = 0.07, maker_coefficient: float = 0.0):
        self.taker_coefficient = taker_coefficient
        self.maker_coefficient = maker_coefficient

    def _fee(self, coefficient: float, shares: float, price: float) -> float:
        if shares <= 0 or coefficient <= 0:
            return 0.0
        price = min(max(price, 0.0), 1.0)
        raw_cents = coefficient * shares * price * (1.0 - price) * 100.0
        # Kill float representation error BEFORE the ceil. At the exact peak,
        # 0.07 * 100 * 0.5 * 0.5 * 100 evaluates to 175.00000000000003, which
        # ceils to 176 -- charging $1.76 where Kalshi's published schedule caps
        # taker fees on 100 contracts at $1.75. The 9dp round is far tighter
        # than a cent, so genuine round-ups (0.0175 -> 2c) are untouched.
        return math.ceil(round(raw_cents, 9)) / 100.0

    def entry_fee(self, shares: float, price: float) -> float:
        return self._fee(self.taker_coefficient, shares, price)

    def exit_fee(self, shares: float, price: float) -> float:
        return self._fee(self.taker_coefficient, shares, price)

    def maker_fee(self, shares: float, price: float) -> float:
        return self._fee(self.maker_coefficient, shares, price)

    def settlement_fee(self, shares: float, entry_price: float, won: bool) -> float:
        # Kalshi charges at trade time, not on settlement.
        return 0.0

    def round_trip_cost(self, shares: float, price: float) -> float:
        """Entry fee plus the expected cost of getting out.

        A winning contract settles at $1 with no exit fee, so only the losing
        branch pays twice. Being slightly conservative here is deliberate: the
        failure we care about is trading an edge that fees quietly eat.
        """
        entry = self.entry_fee(shares, price)
        # Probability-weighted exit: if it goes against us we sell into a
        # cheaper book, where the fee is smaller but nonzero.
        expected_exit = self.exit_fee(shares, price * 0.5)
        return entry + expected_exit * (1.0 - price)


def build_fee_model(venue: str, cfg) -> FeeModel:
    if venue != "kalshi":
        raise RuntimeError(f"no fee model for venue: {venue}")
    return KalshiFees(
        taker_coefficient=getattr(cfg, "kalshi_taker_coefficient", 0.07),
        maker_coefficient=getattr(cfg, "kalshi_maker_coefficient", 0.0),
    )
