"""Local control panel: paper trading, live model view, recorder status.

    python -m btcbot serve

Binds 127.0.0.1 only. This server can start PAPER trading and nothing else --
there is no code path here that places a real order, and `serve` refuses to
start if the config is set to live. Keeping that impossible rather than merely
discouraged is the point: a web button is exactly the wrong way to arm real
money.

The same JSON that drives the page is served at /api/state, which is what the
Chrome extension overlay consumes.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from .config import Config, load_config
from .models import DOWN, UP
from .signals import fair_probability_up, market_implied_up
from .strategies import build_strategy

log = logging.getLogger(__name__)

UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"

# Most recent settled trades included in /api/state. The panel polls every 2s,
# so the whole ledger would grow the payload without bound over a long session.
MAX_TRADES_IN_STATE = 100


def seed_spot_history(spot_feed, data_dir: str, tail_bytes: int = 2_000_000) -> int:
    """Preload a single spot feed from snapshots already on disk."""
    return _seed_one(spot_feed, data_dir, tail_bytes)


def seed_spot_manager(manager, data_dir: str, tail_bytes: int = 2_000_000) -> int:
    """Preload all spot feeds from snapshot data on disk.

    Recorded snapshots carry a single `spot` field (BTC).  Newer data may
    carry per-family spots once the recorder is updated; for now only BTC
    is seeded from history, and other feeds start cold.
    """
    total = 0
    for sym in list(manager._feeds.keys()):
        total += _seed_one(manager._feed(sym), data_dir, tail_bytes)
    # Seed the first (BTC) feed even if not yet created.
    if total == 0:
        from .config import FamilyConfig

        for fam in manager._families.values():
            total += _seed_one(manager._feed(fam.spot_symbol), data_dir, tail_bytes)
            break
    return total


def _seed_one(feed, data_dir: str, tail_bytes: int) -> int:
    """Preload *feed* from the `spot` field in recorded snapshots."""
    directory = Path(data_dir)
    if not directory.exists():
        return 0
    files = sorted(directory.glob("snapshots-*.jsonl"))
    if not files:
        return 0

    newest = files[-1]
    try:
        size = newest.stat().st_size
        with newest.open("rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        log.warning("could not seed spot history: %s", exc)
        return 0

    points: list[tuple[float, float]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts, spot = row.get("ts"), row.get("spot")
        if ts is None or spot is None:
            continue
        if points and points[-1][0] >= float(ts):
            continue
        points.append((float(ts), float(spot)))

    for point in points:
        feed._history.append(point)
    return len(points)


@dataclass
class MarketView:
    """Everything the UI shows for one open window."""

    slug: str
    question: str
    strike: Optional[float]
    seconds_left: float
    volume: float
    spot: Optional[float]
    up_bid: Optional[float]
    up_ask: Optional[float]
    down_bid: Optional[float]
    down_ask: Optional[float]
    market_p_up: Optional[float]
    model_p_up: Optional[float]
    edge: Optional[float]
    vol_annual: Optional[float]
    family: str = "btc"
    entry_fee_100: Optional[float] = None
    breakeven_up: Optional[float] = None
    held: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class SessionState:
    running: bool = False
    ticks: int = 0
    started_at: Optional[float] = None
    last_tick_at: Optional[float] = None
    last_error: Optional[str] = None
    markets: list[dict[str, Any]] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)


class PaperSession:
    """Owns the paper Runner and the display poller.

    One lock guards `state`, which the HTTP threads read and the tick thread
    writes.
    """

    def __init__(self, cfg: Config):
        if cfg.is_live:
            raise RuntimeError(
                "serve refuses to run with mode=live. This panel is paper only."
            )
        self.cfg = cfg
        self.state = SessionState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._runner = None
        self._display_runner = None

    # -- lifecycle -----------------------------------------------------

    def _build_runner(self, trade: bool):
        from .runner import Runner

        cfg = self.cfg
        cfg.mode = "paper"  # belt and braces; __init__ already refused live
        strategy = (
            build_strategy(cfg.strategy.name, cfg.strategy.params) if trade else None
        )
        runner = Runner(cfg, strategy=strategy, record=trade, trade=trade)
        seeded = seed_spot_history(runner.spot, cfg.data_dir)
        if seeded:
            log.info("seeded %d spot points from %s", seeded, cfg.data_dir)
        return runner

    def _display(self):
        if self._display_runner is None:
            self._display_runner = self._build_runner(trade=False)
        return self._display_runner

    def start(self) -> None:
        with self._lock:
            if self.state.running:
                return
            self._stop.clear()
            self._runner = self._build_runner(trade=True)
            self.state.running = True
            self.state.started_at = time.time()
            self.state.ticks = 0
            self.state.last_error = None
            self._note("paper trading started")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
        with self._lock:
            self.state.running = False
            self._note("paper trading stopped")
        if self._runner is not None:
            try:
                self._runner.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("closing runner: %s", exc)
            self._runner = None

    def _note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.state.log_lines.append(f"[{stamp}] {message}")
        del self.state.log_lines[:-200]

    def _loop(self) -> None:
        while not self._stop.is_set():
            start = time.time()
            try:
                runner = self._runner
                if runner is not None:
                    runner.tick()
                with self._lock:
                    self.state.ticks += 1
                    self.state.last_tick_at = time.time()
                    self.state.last_error = None
            except Exception as exc:  # noqa: BLE001 - a blip must not kill the session
                log.exception("paper tick failed")
                with self._lock:
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    self._note(f"tick failed: {exc}")
            self._stop.wait(max(0.0, self.cfg.poll_seconds - (time.time() - start)))

    # -- read side -----------------------------------------------------

    def _active_runner(self):
        return self._runner if self._runner is not None else self._display()

    def snapshot_markets(self) -> list[dict[str, Any]]:
        """Fetch open windows and score them exactly as the strategy would.

        Costs one book fetch per market. When paper trading is running this is
        a second fetch on top of the runner's own -- deliberate, so the display
        stays honest even while the trading loop is between ticks.
        """
        runner = self._active_runner()
        now = time.time()

        from .signals import annualize

        strategy = getattr(self._runner, "strategy", None) if self._runner else None
        vol_window = getattr(strategy, "realized_vol_window", None) if strategy else None

        out: list[MarketView] = []
        for market in runner._markets(now):
            remaining = market.seconds_remaining(now)
            if remaining <= 0 or remaining > self.cfg.markets.max_seconds_remaining:
                continue

            family = self.cfg.markets.family_for(market.slug)
            spot_px = runner.spot_manager.price(family) if family else runner.spot_manager.first_price

            snap = runner._snapshot(market, spot_px, now)
            if snap is None:
                continue

            # Per-family vol.
            vol_annual = None
            if vol_window and family:
                strategy.current_realized_vol = runner.spot_manager.realized_vol(
                    family, float(vol_window)
                )
                vol_annual = strategy._vol()
            if vol_annual is None:
                measured = runner.spot_manager.realized_vol(family, 900.0) if family else None
                if measured is None:
                    measured = runner.spot_manager.first_feed.realized_vol(900.0)
                vol_annual = annualize(measured, 900.0) if measured else None

            market_p = market_implied_up(snap)
            model_p = None
            if spot_px is not None and market.strike and vol_annual:
                model_p = fair_probability_up(
                    spot_px, market.strike, remaining, vol_annual
                )

            fee100 = runner.fee_model.entry_fee(100, 0.50)
            up_ask = snap.up_book.best_ask
            breakeven_up = None
            if up_ask is not None:
                breakeven_up = up_ask + runner.fee_model.entry_fee(1.0, up_ask)

            held = False
            if self._runner is not None:
                held = self._runner.portfolio.has_position(market.slug)

            out.append(
                MarketView(
                    slug=market.slug,
                    question=market.question,
                    strike=market.strike,
                    seconds_left=remaining,
                    volume=market.volume,
                    spot=spot_px,
                    up_bid=snap.up_book.best_bid,
                    up_ask=up_ask,
                    down_bid=snap.down_book.best_bid,
                    down_ask=snap.down_book.best_ask,
                    market_p_up=market_p,
                    model_p_up=model_p,
                    edge=(model_p - market_p) if (model_p and market_p) else None,
                    vol_annual=vol_annual,
                    family=family or "btc",
                    entry_fee_100=fee100,
                    breakeven_up=breakeven_up,
                    held=held,
                )
            )
        return [m.to_dict() for m in out]

    def portfolio_dict(self) -> dict[str, Any]:
        if self._runner is None:
            return {"active": False}
        p = self._runner.portfolio
        positions = []
        for slug, pos in p.positions.items():
            positions.append(
                {
                    "slug": slug,
                    "side": pos.side,
                    "shares": pos.shares,
                    "entry_price": pos.entry_price,
                }
            )
        wins = sum(1 for t in p.closed if t.won)
        # Full ledger of settled trades, newest first. Capped so a long session
        # cannot grow the /api/state payload without bound -- the panel polls
        # this every 2s.
        trades = []
        for t in reversed(p.closed[-MAX_TRADES_IN_STATE:]):
            cost = t.shares * t.entry_price
            trades.append(
                {
                    "slug": t.slug,
                    "side": t.side,
                    "shares": t.shares,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "entry_ts": t.entry_ts,
                    "exit_ts": t.exit_ts,
                    "held_seconds": t.held_seconds,
                    "pnl": t.pnl,
                    "fees_paid": t.fees_paid,
                    "exit_reason": t.exit_reason,
                    "outcome": t.outcome,
                    "won": t.won,
                    "cost_basis": cost,
                    # Return on what the trade actually tied up. A $0.40 win on
                    # a $0.50 stake is a different animal from the same $0.40 on
                    # a $5 stake, and the raw P&L column hides that.
                    "return_pct": (t.pnl / cost) if cost else None,
                }
            )
        return {
            "trades": trades,
            "trades_truncated": max(0, len(p.closed) - MAX_TRADES_IN_STATE),
            "active": True,
            "equity": p.equity,
            "cash": getattr(p, "cash", None),
            "starting_cash": p.starting_cash,
            "pnl": p.equity - p.starting_cash,
            "realized_pnl": p.realized_pnl,
            "positions": positions,
            "n_positions": len(positions),
            "closed_trades": len(p.closed),
            "wins": wins,
            "losses": len(p.closed) - wins,
        }

    def recorder_dict(self) -> dict[str, Any]:
        data_dir = Path(self.cfg.data_dir)
        files = sorted(data_dir.glob("snapshots-*.jsonl")) if data_dir.exists() else []
        lines = 0
        for f in files:
            try:
                with f.open("r", encoding="utf-8") as fh:
                    lines += sum(1 for _ in fh)
            except OSError:
                continue
        return {
            "data_dir": str(data_dir),
            "files": [f.name for f in files],
            "snapshots": lines,
            # 96 fifteen-minute windows a day; ~1,300 needed for a 5% edge.
            "windows_needed_5pct": 1283,
        }

    # -- shadow ledger --------------------------------------------------

    def shadow_status_dict(self) -> dict[str, Any]:
        """Basic shadow ledger stats for the extension panel."""
        from .shadow import ShadowLedger

        ledger_path = Path(self.cfg.data_dir) / (self.cfg.shadow.ledger_file or "shadow.jsonl")
        if not ledger_path.exists():
            return {"exists": False, "records": 0, "settled": 0, "windows": 0}
        ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
        records = ledger.load_records()
        settled = [r for r in records if r.won is not None]
        windows = len(set(r.slug for r in settled)) if settled else 0
        return {
            "exists": True,
            "records": len(records),
            "settled": len(settled),
            "windows": windows,
            "unsettled_slugs": len(ledger.unsettled_slugs()),
        }

    def shadow_report_dict(self) -> list[dict[str, Any]]:
        """Rung x direction performance data, same computation as shadow-report."""
        import math
        from collections import defaultdict

        from .shadow import (
            DIRECTIONS,
            ShadowLedger,
            assert_no_history_in_ranking,
            ranking_records,
        )

        ledger_path = Path(self.cfg.data_dir) / (self.cfg.shadow.ledger_file or "shadow.jsonl")
        if not ledger_path.exists():
            return []

        ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
        records = ledger.load_records()
        settled = ranking_records([r for r in records if r.won is not None])
        if not settled:
            return []

        assert_no_history_in_ranking(settled)

        groups: dict[tuple[int, str], list] = defaultdict(list)
        windows_per_rung: dict[tuple[int, str], set[str]] = defaultdict(set)
        for r in settled:
            key = (r.rung, r.direction)
            groups[key].append(r)
            windows_per_rung[key].add(r.slug)

        # Keyed on (window, direction): keying on slug alone would let a fade
        # baseline be subtracted from a follow rung.
        r0_by_window: dict[tuple[str, str], float] = {}
        for (rung, direction), recs in groups.items():
            if rung == 0:
                for r in recs:
                    r0_by_window[(r.slug, r.direction)] = r.net_pnl

        rows = []
        for rung in sorted({k[0] for k in groups}):
            for direction in sorted(DIRECTIONS):
                key = (rung, direction)
                recs = groups.get(key, [])
                if not recs:
                    continue

                n_windows = len(windows_per_rung[key])
                n_records = len(recs)
                wins = sum(1 for r in recs if r.won is True)
                win_rate = wins / n_records if n_records else 0.0
                mean_pnl = sum(r.net_pnl for r in recs) / n_records if n_records else 0.0

                pnl_by_window: dict[str, float] = defaultdict(float)
                counts_by_window: dict[str, int] = defaultdict(int)
                for r in recs:
                    pnl_by_window[r.slug] += r.net_pnl
                    counts_by_window[r.slug] += 1
                window_means = [pnl_by_window[s] / counts_by_window[s] for s in pnl_by_window]
                if len(window_means) >= 2:
                    wm_mean = sum(window_means) / len(window_means)
                    wm_var = sum((m - wm_mean) ** 2 for m in window_means) / (len(window_means) - 1)
                    stderr = math.sqrt(wm_var / len(window_means))
                    lcb = wm_mean - 1.645 * stderr
                else:
                    lcb = mean_pnl

                paired_diff = None
                if rung > 0:
                    diffs = [
                        r.net_pnl - r0_by_window[(r.slug, r.direction)]
                        for r in recs
                        if (r.slug, r.direction) in r0_by_window
                    ]
                    if diffs:
                        paired_diff = sum(diffs) / len(diffs)

                # Infer family from the first record's slug prefix.
                first_slug = recs[0].slug if recs else ""
                row_family = self.cfg.markets.family_for(first_slug) or "btc"
                rows.append({
                    "rung": rung,
                    "direction": direction,
                    "family": row_family,
                    "windows": n_windows,
                    "records": n_records,
                    "win_rate": round(win_rate, 4),
                    "mean_net_pnl": round(mean_pnl, 6),
                    "total_net_pnl": round(sum(r.net_pnl for r in recs), 6),
                    # Notional each record was sized at. The UI rescales to the
                    # user's unit size; P&L is linear in notional so this is
                    # exact, not an approximation.
                    "notional_usd": self.cfg.shadow.notional_usd or 1.0,
                    "lcb95": round(lcb, 6),
                    "paired_diff_r0": round(paired_diff, 6) if paired_diff is not None else None,
                })
        return rows

    def run_shadow_replay(self) -> dict[str, Any]:
        """Run shadow-replay from recorded snapshots on the server side."""
        from .backtest import group_windows, infer_outcome
        from .fees import build_fee_model
        from .shadow import ShadowLedger
        from .recorder import load_dataset

        data_dir = self.cfg.data_dir
        snapshots = load_dataset(data_dir)
        if not snapshots:
            return {"ok": False, "error": "no snapshots found"}

        windows = group_windows(snapshots)
        fee_model = build_fee_model(self.cfg.venue, self.cfg.fees)
        rung_defs = [
            (i, self.cfg.markets.min_seconds_remaining, float(max_r))
            for i, max_r in enumerate(self.cfg.shadow.rungs)
        ]
        output = Path(data_dir) / (self.cfg.shadow.ledger_file or "shadow.jsonl")

        ledger = ShadowLedger(
            ledger_path=output,
            producer="replay",
            fee_model=fee_model,
            rung_defs=rung_defs,
            enabled=True,
        )

        ordered = sorted(
            (s for w in windows.values() for s in w), key=lambda s: (s.ts, s.market.slug)
        )
        for snap in ordered:
            records = ledger.evaluate(snap)
            for rec in records:
                ledger.append(rec)

            slug = snap.market.slug
            outcome = infer_outcome(windows[slug])
            final_ts = windows[slug][-1].ts
            if snap.ts >= final_ts:
                settle_spot = snap.spot
                settled = ledger.settle(slug, outcome, settle_spot, final_ts)
                for s in settled:
                    ledger.append_settled(s)

        for slug in list(ledger.unsettled_slugs()):
            if slug in windows:
                outcome = infer_outcome(windows[slug])
                final_ts = windows[slug][-1].ts
                settle_spot = windows[slug][-1].spot
                settled = ledger.settle(slug, outcome, settle_spot, final_ts)
                for s in settled:
                    ledger.append_settled(s)

        records = ledger.load_records()
        unsettled = sum(1 for r in records if r.won is None)
        return {
            "ok": True,
            "records": len(records),
            "unsettled": unsettled,
            "windows": len(windows),
            "file": str(output),
        }

    def state_dict(self) -> dict[str, Any]:
        with self._lock:
            base = {
                "running": self.state.running,
                "ticks": self.state.ticks,
                "started_at": self.state.started_at,
                "last_tick_at": self.state.last_tick_at,
                "last_error": self.state.last_error,
                "log": list(self.state.log_lines[-40:]),
            }
        try:
            markets = self.snapshot_markets()
        except Exception as exc:  # noqa: BLE001
            markets = []
            base["last_error"] = f"market fetch failed: {exc}"

        base.update(
            {
                "mode": self.cfg.mode,
                "venue": self.cfg.venue,
                "strategy": self.cfg.strategy.name,
                "strategy_params": dict(self.cfg.strategy.params),
                "poll_seconds": self.cfg.poll_seconds,
                "markets": markets,
                "portfolio": self.portfolio_dict(),
                "recorder": self.recorder_dict(),
                "live_trading_possible": False,
            }
        )
        return base


class Handler(BaseHTTPRequestHandler):
    session: PaperSession = None  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Allow extension content scripts (which send the page's origin, e.g.
        # https://kalshi.com) and extension background pages (moz-extension://
        # or chrome-extension://).  Server is localhost-only so this is safe.
        origin = self.headers.get("Origin", "")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(payload, default=str).encode(), "application/json")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/state"):
            self._json(self.session.state_dict())
            return
        if self.path == "/api/shadow/status":
            self._json(self.session.shadow_status_dict())
            return
        if self.path == "/api/shadow/report":
            self._json({"rows": self.session.shadow_report_dict()})
            return
        if self.path in ("/", "/index.html"):
            try:
                body = UI_PATH.read_bytes()
            except OSError:
                self._send(500, b"UI file missing", "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/paper/start":
            try:
                self.session.start()
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, code=400)
                return
            self._json({"ok": True})
            return
        if self.path == "/api/paper/stop":
            self.session.stop()
            self._json({"ok": True})
            return
        if self.path == "/api/shadow/replay":
            try:
                result = self.session.run_shadow_replay()
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)}, code=400)
                return
            self._json(result)
            return
        self._send(404, b"not found", "text/plain")


def serve(cfg: Optional[Config] = None, host: str = "127.0.0.1", port: int = 8787) -> int:
    cfg = cfg or load_config(None)
    session = PaperSession(cfg)
    Handler.session = session

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  btcbot control panel: http://{host}:{port}")
    print("  paper trading only -- this server cannot place a real order.")
    print("  Ctrl-C to stop.\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        session.stop()
        httpd.server_close()
    return 0
