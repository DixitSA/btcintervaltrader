"""Order execution.

Two executors with the same interface. Paper is the default everywhere; live
requires both `mode: live` in config AND an explicit environment opt-in, so
that no config typo can move real money.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

from .config import Config, require_live_confirmation
from .fees import build_fee_model
from .models import Fill, Order, Snapshot

log = logging.getLogger(__name__)


class Executor(Protocol):
    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]: ...

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]: ...


class PaperExecutor:
    """Simulates a fill by walking the recorded ask side of the book."""

    def __init__(self, cfg: Config, fee_model=None):
        self.cfg = cfg
        self.fee_model = fee_model or build_fee_model(cfg.venue, cfg.fees)
        self.fills: list[Fill] = []

    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        book = snap.book(order.side)
        avg = book.sweep_cost(order.shares)
        if avg is None:
            log.info("paper: book too thin for %.2f shares", order.shares)
            return None

        price = avg + self.cfg.fees.slippage
        # The limit is rounded to 3dp when the order is built, so compare with a
        # tolerance -- otherwise float noise rejects fills that are exactly at
        # the limit.
        if price > order.limit_price + 1e-6:
            log.debug("paper: fill %.4f worse than limit %.4f, no trade", price, order.limit_price)
            return None

        fee = self.fee_model.entry_fee(order.shares, price)
        fill = Fill(
            ts=snap.ts,
            market_slug=snap.market.slug,
            side=order.side,
            shares=order.shares,
            price=price,
            fee=fee,
        )
        self.fills.append(fill)
        return fill

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        """Exit by hitting the bid side. Slippage works against us here too."""
        book = snap.book(order.side)
        avg = book.sweep_proceeds(order.shares)
        if avg is None:
            log.debug("paper: not enough bid depth to exit %.2f shares", order.shares)
            return None

        price = max(0.0, avg - self.cfg.fees.slippage)
        if price < order.limit_price - 1e-6:
            log.debug(
                "paper: exit %.4f below floor %.4f, holding", price, order.limit_price
            )
            return None

        fee = self.fee_model.exit_fee(order.shares, price)
        fill = Fill(
            ts=snap.ts,
            market_slug=snap.market.slug,
            side=order.side,
            shares=order.shares,
            price=price,
            fee=fee,
        )
        self.fills.append(fill)
        return fill


class LiveExecutor:
    """Places real orders through whichever venue is configured.

    UNVERIFIED against a real venue from this repo -- place one minimum-size
    order by hand and confirm it in the web UI before running unattended.
    """

    def __init__(self, cfg: Config, venue=None, fee_model=None):
        if not cfg.is_live:
            raise RuntimeError("LiveExecutor requires mode: live")
        if not require_live_confirmation():
            raise RuntimeError(
                "live trading blocked. Set BTCBOT_I_UNDERSTAND_REAL_MONEY=yes to enable. "
                "Do this only after a paper run over a real dataset showed a positive "
                "net edge on a meaningful sample."
            )

        if venue is None:
            from .venues import build_venue

            venue = build_venue(cfg)

        self.cfg = cfg
        self.venue = venue
        self.fee_model = fee_model or build_fee_model(cfg.venue, cfg.fees)
        self.fills: list[Fill] = []

    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        return self._submit(snap, order, selling=False)

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        return self._submit(snap, order, selling=True)

    @staticmethod
    def _extract(resp: dict, *names: str) -> Optional[float]:
        for name in names:
            value = resp.get(name)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return None

    def _submit(self, snap: Snapshot, order: Order, selling: bool) -> Optional[Fill]:
        try:
            resp = self.venue.place_order(snap.market, order, selling=selling)
        except Exception as exc:  # noqa: BLE001 - never let a venue error kill the loop
            log.error("live order failed: %s", exc)
            return None

        if not resp or not resp.get("success", True):
            log.error("live order rejected: %s", resp)
            return None

        # Kalshi nests the order under "order"; unwrap before reading fills.
        payload = resp.get("order") if isinstance(resp.get("order"), dict) else resp

        price = self._extract(payload, "price", "yes_price", "no_price", "avgPrice")
        if price is not None and price > 1.0:
            price = price / 100.0  # Kalshi reports cents
        shares = self._extract(payload, "count", "filled_count", "size", "taker_fill_count")

        price = price if price is not None else order.limit_price
        shares = shares if shares is not None else order.shares

        fee = (
            self.fee_model.exit_fee(shares, price)
            if selling
            else self.fee_model.entry_fee(shares, price)
        )
        fill = Fill(
            ts=time.time(),
            market_slug=snap.market.slug,
            side=order.side,
            shares=shares,
            price=price,
            fee=fee,
        )
        self.fills.append(fill)
        log.info(
            "LIVE %s fill: %s %.2f @ %.3f (fee $%.2f)",
            "sell" if selling else "buy",
            order.side,
            shares,
            price,
            fee,
        )
        return fill


def build_executor(cfg: Config, venue=None) -> Executor:
    backend = cfg.execution.backend

    if backend == "paper":
        return PaperExecutor(cfg)

    if not cfg.is_live:
        raise RuntimeError(
            f"execution backend '{backend}' places real orders but mode is "
            f"'{cfg.mode}'. Set mode: live to use it, or backend: paper to simulate."
        )
    if not require_live_confirmation():
        raise RuntimeError(
            "live trading blocked. Set BTCBOT_I_UNDERSTAND_REAL_MONEY=yes to enable. "
            "Do this only after a paper run over a real dataset showed a positive "
            "net edge on a meaningful sample."
        )

    if backend in ("venue", "kalshi"):
        return LiveExecutor(cfg, venue=venue)
    raise RuntimeError(f"unknown execution backend: {backend}")
