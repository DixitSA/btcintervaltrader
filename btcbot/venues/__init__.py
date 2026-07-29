"""Venue registry.

Kalshi is the only venue. The Venue protocol is kept rather than inlined
because it is what keeps the layers above it honest: strategies, risk sizing,
the portfolio, exits and the backtester all speak `Market`/`Book`/`Snapshot`
and so cannot reach venue-specific fields even by accident.
"""

from __future__ import annotations

from .base import Venue
from .kalshi import KalshiAuth, KalshiVenue

__all__ = [
    "Venue",
    "KalshiVenue",
    "KalshiAuth",
    "build_venue",
    "REGISTRY",
]

REGISTRY = {
    "kalshi": KalshiVenue,
}


def build_venue(cfg) -> Venue:
    name = cfg.venue
    if name == "kalshi":
        return KalshiVenue(base_url=cfg.kalshi_url, auth=KalshiAuth.from_env())
    raise RuntimeError(f"unknown venue: {name}. available: {sorted(REGISTRY)}")
