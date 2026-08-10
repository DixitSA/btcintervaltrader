"""Read-only access to btcbot, for the research crew.

This module is the security boundary, and it is deliberately dependency-free
(stdlib only, no crewai) so that `tests/test_crew_tools.py` can test it in the
main suite without installing the agent stack.

THE RULE: an agent can ask for analysis. It cannot trade, cannot record, cannot
start a server, and cannot write to config. That is enforced here, in code, by
constructing every argv from validated parameters and refusing anything not on
the allowlist.

It is NOT enforced by telling the model not to. Prompt instructions are not a
security boundary -- a model that has been talked into wanting to run `live`
must find that there is no code path that would let it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent

# Commands the crew may run. Every one of these is pure analysis over data that
# already exists: it terminates, places no orders, and touches no live state.
#
# Deliberately absent, and the reason:
#   live           -- real money
#   paper          -- mutates portfolio state, runs forever
#   record         -- runs forever; the recorder is managed by systemd
#   serve          -- starts an unauthenticated web server
#   shadow-replay  -- rewrites the shadow ledger
#   simulate       -- overwrites a data directory
ALLOWED_COMMANDS = frozenset(
    {
        "backtest",
        "sweep",
        "compare-exits",
        "hurst",
        "calibrate",
        "shadow-report",
    }
)

# Placing no orders is documented behaviour for verify-venue, but it does hit
# the network, so it is opt-in rather than on by default.
NETWORK_COMMANDS = frozenset({"verify-venue"})

# Strategy parameter overrides: `--set key=value`. Conservative on purpose --
# keys are identifiers, values are numbers, booleans or short bare words.
_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

DEFAULT_TIMEOUT = 900.0


class ToolError(RuntimeError):
    """Raised when a request is refused or a command fails."""


def _python() -> str:
    """The interpreter that runs btcbot -- its venv, not the crew's.

    The crew has its own virtualenv full of crewai and litellm; btcbot has one
    with httpx and PyYAML. Running btcbot with the crew's interpreter would
    work by accident today and break the moment the trees diverge.
    """
    candidate = ROOT / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    candidate = ROOT / ".venv" / "Scripts" / "python.exe"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _validate_overrides(overrides: Optional[dict[str, Any]]) -> list[str]:
    args: list[str] = []
    for key, value in (overrides or {}).items():
        key = str(key)
        value = str(value)
        if not _KEY_RE.match(key):
            raise ToolError(f"refused: bad parameter name {key!r}")
        if not _VALUE_RE.match(value):
            raise ToolError(f"refused: bad value for {key}: {value!r}")
        args += ["--set", f"{key}={value}"]
    return args


def run_btcbot(
    command: str,
    *,
    data_dir: Optional[str] = None,
    strategy: Optional[str] = None,
    overrides: Optional[dict[str, Any]] = None,
    allow_network: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Run one allowlisted btcbot analysis command and return its output.

    Every argument is validated and the argv is built here; nothing the agent
    says is passed to a shell.
    """
    command = str(command).strip()
    if command in NETWORK_COMMANDS:
        if not allow_network:
            raise ToolError(
                f"refused: '{command}' reaches the network and allow_network is off"
            )
    elif command not in ALLOWED_COMMANDS:
        raise ToolError(
            f"refused: '{command}' is not an allowed command. "
            f"available: {sorted(ALLOWED_COMMANDS)}"
        )

    argv = [_python(), "-m", "btcbot", command]

    if data_dir is not None:
        # Must stay inside the repo. Blocks ../.. traversal and absolute paths
        # pointing somewhere else on the box.
        resolved = (ROOT / str(data_dir)).resolve()
        if not str(resolved).startswith(str(ROOT)):
            raise ToolError(f"refused: data_dir outside the repo: {data_dir!r}")
        argv += ["--data-dir", str(resolved)]

    if strategy is not None:
        strategy = str(strategy)
        if not _KEY_RE.match(strategy):
            raise ToolError(f"refused: bad strategy name {strategy!r}")
        argv += ["--strategy", strategy]

    argv += _validate_overrides(overrides)

    try:
        proc = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"'{command}' timed out after {timeout:.0f}s") from None

    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return f"[exit {proc.returncode}]\n{out.strip()}"
    return out.strip() or "(no output)"


def count_windows(data_dir: str = "data") -> str:
    """Windows recorded, which is what gates analysis -- not snapshots.

    They differ by roughly 300x. Reporting snapshots is how someone talks
    themselves into tuning parameters on 19 windows.
    """
    resolved = (ROOT / str(data_dir)).resolve()
    if not str(resolved).startswith(str(ROOT)):
        raise ToolError(f"refused: data_dir outside the repo: {data_dir!r}")

    code = (
        "import json;"
        "from btcbot.recorder import load_dataset;"
        "from btcbot.backtest import group_windows;"
        f"snaps=load_dataset({str(resolved)!r});"
        "w=group_windows(snaps);"
        "print(json.dumps({'snapshots':len(snaps),'windows':len(w)}))"
    )
    proc = subprocess.run(
        [_python(), "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise ToolError(f"could not count windows: {(proc.stderr or '').strip()}")

    try:
        counts = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        raise ToolError(f"unexpected output: {proc.stdout!r}") from None

    windows = counts["windows"]
    verdict = (
        "ENOUGH to begin analysis"
        if windows >= 100
        else f"NOT ENOUGH -- need >=100, short by {100 - windows}"
    )
    return (
        f"snapshots={counts['snapshots']} windows={windows} ({verdict}). "
        "Windows are the sample size; snapshots are polls within a window and "
        "overstate it by roughly 300x."
    )


def recorder_health(log_path: str = "recorder-supervisor.log", lines: int = 40) -> str:
    """Recent restarts. Gaps in the dataset show up here, not in the data."""
    path = (ROOT / str(log_path)).resolve()
    if not str(path).startswith(str(ROOT)):
        raise ToolError(f"refused: path outside the repo: {log_path!r}")
    if not path.exists():
        return (
            f"No supervisor log at {path.name}. Either the recorder has never "
            "run, or it is being run without scripts/record_forever.py."
        )
    lines = max(1, min(int(lines), 200))
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    restarts = sum(1 for line in tail if "restarting in" in line)
    return f"Last {len(tail)} supervisor lines ({restarts} restarts):\n" + "\n".join(tail)


def disk_free() -> str:
    """The recorder never prunes, and a full disk stops collection silently."""
    usage = shutil.disk_usage(str(ROOT))
    data = ROOT / "data"
    size = 0
    if data.exists():
        size = sum(f.stat().st_size for f in data.glob("*.jsonl"))
    free_gb = usage.free / 1e9
    note = " -- LOW, recording will stop silently" if free_gb < 2.0 else ""
    return (
        f"disk free {free_gb:.1f} GB of {usage.total / 1e9:.1f} GB{note}; "
        f"data/ is {size / 1e6:.0f} MB. Recording adds roughly 30-60 MB/day."
    )


def read_repo_doc(name: str) -> str:
    """Read one of the repo's own documents, so the crew argues from the source."""
    allowed = {
        "README.md",
        "QUICKSTART.md",
        "goal.md",
        "handoff.md",
        "config.yaml",
        "docs/systematic-trading.md",
    }
    name = str(name).strip()
    if name not in allowed:
        raise ToolError(f"refused: {name!r} is not readable. available: {sorted(allowed)}")
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")
