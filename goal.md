# Goal: Audit & harden the pipeline, then optimize strategy

## Rules (never violate)
1. Never enable live trading. 2. Never commit `.env`. 3. Never weaken/delete a test. 4. Never restructure architecture. 5. Run `python -m pytest tests/ -q` after every change — all non-skipped must pass. 6. Before changing params, count backtest windows. < 100 = noise, don't tune.

## Phase 1 — Fix known bugs

**1a. Kelly sizing bug** — `btcbot/risk.py:144` uses gross edge. Fix: `kelly_fraction(signal.prob - fee_per_share, entry)`. Check if any test asserts old value; update expected. Run tests.

**1b. Fee formula** — Read `btcbot/fees.py`. Does `ceil(0.07 * C * P * (1-P))` match Kalshi? Check `tests/test_kalshi.py`.

**1c. Orderbook** — Kalshi has only bids. Read venue parser; confirm `up_bid + down_ask = 1.00`. Run `python -m btcbot verify-venue`.

## Phase 2 — Verify paper sim

Use explore agents in parallel:
- Agent A: read `btcbot/execution.py` PaperExecutor. How does `place_order` fill? Walk book or top level? Slippage?
- Agent B: read `btcbot/runner.py` `tick()`. Exact sequence fetch→signal→size→order? Per-market error handling?
- Agent C: read `btcbot/portfolio.py`. Cash debit/credit? Mark-to-market at mid or last? P&L cumulative or per-tick?
- Agent D: read `btcbot/risk.py`. What gates prevent trades? Checked before or after sizing?

Smoke test: `python -m btcbot paper` for 30s. No crashes, data files appear. `python -m btcbot compare-exits` — print stop cost.

## Phase 3 — Verify backtest harness

**Critical: control experiment must show zero edge.**
- `python -m btcbot simulate`. Read t-stat. |t| < 2.0. If t > 2.0: STOP, find the leak.
- `python -m btcbot sweep --data-dir data-sim`. Same check.
- `python -m btcbot sweep`. Read t column for all directions.
- `python -m btcbot backtest --strategy edge_threshold` then `volume_threshold`.

## Phase 4 — Optimize strategy

Count windows — `load_dataset` returns SNAPSHOTS (one per poll, ~300 per 15-min window), so `len(ds)` overstates the sample by ~300x. Group them:

`python -c "from btcbot.recorder import load_dataset; from btcbot.backtest import group_windows; print(len(group_windows(load_dataset('data'))))"`

(`python -m btcbot backtest` also prints `windows seen`.) If < 100 windows, skip to Phase 5.

Use explore agents in parallel:
- Agent A: read `btcbot/strategies/volume_threshold.py`. Entry condition, side, params.
- Agent B: read `btcbot/strategies/edge_threshold.py`. Vol usage, uncalibrated behavior, min_edge.

If ≥100 windows, grid search via `--set` (never edit config.yaml):
- `min_edge=0.03, 0.05, 0.08, 0.10` with `edge_threshold`
- `direction=follow` vs `direction=fade`
- Compare t-statistics, pick best.

Run `python scripts/setup.py` to check windows needed for 5% edge detection.

## Phase 5 — Summary

`python -m pytest tests/ -q` final. Print table:

| Component | Status | Detail |
|-----------|--------|--------|
| Kelly bug | fixed/skipped | |
| Fee formula | correct/wrong | |
| Book derivation | verified | |
| Paper sim | working | |
| Exit costs | measured | |
| Control t | value | |
| Sweep t | value | |
| Windows | N | |
| Strategy params | final | |
