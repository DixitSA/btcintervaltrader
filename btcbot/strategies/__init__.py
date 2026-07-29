"""Strategy registry."""

from __future__ import annotations

from typing import Any

from .always_trade import AlwaysTradeStrategy
from .base import Signal, Strategy
from .edge_threshold import EdgeThresholdStrategy
from .volume_threshold import VolumeThresholdStrategy

REGISTRY: dict[str, type[Strategy]] = {
    AlwaysTradeStrategy.name: AlwaysTradeStrategy,
    VolumeThresholdStrategy.name: VolumeThresholdStrategy,
    EdgeThresholdStrategy.name: EdgeThresholdStrategy,
}

__all__ = [
    "Signal",
    "Strategy",
    "AlwaysTradeStrategy",
    "EdgeThresholdStrategy",
    "VolumeThresholdStrategy",
    "REGISTRY",
    "build_strategy",
]


def build_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    if name not in REGISTRY:
        raise ValueError(f"unknown strategy '{name}'. available: {sorted(REGISTRY)}")
    return REGISTRY[name](**(params or {}))
