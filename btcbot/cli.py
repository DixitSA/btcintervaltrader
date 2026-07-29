"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
import time
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
    use_exits = None
    if args.no_exits:
        use_exits = False
    elif args.exits:
        use_exits = True

    report = run_backtest(snapshots, strategy, cfg, use_exits=use_exits)
    print(f"\nstrategy: {strategy.describe()}")
    print(report.render())
    if report.portfolio is not None and report.n:
        print(report.portfolio.render())
    return 0


def cmd_compare_exits(args: argparse.Namespace) -> int:
    """Run the same strategy with and without stops, side by side.

    A stop loss in a binary market is not free protection -- you cross the
    spread twice. This is the command that tells you what yours actually costs.
    """
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg.data_dir
    snapshots = load_dataset(data_dir)
    if not snapshots:
        print(f"No recorded snapshots in '{data_dir}'. Run `record` first.", file=sys.stderr)
        return 1

    name = args.strategy or cfg.strategy.name
    # Only carry the configured params when they belong to the strategy being
    # run -- otherwise volume_threshold's settings leak into edge_threshold.
    params = cfg.strategy.params if name == cfg.strategy.name else {}
    strategy = build_strategy(name, params)

    rows = []
    for label, use_exits in (("no stops", False), ("with stops", True)):
        rep = run_backtest(snapshots, strategy, cfg, use_exits=use_exits)
        rows.append((label, rep))

    print(f"\nstrategy: {strategy.describe()}")
    print(f"stop_loss_drop={cfg.exits.stop_loss_drop} "
          f"take_profit_rise={cfg.exits.take_profit_rise} "
          f"trailing_stop_drop={cfg.exits.trailing_stop_drop}")
    print(f"\n{'':<12}{'trades':>8}{'profit%':>9}{'ROI':>10}{'P&L':>12}{'maxDD':>10}{'t':>8}")
    print("-" * 69)
    for label, rep in rows:
        f = lambda v, fmt: (format(v, fmt) if v is not None else "-")
        dd = rep.portfolio.max_drawdown if rep.portfolio else rep.max_drawdown
        print(
            f"{label:<12}{rep.n:>8}{f(rep.win_rate, '.1%'):>9}{f(rep.roi, '+.2%'):>10}"
            f"{rep.total_pnl:>+12,.2f}{dd:>10,.2f}{f(rep.roi_t_stat, '+.2f'):>8}"
        )

    without, with_stops = rows[0][1], rows[1][1]
    if not with_stops.n and not without.n:
        print("\nNo trades in either run, so there is nothing to compare.")
        return 0

    delta = with_stops.total_pnl - without.total_pnl
    if abs(delta) < 0.005:
        verdict = "No measurable difference on this sample."
    elif delta < 0:
        verdict = "They cost money here."
    else:
        verdict = "They helped here."
    print(
        f"\nStops changed P&L by ${delta:+,.2f} on this sample.\n{verdict} "
        "Judge with the t column, not P&L alone -- one sample is not proof."
    )
    if with_stops.exits:
        print("\nExit breakdown (with stops):")
        pnl_by = {}
        for t in with_stops.trades:
            pnl_by[t.exit_reason] = pnl_by.get(t.exit_reason, 0.0) + t.pnl
        for reason in sorted(with_stops.exits, key=lambda r: -with_stops.exits[r]):
            print(f"  {reason:<16}{with_stops.exits[reason]:>6}  ${pnl_by.get(reason, 0.0):+,.2f}")
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

    # Stops are disabled for the same reason the risk caps are: a sweep measures
    # the RULE. It also keeps the payoff BINARY, which is what makes the
    # win-rate z-score a valid statistic at all -- with stops on, `won` means
    # "P&L > 0" while break-even is an entry PRICE, and comparing the two
    # produces large negative z on data with no edge whatsoever. What stops
    # actually cost you is a separate question, answered by `compare-exits`.
    directions = ("follow", "fade", "up", "down")
    n_tests = len(args.thresholds) * len(directions)

    print(f"\n{'direction':<10} {'thresh':>10} {'trades':>7} {'win%':>7} {'BE%':>7} {'ROI':>9} {'t':>7} {'z':>7}")
    print("-" * 70)
    best = None
    for threshold in args.thresholds:
        for direction in directions:
            strategy = build_strategy(
                "volume_threshold",
                {
                    "min_volume_usd": threshold,
                    "direction": direction,
                    "assumed_edge": args.assumed_edge,
                },
            )
            rep = run_backtest(snapshots, strategy, cfg, use_exits=False)
            wr = f"{rep.win_rate:.1%}" if rep.win_rate is not None else "-"
            be = f"{rep.breakeven_win_rate:.1%}" if rep.breakeven_win_rate is not None else "-"
            roi = f"{rep.roi:+.2%}" if rep.roi is not None else "-"
            t = f"{rep.roi_t_stat:+.2f}" if rep.roi_t_stat is not None else "-"
            z = f"{rep.z_score:+.2f}" if rep.z_score is not None else "-"
            if rep.roi_t_stat is not None and (best is None or rep.roi_t_stat > best[0]):
                best = (rep.roi_t_stat, f"{direction}@{threshold:,.0f}", rep.n)
            print(
                f"{direction:<10} {threshold:>10,.0f} {rep.n:>7} {wr:>7} {be:>7} {roi:>9} {t:>7} {z:>7}"
            )

    print(
        f"\nRead the t column, not the ROI column and not the win rate.\n"
        f"This grid ran {n_tests} tests ({len(args.thresholds)} thresholds x "
        f"{len(directions)} directions). Picking the best cell out of {n_tests} and\n"
        f"judging it at |t| > 2 is not a 5% test -- that threshold is for ONE\n"
        f"pre-chosen hypothesis. Across {n_tests} tries something clears it routinely\n"
        f"on pure noise, and that cell is the one you will most want to trade.\n"
        f"\n"
        f"'thresh' is in CONTRACTS on Kalshi, not dollars -- the venue reports\n"
        f"volume in contracts. It is also cumulative within a window, so it gates\n"
        f"HOW LATE you enter, not which windows you take. Every window crosses\n"
        f"every threshold eventually."
    )
    if best is not None:
        t_best, label, n_best = best
        print(f"\nbest cell by t: {label} (t={t_best:+.2f}, n={n_best})")
        if t_best <= 2.0:
            print("  -> Nothing here clears even the single-test bar. No edge found.")
        else:
            print(
                "  -> Clears the single-test bar, but it was SELECTED from "
                f"{n_tests}.\n     Confirm it on data you did not sweep over before "
                "believing it."
            )
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    from .simulate import generate

    cfg = load_config(args.config)
    out = args.data_dir or "data-sim"
    families = args.families or cfg.markets.slug_prefixes
    n = generate(out, n_windows=args.windows, seed=args.seed, families=families)
    print(
        f"wrote {n} synthetic snapshots across {args.windows} windows x "
        f"{len(families)} families to '{out}'"
    )
    print(
        "\nThis is a NO-EDGE control world. Run:\n"
        f"  python -m btcbot sweep --data-dir {out}\n"
        "and confirm the z-scores hover near zero. If they do not, the harness "
        "is lying to you and needs fixing before any real data goes through it."
    )
    _ = cfg
    return 0


def cmd_verify_venue(args: argparse.Namespace) -> int:
    """Check venue connectivity, credentials and market discovery.

    Places no orders. Run this before anything else after switching venues.
    """
    from .fees import build_fee_model
    from .venues import build_venue

    cfg = load_config(args.config)
    print(f"venue                 : {cfg.venue}")
    print(f"series / prefixes     : {', '.join(cfg.markets.slug_prefixes)}")

    fee_model = build_fee_model(cfg.venue, cfg.fees)
    print(f"fee model             : {fee_model.name}")
    print(
        f"  100 contracts @ 0.50: ${fee_model.entry_fee(100, 0.50):.2f} entry "
        f"({fee_model.entry_fee(100, 0.50) / 50.0:.2%} of stake)"
    )

    try:
        venue = build_venue(cfg)
    except RuntimeError as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        return 1

    if cfg.venue == "kalshi":
        authed = getattr(venue, "auth", None) is not None
        print(
            f"credentials           : {'loaded' if authed else 'NOT SET'}"
            f"{'' if authed else '  (fine for record/paper; required to trade)'}"
        )

    try:
        markets = venue.discover_markets(cfg.markets.slug_prefixes)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: market discovery failed: {exc}", file=sys.stderr)
        venue.close()
        return 1

    if not markets:
        print(
            "\nNo open markets found. Check the series tickers in "
            "markets.slug_prefixes against the venue.",
            file=sys.stderr,
        )
        venue.close()
        return 1

    print(f"\nopen markets found    : {len(markets)}")
    for market in markets[:5]:
        strike = f"${market.strike:,.2f}" if market.strike else "UNPARSED"
        print(f"  {market.slug:<32} strike {strike}")

    probe = markets[0]
    books = None
    try:
        books = venue.get_books(probe)
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: orderbook fetch failed: {exc}", file=sys.stderr)

    if books:
        up, down = books
        print(f"\norderbook for {probe.slug}:")
        print(f"  UP   bid {up.best_bid}  ask {up.best_ask}")
        print(f"  DOWN bid {down.best_bid}  ask {down.best_ask}")
        if up.best_bid is not None and down.best_ask is not None:
            total = up.best_bid + down.best_ask
            ok = abs(total - 1.0) < 1e-6
            print(
                f"  sanity: up_bid + down_ask = {total:.3f} "
                f"{'OK' if ok else 'WRONG -- ask derivation is broken'}"
            )
            if not ok:
                venue.close()
                return 1

    if args.dump:
        _dump_raw(cfg, venue, markets[:3], args.dump)

    if any(m.strike is None for m in markets[:5]):
        print(
            "\nWARNING: some strikes could not be parsed. Model-based strategies "
            "skip those markets rather than guess.",
            file=sys.stderr,
        )

    venue.close()
    print("\nok. Next: `python -m btcbot record` to start collecting data.")
    return 0


def _dump_raw(cfg, venue, markets, path: str) -> None:
    """Save raw API responses so the parsers can be checked against real data.

    This exists because the machine this code was written on cannot reach the
    venue. Run it where you can, commit the file, and the parsers get validated
    against genuine payloads instead of assumptions.

    Contains only public market data -- no account info, no credentials. Read it
    before sharing anyway.
    """
    import json as _json

    payload = {
        "venue": cfg.venue,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "series": cfg.markets.slug_prefixes,
        "markets_raw": [],
        "orderbooks_raw": {},
    }

    try:
        if cfg.venue == "kalshi":
            for series in cfg.markets.slug_prefixes:
                raw = venue._request(
                    "GET",
                    "/markets",
                    params={"series_ticker": series, "status": "open", "limit": 5},
                )
                payload["markets_raw"].append({"series": series, "response": raw})
            for market in markets:
                payload["orderbooks_raw"][market.slug] = venue._request(
                    "GET", f"/markets/{market.slug}/orderbook"
                )
        else:
            payload["note"] = "raw dump is implemented for kalshi only"
    except Exception as exc:  # noqa: BLE001
        payload["error"] = str(exc)

    Path(path).write_text(_json.dumps(payload, indent=2))
    print(f"\nwrote raw API responses to {path}")
    print("This is public market data only. Commit it and the parsers can be")
    print("validated against real payloads (see tests/test_fixtures.py).")


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


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Inspect the calibration curve from past trade outcomes."""
    from .learner import Calibrator, OutcomeStore

    cfg = load_config(args.config)
    data_dir = Path(args.data_dir or cfg.data_dir)
    outcome_path = data_dir / (cfg.learning.outcome_file or "outcomes.jsonl")

    if not outcome_path.exists():
        print(f"No outcome file found at {outcome_path}.", file=sys.stderr)
        print("Run paper trading with learning enabled to accumulate outcomes.", file=sys.stderr)
        return 1

    calibrator = Calibrator(
        alpha_prior=cfg.learning.alpha_prior,
        beta_prior=cfg.learning.beta_prior,
    )
    store = OutcomeStore(outcome_path)
    n = store.feed(calibrator)
    if n == 0:
        print(f"'{outcome_path}' exists but is empty. No trades to calibrate from.")
        return 0

    print(f"\nCalibration from {n} settled trades")
    print(f"Beta({calibrator.alpha_prior}, {calibrator.beta_prior}) prior")
    print("-" * 60)
    print(f"{'bucket':>7}  {'n':>5}  {'wins':>5}  {'raw':>5}  {'calibrated':>10}  {'delta':>8}")
    print("-" * 60)
    for row in calibrator.table:
        delta = row["calibrated"] - row["raw_mid"]
        print(
            f"{row['bucket']:>7.2f}  {row['n']:>5}  {row['wins']:>5}  "
            f"{row['raw_mid']:>5.2f}  {row['calibrated']:>10.4f}  {delta:>+8.4f}"
        )
    print("-" * 60)
    print("Delta = calibrated - raw. Positive means the strategy beats its own")
    print("probability estimate in this bucket; negative means it overestimates.")
    print("With < ~100 trades per bucket, the posterior is dominated by the prior")
    print("and deltas will be small.")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    from .runner import Runner

    cfg = load_config(args.config)
    cfg.mode = "paper"
    strategy = build_strategy(args.strategy or cfg.strategy.name, cfg.strategy.params)
    runner = Runner(cfg, strategy=strategy, record=True, trade=True)
    runner.run(max_ticks=args.max_ticks)
    if args.report and runner.portfolio is not None:
        print(runner.portfolio.log_trades())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Local control panel. Paper only -- it cannot place a real order."""
    from .server import serve

    cfg = load_config(args.config)
    if cfg.is_live:
        print(
            "config has mode=live. `serve` is a paper-only panel and will not "
            "run against live settings.\nSet mode: paper in config.yaml.",
            file=sys.stderr,
        )
        return 1
    return serve(cfg, host=args.host, port=args.port)


def cmd_live(args: argparse.Namespace) -> int:
    from .runner import Runner

    cfg = load_config(args.config)
    cfg.mode = "live"
    strategy = build_strategy(args.strategy or cfg.strategy.name, cfg.strategy.params)
    runner = Runner(cfg, strategy=strategy, record=True, trade=True)
    runner.run(max_ticks=args.max_ticks)
    return 0


def cmd_shadow_replay(args: argparse.Namespace) -> int:
    """Regenerate shadow ledger from recorded snapshots."""
    cfg = load_config(args.config)
    data_dir = args.data_dir or cfg.data_dir
    snapshots = load_dataset(data_dir)
    if not snapshots:
        print(f"No recorded snapshots in '{data_dir}'. Run `record` first.", file=sys.stderr)
        return 1

    from .backtest import group_windows, infer_outcome
    from .fees import build_fee_model
    from .shadow import ShadowLedger

    windows = group_windows(snapshots)
    fee_model = build_fee_model(cfg.venue, cfg.fees)
    rung_defs = [
        (i, cfg.markets.min_seconds_remaining, float(max_r))
        for i, max_r in enumerate(cfg.shadow.rungs)
    ]
    output = Path(args.output or Path(data_dir) / cfg.shadow.ledger_file)

    ledger = ShadowLedger(
        ledger_path=output,
        producer="replay",
        fee_model=fee_model,
        rung_defs=rung_defs,
        enabled=True,
    )

    # Replay in time order, same as run_backtest.
    ordered = sorted(
        (s for w in windows.values() for s in w), key=lambda s: (s.ts, s.market.slug)
    )
    for snap in ordered:
        records = ledger.evaluate(snap)
        for rec in records:
            ledger.append(rec)

        slug = snap.market.slug
        outcome = infer_outcome(windows[slug])
        final_ts = windows[slug][-1].ts
        if snap.ts >= final_ts:
            settle_spot = snap.spot
            settled = ledger.settle(slug, outcome, settle_spot, final_ts)
            for s in settled:
                ledger.append_settled(s)

    # Anything still unsettled after replay
    for slug in list(ledger.unsettled_slugs()):
        if slug in windows:
            outcome = infer_outcome(windows[slug])
            final_ts = windows[slug][-1].ts
            settle_spot = windows[slug][-1].spot
            settled = ledger.settle(slug, outcome, settle_spot, final_ts)
            for s in settled:
                ledger.append_settled(s)

    records = ledger.load_records()
    unsettled = sum(1 for r in records if r.won is None)
    print(f"shadow replay: {len(records)} records ({unsettled} unsettled) -> {output}")
    print(f"  {len(windows)} windows, {len(snapshots)} snapshots")
    return 0


def cmd_shadow_report(args: argparse.Namespace) -> int:
    """Window-clustered performance by (rung x direction)."""
    import math
    from collections import defaultdict

    cfg = load_config(args.config)
    ledger_path = Path(args.ledger or args.data_dir or cfg.data_dir) / cfg.shadow.ledger_file
    if args.data_dir:
        ledger_path = Path(args.data_dir) / cfg.shadow.ledger_file
    if args.ledger:
        ledger_path = Path(args.ledger)

    if not ledger_path.exists():
        print(f"No shadow ledger found at {ledger_path}.", file=sys.stderr)
        print("Run `shadow-replay` or record with `shadow.enabled: true`.", file=sys.stderr)
        return 1

    from .shadow import DIRECTIONS, ShadowLedger, assert_no_history_in_ranking

    ledger = ShadowLedger(ledger_path=ledger_path, enabled=True)
    records = ledger.load_records()
    if not records:
        print("Shadow ledger is empty.", file=sys.stderr)
        return 0

    settled = [r for r in records if r.won is not None]
    if not settled:
        print("No settled records yet. Wait for windows to expire.", file=sys.stderr)
        return 0

    # Enforce the history firewall
    assert_no_history_in_ranking(settled)

    # Group by (rung, direction)
    groups: dict[tuple[int, str], list] = defaultdict(list)
    windows_per_rung: dict[tuple[int, str], set[str]] = defaultdict(set)
    for r in settled:
        key = (r.rung, r.direction)
        groups[key].append(r)
        windows_per_rung[key].add(r.slug)

    n_windows_total = len(set(r.slug for r in settled))
    print(f"\nshadow report — {len(settled)} settled records across {n_windows_total} windows")
    print(f"{'rung':>4} {'dir':<8} {'windows':>8} {'records':>8} {'win%':>7} {'mean P&L':>9} {'LCB95':>8} {'diff r0':>8}")
    print("-" * 68)

    # Baseline for paired diff: rung 0 net_pnl per window
    r0_by_window: dict[str, float] = {}
    for k, recs in groups.items():
        rung, direction = k
        if rung == 0:
            for r in recs:
                r0_by_window[r.slug] = r.net_pnl

    for rung in sorted({k[0] for k in groups}):
        for direction in sorted(DIRECTIONS):
            key = (rung, direction)
            recs = groups.get(key, [])
            if not recs:
                continue

            n_windows = len(windows_per_rung[key])
            n_records = len(recs)
            wins = sum(1 for r in recs if r.won is True)
            win_rate = wins / n_records if n_records else 0.0
            mean_pnl = sum(r.net_pnl for r in recs) / n_records if n_records else 0.0

            # Window-clustered standard error
            pnl_by_window: dict[str, float] = defaultdict(float)
            counts_by_window: dict[str, int] = defaultdict(int)
            for r in recs:
                pnl_by_window[r.slug] += r.net_pnl
                counts_by_window[r.slug] += 1
            window_means = [pnl_by_window[s] / counts_by_window[s] for s in pnl_by_window]
            if len(window_means) >= 2:
                wm_mean = sum(window_means) / len(window_means)
                wm_var = sum((m - wm_mean) ** 2 for m in window_means) / (len(window_means) - 1)
                stderr = math.sqrt(wm_var / len(window_means))
                lcb = wm_mean - 1.645 * stderr
            else:
                lcb = mean_pnl
                stderr = None

            # Paired diff vs rung 0 (same direction only)
            paired_diffs = []
            if rung > 0:
                for r in recs:
                    r0_pnl = r0_by_window.get(r.slug)
                    if r0_pnl is not None:
                        paired_diffs.append(r.net_pnl - r0_pnl)
            paired_str = ""
            if paired_diffs:
                pd_mean = sum(paired_diffs) / len(paired_diffs)
                paired_str = f"{pd_mean:+.4f}"

            print(
                f"{rung:>4} {direction:<8} {n_windows:>8} {n_records:>8} "
                f"{win_rate:>6.1%} {mean_pnl:>+8.4f} {lcb:>+8.4f} {paired_str:>8}"
            )

    print("\nSample sizes are in WINDOWS, not records. Do not pool within-window")
    print("observations as independent — they share one BTC path.")
    return 0


def cmd_verify_historical(args: argparse.Namespace) -> int:
    """Probe Kalshi API for settled market / candlestick availability.

    Three questions only:
    1. How far back does KXBTC15M history go?
    2. What is the finest granularity?
    3. Does it carry the settlement result?

    If the finest interval is hourly, the historical path is useless here.
    """
    cfg = load_config(args.config)
    from .venues import build_venue

    venue = build_venue(cfg)
    if venue.name != "kalshi":
        print("Historical probe is currently Kalshi-specific.", file=sys.stderr)
        venue.close()
        return 1

    print("=== Kalshi Historical Data Probe ===\n")

    # 1. Probe settled markets endpoint
    print("1. Settled markets...")
    try:
        data = venue._request(
            "GET",
            "/markets",
            params={
                "series_ticker": "KXBTC15M",
                "status": "settled",
                "limit": 5,
            },
        )
        markets = data.get("markets", []) or []
        print(f"   Found {len(markets)} settled KXBTC15M markets")
        if markets:
            m = markets[0]
            print(f"   Sample ticker: {m.get('ticker', '?')}")
            print(f"   Has 'result' field: {'result' in m}")
            result = m.get("result")
            print(f"   Result value: {result}")
            close_time = m.get("close_time", m.get("expiration_time", "?"))
            print(f"   Close time: {close_time}")
            # Check cursor for pagination
            cursor = data.get("cursor", {})
            print(f"   Cursor pagination: {'yes' if cursor else 'no'}")
            if cursor:
                print(f"   Next cursor: {cursor.get('next_cursor', 'none')}")
    except Exception as exc:
        print(f"   FAIL: {exc}")

    # 2. Probe candlesticks endpoint
    print("\n2. Candlesticks...")
    for interval in [1, 5, 15, 60]:
        try:
            candles = venue._request(
                "GET",
                "/markets/candlesticks",
                params={
                    "series_ticker": "KXBTC15M",
                    "period_interval": interval,
                    "limit": 3,
                },
            )
            bars = candles.get("candlesticks", []) if isinstance(candles, dict) else candles
            if bars and len(bars) > 0:
                bar = bars[0]
                print(f"   interval={interval}m: {len(bars)} bars available")
                print(f"     sample: open={bar.get('open')} high={bar.get('high')} "
                      f"low={bar.get('low')} close={bar.get('close')} "
                      f"ts={bar.get('end_period_ts', bar.get('start_period_ts', '?'))}")
        except Exception as exc:
            print(f"   interval={interval}m: FAIL: {exc}")

    # 3. Summary
    print("\n3. Verdict:")
    print("   Run this from a machine with Kalshi API access to get results.")
    print("   The granularity kill criterion is: finest interval >= 60m -> historical path dead.")

    venue.close()
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
    p_bt.add_argument("--no-exits", action="store_true", help="hold every position to expiry")
    p_bt.add_argument("--exits", action="store_true", help="force stops on")
    p_bt.set_defaults(func=cmd_backtest)

    p_ce = sub.add_parser("compare-exits", help="same strategy with and without stops")
    p_ce.add_argument("--data-dir", default=None)
    p_ce.add_argument("--strategy", choices=sorted(REGISTRY), default=None)
    p_ce.set_defaults(func=cmd_compare_exits)

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

    p_vv = sub.add_parser(
        "verify-venue", help="check venue connectivity and discovery (places no orders)"
    )
    p_vv.add_argument(
        "--dump",
        metavar="PATH",
        default=None,
        help="save raw API responses to PATH so parsers can be validated offline",
    )
    p_vv.set_defaults(func=cmd_verify_venue)

    p_vb = sub.add_parser(
        "verify-bullpen", help="check the configured Bullpen CLI invocation (places no order)"
    )
    p_vb.set_defaults(func=cmd_verify_bullpen)

    p_sim = sub.add_parser("simulate", help="generate a synthetic no-edge control dataset")
    p_sim.add_argument("--data-dir", default=None)
    p_sim.add_argument("--windows", type=int, default=400)
    p_sim.add_argument("--seed", type=int, default=42)
    p_sim.add_argument(
        "--families",
        nargs="+",
        default=None,
        help="slug prefixes to simulate; more than one produces overlapping windows",
    )
    p_sim.set_defaults(func=cmd_simulate)

    p_srv = sub.add_parser(
        "serve", help="local paper-trading control panel in your browser"
    )
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.set_defaults(func=cmd_serve)

    p_cal = sub.add_parser("calibrate", help="inspect calibration curve from past trade outcomes")
    p_cal.add_argument("--data-dir", default=None)
    p_cal.set_defaults(func=cmd_calibrate)

    p_pa = sub.add_parser("paper", help="trade with simulated money against live books")
    p_pa.add_argument("--strategy", choices=sorted(REGISTRY), default=None)
    p_pa.add_argument("--max-ticks", type=int, default=None)
    p_pa.add_argument("--report", action="store_true", help="print detailed trade log")
    p_pa.set_defaults(func=cmd_paper)

    p_sr = sub.add_parser(
        "shadow-replay", help="regenerate shadow ledger from recorded snapshots"
    )
    p_sr.add_argument("--data-dir", default=None)
    p_sr.add_argument("--output", default=None, help="path to write shadow ledger (defaults to data-dir/shadow.jsonl)")
    p_sr.set_defaults(func=cmd_shadow_replay)

    p_srep = sub.add_parser(
        "shadow-report", help="window-clustered performance by rung x direction"
    )
    p_srep.add_argument("--data-dir", default=None)
    p_srep.add_argument("--ledger", default=None, help="path to shadow ledger file")
    p_srep.set_defaults(func=cmd_shadow_report)

    p_vh = sub.add_parser(
        "verify-historical",
        help="probe Kalshi API for settled market / candlestick availability",
    )
    p_vh.set_defaults(func=cmd_verify_historical)

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
