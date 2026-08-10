#!/usr/bin/env python3
"""A CrewAI research crew over a local Ollama, reporting on btcbot's data.

    python research_crew.py --model ollama/qwen2.5:7b

Writes a dated digest to `reports/`. It reads; it never trades. Every number in
the digest comes from btcbot itself via the allowlisted tools in
`btcbot_tools.py` -- the agents run the commands and interpret the output, they
do not compute statistics themselves and must not be asked to.

THE ROLE THESE AGENTS PLAY. They are not a signal. A language model cannot tell
you whether a trading rule has edge; the sweep's t-statistic and deflated
Sharpe ratio can, and those are computed by `btcbot/multiple_testing.py`. What
the crew is genuinely good at is the part that is tedious and easy to skip:
running the same checks every day, quoting the numbers accurately, noticing
that the window count crossed 100, and writing down why a 76% win rate is not
a finding. Treat the output as a lab notebook, not an oracle.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import btcbot_tools as bt  # noqa: E402

try:
    from crewai import Agent, Crew, Process, Task
    from crewai.tools import tool
    from crewai import LLM
except ImportError:  # pragma: no cover - the crew venv may not be installed
    print(
        "crewai is not installed in this interpreter.\n"
        "This must run from the CREW virtualenv, not btcbot's:\n"
        "    crew/.venv/bin/python crew/research_crew.py\n"
        "See crew/README.md.",
        file=sys.stderr,
    )
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent


# --- tools. Thin wrappers; the boundary lives in btcbot_tools.py ---------

@tool("count_recorded_windows")
def count_recorded_windows() -> str:
    """Recorded WINDOWS and snapshots. Windows are the sample size that gates
    every other analysis; 100 is the minimum before tuning anything."""
    return bt.count_windows("data")


@tool("run_sweep")
def run_sweep() -> str:
    """Run the volume-rule sweep across every threshold and direction. Returns
    the table plus the multiple-testing correction: the Sidak-corrected
    critical t, the family-wise p-value, and the deflated Sharpe ratio."""
    return bt.run_btcbot("sweep")


@tool("run_backtest")
def run_backtest(strategy: str) -> str:
    """Replay recorded data through one strategy. Valid names: edge_threshold,
    volume_threshold, always_trade."""
    return bt.run_btcbot("backtest", strategy=strategy)


@tool("run_hurst")
def run_hurst() -> str:
    """Measure whether the intra-window BTC path is a random walk, against a
    synthetic control of the same shape. Compare to the reported null, never
    to 0.50."""
    return bt.run_btcbot("hurst")


@tool("run_compare_exits")
def run_compare_exits() -> str:
    """What the stop loss actually costs: same strategy with and without."""
    return bt.run_btcbot("compare-exits")


@tool("check_recorder_health")
def check_recorder_health() -> str:
    """Recent recorder restarts. Gaps in the dataset appear here."""
    return bt.recorder_health()


@tool("check_disk")
def check_disk() -> str:
    """Free disk. The recorder never prunes and a full disk stops it silently."""
    return bt.disk_free()


@tool("read_repo_doc")
def read_repo_doc(name: str) -> str:
    """Read one of the repo's own docs to check a claim against the source.
    One of: README.md, QUICKSTART.md, goal.md, handoff.md, config.yaml,
    docs/systematic-trading.md."""
    return bt.read_repo_doc(name)


OPS_TOOLS = [count_recorded_windows, check_recorder_health, check_disk]
ANALYST_TOOLS = [run_sweep, run_backtest, run_hurst, run_compare_exits,
                 count_recorded_windows]
SKEPTIC_TOOLS = [read_repo_doc, count_recorded_windows]


def build_crew(llm: "LLM") -> Crew:
    ops = Agent(
        role="Data Collection Steward",
        goal=(
            "Report the state of data collection: how many WINDOWS are "
            "recorded, whether the recorder is healthy, and whether disk will "
            "run out."
        ),
        backstory=(
            "You look after a recorder that must run for days unattended. You "
            "know that windows, not snapshots, are the sample size, and that "
            "the two differ by roughly 300x -- quoting snapshots as though "
            "they were the sample is the mistake you exist to prevent. You "
            "report numbers exactly as the tools give them and never estimate."
        ),
        tools=OPS_TOOLS,
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    analyst = Agent(
        role="Quantitative Analyst",
        goal=(
            "Run the analysis commands and report what they returned, "
            "accurately and without embellishment."
        ),
        backstory=(
            "You run a harness that was built to disprove trading rules rather "
            "than confirm them. You never compute a statistic yourself -- the "
            "tools do that, and you quote them. You know the t-statistic and "
            "the deflated Sharpe ratio are the numbers that matter, that ROI "
            "and win rate are not, and that a strategy declining to trade is "
            "usually correct behaviour rather than a bug."
        ),
        tools=ANALYST_TOOLS,
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    skeptic = Agent(
        role="Research Skeptic",
        goal=(
            "Attack the day's findings. Identify every reason a result might "
            "be noise, and state plainly whether anything was actually shown."
        ),
        backstory=(
            "You have watched people lose money to backtests. Your standing "
            "objections: a win rate above the break-even price proves nothing "
            "because buying favourites always wins often; the best cell of a "
            "20-cell sweep must clear the Sidak-corrected bar of about 3.02, "
            "not 2.0; fewer than 100 windows means no tuning at all; and an "
            "in-sample result is not a result. You would rather report 'no "
            "edge found' than manufacture a finding, and you know that is a "
            "successful outcome for this project, not a failure."
        ),
        tools=SKEPTIC_TOOLS,
        llm=llm,
        allow_delegation=False,
        verbose=True,
    )

    collect = Task(
        description=(
            "Report data collection state. Use count_recorded_windows, "
            "check_recorder_health and check_disk. State the window count "
            "plainly and say whether it clears 100."
        ),
        expected_output=(
            "A short status block: window count, whether it clears 100, "
            "recorder restarts seen, and disk headroom."
        ),
        agent=ops,
    )

    analyse = Task(
        description=(
            "If and only if there are at least 100 recorded windows, run "
            "run_sweep, run_hurst and run_backtest for edge_threshold and "
            "volume_threshold, and report what they returned. If there are "
            "fewer than 100 windows, run nothing and say that analysis is "
            "blocked on data collection -- do not run the commands anyway and "
            "do not interpret results from too small a sample."
        ),
        expected_output=(
            "For each command run: the headline numbers, quoted exactly. Or a "
            "single sentence explaining that analysis is blocked on sample size."
        ),
        agent=analyst,
        context=[collect],
    )

    critique = Task(
        description=(
            "Review the analysis. For every apparent finding, state whether it "
            "clears the corrected significance bar and whether the sample "
            "supports it. Quote the t-statistic and the deflated Sharpe ratio "
            "where they exist. End with an explicit verdict: either 'No edge "
            "demonstrated' or a precise statement of what was shown and what "
            "would confirm it out of sample. Never recommend placing a trade."
        ),
        expected_output=(
            "A markdown digest: Data Collection, Results, Critique, Verdict, "
            "and Next Action. The verdict must be unambiguous."
        ),
        agent=skeptic,
        context=[collect, analyse],
    )

    return Crew(
        agents=[ops, analyst, skeptic],
        tasks=[collect, analyse, critique],
        process=Process.sequential,
        verbose=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get("BTCBOT_CREW_MODEL", "ollama/qwen2.5:7b"),
        help="LiteLLM model id. Must carry the ollama/ prefix.",
    )
    parser.add_argument(
        "--base-url", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    )
    parser.add_argument("--output-dir", default=str(ROOT / "reports"))
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Low by default: this job is reporting, not ideation.",
    )
    args = parser.parse_args()

    if not args.model.startswith("ollama/"):
        print(
            f"error: --model must start with 'ollama/' (got {args.model!r}).\n"
            "LiteLLM routes on that prefix; without it it will try to reach a "
            "hosted provider.",
            file=sys.stderr,
        )
        return 2

    # CrewAI validates OPENAI_API_KEY at startup even when every agent is
    # local. Without this you get a validation error that has nothing to do
    # with your actual configuration.
    os.environ.setdefault("OPENAI_API_KEY", "NA")

    llm = LLM(model=args.model, base_url=args.base_url, temperature=args.temperature)

    result = build_crew(llm).kickoff()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = out_dir / f"digest-{stamp}.md"
    header = (
        f"# btcintervaltrader research digest — {stamp}\n\n"
        f"Model: `{args.model}` (local). Generated by `crew/research_crew.py`.\n\n"
        "> Written by a language model from tool output. The numbers come from\n"
        "> btcbot; the prose does not. Nothing here is a trading "
        "recommendation.\n\n---\n\n"
    )
    path.write_text(header + str(result), encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
