"""The research crew's read-only boundary.

These tests exist because the boundary is enforced in code rather than by
telling a language model not to misbehave. If any of them fail, an agent can
reach something it should not.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crew"))

import btcbot_tools as tools  # noqa: E402


# --- the allowlist ------------------------------------------------------

@pytest.mark.parametrize(
    "command",
    ["live", "paper", "record", "serve", "shadow-replay", "simulate"],
)
def test_trading_and_long_running_commands_are_refused(command):
    """The whole point. `live` moves real money; the rest mutate state."""
    with pytest.raises(tools.ToolError, match="refused"):
        tools.run_btcbot(command)


def test_unknown_commands_are_refused():
    """Default deny, so a new command is unreachable until someone allows it."""
    with pytest.raises(tools.ToolError, match="refused"):
        tools.run_btcbot("definitely-not-a-command")
    with pytest.raises(tools.ToolError, match="refused"):
        tools.run_btcbot("")


def test_network_commands_need_explicit_opt_in():
    with pytest.raises(tools.ToolError, match="allow_network"):
        tools.run_btcbot("verify-venue")


def test_live_is_not_reachable_by_any_spelling():
    """Guards against the allowlist being bypassed by argument smuggling."""
    for attempt in ["live", " live ", "LIVE", "live --yes", "backtest;live", "../live"]:
        with pytest.raises(tools.ToolError):
            tools.run_btcbot(attempt)


def test_analysis_commands_are_allowed():
    assert "backtest" in tools.ALLOWED_COMMANDS
    assert "sweep" in tools.ALLOWED_COMMANDS
    assert "hurst" in tools.ALLOWED_COMMANDS
    # And the dangerous ones are not in it at all.
    for command in ("live", "paper", "record", "serve"):
        assert command not in tools.ALLOWED_COMMANDS


# --- argument validation ------------------------------------------------

def test_override_keys_and_values_are_validated():
    """`--set` is the one place agent text reaches an argv."""
    for bad_key in ["--strategy", "a b", "x;y", "-set", "A" * 60, ""]:
        with pytest.raises(tools.ToolError, match="bad parameter name"):
            tools.run_btcbot("backtest", overrides={bad_key: "1"})

    for bad_value in ["a b", "x;rm -rf /", "$(whoami)", "`id`", "a" * 40, ""]:
        with pytest.raises(tools.ToolError, match="bad value"):
            tools.run_btcbot("backtest", overrides={"min_edge": bad_value})


def test_reasonable_overrides_pass_validation():
    """Sanity check that validation is not simply refusing everything."""
    assert tools._validate_overrides({"min_edge": "0.05"}) == ["--set", "min_edge=0.05"]
    assert tools._validate_overrides({"direction": "fade"}) == ["--set", "direction=fade"]
    assert tools._validate_overrides({"fair_value": "microprice"}) == [
        "--set", "fair_value=microprice"
    ]
    assert tools._validate_overrides(None) == []


def test_strategy_names_are_validated():
    with pytest.raises(tools.ToolError, match="bad strategy"):
        tools.run_btcbot("backtest", strategy="../../etc/passwd")
    with pytest.raises(tools.ToolError, match="bad strategy"):
        tools.run_btcbot("backtest", strategy="edge; live")


@pytest.mark.parametrize(
    "bad_dir", ["../../../etc", "/etc", "../..", "/var/lib", "data/../../.."]
)
def test_data_dir_cannot_escape_the_repo(bad_dir):
    with pytest.raises(tools.ToolError, match="outside the repo"):
        tools.run_btcbot("backtest", data_dir=bad_dir)
    with pytest.raises(tools.ToolError, match="outside the repo"):
        tools.count_windows(bad_dir)


def test_doc_reads_are_restricted_to_a_known_set():
    with pytest.raises(tools.ToolError, match="refused"):
        tools.read_repo_doc(".env")
    with pytest.raises(tools.ToolError, match="refused"):
        tools.read_repo_doc("../../etc/passwd")
    with pytest.raises(tools.ToolError, match="refused"):
        tools.read_repo_doc("btcbot/execution.py")
    # And one that should work.
    assert "btcintervaltrader" in tools.read_repo_doc("README.md")


def test_env_file_is_not_reachable():
    """A leaked Kalshi key means someone else can trade the account."""
    for attempt in [".env", ".env.example", "../.env", "/home/user/.env"]:
        if attempt == ".env.example":
            continue  # harmless, but also not on the list
        with pytest.raises(tools.ToolError):
            tools.read_repo_doc(attempt)


# --- ops helpers --------------------------------------------------------

def test_recorder_health_handles_a_missing_log():
    out = tools.recorder_health("nonexistent-supervisor.log")
    assert "never" in out.lower() or "no supervisor log" in out.lower()


def test_recorder_health_refuses_paths_outside_the_repo():
    with pytest.raises(tools.ToolError, match="outside the repo"):
        tools.recorder_health("../../../var/log/syslog")


def test_disk_free_reports_something_sane():
    out = tools.disk_free()
    assert "disk free" in out
    assert "GB" in out


def test_window_count_language_warns_about_the_300x_trap(tmp_path):
    """The single most expensive misreading available in this repo."""
    empty = ROOT / "data"
    if empty.exists() and any(empty.glob("*.jsonl")):
        out = tools.count_windows("data")
        assert "windows" in out
        assert "300x" in out or "overstate" in out
