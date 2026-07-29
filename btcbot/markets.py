"""Market discovery via the Polymarket Gamma API.

Finds the currently-open BTC 15-minute Up/Down windows and normalises them
into `Market` objects.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .config import strike_bounds_for_prefix
from .models import Market

log = logging.getLogger(__name__)

# Polymarket writes the reference price into the market text, e.g.
# "... will be above $109,412.55 at ...". We parse it best-effort.
_STRIKE_RE = re.compile(r"\$\s?([0-9][0-9,]*\.?[0-9]*)")


class GammaClient:
    def __init__(self, base_url: str = "https://gamma-api.polymarket.com", timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "btcintervaltrader/0.1"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GammaClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def fetch_open_markets(
        self,
        slug_prefixes: str | list[str],
        limit: int = 250,
        strike_bounds: dict[str, tuple[float, float]] | None = None,
    ) -> list[Market]:
        """All open windows whose slug starts with any of `slug_prefixes`.

        `strike_bounds` maps prefix -> (min, max) plausible strike, normally from
        family config; missing entries fall back to the asset's default range.
        """
        if isinstance(slug_prefixes, str):
            slug_prefixes = [slug_prefixes]
        bounds = strike_bounds or {}

        resp = self._http.get(
            f"{self.base_url}/markets",
            params={
                "closed": "false",
                "active": "true",
                "limit": limit,
                "order": "endDate",
                "ascending": "true",
            },
        )
        resp.raise_for_status()
        markets = []
        for raw in resp.json():
            slug = str(raw.get("slug", ""))
            match = next((p for p in slug_prefixes if slug.startswith(p)), None)
            if match is None:
                continue
            lo, hi = bounds.get(match) or strike_bounds_for_prefix(match)
            parsed = parse_market(raw, strike_min=lo, strike_max=hi)
            if parsed is not None:
                markets.append(parsed)
        return markets


def _parse_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


#: Re-exported so callers can resolve bounds without importing config.
_strike_bounds_for_prefix = strike_bounds_for_prefix


def parse_strike(*texts: Any, strike_min: float | None = None, strike_max: float | None = None) -> Optional[float]:
    """Pull the 'price to beat' out of market text.

    Returns None when it cannot be read unambiguously. Callers must skip the
    market rather than guess -- an incorrect strike silently inverts every
    signal that depends on it.

    Pass *strike_min* and *strike_max* to override the default BTC bounds
    (e.g. 1-10k for SOL, 100-100k for ETH).
    """
    lo = strike_min if strike_min is not None else 1_000
    hi = strike_max if strike_max is not None else 10_000_000
    for text in texts:
        if not text:
            continue
        matches = _STRIKE_RE.findall(str(text))
        candidates = set()
        for m in matches:
            try:
                candidates.add(float(m.replace(",", "")))
            except ValueError:
                continue
        plausible = {c for c in candidates if lo <= c <= hi}
        if len(plausible) == 1:
            return plausible.pop()
    return None


def parse_market(raw: dict[str, Any], strike_min: float | None = None, strike_max: float | None = None) -> Optional[Market]:
    token_ids = raw.get("clobTokenIds")
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except json.JSONDecodeError:
            return None
    if not token_ids or len(token_ids) < 2:
        return None

    outcomes = raw.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None

    up_idx = 0
    if outcomes and len(outcomes) >= 2:
        lowered = [str(o).strip().lower() for o in outcomes]
        for candidate in ("up", "yes"):
            if candidate in lowered:
                up_idx = lowered.index(candidate)
                break

    start_ts = _parse_ts(raw.get("startDate")) or _parse_ts(raw.get("createdAt"))
    end_ts = _parse_ts(raw.get("endDate"))
    if end_ts is None:
        return None

    return Market(
        condition_id=str(raw.get("conditionId", "")),
        slug=str(raw.get("slug", "")),
        question=str(raw.get("question", "")),
        up_token_id=str(token_ids[up_idx]),
        down_token_id=str(token_ids[1 - up_idx]),
        start_ts=start_ts if start_ts is not None else end_ts - 900,
        end_ts=end_ts,
        volume=float(raw.get("volumeNum") or raw.get("volume") or 0.0),
        liquidity=float(raw.get("liquidityNum") or raw.get("liquidity") or 0.0),
        strike=parse_strike(raw.get("description"), raw.get("question"), strike_min=strike_min, strike_max=strike_max),
    )
