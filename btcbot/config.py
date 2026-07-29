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
    # Cap on the TOTAL cost basis of all open positions, as a fraction of the
    # starting bankroll. This is the guard that matters once you trade many
    # windows at once: Kelly sizes each position as if it were the only one, so
    # N concurrent positions is N times the intended risk without this.
    max_total_exposure_fraction: float = 0.10
    daily_loss_limit_usd: float = 20.0
    # Refuse to buy above this price: the downside tail is not worth the
    # remaining upside, and fees bite hardest here.
    max_entry_price: float = 0.90
    min_entry_price: float = 0.10
    kelly_fraction: float = 0.25


@dataclass
class FeeConfig:
    """Fees are venue policy and change. Verify against live fills before
    trusting any backtest produced with these numbers.

    Polymarket fields are the *_bps pair. Kalshi uses its published taker
    formula instead -- ceil(0.07 * C * P * (1-P)) charged on every fill, which
    peaks at 1.75c per contract at 50c. See fees.py.
    """

    taker_fee_bps: float = 0.0
    winnings_fee_bps: float = 200.0
    kalshi_taker_coefficient: float = 0.07
    kalshi_maker_coefficient: float = 0.0
    # Modelled slippage beyond the quoted book, in cents of probability.
    slippage: float = 0.005


@dataclass
class FamilyConfig:
    """One asset family: market prefix + spot config + strike bounds.

    `prefix` is the Kalshi series ticker (KXBTC15M) or Polymarket slug prefix
    (btc-updown-15m).  `spot_symbol` is the Binance trading pair (BTCUSDT).
    `display_name` is a short label for the UI (BTC, ETH, SOL).
    """
    prefix: str
    spot_symbol: str = ""
    display_name: str = ""
    strike_min: float = 1_000
    strike_max: float = 10_000_000
    window_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.spot_symbol:
            self.spot_symbol = _default_spot(self.prefix)
        if not self.display_name:
            self.display_name = _default_display(self.prefix)


def _default_spot(prefix: str) -> str:
    """Derive a Binance symbol from a Kalshi series or Polymarket slug."""
    # KXBTC15M / KXETH15M / KXSOL15M → BTCUSDT / ETHUSDT / SOLUSDT
    for sym in ("BTC", "ETH", "SOL"):
        if sym in prefix.upper():
            return f"{sym}USDT"
    return "BTCUSDT"


def _default_display(prefix: str) -> str:
    for sym in ("BTC", "ETH", "SOL"):
        if sym in prefix.upper():
            return sym
    return prefix


@dataclass
class MarketsConfig:
    # Every family of windows to trade. Key is a short slug like "btc".
    # Each family defines its market prefix, spot symbol, strike bounds,
    # and window duration.  Global gates (min_seconds_remaining, spread,
    # depth, etc.) apply across all families.
    families: dict[str, FamilyConfig] = field(default_factory=lambda: {
        "btc": FamilyConfig(prefix="KXBTC15M")
    })
    # Common gates (applied to every family entry attempt).
    max_seconds_remaining: int = 900
    min_seconds_remaining: int = 30
    min_book_depth_usd: float = 50.0
    max_spread: float = 0.05

    @property
    def slug_prefixes(self) -> list[str]:
        return [f.prefix for f in self.families.values()]

    @property
    def window_seconds(self) -> int:
        """Backward-compat: first family's window or 900."""
        for f in self.families.values():
            return f.window_seconds
        return 900

    def family_for(self, slug: str) -> str | None:
        """Return the family key whose prefix matches *slug*, or None."""
        slug_upper = slug.upper()
        for key, fam in self.families.items():
            if slug_upper.startswith(fam.prefix.upper()):
                return key
        return None


@dataclass
class ExitsConfig:
    """Stop loss / take profit. Thresholds are in PROBABILITY POINTS.

    Entered at 0.60 with stop_loss_drop 0.15 -> exits when the bid hits 0.45.
    Set any threshold to null to disable just that rule.
    """

    enabled: bool = True
    stop_loss_drop: Optional[float] = 0.15
    take_profit_rise: Optional[float] = None
    trailing_stop_drop: Optional[float] = None
    max_hold_seconds: Optional[float] = None
    # Do not churn in the last few seconds: the book widens and the outcome is
    # nearly decided, so exiting there usually pays the spread for nothing.
    no_exit_within_seconds: float = 20.0
    min_hold_seconds: float = 0.0
    # Account-level kill switch on equity drawdown (sees open positions too,
    # unlike the daily loss limit which only counts closed trades).
    max_drawdown_usd: Optional[float] = None
    max_drawdown_pct: Optional[float] = 0.25


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
    # Used to exit a position early (stop loss / take profit).
    sell_template: list[str] = field(
        default_factory=lambda: [
            "bullpen",
            "polymarket",
            "sell",
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
class LearningConfig:
    """Online calibration from observed trade outcomes.

    Disabled by default. Enable only after you have accumulated hundreds of
    settled trades — with fewer data points the posterior barely budges from
    the prior, so there is nothing to gain and configuration overhead to pay.
    """

    enabled: bool = False
    alpha_prior: float = 1.0
    beta_prior: float = 1.0
    outcome_file: str = "outcomes.jsonl"


@dataclass
class StrategyConfig:
    name: str = "volume_threshold"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ShadowConfig:
    enabled: bool = True
    rungs: list[int] = field(default_factory=lambda: [120, 240, 420, 900])
    directions: list[str] = field(default_factory=lambda: ["follow", "fade"])
    notional_usd: float = 1.0
    ledger_file: str = "shadow.jsonl"


@dataclass
class Config:
    mode: str = "paper"  # paper | live
    venue: str = "kalshi"  # kalshi | polymarket
    risk: RiskConfig = field(default_factory=RiskConfig)
    fees: FeeConfig = field(default_factory=FeeConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    exits: ExitsConfig = field(default_factory=ExitsConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    kalshi_url: str = "https://api.elections.kalshi.com/trade-api/v2"
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
    markets_raw = raw.get("markets") or {}

    # Backward compat: upgrade old slug_prefixes to families.
    if "slug_prefix" in markets_raw:
        legacy = markets_raw.pop("slug_prefix")
        markets_raw.setdefault("slug_prefixes", [legacy] if isinstance(legacy, str) else legacy)
    if "slug_prefixes" in markets_raw and "families" not in markets_raw:
        prefixes = markets_raw.pop("slug_prefixes")
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        families = {}
        for p in prefixes:
            key = _family_key(p)
            families[key] = FamilyConfig(prefix=p)
        markets_raw["families"] = families

    for section, target in (
        ("risk", cfg.risk),
        ("fees", cfg.fees),
        ("markets", cfg.markets),
        ("exits", cfg.exits),
        ("learning", cfg.learning),
        ("shadow", cfg.shadow),
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


def _family_key(prefix: str) -> str:
    """Derive a short key like 'btc' from a market prefix."""
    upper = prefix.upper()
    for sym in ("BTC", "ETH", "SOL"):
        if sym in upper:
            return sym.lower()
    return upper.lower().replace("KX", "").replace("15M", "").replace("5M", "").replace("1H", "").strip("-")


def require_live_confirmation() -> bool:
    """Live trading needs an explicit, separate opt-in beyond mode: live."""
    return os.getenv("BTCBOT_I_UNDERSTAND_REAL_MONEY") == "yes"
