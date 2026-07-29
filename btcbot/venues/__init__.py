"""Venue registry."""

from __future__ import annotations

from .base import Venue
from .kalshi import KalshiAuth, KalshiVenue
from .polymarket import PolymarketVenue

__all__ = [
    "Venue",
    "KalshiVenue",
    "KalshiAuth",
    "PolymarketVenue",
    "build_venue",
    "REGISTRY",
]

REGISTRY = {
    "kalshi": KalshiVenue,
    "polymarket": PolymarketVenue,
}


def build_venue(cfg) -> Venue:
    name = cfg.venue
    if name == "kalshi":
        return KalshiVenue(base_url=cfg.kalshi_url, auth=KalshiAuth.from_env())
    if name == "polymarket":
        return PolymarketVenue(gamma_url=cfg.gamma_url, clob_url=cfg.clob_url)
    raise RuntimeError(f"unknown venue: {name}. available: {sorted(REGISTRY)}")
