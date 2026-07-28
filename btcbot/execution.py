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
from .models import Fill, Order, Snapshot

log = logging.getLogger(__name__)


class Executor(Protocol):
    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]: ...


class PaperExecutor:
    """Simulates a fill by walking the recorded ask side of the book."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
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

        fee = order.shares * price * (self.cfg.fees.taker_fee_bps / 10_000.0)
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
    def __init__(self, cfg: Config):
        if not cfg.is_live:
            raise RuntimeError("LiveExecutor requires mode: live")
        if not require_live_confirmation():
            raise RuntimeError(
                "live trading blocked. Set BTCBOT_I_UNDERSTAND_REAL_MONEY=yes to enable. "
                "Do this only after a paper run over a real dataset showed a positive "
                "net edge on a meaningful sample."
            )
        from .clob import LiveOrderClient

        self.cfg = cfg
        self.client = LiveOrderClient(host=cfg.clob_url)
        self.fills: list[Fill] = []

    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        token_id = snap.market.token_id(order.side)
        try:
            resp = self.client.submit(token_id, order)
        except Exception as exc:  # noqa: BLE001 - never let a venue error kill the loop
            log.error("live order failed: %s", exc)
            return None

        if not resp or not resp.get("success", True):
            log.error("live order rejected: %s", resp)
            return None

        # The venue is the source of truth for the actual fill price; fall back
        # to the limit only when it does not tell us.
        price = float(resp.get("price") or order.limit_price)
        shares = float(resp.get("size") or order.shares)
        fill = Fill(
            ts=time.time(),
            market_slug=snap.market.slug,
            side=order.side,
            shares=shares,
            price=price,
            fee=shares * price * (self.cfg.fees.taker_fee_bps / 10_000.0),
        )
        self.fills.append(fill)
        log.info("LIVE fill: %s %.2f @ %.3f", order.side, shares, price)
        return fill


def build_executor(cfg: Config) -> Executor:
    if cfg.is_live:
        return LiveExecutor(cfg)
    return PaperExecutor(cfg)
