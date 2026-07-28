"""Order execution.

Two executors with the same interface. Paper is the default everywhere; live
requires both `mode: live` in config AND an explicit environment opt-in, so
that no config typo can move real money.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from typing import Optional, Protocol

from .config import Config, require_live_confirmation
from .models import Fill, Order, Snapshot

log = logging.getLogger(__name__)


class Executor(Protocol):
    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]: ...

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]: ...


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
        return self._submit(snap, order, selling=False)

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        return self._submit(snap, order, selling=True)

    def _submit(self, snap: Snapshot, order: Order, selling: bool) -> Optional[Fill]:
        token_id = snap.market.token_id(order.side)
        try:
            resp = self.client.submit(token_id, order, selling=selling)
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
        log.info(
            "LIVE %s fill: %s %.2f @ %.3f",
            "sell" if selling else "buy",
            order.side,
            shares,
            price,
        )
        return fill


class BullpenExecutor:
    """Executes via the external `bullpen` CLI instead of signing orders here.

    The exact command line is CONFIGURATION, not a hardcoded guess. The flag
    syntax could not be verified from this environment (cli.bullpen.fi is
    blocked by network policy), and guessing flags on a path that moves money
    is how you get silent no-ops or wrong-sized orders.

    Run `btcbot verify-bullpen` to check the configured invocation against the
    real binary before trading. Adjust `execution.bullpen.buy_template` in
    config.yaml to match `bullpen polymarket buy --help` on your machine.

    Template placeholders: {token_id} {side} {shares} {price} {slug}
    {condition_id} {outcome}
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bp = cfg.execution.bullpen
        self.fills: list[Fill] = []
        if not shutil.which(self.bp.binary):
            raise RuntimeError(
                f"bullpen binary '{self.bp.binary}' not found on PATH. "
                "Install the Bullpen CLI, or set execution.bullpen.binary."
            )

    def render(self, snap: Snapshot, order: Order, selling: bool = False) -> list[str]:
        template = self.bp.sell_template if selling else self.bp.buy_template
        values = {
            "token_id": snap.market.token_id(order.side),
            "side": order.side.lower(),
            "outcome": order.side,
            "shares": f"{order.shares:.2f}",
            "price": f"{order.limit_price:.3f}",
            "slug": snap.market.slug,
            "condition_id": snap.market.condition_id,
        }
        field = "sell_template" if selling else "buy_template"
        try:
            return [part.format(**values) for part in template]
        except KeyError as exc:
            raise RuntimeError(
                f"unknown placeholder {exc} in execution.bullpen.{field}"
            ) from exc

    def buy(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        return self._execute(snap, order, selling=False)

    def sell(self, snap: Snapshot, order: Order) -> Optional[Fill]:
        return self._execute(snap, order, selling=True)

    def _execute(self, snap: Snapshot, order: Order, selling: bool) -> Optional[Fill]:
        argv = self.render(snap, order, selling=selling)
        log.info("bullpen: %s", " ".join(argv))

        if self.bp.dry_run:
            log.warning("bullpen dry_run=true; command NOT executed")
            return None

        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.bp.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("bullpen invocation failed: %s", exc)
            return None

        if proc.returncode != 0:
            log.error(
                "bullpen exited %d: %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[:500],
            )
            return None

        price, shares = self._parse_fill(proc.stdout)
        fill = Fill(
            ts=time.time(),
            market_slug=snap.market.slug,
            side=order.side,
            shares=shares if shares is not None else order.shares,
            price=price if price is not None else order.limit_price,
            fee=0.0,
        )
        if price is None:
            log.warning(
                "could not parse a fill price from bullpen output; assuming the "
                "limit price. P&L tracking will be approximate. Raw output: %s",
                proc.stdout.strip()[:300],
            )
        self.fills.append(fill)
        log.info(
            "bullpen %s fill: %s %.2f @ %.3f",
            "sell" if selling else "buy",
            fill.side,
            fill.shares,
            fill.price,
        )
        return fill

    @staticmethod
    def _parse_fill(stdout: str) -> tuple[Optional[float], Optional[float]]:
        """Best-effort extraction of (price, shares) from CLI output.

        Returns (None, None) rather than a fabricated number when the shape is
        unrecognised -- callers must not treat a guess as a confirmed fill.
        """
        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            return None, None

        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None, None

        for key in ("data", "order", "result", "fill"):
            inner = data.get(key)
            if isinstance(inner, dict):
                data = {**data, **inner}

        def pick(*names: str) -> Optional[float]:
            for name in names:
                val = data.get(name)
                if isinstance(val, (int, float)):
                    return float(val)
                if isinstance(val, str):
                    try:
                        return float(val)
                    except ValueError:
                        continue
            return None

        return (
            pick("avgPrice", "average_price", "fillPrice", "filled_price", "price"),
            pick("filledSize", "filled_size", "matched", "size", "shares"),
        )


def build_executor(cfg: Config) -> Executor:
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

    if backend == "bullpen":
        return BullpenExecutor(cfg)
    if backend == "clob":
        return LiveExecutor(cfg)
    raise RuntimeError(f"unknown execution backend: {backend}")
