"""The live loop: discover windows, build snapshots, optionally trade.

`record=True, trade=False` is the data-collection mode you should run first.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import Config
from .execution import build_executor
from .exits import DrawdownGuard, ExitPolicy
from .fees import build_fee_model
from .models import Market, Order, Snapshot
from .portfolio import Portfolio, mark_for
from .recorder import SnapshotWriter
from .risk import RiskManager
from .spot import SpotFeed
from .strategies.base import Strategy
from .venues import build_venue

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        cfg: Config,
        strategy: Optional[Strategy] = None,
        record: bool = True,
        trade: bool = False,
        venue=None,
    ):
        self.cfg = cfg
        self.strategy = strategy
        self.record = record
        self.trade = trade

        self.venue = venue if venue is not None else build_venue(cfg)
        self.spot = SpotFeed(cfg.spot_url)
        self.writer = SnapshotWriter(cfg.data_dir) if record else None
        self.fee_model = build_fee_model(cfg.venue, cfg.fees)
        self.risk = RiskManager(
            cfg.risk, cfg.fees, cfg.markets, fee_model=self.fee_model
        )
        self.executor = build_executor(cfg, venue=self.venue) if trade else None

        self.portfolio = Portfolio(cfg.risk.bankroll_usd)
        self.exit_policy = ExitPolicy(cfg.exits)
        self.guard = DrawdownGuard(cfg.exits.max_drawdown_usd, cfg.exits.max_drawdown_pct)

        self._traded_windows: set[str] = set()
        self._market_cache: tuple[float, list[Market]] = (0.0, [])
        # slug -> window end, so families with different durations (5m/15m/1h)
        # each settle at their own expiry rather than a single global one.
        self._window_end: dict[str, float] = {}

    def close(self) -> None:
        self.venue.close()
        self.spot.close()
        if self.writer:
            self.writer.close()

    def _markets(self, now: float) -> list[Market]:
        cached_at, cached = self._market_cache
        if now - cached_at < 20.0 and cached:
            return cached
        try:
            markets = self.venue.discover_markets(self.cfg.markets.slug_prefixes)
        except Exception as exc:  # noqa: BLE001 - a discovery blip must not kill the loop
            log.warning("market discovery failed: %s", exc)
            return cached
        self._market_cache = (now, markets)
        return markets

    def _snapshot(self, market: Market, spot_px: Optional[float], now: float) -> Optional[Snapshot]:
        try:
            books = self.venue.get_books(market)
        except Exception as exc:  # noqa: BLE001
            log.warning("book fetch failed for %s: %s", market.slug, exc)
            return None
        if books is None:
            return None
        up_book, down_book = books

        return Snapshot(
            ts=now,
            market=market,
            up_book=up_book,
            down_book=down_book,
            spot=spot_px,
            window_volume=market.volume,
        )

    def _manage_position(self, snap: Snapshot) -> None:
        """Mark an open position and run the exit policy against it."""
        slug = snap.market.slug
        pos = self.portfolio.positions.get(slug)
        if pos is None:
            return

        mark = mark_for(snap, pos.side)
        if mark is not None:
            self.portfolio.update_mark(slug, mark)

        decision = self.exit_policy.evaluate(
            pos, mark, snap.ts, snap.market.seconds_remaining(snap.ts)
        )
        if not decision.should_exit:
            return
        if self.executor is None:
            return

        fill = self.executor.sell(
            snap,
            Order(side=pos.side, shares=pos.shares, limit_price=0.0, reason=decision.detail),
        )
        if fill is None:
            log.warning("%s: %s triggered but exit did not fill", slug, decision.reason)
            return

        trade = self.portfolio.close(
            slug, fill.price, snap.ts, decision.reason, fee=fill.fee
        )
        self.risk.on_settlement(trade.pnl)
        log.info(
            "%s: %s at %.3f (%s) P&L $%+.2f | equity $%.2f",
            slug,
            decision.reason,
            fill.price,
            decision.detail,
            trade.pnl,
            self.portfolio.equity,
        )

    def _settle_expired(self, now: float) -> None:
        """Resolve positions whose window has ended.

        The venue is the authority on the outcome, and this bot does not yet
        query it (see README, "Known gaps"). We settle from the last mark we
        saw, which converges to 0 or 1 as a window closes, and log loudly so
        the approximation is never silent.
        """
        for slug, pos in list(self.portfolio.positions.items()):
            end_ts = self._window_end.get(
                slug, pos.entry_ts + self.cfg.markets.window_seconds
            )
            if now <= end_ts + 60:
                continue

            mark = pos.last_mark if pos.last_mark is not None else pos.entry_price
            outcome = pos.side if mark >= 0.5 else None
            trade = self.portfolio.settle(
                slug, outcome, now, fee_model=self.fee_model
            )
            self.risk.on_settlement(trade.pnl)
            log.info(
                "%s: settled from last mark %.3f -> P&L $%+.2f (APPROXIMATE; "
                "reconcile against the venue) | equity $%.2f",
                slug,
                mark,
                trade.pnl,
                self.portfolio.equity,
            )

    def tick(self) -> None:
        now = time.time()
        spot_px = self.spot.price()
        self._settle_expired(now)

        # Hand the strategy a measured volatility if it wants one. The window
        # configured on the strategy drives both the measurement here and the
        # annualisation inside it, so there is a single source of truth. The
        # backtester does exactly the same thing from the recorded spot series.
        vol_window = getattr(self.strategy, "realized_vol_window", None)
        if vol_window:
            self.strategy.current_realized_vol = self.spot.realized_vol(
                float(vol_window)
            )

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

            if self.portfolio.has_position(market.slug):
                self._manage_position(snap)
                continue

            if market.slug in self._traded_windows:
                continue

            signal = self.strategy.decide(snap)
            if signal is None:
                continue

            halted = self.guard.check(self.portfolio)
            if halted:
                log.warning("%s: no entry, %s", market.slug, halted)
                continue

            order, rejection = self.risk.evaluate(snap, signal, portfolio=self.portfolio)
            if order is None:
                log.debug("%s: declined (%s)", market.slug, rejection)
                continue

            fill = self.executor.buy(snap, order)
            if fill is None:
                continue

            try:
                self.portfolio.open(
                    market.slug, fill.side, fill.shares, fill.price, now, fee=fill.fee
                )
            except ValueError as exc:
                log.error("%s: filled but cannot book the position: %s", market.slug, exc)
                continue

            self.risk.on_trade(now)
            self._traded_windows.add(market.slug)
            self._window_end[market.slug] = market.end_ts
            log.info(
                "%s: bought %s %.2f @ %.3f | %s | equity $%.2f",
                market.slug,
                fill.side,
                fill.shares,
                fill.price,
                order.reason,
                self.portfolio.equity,
            )

        self.portfolio.record_equity(now)

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
            if self.trade:
                print("\n" + self.portfolio.render())
