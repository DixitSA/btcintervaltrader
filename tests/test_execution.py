"""Executor tests.

These cover the gates that stand between a config edit and a real order. The
Bullpen CLI backend these once also covered was Polymarket-only and has been
removed along with the rest of that venue.
"""

from __future__ import annotations

import pytest

from btcbot.config import Config
from btcbot.execution import PaperExecutor, build_executor


def live_cfg() -> Config:
    cfg = Config()
    cfg.mode = "live"
    cfg.execution.backend = "venue"
    return cfg


def test_build_executor_defaults_to_paper():
    assert isinstance(build_executor(Config()), PaperExecutor)


def test_build_executor_refuses_real_backend_in_paper_mode():
    cfg = Config()
    cfg.execution.backend = "venue"  # mode is still "paper"
    with pytest.raises(RuntimeError, match="real orders"):
        build_executor(cfg)


def test_build_executor_requires_env_optin(monkeypatch):
    monkeypatch.delenv("BTCBOT_I_UNDERSTAND_REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="BTCBOT_I_UNDERSTAND_REAL_MONEY"):
        build_executor(live_cfg())


def test_build_executor_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("BTCBOT_I_UNDERSTAND_REAL_MONEY", "yes")
    cfg = Config()
    cfg.mode = "live"
    cfg.execution.backend = "nonsense"
    with pytest.raises(RuntimeError, match="unknown execution backend"):
        build_executor(cfg)


def test_retired_polymarket_backends_are_rejected(monkeypatch):
    """bullpen/clob were Polymarket paths. They must not silently fall through
    to the live Kalshi executor now that the names are gone."""
    monkeypatch.setenv("BTCBOT_I_UNDERSTAND_REAL_MONEY", "yes")
    for backend in ("bullpen", "clob"):
        cfg = Config()
        cfg.mode = "live"
        cfg.execution.backend = backend
        with pytest.raises(RuntimeError, match="unknown execution backend"):
            build_executor(cfg)
