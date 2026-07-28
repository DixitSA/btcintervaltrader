"""Fee models.

Fees are not a detail here -- on a near-coin-flip they are most of the reason
the game is negative sum, and Kalshi's and Polymarket's schedules are shaped so
differently that a strategy tuned on one can be nonsense on the other.

Polymarket: no taker fee historically, a percentage of PROFIT at settlement.
Kalshi:     a taker fee charged UP FRONT on every fill, following

                fee = ceil(0.07 * C * P * (1 - P) * 100) / 100

            where C is contracts and P the price in dollars. That peaks at
            1.75c per contract at P = 0.50 -- i.e. 3.5% of a 50c stake, paid
            on entry, and again on exit if you stop out. It falls away toward
            the extremes, which is why cheap longshots look deceptively cheap
            to trade.
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


class PolymarketFees:
    name = "polymarket"

    def __init__(self, taker_fee_bps: float = 0.0, winnings_fee_bps: float = 200.0):
        self.taker_fee_bps = taker_fee_bps
        self.winnings_fee_bps = winnings_fee_bps

    def entry_fee(self, shares: float, price: float) -> float:
        return shares * price * (self.taker_fee_bps / 10_000.0)

    def exit_fee(self, shares: float, price: float) -> float:
        return shares * price * (self.taker_fee_bps / 10_000.0)

    def settlement_fee(self, shares: float, entry_price: float, won: bool) -> float:
        if not won:
            return 0.0
        profit = max(0.0, shares * (1.0 - entry_price))
        return profit * (self.winnings_fee_bps / 10_000.0)

    def round_trip_cost(self, shares: float, price: float) -> float:
        expected_profit = shares * (1.0 - price)
        return self.entry_fee(shares, price) + expected_profit * (
            self.winnings_fee_bps / 10_000.0
        )


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
        raw = coefficient * shares * price * (1.0 - price)
        return math.ceil(raw * 100.0) / 100.0

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
    if venue == "kalshi":
        return KalshiFees(
            taker_coefficient=getattr(cfg, "kalshi_taker_coefficient", 0.07),
            maker_coefficient=getattr(cfg, "kalshi_maker_coefficient", 0.0),
        )
    return PolymarketFees(
        taker_fee_bps=cfg.taker_fee_bps,
        winnings_fee_bps=cfg.winnings_fee_bps,
    )
