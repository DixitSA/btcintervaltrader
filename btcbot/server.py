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


def seed_spot_history(spot_feed, data_dir: str, tail_bytes: int = 2_000_000) -> int:
    """Preload the spot feed from snapshots already on disk.

    Without this the volatility estimator needs to observe half its lookback
    live before it will report anything -- around 8 minutes at a 900s window --
    and until then the model declines every trade. The recorder has usually
    been running for hours, so the history is right there.

    Reads only the tail of the newest file so this stays cheap as the dataset
    grows into days.
    """
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
                fh.readline()  # discard the partial line
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
        # Spot is global; concurrent markets repeat it at the same timestamp.
        if points and points[-1][0] >= float(ts):
            continue
        points.append((float(ts), float(spot)))

    for point in points:
        spot_feed._history.append(point)
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
    entry_fee_100: Optional[float]
    breakeven_up: Optional[float]
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
        spot_px = runner.spot.price()

        vol_annual = None
        strategy = getattr(self._runner, "strategy", None) if self._runner else None
        window = getattr(strategy, "realized_vol_window", None) if strategy else None
        if window:
            strategy.current_realized_vol = runner.spot.realized_vol(float(window))
            vol_annual = strategy._vol()
        if vol_annual is None:
            # Show a measurement even when no strategy is loaded, so the panel
            # is informative before you press start.
            from .signals import annualize

            measured = runner.spot.realized_vol(900.0)
            vol_annual = annualize(measured, 900.0) if measured else None

        out: list[MarketView] = []
        for market in runner._markets(now):
            remaining = market.seconds_remaining(now)
            if remaining <= 0 or remaining > self.cfg.markets.max_seconds_remaining:
                continue
            snap = runner._snapshot(market, spot_px, now)
            if snap is None:
                continue

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
        return {
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
