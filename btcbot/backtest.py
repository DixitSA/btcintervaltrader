"""Replay recorded snapshots through the live strategy + risk code.

The point of this module is to answer one question honestly: does the rule
make money net of fees, and is the result distinguishable from luck?

That second half is what separates this from a video's backtest. A 15-minute
binary is close to a coin flip, so a 55% win rate over 40 trades is entirely
consistent with having no edge at all. The report prints the break-even win
rate and a standard error so you can see whether your sample says anything.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from .config import Config
from .execution import PaperExecutor
from .fees import build_fee_model
from .exits import DrawdownGuard, ExitPolicy
from .models import DOWN, UP, Fill, Order, Snapshot
from .portfolio import EXIT_EXPIRY, EXIT_VOID, Portfolio, mark_for
from .risk import RiskManager
from .signals import market_implied_up, realized_vol_from_series, settlement_side
from .strategies.base import Strategy


@dataclass
class WindowResult:
    slug: str
    side: str
    shares: float
    entry_price: float
    outcome: Optional[str]
    pnl: float
    reason: str = ""
    exit_reason: str = EXIT_EXPIRY
    exit_price: float = 0.0

    @property
    def won(self) -> bool:
        """Profitable, not merely "picked the right side".

        A stopped-out position can hold the winning side and still lose money,
        so P&L is the honest criterion once exits are in play.
        """
        return self.pnl > 0

    @property
    def picked_correctly(self) -> bool:
        return self.outcome is not None and self.outcome == self.side


@dataclass
class BacktestReport:
    trades: list[WindowResult] = field(default_factory=list)
    windows_seen: int = 0
    windows_with_signal: int = 0
    rejections: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    exits: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    starting_bankroll: float = 0.0
    ending_bankroll: float = 0.0
    portfolio: Optional[Portfolio] = None
    # Set by run_backtest so break-even can include the entry fee. Left None on
    # hand-built reports, which then fall back to the fee-free break-even.
    fee_model: Optional[object] = None

    @property
    def resolved_trades(self) -> list:
        """Trades whose window actually resolved.

        A void is a window we could not determine the winner of (see
        infer_outcome): the stake comes back and only the entry fee is lost. Its
        P&L is real and stays in the money numbers, but it is NOT evidence about
        whether the rule picks the right side, so it must stay out of the
        win-rate denominator. Counting voids as losses -- which `won = pnl > 0`
        did, since a void nets the entry fee -- pushed the win rate down and
        made it look like the strategy was being beaten by windows that were
        never scored at all.
        """
        return [t for t in self.trades if t.exit_reason != EXIT_VOID]

    @property
    def voided(self) -> int:
        return len(self.trades) - len(self.resolved_trades)

    @property
    def n(self) -> int:
        return len(self.resolved_trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.resolved_trades if t.won)

    @property
    def trade_returns(self) -> list[float]:
        """Per-trade return on stake. The basis for the significance test.

        Voids are excluded: a void is a MISSING observation, not a ~0% return.
        The window did resolve in reality -- we just could not tell how -- so
        feeding it in as a small loss would shrink the measured variance and
        flatter the t-statistic. The fee it cost still shows up in total_pnl and
        the equity curve, which track simulated cash rather than evidence.
        """
        out = []
        for t in self.resolved_trades:
            stake = t.shares * t.entry_price
            if stake > 0:
                out.append(t.pnl / stake)
        return out

    @property
    def roi_t_stat(self) -> Optional[float]:
        """t-statistic of mean per-trade return against zero.

        Preferred over the win-rate z-score once stops are enabled: with early
        exits the payoff is no longer binary, so comparing a win rate to the
        entry price stops being the right null.
        """
        rets = self.trade_returns
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        if var <= 0:
            return None
        return mean / math.sqrt(var / len(rets))

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.n if self.n else None

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_staked(self) -> float:
        return sum(t.shares * t.entry_price for t in self.trades)

    @property
    def roi(self) -> Optional[float]:
        staked = self.total_staked
        return self.total_pnl / staked if staked > 0 else None

    @property
    def avg_entry_price(self) -> Optional[float]:
        if not self.n:
            return None
        return sum(t.entry_price for t in self.trades) / self.n

    @property
    def avg_entry_fee_per_contract(self) -> float:
        """Taker fee paid per contract at the average entry price.

        Zero when no fee model is attached, which keeps hand-built reports on
        the old fee-free break-even.
        """
        fees = self.fee_model
        price = self.avg_entry_price
        if fees is None or price is None:
            return 0.0
        try:
            return float(fees.entry_fee(1.0, price))
        except Exception:  # noqa: BLE001 - a report must always render
            return 0.0

    @property
    def breakeven_win_rate(self) -> Optional[float]:
        """The win rate that actually leaves you flat, fees included.

        Buying at price c costs c plus the taker fee f per contract and pays 1
        on a win, so you must win (c + f) of the time -- not c. On Kalshi at
        50c that fee is 1.75c, so omitting it understates the bar by 1.75
        points and flatters every z-score computed against it. That bias grows
        in importance exactly as the sample gets big enough to act on: it is
        worth ~0.35 standard errors at n=100 and ~1.1 at n=1000.
        """
        price = self.avg_entry_price
        if price is None:
            return None
        return price + self.avg_entry_fee_per_contract

    @property
    def win_rate_stderr(self) -> Optional[float]:
        """Standard error of the observed win rate, or None if undefined.

        At p = 0 or p = 1 the binomial standard error is exactly zero: the
        sample shows no variation, so it carries no information about how much
        the win rate would move on a rerun. That is a degenerate sample, not a
        precise one, and it is overwhelmingly likely to be small -- two wins
        out of two is p = 1. Flooring the variance at some epsilon to keep the
        arithmetic alive turns that into a microscopic standard error, which
        `z_score` then divides by and reports as certainty (a 2-trade, 2-win
        sample used to print z = +530330). Refuse instead, the same way
        realized_vol_from_series refuses a span it cannot measure.
        """
        if self.n < 2:
            return None
        if self.wins == 0 or self.wins == self.n:
            return None
        p = self.wins / self.n
        return math.sqrt(p * (1.0 - p) / self.n)

    @property
    def z_score(self) -> Optional[float]:
        """How many standard errors the win rate sits above break-even.

        Below ~2, the sample does not support a claim of edge. None means the
        sample cannot support the claim either way -- see win_rate_stderr.
        """
        be = self.breakeven_win_rate
        se = self.win_rate_stderr
        wr = self.win_rate
        if be is None or se in (None, 0) or wr is None:
            return None
        return (wr - be) / se

    @property
    def max_drawdown(self) -> float:
        peak = self.starting_bankroll
        equity = self.starting_bankroll
        worst = 0.0
        for t in self.trades:
            equity += t.pnl
            peak = max(peak, equity)
            worst = min(worst, equity - peak)
        return abs(worst)

    def render(self) -> str:
        lines = [
            "=" * 62,
            "BACKTEST REPORT",
            "=" * 62,
            f"windows seen          : {self.windows_seen}",
            f"windows with signal   : {self.windows_with_signal}",
            f"trades taken          : {self.n}",
        ]
        if self.voided:
            lines.append(
                f"  (+{self.voided} voided        : outcome undeterminable -- not scored. "
                "Stake returned,\n"
                "                           entry fee lost. Usually a window whose "
                "recording stopped\n"
                "                           before the close, or spot too near the "
                "strike to call.)"
            )
        if not self.n:
            lines += [
                "",
                "No trades were taken. Most common reasons for declining:",
            ]
            for reason, count in sorted(
                self.rejections.items(), key=lambda kv: -kv[1]
            )[:8]:
                lines.append(f"  {count:6d}  {reason}")
            lines.append("=" * 62)
            return "\n".join(lines)

        wr = self.win_rate or 0.0
        be = self.breakeven_win_rate or 0.0
        dd = self.portfolio.max_drawdown if self.portfolio else self.max_drawdown
        lines += [
            f"profitable trades     : {wr:.1%} ({self.wins}/{self.n})",
            f"break-even win rate   : {be:.1%}  (avg entry ${be:.3f})",
            f"total staked          : ${self.total_staked:,.2f}",
            f"total P&L             : ${self.total_pnl:,.2f}",
            f"ROI on stake          : {(self.roi or 0.0):+.2%}",
            f"equity                : ${self.starting_bankroll:,.2f} -> ${self.ending_bankroll:,.2f}",
            f"max drawdown          : ${dd:,.2f}",
        ]

        if self.exits:
            lines.append("exits                 :")
            pnl_by = {}
            for t in self.trades:
                pnl_by[t.exit_reason] = pnl_by.get(t.exit_reason, 0.0) + t.pnl
            for reason in sorted(self.exits, key=lambda r: -self.exits[r]):
                lines.append(
                    f"  {reason:<16} {self.exits[reason]:>5}  "
                    f"${pnl_by.get(reason, 0.0):+,.2f}"
                )

        t_stat = self.roi_t_stat
        if t_stat is not None:
            lines.append(f"ROI t-statistic       : {t_stat:+.2f}")
            if abs(t_stat) < 2.0:
                lines.append(
                    "  -> NOT statistically distinguishable from no edge. "
                    "Do not trade this live."
                )
            elif t_stat > 0:
                lines.append(
                    "  -> Positive and significant on THIS sample. Confirm it holds "
                    "out-of-sample before believing it."
                )
            else:
                lines.append("  -> Significantly LOSING.")

        if not self.exits.keys() - {EXIT_EXPIRY}:
            # Only meaningful when every trade ran to expiry (binary payoff).
            z = self.z_score
            if z is not None:
                lines.append(f"win-rate z-score      : {z:+.2f}")
            elif self.n >= 2 and self.wins in (0, self.n):
                # Say why rather than dropping the line: a missing z-score on
                # an all-wins run reads as a rendering quirk, when it is the
                # single most important thing to know about that sample.
                every = "won" if self.wins else "lost"
                lines.append(
                    f"win-rate z-score      : -  (every trade {every}; a win "
                    "rate of 0% or 100% has no\n"
                    "                           spread to measure, so the "
                    "sample cannot show significance)"
                )

        if self.n < 100:
            lines.append(
                f"  note: {self.n} trades is a small sample for a near-coin-flip market."
            )
        lines.append("=" * 62)
        return "\n".join(lines)


#: A window whose last snapshot is further than this from its close was not
#: observed to resolution, so nothing in it identifies the winner.
MAX_SECONDS_FROM_CLOSE = 30.0


def infer_outcome(
    window: list[Snapshot], max_seconds_from_close: float = MAX_SECONDS_FROM_CLOSE
) -> Optional[str]:
    """Which side won a recorded window, or None when the data cannot say.

    Validated against Kalshi's own `result` field for 45 recorded windows. The
    previous version -- spot vs strike at the last snapshot, market price only
    as a fallback -- agreed with Kalshi on 84.4% of them. This one agrees on
    100% of the windows it will answer for (41/41), refusing the other 4.

    Order matters, and it is the reverse of what it was:

    1. Refuse if the last snapshot is not near the close. Recording stops when
       the machine sleeps or the supervisor restarts, and a snapshot from
       mid-window says nothing about the result. Treating one as terminal was
       wrong on 4 of 4 such windows -- not noise, a fabricated answer.

    2. Prefer the terminal MARKET price once it has converged. The market
       settles on the same CF Benchmarks index Kalshi does, so at resolution its
       price is evidence about the real outcome; our Binance spot is a proxy for
       a different index over a different interval. Converged market price was
       right 39/39, spot 38/41.

    3. Fall back to spot vs strike, which abstains inside the noise band. See
       settlement_side.
    """
    if not window:
        return None
    last = window[-1]

    if last.market.end_ts - last.ts > max_seconds_from_close:
        return None

    p_up = market_implied_up(last)
    if p_up is not None:
        if p_up >= 0.9:
            return UP
        if p_up <= 0.1:
            return DOWN

    return settlement_side(last.spot, last.market.strike)


def group_windows(snaps: Iterable[Snapshot]) -> dict[str, list[Snapshot]]:
    windows: dict[str, list[Snapshot]] = defaultdict(list)
    for snap in snaps:
        windows[snap.market.slug].append(snap)
    for slug in windows:
        windows[slug].sort(key=lambda s: s.ts)
    return windows


def run_backtest(
    snapshots: Iterable[Snapshot],
    strategy: Strategy,
    cfg: Config,
    one_trade_per_window: bool = True,
    use_exits: Optional[bool] = None,
) -> BacktestReport:
    """Replay windows through strategy -> risk -> execution -> portfolio.

    `use_exits` overrides cfg.exits.enabled, so the same dataset can be run
    with and without stops to measure what they actually cost or saved.
    """
    windows = group_windows(snapshots)
    fee_model = build_fee_model(cfg.venue, cfg.fees)
    risk = RiskManager(cfg.risk, cfg.fees, cfg.markets, fee_model=fee_model)
    executor = PaperExecutor(cfg, fee_model=fee_model)
    portfolio = Portfolio(cfg.risk.bankroll_usd)

    exits_cfg = replace(cfg.exits, enabled=use_exits) if use_exits is not None else cfg.exits
    exit_policy = ExitPolicy(exits_cfg)
    guard = DrawdownGuard(exits_cfg.max_drawdown_usd, exits_cfg.max_drawdown_pct)

    report = BacktestReport(starting_bankroll=portfolio.starting_cash)
    report.portfolio = portfolio
    report.fee_model = fee_model
    report.windows_seen = len(windows)

    outcomes = {slug: infer_outcome(w) for slug, w in windows.items()}
    final_ts = {slug: w[-1].ts for slug, w in windows.items()}

    # Replay in TIME order, not window order, so overlapping markets compete
    # for the same capital exactly as they would live. Iterating window by
    # window would let every market spend the full bankroll independently.
    ordered = sorted(
        (s for w in windows.values() for s in w), key=lambda s: (s.ts, s.market.slug)
    )

    entered: set[str] = set()
    signalled: set[str] = set()
    entry_reason: dict[str, str] = {}

    # Feed realized volatility to strategies that want it, from the recorded
    # spot series. The SAME window drives both the measurement and the
    # annualisation, so the two cannot drift apart. Without this the model
    # strategies silently run on their hardcoded default vol in backtest while
    # the live runner uses a measured one -- two different strategies wearing
    # one name.
    vol_window = getattr(strategy, "realized_vol_window", None)
    spot_history: deque[tuple[float, float]] = deque(maxlen=10_000)

    def record(slug: str) -> None:
        last = portfolio.closed[-1]
        report.exits[last.exit_reason] += 1
        report.trades.append(
            WindowResult(
                slug=slug,
                side=last.side,
                shares=last.shares,
                entry_price=last.entry_price,
                outcome=outcomes.get(slug),
                pnl=last.pnl,
                reason=entry_reason.get(slug, ""),
                exit_reason=last.exit_reason,
                exit_price=last.exit_price,
            )
        )

    for snap in ordered:
        slug = snap.market.slug

        # Spot is global, so record it once per timestamp rather than once per
        # concurrent market. This does NOT change the estimate -- duplicating
        # points halves the variance and doubles the count, which the sqrt(n)
        # scaling cancels exactly. It matters because the buffer is bounded:
        # with several families running, duplicates would fill it that many
        # times faster and the lookback would silently cover proportionally
        # less real time than asked for.
        if vol_window and snap.spot is not None:
            if not spot_history or snap.ts > spot_history[-1][0]:
                spot_history.append((snap.ts, snap.spot))
                # Drop points that have fallen out of the lookback, so each
                # estimate scans the window rather than the whole history and
                # memory stays bounded by the window rather than the dataset.
                cutoff = snap.ts - float(vol_window)
                while len(spot_history) > 2 and spot_history[0][0] < cutoff:
                    spot_history.popleft()
            strategy.current_realized_vol = realized_vol_from_series(
                spot_history, float(vol_window)
            )

        # -- manage an open position -----------------------------------
        if portfolio.has_position(slug):
            pos = portfolio.positions[slug]
            mark = mark_for(snap, pos.side)
            if mark is not None:
                portfolio.update_mark(slug, mark)

            decision = exit_policy.evaluate(
                pos, mark, snap.ts, snap.market.seconds_remaining(snap.ts)
            )
            if decision.should_exit:
                exit_fill = executor.sell(
                    snap,
                    Order(
                        side=pos.side,
                        shares=pos.shares,
                        limit_price=0.0,  # take whatever the bid gives
                        reason=decision.detail,
                    ),
                )
                if exit_fill is not None:
                    trade = portfolio.close(
                        slug,
                        exit_fill.price,
                        snap.ts,
                        decision.reason,
                        fee=exit_fill.fee,
                        outcome=outcomes.get(slug),
                    )
                    risk.on_settlement(trade.pnl)
                    record(slug)
                else:
                    report.rejections["exit fill failed"] += 1

            # Window is closing and we still hold: settle at the outcome.
            if snap.ts >= final_ts[slug] and portfolio.has_position(slug):
                trade = portfolio.settle(
                    slug, outcomes.get(slug), snap.ts, fee_model=fee_model
                )
                risk.on_settlement(trade.pnl)
                record(slug)

            portfolio.record_equity(snap.ts)
            continue

        # -- otherwise consider an entry -------------------------------
        if slug in entered:
            continue

        signal = strategy.decide(snap)
        if signal is None:
            continue
        signalled.add(slug)

        if guard.check(portfolio):
            report.rejections[f"halted: {guard.tripped_reason}"] += 1
            continue

        order, rejection = risk.evaluate(snap, signal, portfolio=portfolio)
        if order is None:
            if rejection:
                report.rejections[rejection] += 1
            continue

        fill = executor.buy(snap, order)
        if fill is None:
            report.rejections["paper fill failed"] += 1
            continue

        try:
            portfolio.open(slug, fill.side, fill.shares, fill.price, snap.ts, fee=fill.fee)
        except ValueError as exc:
            report.rejections[str(exc).split(":")[0]] += 1
            continue

        entered.add(slug)
        entry_reason[slug] = order.reason
        risk.on_trade(snap.ts)
        portfolio.record_equity(snap.ts)

    # Anything still open (dataset ended mid-window) settles at its outcome.
    for slug in list(portfolio.positions):
        trade = portfolio.settle(
            slug, outcomes.get(slug), final_ts[slug], fee_model=fee_model
        )
        risk.on_settlement(trade.pnl)
        record(slug)

    report.windows_with_signal = len(signalled)
    report.ending_bankroll = portfolio.equity
    return report
