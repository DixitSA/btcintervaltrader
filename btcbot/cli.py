"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .backtest import run_backtest
from .config import load_config
from .recorder import load_dataset
from .strategies import REGISTRY, build_strategy


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_record(args: argparse.Namespace) -> int:
    from .runner import Runner

    cfg = load_config(args.config)
    if args.data_dir:
        cfg.data_dir = args.data_dir
    runner = Runner(cfg, strategy=None, record=True, trade=False)
    runner.run(max_ticks=args.max_ticks)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg.data_dir

    snapshots = load_dataset(data_dir)
    if not snapshots:
        print(
            f"No recorded snapshots in '{data_dir}'.\n"
            "Run `python -m btcbot record` first -- you cannot evaluate a rule "
            "without data.",
            file=sys.stderr,
        )
        return 1

    name = args.strategy or cfg.strategy.name
    params = dict(cfg.strategy.params) if name == cfg.strategy.name else {}
    for override in args.set or []:
        key, _, value = override.partition("=")
        if not _:
            print(f"bad --set '{override}', expected key=value", file=sys.stderr)
            return 2
        params[key] = _coerce(value)

    strategy = build_strategy(name, params)
    report = run_backtest(snapshots, strategy, cfg)
    print(f"\nstrategy: {strategy.describe()}")
    print(report.render())
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Run the volume rule across all four directions at once.

    This is the command that actually settles the question.
    """
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg.data_dir
    snapshots = load_dataset(data_dir)
    if not snapshots:
        print(f"No recorded snapshots in '{data_dir}'. Run `record` first.", file=sys.stderr)
        return 1

    # A sweep measures the RULE, so the throughput caps and kill switch are
    # relaxed here. They would otherwise halt a run partway and bias the
    # statistics. Live trading still enforces every one of them.
    cfg.risk.max_trades_per_hour = 10**9
    cfg.risk.daily_loss_limit_usd = 1e12
    cfg.risk.bankroll_usd = 100_000.0

    print(f"\n{'direction':<10} {'thresh':>10} {'trades':>7} {'win%':>7} {'BE%':>7} {'ROI':>9} {'z':>7}")
    print("-" * 62)
    for threshold in args.thresholds:
        for direction in ("follow", "fade", "up", "down"):
            strategy = build_strategy(
                "volume_threshold",
                {
                    "min_volume_usd": threshold,
                    "direction": direction,
                    "assumed_edge": args.assumed_edge,
                },
            )
            rep = run_backtest(snapshots, strategy, cfg)
            wr = f"{rep.win_rate:.1%}" if rep.win_rate is not None else "-"
            be = f"{rep.breakeven_win_rate:.1%}" if rep.breakeven_win_rate is not None else "-"
            roi = f"{rep.roi:+.2%}" if rep.roi is not None else "-"
            z = f"{rep.z_score:+.2f}" if rep.z_score is not None else "-"
            print(
                f"{direction:<10} {threshold:>10,.0f} {rep.n:>7} {wr:>7} {be:>7} {roi:>9} {z:>7}"
            )
    print(
        "\nRead the z column, not the ROI column. |z| under 2 means the result "
        "is consistent with having no edge at all."
    )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import generate

    cfg = load_config(args.config)
    out = args.data_dir or "data-sim"
    n = generate(out, n_windows=args.windows, seed=args.seed)
    print(f"wrote {n} synthetic snapshots across {args.windows} windows to '{out}'")
    print(
        "\nThis is a NO-EDGE control world. Run:\n"
        f"  python -m btcbot sweep --data-dir {out}\n"
        "and confirm the z-scores hover near zero. If they do not, the harness "
        "is lying to you and needs fixing before any real data goes through it."
    )
    _ = cfg
    return 0


def cmd_verify_bullpen(args: argparse.Namespace) -> int:
    """Check the configured Bullpen invocation without placing an order."""
    import shutil
    import subprocess

    cfg = load_config(args.config)
    bp = cfg.execution.bullpen

    path = shutil.which(bp.binary)
    if not path:
        print(f"FAIL: '{bp.binary}' not found on PATH.", file=sys.stderr)
        print("Install the Bullpen CLI, or set execution.bullpen.binary.", file=sys.stderr)
        return 1
    print(f"ok   binary: {path}")

    try:
        proc = subprocess.run(
            bp.help_template, capture_output=True, text=True, timeout=bp.timeout_seconds
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: could not run {bp.help_template}: {exc}", file=sys.stderr)
        return 1

    if proc.returncode != 0:
        print(
            f"FAIL: `{' '.join(bp.help_template)}` exited {proc.returncode}.\n"
            f"{(proc.stderr or proc.stdout).strip()[:800]}",
            file=sys.stderr,
        )
        print(
            "\nThe subcommand path in execution.bullpen.help_template is probably "
            "wrong. Fix it and buy_template to match your CLI.",
            file=sys.stderr,
        )
        return 1

    print(f"ok   `{' '.join(bp.help_template)}` succeeded")
    help_text = (proc.stdout or "") + (proc.stderr or "")

    # Render a sample order so the exact argv is visible before it is ever run.
    sample = _render_sample(cfg)
    print("\nThe bot would invoke:\n  " + " ".join(sample))

    flags = {a for a in sample if a.startswith("--")}
    missing = [f for f in sorted(flags) if f not in help_text]
    if missing:
        print(
            "\nWARNING: these flags from buy_template do not appear in the help "
            f"output: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("Update execution.bullpen.buy_template to match.", file=sys.stderr)
        return 1

    print("ok   every flag in buy_template appears in the help output")
    print(
        "\nNext: set execution.bullpen.dry_run=false, then place ONE "
        "minimum-size order and confirm it in the Polymarket UI before "
        "running unattended."
    )
    return 0


def _render_sample(cfg) -> list[str]:
    """Render buy_template with representative values, for display only."""
    values = {
        "token_id": "<TOKEN_ID>",
        "side": "up",
        "outcome": "Up",
        "shares": "10.00",
        "price": "0.520",
        "slug": "btc-updown-15m-example",
        "condition_id": "<CONDITION_ID>",
    }
    out = []
    for part in cfg.execution.bullpen.buy_template:
        try:
            out.append(part.format(**values))
        except KeyError as exc:
            out.append(f"<BAD PLACEHOLDER {exc}>")
    return out


def cmd_paper(args: argparse.Namespace) -> int:
    from .runner import Runner

    cfg = load_config(args.config)
    cfg.mode = "paper"
    strategy = build_strategy(args.strategy or cfg.strategy.name, cfg.strategy.params)
    runner = Runner(cfg, strategy=strategy, record=True, trade=True)
    runner.run(max_ticks=args.max_ticks)
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from .runner import Runner

    cfg = load_config(args.config)
    cfg.mode = "live"
    strategy = build_strategy(args.strategy or cfg.strategy.name, cfg.strategy.params)
    runner = Runner(cfg, strategy=strategy, record=True, trade=True)
    runner.run(max_ticks=args.max_ticks)
    return 0


def _coerce(value: str):
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="btcbot", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-c", "--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_rec = sub.add_parser("record", help="collect market snapshots (do this first)")
    p_rec.add_argument("--data-dir", default=None)
    p_rec.add_argument("--max-ticks", type=int, default=None)
    p_rec.set_defaults(func=cmd_record)

    p_bt = sub.add_parser("backtest", help="replay recorded data through a strategy")
    p_bt.add_argument("--data-dir", default=None)
    p_bt.add_argument("--strategy", choices=sorted(REGISTRY), default=None)
    p_bt.add_argument("--set", action="append", metavar="KEY=VALUE")
    p_bt.set_defaults(func=cmd_backtest)

    p_sw = sub.add_parser("sweep", help="test the volume rule in every direction")
    p_sw.add_argument("--data-dir", default=None)
    p_sw.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=[0.0, 10_000.0, 50_000.0, 100_000.0, 500_000.0],
    )
    p_sw.add_argument("--assumed-edge", type=float, default=0.03)
    p_sw.set_defaults(func=cmd_sweep)

    p_vb = sub.add_parser(
        "verify-bullpen", help="check the configured Bullpen CLI invocation (places no order)"
    )
    p_vb.set_defaults(func=cmd_verify_bullpen)

    p_sim = sub.add_parser("simulate", help="generate a synthetic no-edge control dataset")
    p_sim.add_argument("--data-dir", default=None)
    p_sim.add_argument("--windows", type=int, default=400)
    p_sim.add_argument("--seed", type=int, default=42)
    p_sim.set_defaults(func=cmd_simulate)

    p_pa = sub.add_parser("paper", help="trade with simulated money against live books")
    p_pa.add_argument("--strategy", choices=sorted(REGISTRY), default=None)
    p_pa.add_argument("--max-ticks", type=int, default=None)
    p_pa.set_defaults(func=cmd_paper)

    p_li = sub.add_parser("live", help="trade with REAL money (requires opt-in env var)")
    p_li.add_argument("--strategy", choices=sorted(REGISTRY), default=None)
    p_li.add_argument("--max-ticks", type=int, default=None)
    p_li.set_defaults(func=cmd_live)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except RuntimeError as exc:
        # Config/safety-gate failures should read as a message, not a traceback.
        print(f"\nerror: {exc}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
