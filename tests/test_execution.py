"""Executor tests, including the Bullpen CLI backend.

The Bullpen tests drive a fake `bullpen` script on PATH, so they verify the
argv we construct and the output we parse without touching a real venue.
"""

from __future__ import annotations

import os
import stat

import pytest

from btcbot.config import Config
from btcbot.execution import BullpenExecutor, PaperExecutor, build_executor
from btcbot.models import UP, Order

from .test_core import make_snapshot


def write_fake_bullpen(tmp_path, body: str) -> str:
    """Install a fake `bullpen` on PATH and return the new PATH value."""
    script = tmp_path / "bullpen"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f"{tmp_path}{os.pathsep}{os.environ['PATH']}"


@pytest.fixture
def bullpen_cfg() -> Config:
    cfg = Config()
    cfg.mode = "live"
    cfg.execution.backend = "bullpen"
    cfg.execution.bullpen.dry_run = False
    return cfg


def test_build_executor_defaults_to_paper():
    assert isinstance(build_executor(Config()), PaperExecutor)


def test_build_executor_refuses_real_backend_in_paper_mode():
    cfg = Config()
    cfg.execution.backend = "bullpen"  # mode is still "paper"
    with pytest.raises(RuntimeError, match="real orders"):
        build_executor(cfg)


def test_build_executor_requires_env_optin(bullpen_cfg, monkeypatch):
    monkeypatch.delenv("BTCBOT_I_UNDERSTAND_REAL_MONEY", raising=False)
    with pytest.raises(RuntimeError, match="BTCBOT_I_UNDERSTAND_REAL_MONEY"):
        build_executor(bullpen_cfg)


def test_build_executor_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("BTCBOT_I_UNDERSTAND_REAL_MONEY", "yes")
    cfg = Config()
    cfg.mode = "live"
    cfg.execution.backend = "nonsense"
    with pytest.raises(RuntimeError, match="unknown execution backend"):
        build_executor(cfg)


def test_bullpen_missing_binary_is_a_clear_error(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir: no bullpen
    with pytest.raises(RuntimeError, match="not found on PATH"):
        BullpenExecutor(bullpen_cfg)


def test_bullpen_renders_expected_argv(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", write_fake_bullpen(tmp_path, "exit 0\n"))
    ex = BullpenExecutor(bullpen_cfg)
    snap = make_snapshot(p_up=0.50)
    argv = ex.render(snap, Order(side=UP, shares=12.5, limit_price=0.523))

    assert argv[0:3] == ["bullpen", "polymarket", "buy"]
    assert "--token" in argv
    assert argv[argv.index("--token") + 1] == snap.market.up_token_id
    assert argv[argv.index("--shares") + 1] == "12.50"
    assert argv[argv.index("--limit-price") + 1] == "0.523"
    # No unsubstituted placeholders may survive into a real command.
    assert not any("{" in part for part in argv)


def test_bullpen_dry_run_does_not_execute(bullpen_cfg, monkeypatch, tmp_path):
    marker = tmp_path / "ran"
    monkeypatch.setenv("PATH", write_fake_bullpen(tmp_path, f"touch {marker}\nexit 0\n"))
    bullpen_cfg.execution.bullpen.dry_run = True

    ex = BullpenExecutor(bullpen_cfg)
    assert ex.buy(make_snapshot(), Order(side=UP, shares=10, limit_price=0.5)) is None
    assert not marker.exists()


def test_bullpen_parses_json_fill(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PATH",
        write_fake_bullpen(tmp_path, 'echo \'{"avgPrice":"0.487","filledSize":9.5}\'\nexit 0\n'),
    )
    ex = BullpenExecutor(bullpen_cfg)
    fill = ex.buy(make_snapshot(), Order(side=UP, shares=10, limit_price=0.52))

    assert fill is not None
    assert fill.price == pytest.approx(0.487)
    assert fill.shares == pytest.approx(9.5)


def test_bullpen_parses_nested_result(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PATH",
        write_fake_bullpen(
            tmp_path, 'echo \'{"ok":true,"order":{"price":0.31,"size":4}}\'\nexit 0\n'
        ),
    )
    ex = BullpenExecutor(bullpen_cfg)
    fill = ex.buy(make_snapshot(), Order(side=UP, shares=4, limit_price=0.35))
    assert fill is not None
    assert fill.price == pytest.approx(0.31)


def test_bullpen_nonzero_exit_is_not_a_fill(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv(
        "PATH", write_fake_bullpen(tmp_path, 'echo "insufficient balance" >&2\nexit 1\n')
    )
    ex = BullpenExecutor(bullpen_cfg)
    assert ex.buy(make_snapshot(), Order(side=UP, shares=10, limit_price=0.5)) is None
    assert ex.fills == []


def test_bullpen_unparseable_output_falls_back_to_limit(bullpen_cfg, monkeypatch, tmp_path):
    """A fill we cannot parse must still be recorded as a position -- money moved.

    Assuming "no fill" here would be far more dangerous than an approximate
    price: the bot would re-enter a window it already holds.
    """
    monkeypatch.setenv("PATH", write_fake_bullpen(tmp_path, 'echo "Order placed!"\nexit 0\n'))
    ex = BullpenExecutor(bullpen_cfg)
    fill = ex.buy(make_snapshot(), Order(side=UP, shares=10, limit_price=0.52))

    assert fill is not None
    assert fill.price == pytest.approx(0.52)
    assert fill.shares == pytest.approx(10)


def test_bullpen_bad_placeholder_is_rejected(bullpen_cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", write_fake_bullpen(tmp_path, "exit 0\n"))
    bullpen_cfg.execution.bullpen.buy_template = ["bullpen", "--x", "{not_a_field}"]
    ex = BullpenExecutor(bullpen_cfg)
    with pytest.raises(RuntimeError, match="unknown placeholder"):
        ex.render(make_snapshot(), Order(side=UP, shares=1, limit_price=0.5))
