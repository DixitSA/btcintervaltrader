"""Kalshi venue adapter.

Two things about Kalshi differ from Polymarket in ways that will silently
corrupt results if you get them wrong:

1. THE ORDERBOOK CONTAINS ONLY BIDS. The `yes` and `no` arrays are both resting
   bids. There is no ask side. A bid for YES at 42c is the same object as an
   ask for NO at 58c, so:

       yes_ask = 100 - best_no_bid
       no_ask  = 100 - best_yes_bid

   Reading `no` as the yes-ask inverts every price you compute.

2. FEES ARE CHARGED UP FRONT ON EVERY FILL, not on profit at settlement, and
   they peak at 1.75c per contract at 50c. See fees.py.

Prices are integer cents (1-99) on the wire; this module converts to the
0.0-1.0 probability convention the rest of the bot uses.

Public market data needs no authentication. Only order placement and portfolio
endpoints do, so `record` and `paper` run with no credentials at all.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import strike_bounds_for_prefix
from ..models import DOWN, UP, Book, Level, Market, Order

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# KXBTC15M-26JUL281745 -> series KXBTC15M, 2026-07-28, 17:45 ET
_TICKER_RE = re.compile(r"^(?P<series>[A-Z0-9]+)-(?P<date>\d{2}[A-Z]{3}\d{2})(?P<time>\d{4})")
_STRIKE_RE = re.compile(r"\$\s?([0-9][0-9,]*\.?[0-9]*)")


def cents_to_prob(cents: float) -> float:
    return float(cents) / 100.0


def _as_float(value: Any) -> Optional[float]:
    """Coerce a wire value to float, or None.

    The live API sends numbers as decimal strings on the `*_fp` fields, so an
    isinstance(value, (int, float)) check drops them on the floor.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


class KalshiAuth:
    """RSA-PSS request signing.

    The signed string is timestamp_ms + HTTP method + path, where the path
    excludes the query string. Timestamp is milliseconds -- seconds is the
    single most common cause of signature rejections.
    """

    def __init__(self, key_id: str, private_key_pem: bytes):
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:  # pragma: no cover - optional extra
            raise RuntimeError(
                "Kalshi authentication needs: pip install cryptography"
            ) from exc

        self.key_id = key_id
        self._padding = padding
        self._hashes = hashes
        self._key = serialization.load_pem_private_key(private_key_pem, password=None)

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        # Sign the path WITHOUT any query string.
        bare_path = path.split("?", 1)[0]
        message = f"{timestamp_ms}{method.upper()}{bare_path}".encode()

        signature = self._key.sign(
            message,
            self._padding.PSS(
                mgf=self._padding.MGF1(self._hashes.SHA256()),
                salt_length=self._padding.PSS.DIGEST_LENGTH,
            ),
            self._hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    @classmethod
    def from_env(cls) -> Optional["KalshiAuth"]:
        key_id = os.getenv("KALSHI_API_KEY_ID")
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
        inline = os.getenv("KALSHI_PRIVATE_KEY")

        if not key_id or not (key_path or inline):
            return None
        pem = Path(key_path).read_bytes() if key_path else inline.encode()
        return cls(key_id, pem)


def parse_orderbook(raw: dict[str, Any]) -> tuple[Book, Book]:
    """Turn a Kalshi orderbook into (yes_book, no_book) in probability units.

    Kalshi gives resting bids only. Each side's ASK is synthesised from the
    opposite side's bid, which is what makes a two-sided book for each outcome.
    """
    # The live API nests the levels under `orderbook_fp` (fixed-point: prices
    # and sizes are decimal STRINGS in dollars). Older/documented responses use
    # `orderbook` with integer cents. Accept either, and fall back to a payload
    # that is already the book itself.
    book = raw.get("orderbook_fp") or raw.get("orderbook") or raw or {}

    def levels(key_cents: str, key_dollars: str) -> list[tuple[float, float]]:
        # Newer responses use *_dollars string pairs; older use cent integers.
        entries = book.get(key_dollars)
        scale = 1.0
        if entries is None:
            entries = book.get(key_cents) or []
            scale = 0.01
        out = []
        for entry in entries:
            if not entry or len(entry) < 2:
                continue
            try:
                price = float(entry[0]) * scale
                size = float(entry[1])
            except (TypeError, ValueError):
                continue
            if size > 0:
                out.append((price, size))
        return out

    yes_bids = levels("yes", "yes_dollars")
    no_bids = levels("no", "no_dollars")

    def build(own_bids, other_bids) -> Book:
        bids = [Level(p, s) for p, s in sorted(own_bids, key=lambda x: -x[0])]
        # A bid of p on the other side is an ask of (1 - p) on this one.
        asks = [Level(1.0 - p, s) for p, s in sorted(other_bids, key=lambda x: -x[0])]
        asks.sort(key=lambda lv: lv.price)
        return Book(bids=bids, asks=asks)

    return build(yes_bids, no_bids), build(no_bids, yes_bids)


def parse_close_ts(raw: dict[str, Any]) -> Optional[float]:
    for key in ("close_time", "expiration_time", "latest_expiration_time"):
        value = raw.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


def parse_open_ts(raw: dict[str, Any]) -> Optional[float]:
    for key in ("open_time", "created_time"):
        value = raw.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
    return None


#: Re-exported so callers can resolve bounds without importing config.
_strike_bounds_for_prefix = strike_bounds_for_prefix


def parse_strike(raw: dict[str, Any], strike_min: float | None = None, strike_max: float | None = None) -> Optional[float]:
    """The 'price to beat'.

    Kalshi exposes numeric strikes on range markets; the up/down series carries
    it in the text. Returns None when it cannot be read unambiguously -- a
    wrong strike silently inverts every model-based signal.

    Pass *strike_min* and *strike_max* to override the asset-specific default
    bounds (e.g. 1-10k for SOL, 100-100k for ETH).
    """
    lo = strike_min if strike_min is not None else 1_000
    hi = strike_max if strike_max is not None else 10_000_000

    for key in ("floor_strike", "cap_strike", "strike_value"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and lo <= float(value) <= hi:
            return float(value)

    for key in ("subtitle", "yes_sub_title", "title", "rules_primary"):
        text = raw.get(key)
        if not text:
            continue
        found = set()
        for match in _STRIKE_RE.findall(str(text)):
            try:
                found.add(float(match.replace(",", "")))
            except ValueError:
                continue
        plausible = {v for v in found if lo <= v <= hi}
        if len(plausible) == 1:
            return plausible.pop()
    return None


def parse_market(raw: dict[str, Any], strike_min: float | None = None, strike_max: float | None = None) -> Optional[Market]:
    ticker = str(raw.get("ticker") or "")
    if not ticker:
        return None

    close_ts = parse_close_ts(raw)
    if close_ts is None:
        return None
    open_ts = parse_open_ts(raw) or (close_ts - 900)

    # The live API returns these as decimal STRINGS under `*_fp` names
    # ("644509.10"), not the bare numbers the docs imply. Reading only the
    # documented names leaves volume at 0.0 for every market, which silently
    # makes any volume-gated strategy skip everything.
    #
    # NOTE ON UNITS: Kalshi's volume is CONTRACTS, not dollars. Each contract
    # is $1 notional, so this is an upper bound on matched USD volume, not the
    # figure itself. `min_volume_usd` compares against it directly.
    volume = 0.0
    for key in ("dollar_volume", "volume_fp", "volume", "volume_24h_fp", "volume_24h"):
        value = _as_float(raw.get(key))
        if value is not None:
            volume = value
            break

    return Market(
        condition_id=ticker,
        slug=ticker,
        question=str(raw.get("title") or raw.get("subtitle") or ticker),
        # Kalshi is single-token: YES and NO are two sides of one market, so
        # both "token ids" are the ticker. The side is carried in the order.
        up_token_id=ticker,
        down_token_id=ticker,
        start_ts=open_ts,
        end_ts=close_ts,
        volume=volume,
        liquidity=(
            _as_float(raw.get("open_interest_fp"))
            or _as_float(raw.get("open_interest"))
            or 0.0
        ),
        strike=parse_strike(raw, strike_min, strike_max),
    )


class KalshiVenue:
    name = "kalshi"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        auth: Optional[KalshiAuth] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._http = httpx.Client(
            timeout=timeout, headers={"User-Agent": "btcintervaltrader/0.1"}
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "KalshiVenue":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _path(self, path: str) -> str:
        from urllib.parse import urlparse

        return urlparse(self.base_url).path.rstrip("/") + path

    def _request(
        self, method: str, path: str, *, authed: bool = False, **kwargs: Any
    ) -> Any:
        headers = {}
        if authed:
            if self.auth is None:
                raise RuntimeError(
                    "Kalshi credentials are not configured. Set KALSHI_API_KEY_ID "
                    "and KALSHI_PRIVATE_KEY_PATH."
                )
            headers = self.auth.headers(method, self._path(path))

        resp = self._http.request(
            method, f"{self.base_url}{path}", headers=headers, **kwargs
        )
        resp.raise_for_status()
        return resp.json()

    # -- market data ---------------------------------------------------

    def discover_markets(
        self,
        prefixes: list[str],
        strike_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> list[Market]:
        """Open markets in any of the given series.

        `prefixes` are series tickers (e.g. KXBTC15M), matched case-insensitively
        against the market ticker prefix.

        `strike_bounds` maps prefix -> (min, max) plausible strike, normally from
        family config. Anything missing falls back to the asset's default range,
        which matters because BTC's range rejects every SOL strike.
        """
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        wanted = [p.upper() for p in prefixes]
        bounds = {k.upper(): v for k, v in (strike_bounds or {}).items()}

        found: list[Market] = []
        seen: set[str] = set()
        for series in wanted:
            try:
                data = self._request(
                    "GET",
                    "/markets",
                    params={"series_ticker": series, "status": "open", "limit": 200},
                )
            except httpx.HTTPError as exc:
                log.warning("kalshi market discovery failed for %s: %s", series, exc)
                continue

            for raw in data.get("markets", []) or []:
                ticker = str(raw.get("ticker") or "")
                if ticker in seen:
                    continue
                # Resolve bounds from the prefix this ticker actually matches --
                # the /markets query is per-series but the filter accepts any
                # wanted prefix, so `series` is not necessarily the right asset.
                match = next(
                    (p for p in wanted if ticker.upper().startswith(p)), None
                )
                if match is None:
                    continue
                lo, hi = bounds.get(match) or strike_bounds_for_prefix(match)
                market = parse_market(raw, strike_min=lo, strike_max=hi)
                if market is not None:
                    seen.add(ticker)
                    found.append(market)
        return found

    def get_books(self, market: Market) -> Optional[tuple[Book, Book]]:
        try:
            raw = self._request("GET", f"/markets/{market.slug}/orderbook")
        except httpx.HTTPError as exc:
            log.warning("kalshi orderbook fetch failed for %s: %s", market.slug, exc)
            return None
        return parse_orderbook(raw)

    # -- trading -------------------------------------------------------

    def place_order(self, market: Market, order: Order, selling: bool) -> dict[str, Any]:
        """Submit a limit order. Prices go to Kalshi as integer cents."""
        side = "yes" if order.side == UP else "no"
        price_cents = int(round(order.limit_price * 100))
        price_cents = min(99, max(1, price_cents))

        body = {
            "ticker": market.slug,
            "action": "sell" if selling else "buy",
            "side": side,
            "count": int(round(order.shares)),
            "type": "limit",
            f"{side}_price": price_cents,
            "client_order_id": f"btcbot-{int(time.time()*1000)}",
            # Immediate-or-cancel: we priced against the book we just read, and
            # the window expires in minutes. A resting remainder is a liability.
            "time_in_force": "immediate_or_cancel",
        }
        return self._request("POST", "/portfolio/orders", authed=True, json=body)

    def get_balance(self) -> Optional[float]:
        """Account balance in dollars, or None if unavailable."""
        try:
            data = self._request("GET", "/portfolio/balance", authed=True)
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("kalshi balance fetch failed: %s", exc)
            return None
        cents = data.get("balance")
        return float(cents) / 100.0 if isinstance(cents, (int, float)) else None

    def get_positions(self) -> list[dict[str, Any]]:
        try:
            data = self._request("GET", "/portfolio/positions", authed=True)
        except (httpx.HTTPError, RuntimeError) as exc:
            log.warning("kalshi positions fetch failed: %s", exc)
            return []
        return data.get("market_positions", []) or []


def parse_ticker(ticker: str) -> dict[str, Any]:
    """Split a KXBTC15M-style ticker into its parts. Informational only."""
    match = _TICKER_RE.match(ticker.upper())
    if not match:
        return {"series": ticker, "date": None, "time": None}
    return {
        "series": match.group("series"),
        "date": match.group("date"),
        "time": match.group("time"),
    }
