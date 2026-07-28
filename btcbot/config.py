"""Configuration loading.

Trading parameters live in config.yaml (checked in, safe to share).
Secrets live in .env (never checked in).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass
class RiskConfig:
    bankroll_usd: float = 100.0
    max_stake_per_trade_usd: float = 5.0
    max_stake_fraction: float = 0.02
    max_concurrent_positions: int = 1
    max_trades_per_hour: int = 8
    daily_loss_limit_usd: float = 20.0
    # Refuse to buy above this price: the downside tail is not worth the
    # remaining upside, and fees bite hardest here.
    max_entry_price: float = 0.90
    min_entry_price: float = 0.10
    kelly_fraction: float = 0.25


@dataclass
class FeeConfig:
    """Fees are venue policy and change. Verify against live fills before
    trusting any backtest produced with these numbers."""

    taker_fee_bps: float = 0.0
    winnings_fee_bps: float = 200.0
    # Modelled slippage beyond the quoted book, in cents of probability.
    slippage: float = 0.005


@dataclass
class MarketsConfig:
    slug_prefix: str = "btc-updown-15m"
    window_seconds: int = 900
    # Ignore a window until it has at least this much time left; and stop
    # entering once it has less than min_seconds_remaining.
    max_seconds_remaining: int = 900
    min_seconds_remaining: int = 30
    min_book_depth_usd: float = 50.0
    max_spread: float = 0.05


@dataclass
class BullpenConfig:
    """How to invoke the external Bullpen CLI.

    The template is deliberately explicit rather than built from guessed flags:
    the published syntax could not be verified from the build environment, and
    a wrong flag on an order path fails silently or sizes wrongly. Verify with
    `btcbot verify-bullpen` before trading.
    """

    binary: str = "bullpen"
    buy_template: list[str] = field(
        default_factory=lambda: [
            "bullpen",
            "polymarket",
            "buy",
            "--token",
            "{token_id}",
            "--shares",
            "{shares}",
            "--limit-price",
            "{price}",
            "--yes",
            "--json",
        ]
    )
    # Command used by `verify-bullpen` to confirm the binary and subcommand
    # exist without placing an order.
    help_template: list[str] = field(
        default_factory=lambda: ["bullpen", "polymarket", "buy", "--help"]
    )
    timeout_seconds: float = 30.0
    # Log the command without running it. Start here.
    dry_run: bool = True


@dataclass
class ExecutionConfig:
    # paper   -> simulate against the recorded book (default, safe)
    # bullpen -> shell out to the Bullpen CLI
    # clob    -> sign EIP-712 orders directly via py-clob-client
    backend: str = "paper"
    bullpen: BullpenConfig = field(default_factory=BullpenConfig)


@dataclass
class StrategyConfig:
    name: str = "volume_threshold"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    mode: str = "paper"  # paper | live
    risk: RiskConfig = field(default_factory=RiskConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    gamma_url: str = "https://gamma-api.polymarket.com"
    clob_url: str = "https://clob.polymarket.com"
    spot_url: str = "https://api.binance.com"
    poll_seconds: float = 2.0
    data_dir: str = "data"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def _merge(dc: Any, raw: dict[str, Any]) -> Any:
    """Overlay a dict onto a dataclass instance, ignoring unknown keys."""
    known = {f for f in dc.__dataclass_fields__}
    for key, value in raw.items():
        if key not in known:
            raise ValueError(f"unknown config key: {key}")
        setattr(dc, key, value)
    return dc


def load_config(path: str | Path | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = Config()
    if not path.exists():
        return cfg

    raw = yaml.safe_load(path.read_text()) or {}
    for section, target in (
        ("risk", cfg.risk),
        ("fees", cfg.fees),
        ("markets", cfg.markets),
    ):
        if section in raw:
            _merge(target, raw.pop(section))

    if "execution" in raw:
        ex = raw.pop("execution") or {}
        bullpen_raw = ex.pop("bullpen", None)
        _merge(cfg.execution, ex)
        if bullpen_raw:
            _merge(cfg.execution.bullpen, bullpen_raw)

    if "strategy" in raw:
        strat = raw.pop("strategy") or {}
        cfg.strategy = StrategyConfig(
            name=strat.get("name", "volume_threshold"),
            params=strat.get("params", {}) or {},
        )

    _merge(cfg, raw)

    # Environment always wins, so you cannot go live by editing a file alone.
    env_mode = os.getenv("BTCBOT_MODE")
    if env_mode:
        cfg.mode = env_mode
    return cfg


def require_live_confirmation() -> bool:
    """Live trading needs an explicit, separate opt-in beyond mode: live."""
    return os.getenv("BTCBOT_I_UNDERSTAND_REAL_MONEY") == "yes"
