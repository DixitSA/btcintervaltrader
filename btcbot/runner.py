"""The live loop: discover windows, build snapshots, optionally trade.

`record=True, trade=False` is the data-collection mode you should run first.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .clob import ClobClient
from .config import Config
from .execution import build_executor
from .markets import GammaClient
from .models import Fill, Market, Snapshot
from .recorder import SnapshotWriter
from .risk import RiskManager
from .spot import SpotFeed
from .strategies.base import Strategy

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        cfg: Config,
        strategy: Optional[Strategy] = None,
        record: bool = True,
        trade: bool = False,
    ):
        self.cfg = cfg
        self.strategy = strategy
        self.record = record
        self.trade = trade

        self.gamma = GammaClient(cfg.gamma_url)
        self.clob = ClobClient(cfg.clob_url)
        self.spot = SpotFeed(cfg.spot_url)
        self.writer = SnapshotWriter(cfg.data_dir) if record else None
        self.risk = RiskManager(cfg.risk, cfg.fees, cfg.markets)
        self.executor = build_executor(cfg) if trade else None

        # slug -> fill, for windows we are holding to expiry.
        self.open_fills: dict[str, Fill] = {}
        self._traded_windows: set[str] = set()
        self._market_cache: tuple[float, list[Market]] = (0.0, [])

    def close(self) -> None:
        self.gamma.close()
        self.clob.close()
        self.spot.close()
        if self.writer:
            self.writer.close()

    def _markets(self, now: float) -> list[Market]:
        cached_at, cached = self._market_cache
        if now - cached_at < 20.0 and cached:
            return cached
        try:
            markets = self.gamma.fetch_open_markets(self.cfg.markets.slug_prefix)
        except Exception as exc:  # noqa: BLE001 - a discovery blip must not kill the loop
            log.warning("market discovery failed: %s", exc)
            return cached
        self._market_cache = (now, markets)
        return markets

    def _snapshot(self, market: Market, spot_px: Optional[float], now: float) -> Optional[Snapshot]:
        try:
            books = self.clob.get_books([market.up_token_id, market.down_token_id])
        except Exception as exc:  # noqa: BLE001
            log.warning("book fetch failed for %s: %s", market.slug, exc)
            return None

        up_book = books.get(market.up_token_id)
        down_book = books.get(market.down_token_id)
        if up_book is None or down_book is None:
            return None

        return Snapshot(
            ts=now,
            market=market,
            up_book=up_book,
            down_book=down_book,
            spot=spot_px,
            window_volume=market.volume,
        )

    def _settle_expired(self, now: float) -> None:
        for slug, fill in list(self.open_fills.items()):
            # We only know the true outcome once the venue resolves; until then
            # release the position slot a little after expiry so the bot can
            # keep trading, and reconcile P&L from the venue separately.
            if now > fill.ts + self.cfg.markets.window_seconds + 60:
                log.info("window %s expired; releasing position slot", slug)
                self.risk.on_settlement(0.0)
                del self.open_fills[slug]

    def tick(self) -> None:
        now = time.time()
        spot_px = self.spot.price()
        self._settle_expired(now)

        for market in self._markets(now):
            remaining = market.seconds_remaining(now)
            if remaining <= 0 or remaining > self.cfg.markets.max_seconds_remaining:
                continue

            snap = self._snapshot(market, spot_px, now)
            if snap is None:
                continue

            if self.writer:
                self.writer.write(snap)

            if not (self.trade and self.strategy and self.executor):
                continue
            if market.slug in self._traded_windows:
                continue

            signal = self.strategy.decide(snap)
            if signal is None:
                continue

            order, rejection = self.risk.evaluate(snap, signal)
            if order is None:
                log.debug("%s: declined (%s)", market.slug, rejection)
                continue

            fill = self.executor.buy(snap, order)
            if fill is None:
                continue

            self.risk.on_trade(now)
            self.open_fills[market.slug] = fill
            self._traded_windows.add(market.slug)
            log.info(
                "%s: bought %s %.2f @ %.3f | %s",
                market.slug,
                fill.side,
                fill.shares,
                fill.price,
                order.reason,
            )

    def run(self, max_ticks: Optional[int] = None) -> None:
        mode = "LIVE" if self.cfg.is_live and self.trade else ("paper" if self.trade else "record-only")
        log.info(
            "starting runner in %s mode (strategy=%s)",
            mode,
            self.strategy.describe() if self.strategy else "none",
        )
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                start = time.time()
                try:
                    self.tick()
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("tick failed: %s", exc)
                ticks += 1
                elapsed = time.time() - start
                time.sleep(max(0.0, self.cfg.poll_seconds - elapsed))
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        finally:
            self.close()
