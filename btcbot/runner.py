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
from .models import Market, Order, Snapshot
from .portfolio import Portfolio, mark_for
from .recorder import SnapshotWriter
from .risk import RiskManager
from .shadow import ShadowLedger
from .spot import SpotFeed
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
        # slug -> strike price, used to determine actual settlement outcome
        # (BTC vs strike) instead of inferring from last mark.
        self._strikes: dict[str, float] = {}
        # slug -> signal probability at trade time, used for calibration.
        self._signal_probs: dict[str, float] = {}

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

    def _settle_shadow_expired(self, now: float) -> None:
        """Settle shadow records for any expired window, including those we
        never held a real position in."""
        if not self.shadow:
            return
        spot_px = self.spot.price()
        for slug in self.shadow.unsettled_slugs():
            end_ts = self._window_end.get(slug, 0.0)
            if now <= end_ts + 60:
                continue
            strike = self._strikes.get(slug)
            if spot_px is not None and strike is not None and strike > 0:
                winning_side = "Up" if spot_px > strike else "Down"
            else:
                winning_side = None
            settled = self.shadow.settle(slug, winning_side, spot_px, now)
            for s in settled:
                self.shadow.append_settled(s)

    def _settle_expired(self, now: float) -> None:
        """Resolve positions whose window has ended.

        Determines win/loss from BTC spot vs the market's strike. If the spot
        feed or strike is unavailable, falls back to the last mark.
        """
        spot_px = self.spot.price()
        for slug, pos in list(self.portfolio.positions.items()):
            end_ts = self._window_end.get(
                slug, pos.entry_ts + self.cfg.markets.window_seconds
            )
            if now <= end_ts + 60:
                continue

            strike = self._strikes.get(slug)
            if spot_px is not None and strike is not None and strike > 0:
                # BTC above strike -> UP wins, below strike -> DOWN wins
                winning_side = "Up" if spot_px > strike else "Down"
                source = f"spot ${spot_px:,.2f} vs strike ${strike:,.2f}"
            else:
                # Fallback: use last mark (converges to 0 or 1 near expiry)
                mark = pos.last_mark if pos.last_mark is not None else pos.entry_price
                winning_side = pos.side if mark >= 0.5 else None
                source = f"last mark {mark:.3f} (APPROXIMATE)"

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
        spot_px = self.spot.price()
        self._settle_expired(now)
        self._settle_shadow_expired(now)

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
