# Handoff — Session 4

## Branch

`sahil/paper-sim`, tracking `origin/sahil/paper-sim`. 3 commits ahead of `claude/btc-prediction-market-bot-q4wjej`.

## This session

Two fixes, both verified.

### 1. Paper trading now actually simulates — `config.yaml`

Default strategy was `volume_threshold` with `assumed_edge: 0.0`, which makes the
risk layer decline every trade by construction (`signal.prob ≈ mid < ask = entry`
→ negative edge). Paper mode looked broken but was working as designed.

Now `always_trade` / `direction: follow` / `assumed_edge: 0.06`. The old honest
no-edge config is preserved as a comment block directly above it.

`assumed_edge: 0.06` is a **placeholder**, not a measured edge — it exists to clear
the "edge must survive fees" gate (spread + Kalshi taker fee ≈ 4–5c round trip on a
coin flip). P&L from this config validates the pipeline, NOT the strategy.

Risk gates (spread, depth, exposure cap, Kelly) left intact deliberately.

Verified with `python -m btcbot paper --max-ticks 8 --report`:
```
cash $97.98 | open 1 ($1.99) | equity $99.97
HOLDING KXBTC15M-26JUL291345-45 Up 2.31 @ 0.865 mark 0.860 (-0.03)
```

### 2. Window count was inflated ~300x — `goal.md` Phase 4

`load_dataset()` returns **snapshots** (one per poll), not windows. Phase 4's
`len(load_dataset('data'))` counted rows. Corrected to group first:

```
python -c "from btcbot.recorder import load_dataset; from btcbot.backtest import group_windows; print(len(group_windows(load_dataset('data'))))"
```

Real numbers on `data/`: **6370 snapshots → 19 windows.** The old command said 6370
and would have sent you into parameter tuning; 19 is deep in noise. Phase 4's own
rule (<100 windows → skip tuning) applies.

`python -m btcbot backtest` also prints `windows seen`.

## Test suite

160 passed, 7 skipped (bullpen CLI, Windows-only skip). Unchanged by this session.

## Repo hygiene checked

- `.env` is gitignored and byte-identical to `.env.example` with all values empty — no credentials present
- Only `.env.example` is tracked
- `data/`, `data-sim/`, `*.jsonl`, `.venv/` all ignored

## Still open from goal.md

- Phase 1b — fee formula vs real Kalshi fills (unverified)
- Phase 1c — orderbook derivation, `up_bid + down_ask = 1.00` (unverified)
- Phase 3 — control experiment t-stat, sweeps (not run)
- Phase 4 — blocked: only 19 windows, need ≥100. Recorder needs to keep running.
- Phase 5 — summary table

## Carried over from Session 3 (still true)

- Settlement resolves on BTC spot vs strike (`runner.py:_settle_expired`)
- Net-edge Kelly (`risk.py:147`)
- `learner.py` — Beta-Binomial calibrator + `outcomes.jsonl`; `learning.enabled: false` (opt-in)
- `python -m btcbot calibrate` inspects the calibration curve
- Extension needs `python -m btcbot serve` running + native host registered

## Rules

Never enable live trading. Never commit `.env`. Never weaken or delete a test.
Ask before merging to `main`.
