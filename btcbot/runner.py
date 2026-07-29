"""The live loop: discover windows, build snapshots, optionally trade.

`record=True, trade=False` is the data-collection mode you should run first.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .execution import build_executor
from .exits import DrawdownGuard, ExitPolicy
from .fees import build_fee_model
from .learner import Calibrator, OutcomeRecord, OutcomeStore
from .models import DOWN, UP, Market, Order, Snapshot
from .portfolio import Portfolio, mark_for
from .recorder import SnapshotWriter
from .risk import RiskManager
from .shadow import ShadowLedger
from .signals import settlement_side
from .spot import SpotFeed, SpotFeedManager
from .strategies.base import Signal, Strategy
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
        self.spot_manager = SpotFeedManager(cfg.markets.families, cfg.spot_url)
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
        # slug -> strike price, used to determine actual settlement outcome
        # (BTC vs strike) instead of inferring from last mark.
        self._strikes: dict[str, float] = {}
        # slug -> signal probability at trade time, used for calibration.
        self._signal_probs: dict[str, float] = {}
        # slug -> (ts, spot) of the last reading taken while the window was open.
        # Settlement must use this, not a price fetched after the close.
        self._last_spot: dict[str, tuple[float, float]] = {}

        # Online calibration from observed trade outcomes.
        self.calibrator: Optional[Calibrator] = None
        self._outcome_store: Optional[OutcomeStore] = None
        if cfg.learning and cfg.learning.enabled:
            self.calibrator = Calibrator(
                alpha_prior=cfg.learning.alpha_prior,
                beta_prior=cfg.learning.beta_prior,
            )
            outcome_path = Path(cfg.data_dir) / (cfg.learning.outcome_file or "outcomes.jsonl")
            self._outcome_store = OutcomeStore(outcome_path)
            stored = self._outcome_store.feed(self.calibrator)
            if stored:
                log.info("calibrator seeded from %d past outcomes", stored)

        # Shadow ledger tracks hypothetical trades at every rung x direction.
        self.shadow: Optional[ShadowLedger] = None
        sc = cfg.shadow
        if sc and sc.enabled:
            rung_defs = [
                (i, cfg.markets.min_seconds_remaining, float(max_r))
                for i, max_r in enumerate(sc.rungs)
            ]
            shadow_path = Path(cfg.data_dir) / sc.ledger_file
            self.shadow = ShadowLedger(
                ledger_path=shadow_path,
                producer="live",
                fee_model=self.fee_model,
                rung_defs=rung_defs,
                enabled=sc.enabled,
                notional_usd=sc.notional_usd,
                directions=sc.directions,
            )

    @property
    def spot(self) -> SpotFeed:
        """Backward-compat: first feed (usually BTCUSDT)."""
        return self.spot_manager.first_feed

    def close(self) -> None:
        self.venue.close()
        self.spot_manager.close()
        if self.writer:
            self.writer.close()

    def _markets(self, now: float) -> list[Market]:
        cached_at, cached = self._market_cache
        if now - cached_at < 20.0 and cached:
            return cached
        try:
            markets = self.venue.discover_markets(
                self.cfg.markets.slug_prefixes,
                strike_bounds=self.cfg.markets.strike_bounds,
            )
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

        # Record early exit for calibration.
        signal_prob = self._signal_probs.pop(slug, pos.entry_price)
        if self._outcome_store is not None and self.calibrator is not None:
            rec = OutcomeRecord(
                slug=slug,
                side=pos.side,
                signal_prob=signal_prob,
                entry_price=pos.entry_price,
                entry_ts=pos.entry_ts,
                exit_ts=snap.ts,
                outcome=pos.side if trade.pnl > 0 else None,
                pnl=trade.pnl,
                exit_reason=trade.exit_reason,
            )
            self._outcome_store.append(rec)
            self.calibrator.observe(signal_prob, trade.pnl > 0)

    def _close_spot(
        self, slug: str, end_ts: float, max_age: float = 30.0
    ) -> Optional[float]:
        """Last spot observed while the window was open, if near enough its close.

        Returns None when the newest in-window reading is more than `max_age`
        before the close: on recorded data, calling such a reading terminal got
        the winner wrong on 4 of 4 windows.
        """
        entry = self._last_spot.get(slug)
        if entry is None:
            return None
        ts, spot = entry
        if end_ts - ts > max_age:
            return None
        return spot

    def _spot_for(self, slug: str) -> Optional[float]:
        family = self.cfg.markets.family_for(slug)
        if family:
            return self.spot_manager.price(family)
        return self.spot_manager.first_price

    def _settle_shadow_expired(self, now: float) -> None:
        """Settle shadow records for any expired window, including those we
        never held a real position in."""
        if not self.shadow:
            return
        for slug in self.shadow.unsettled_slugs():
            end_ts = self._window_end.get(slug, 0.0)
            if now <= end_ts + 60:
                continue
            strike = self._strikes.get(slug)
            # Same rule as _settle_expired: the spot from while the window was
            # open, and no verdict when it sits inside the noise band.
            spot_px = self._close_spot(slug, end_ts)
            winning_side = settlement_side(spot_px, strike)
            settled = self.shadow.settle(slug, winning_side, spot_px, now)
            for s in settled:
                self.shadow.append_settled(s)

    def _settle_expired(self, now: float) -> None:
        """Resolve positions whose window has ended.

        Uses the last spot seen while the window was still OPEN, compared to the
        strike, and abstains when spot is too close to the strike to call. A
        None winning_side voids the position: the stake comes back rather than
        the bot booking a coin flip as though it knew the answer.
        """
        for slug, pos in list(self.portfolio.positions.items()):
            end_ts = self._window_end.get(
                slug, pos.entry_ts + self.cfg.markets.window_seconds
            )
            if now <= end_ts + 60:
                continue

            strike = self._strikes.get(slug)
            close_spot = self._close_spot(slug, end_ts)
            winning_side = settlement_side(close_spot, strike)
            if winning_side is not None:
                winning_side = UP if winning_side == UP else DOWN
                source = f"spot ${close_spot:,.2f} vs strike ${strike:,.2f}"
            else:
                # Either no close-adjacent spot, or spot inside the noise band.
                # The last mark is the market's own verdict and it settles on the
                # index Kalshi uses, so prefer it -- but only once converged.
                mark = pos.last_mark
                if mark is not None and (mark >= 0.9 or mark <= 0.1):
                    winning_side = pos.side if mark >= 0.9 else (
                        DOWN if pos.side == UP else UP
                    )
                    source = f"terminal mark {mark:.3f}"
                else:
                    winning_side = None
                    source = (
                        f"UNDETERMINED (spot {close_spot} vs strike {strike}, "
                        f"mark {mark}) -- voided"
                    )

            trade = self.portfolio.settle(
                slug, winning_side, now, fee_model=self.fee_model
            )
            self.risk.on_settlement(trade.pnl)
            log.info(
                "%s: settled %s -> P&L $%+.2f (%s) | equity $%.2f",
                slug,
                source,
                trade.pnl,
                "won" if trade.pnl > 0 else "lost",
                self.portfolio.equity,
            )
            self._strikes.pop(slug, None)
            self._last_spot.pop(slug, None)
            signal_prob = self._signal_probs.pop(slug, 0.5)

            if self._outcome_store is not None and self.calibrator is not None:
                rec = OutcomeRecord(
                    slug=slug,
                    side=pos.side,
                    signal_prob=signal_prob,
                    entry_price=pos.entry_price,
                    entry_ts=pos.entry_ts,
                    exit_ts=now,
                    outcome=winning_side,
                    pnl=trade.pnl,
                    exit_reason=trade.exit_reason,
                )
                self._outcome_store.append(rec)
                self.calibrator.observe(signal_prob, trade.pnl > 0)

            # Shadow ledger: settle hypothetical records for this window.
            if self.shadow:
                settled = self.shadow.settle(slug, winning_side, spot_px, now)
                for s in settled:
                    self.shadow.append_settled(s)

    def tick(self) -> None:
        now = time.time()
        self._settle_expired(now)
        self._settle_shadow_expired(now)

        vol_window = getattr(self.strategy, "realized_vol_window", None) if self.strategy else None

        for market in self._markets(now):
            remaining = market.seconds_remaining(now)
            if remaining <= 0 or remaining > self.cfg.markets.max_seconds_remaining:
                continue

            family = self.cfg.markets.family_for(market.slug)
            spot_px = self.spot_manager.price(family) if family else self.spot_manager.first_price

            # Set per-family vol before the strategy decides.
            if vol_window and family:
                self.strategy.current_realized_vol = self.spot_manager.realized_vol(
                    family, float(vol_window)
                )

            snap = self._snapshot(market, spot_px, now)
            if snap is None:
                continue

            if self.writer:
                self.writer.write(snap)

            # Shadow ledger: record hypothetical trades at every rung x direction.
            if self.shadow:
                records = self.shadow.evaluate(snap)
                for rec in records:
                    self.shadow.append(rec)

            # Track ALL window end times and strikes, not just traded ones,
            # so shadow records settle correctly even on windows we skip.
            self._window_end.setdefault(market.slug, market.end_ts)
            if market.strike is not None:
                self._strikes.setdefault(market.slug, market.strike)

            # Keep the newest spot seen while the window was still OPEN. The
            # outcome is fixed at the close, but settlement runs 60s later, and
            # fetching spot then reads a price the window never saw. Spot moves
            # a median 0.0386% per minute against a median strike distance of
            # 0.2663%, so that drift silently flips near-strike windows.
            if spot_px is not None and now <= market.end_ts:
                self._last_spot[market.slug] = (now, spot_px)

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

            # Calibrate the signal's probability from historical outcomes.
            if self.calibrator is not None:
                cal_prob, n_cal = self.calibrator.calibrate(signal.prob)
                if n_cal > 0 and cal_prob != signal.prob:
                    signal = Signal(
                        side=signal.side,
                        prob=cal_prob,
                        reason=f"{signal.reason} | calibrated({n_cal})",
                    )

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
            self._strikes[market.slug] = market.strike
            self._signal_probs[market.slug] = signal.prob
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
