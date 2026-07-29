# Plan — Historical probe, shadow ledger, entry-time ladder

Three parts. **Part 0 runs first** — an afternoon's work that can kill Part 2 outright
and, if it doesn't, removes the two-week data blocker. **Part 1 builds now**
(instrumentation, no trading-behaviour change). **Part 2 is gated on data** — do not
start it early.

---

## Framing: what problem each part solves

**The premise.** "Late in the window is less risky, early pays more." True, but those
are not two independent facts — they are the same fact, the price. Late, the
favourite trades ~0.95: pay 95c to win $1, ~95% hit rate. Early it trades ~0.55:
82% return, ~55% hit rate. Before fees the EV is identical. The market has already
priced the risk/payout trade-off; a ladder over entry time does not harvest it.

**Two structural asymmetries are real, and both favour starting late.**

1. *Kalshi's fee curve is a parabola peaking at 0.50* — `fee = 0.07·P·(1−P)` per contract:

| Entry price | Fee/contract | % of stake | Breakeven edge |
|---|---|---|---|
| 0.50 | 1.75c | 3.5% | 1.75 prob points |
| 0.85 | 0.89c | 1.05% | 0.89 points |
| 0.95 | 0.33c | 0.35% | 0.33 points |

Early entries need ~5x more edge just to break even.

2. *Statistical detectability* — Sharpe per trade ≈ `(e − fee) / √(p(1−p))`.
   For the same 2-point edge:
   - p=0.50: `(0.020 − 0.0175)/0.500` = 0.005 → **~160,000 trades** for t=2 (≈4.5 years at 4 windows/hr)
   - p=0.95: `(0.020 − 0.0033)/0.218` = 0.077 → **~675 trades** for t=2 (≈1 week)

   Late entries are the only place anything can be learned in finite time. This is a
   stronger reason to start late than the risk argument.

**Therefore:** the ladder is *risk-staged capital release*, not edge discovery. If there
is no edge, no rung ordering saves you — you lose slower at one end. Part 1 exists to
find out whether there is one, without paying to find out.

---

# PART 0 — Historical data probe (do this first)

## The data requirement is two requirements, not one

Treating "100+ windows" as a single gate is the mistake. It is two questions with very
different appetites:

| | Needs | Sample required | Source |
|---|---|---|---|
| **(a) Is the market miscalibrated by entry time?** | price + time-to-expiry + outcome | **thousands** of windows | history |
| **(b) What does it cost to trade at each rung?** | real order books | **~20–30** windows | recorder only |

(a) is detecting a small edge in a noisy binary. (b) is estimating the mean of a
low-variance quantity — spread and depth at a given time-to-expiry are stable.

The plan as originally written gates (a) on the collection rate of (b). History can supply
(a) in bulk; the recorder is already 19 windows into a ~30-window requirement for (b).
**That collapses the blocker from ~2 weeks to days.**

## What each source can and cannot give

| Source | Price path | Outcome | Spread/depth | Volume |
|---|---|---|---|---|
| Kalshi settled markets | — | **yes, authoritative** | no | yes |
| Kalshi candlesticks | yes (OHLC) | — | **no** | yes |
| Kalshi public trades | executed prices | — | partial (taker side only) | yes |
| Binance klines | spot only | derivable | n/a | n/a |
| Your recorder | yes | yes | **yes** | yes |

**Hard constraint: nobody sells historical order book depth.** Kalshi's orderbook endpoint
is snapshot-only ([kalshi.py:352](btcbot/venues/kalshi.py:352)). The `sweep_cost` fills the
shadow ledger depends on can never be reconstructed historically — and this entire thesis
lives inside a 1–3c spread, so that is not a rounding error.

**One clear win if settled-market history works:** the market's `result` field is BRTI
ground truth — strictly better than Binance-derived labels, and it retires the
`margin_bps` mislabeling problem for historical records entirely.

## Phase 0.1 — Probe, don't assume

Only two endpoints are currently wired: `/markets?status=open` and
`/markets/{ticker}/orderbook` ([kalshi.py:331](btcbot/venues/kalshi.py:331)).

Treat these as **hypotheses to verify, not facts** — the API changes and this is from
recollection:

- `/markets` supporting `status=settled` with `min_close_ts` / `max_close_ts` and cursor pagination
- a candlesticks endpoint with sub-hourly `period_interval`

Small probe script, `verify-venue` as the precedent to copy. Three questions only:

1. How far back does KXBTC15M history go?
2. What is the finest granularity?
3. Does it carry the settlement result?

**Granularity kill criterion:** 1-minute candles are fine for rungs at 120s/240s/420s. If
the finest interval is hourly, the historical path is useless here and Part 0 stops — back
to the recorder.

## Phase 0.2 — The firewall (build this into the schema, not the docs)

Historical records enter the shadow ledger as `producer: "history"` with `depth: null`,
`spread: null`.

**Enforce in code that depth-less records feed calibration only — never the net-P&L
ranking that decides rung promotion.** If that firewall is a convention rather than an
assertion, someone will pool them in six weeks and get a beautiful, fake answer.

## Phase 0.3 — Use it as a kill shot, not a confirmation

KXBTC15M runs 96 windows/day and is actively market-made. The prior that it is
well-calibrated by time-to-expiry is **strong**.

Pull a few thousand historical windows and ask one question: does calibration vary with
time remaining *at all*? If it does not, **Part 2 is dead** — and it died for the cost of
an afternoon rather than two weeks of recording plus live capital.

This is the highest-value thing history can do here, which is why it runs before Phase 1.1.

---

# PART 1 — Shadow ledger (build now)

## The bottleneck

The paper sim produces **at most one datapoint per window**, often zero. Not because of
the strategy — because of capital rationing in the risk layer: `max_concurrent_positions: 1`,
`max_trades_per_hour: 8`, `max_total_exposure_fraction`, and the `stake ≤ $0.50` rejection
at [risk.py:170](btcbot/risk.py:170). Those are correct for deciding what to *do*. They are
disastrous for learning, because they throttle observation to the rate at which you can
afford to bet.

**Decouple what you learn from what you risk.** Leave the real portfolio untouched; add a
shadow ledger recording what *would* have happened at every rung, every window, both
directions, regardless of available capital.

Per window: 4 rungs × 2 directions = **8 records instead of ≤1**.

## The statistical warning that shapes the design

Those 8 records are **not 8 independent observations**. They share one BTC path — for a
given direction, every rung in a window usually resolves to the *identical* outcome.
Pooling them as independent inflates t-stats by ~√8 and manufactures edge that isn't there.

The correct framing is better than independent sampling: this is a **paired within-window
experiment**. Every rung sees the same path, so comparing rung 0 vs rung 2 as a *paired
difference* removes window-level noise entirely — the most efficient design for ranking
entry times. Consequences, non-negotiable:

- **n = number of windows, not number of records.** (Same trap as `len(load_dataset())`, one level up.)
- Standard errors **clustered by window**; rung comparisons **paired**.

## Phase 1.1 — `btcbot/shadow.py` (new module, zero behaviour change)

`ShadowLedger`, one JSONL row per hypothetical trade, appended to `data/shadow.jsonl`:

| Group | Fields |
|---|---|
| Identity | `schema_version`, `producer` (`live`/`replay`/`history`), `slug`, `strike`, `window_end_ts` |
| Entry | `rung`, `seconds_remaining`, `direction`, `side`, `best_ask`, `spread`, `depth`, `fill_price` (via `sweep_cost`), `contracts`, `fee`, `market_implied_prob`, `signal_prob`, `spot_at_entry` |
| Settlement | `final_spot`, `winning_side`, `won`, `gross_pnl`, `net_pnl`, `margin_bps`, `settled_ts` |

- Fixed notional (`$1`) so records are comparable and bankroll-independent.
- Dedup key `(slug, rung, direction)` so a mid-window restart cannot double-record.
- Append-only, resumable, same load/skip-bad-line discipline as
  [`OutcomeStore`](btcbot/learner.py:36).
- `producer: "history"` records carry `spread: null`, `depth: null` and are **structurally
  barred from net-P&L ranking** (Phase 0.2). Assert it; do not document it.

## Phase 1.2 — Hook into `runner.tick()`

One call after the snapshot is built, **before and independent of** `risk.evaluate()`.
The ledger opens a record the first time a window crosses each rung boundary.

Settlement piggybacks on [`_settle_expired`](btcbot/runner.py:176), which already resolves
spot vs strike and already holds `_strikes[slug]`. Reuse it; do not fork the settlement
logic.

**Invariant, enforced by test: shadow trades never touch `portfolio.cash`.** Real P&L must
be byte-identical with the shadow layer on or off.

## Phase 1.3 — Offline replay producer, identical schema

`python -m btcbot shadow-replay` regenerates the same record set from
`data/snapshots-*.jsonl` via `group_windows`.

This is the **primary** generator: it replays every recorded window instantly, versus 4/hour
wall-clock live. The live ledger's job is to **validate that the replay matches reality**.
Both producers write one schema, tagged by `producer`, so the records pool.

## Phase 1.4 — `python -m btcbot shadow-report`

Table by (rung × direction): windows, records, win rate, mean net P&L per $1,
**window-clustered** LCB₉₅, paired diff vs rung 0.

Sample size printed as **windows**, in the header, unmissably.

## Phase 1.5 — Integrity guards

- **Keep fill realism, drop capital rationing.** Route through `PaperExecutor` /
  `book.sweep_cost` so recorded fills respect real depth. Do *not* apply bankroll,
  concurrency, or hourly caps.
- **Log and count skip reasons.** Silently dropping thin-book entries measures a rosier
  market than exists. Absence must be visible.
- **Settlement truth.** Settlement is CF Benchmarks BRTI, not Binance — see
  [config.yaml:21](config.yaml:21). Store `margin_bps` = |final spot − strike| / strike so
  near-strike windows, where the Binance-derived label may simply be wrong, can be excluded
  *and counted*.
- Missing spot → `won: null`; record retained, excluded from win-rate denominators.

## Phase 1.6 — Verify

- Record count == windows × rungs × directions − logged skips.
- Replay-vs-live agreement on overlapping windows.
- `python -m pytest tests/ -q` green (currently 160 passed, 7 skipped).

## Config

```yaml
shadow:
  enabled: true
  rungs: [120, 240, 420, 900]
  directions: [follow, fade]
  notional_usd: 1.0
  ledger_file: shadow.jsonl
```

## Win rate vs the verdict

A 95% win rate at 0.95 entry is *breakeven*. Report win rate — it was asked for and it is
the readable headline — but **rank rungs on mean net P&L per $1 after fees**. Win rate is
the headline; net P&L is the verdict.

## Payoff

Once the shadow ledger exists it collects data whether or not the live strategy trades. That
means `config.yaml` can revert to the honest `volume_threshold` / `assumed_edge: 0.0`
default and still accumulate the full dataset — the `0.06` placeholder becomes unnecessary.
**Data collection stops depending on pretending to have an edge.**

---

# PART 2 — Entry-time ladder (gated on Part 1 data)

Gated on **two** independent thresholds (see Part 0), not one:

| Gate | Threshold | Status | Source |
|---|---|---|---|
| Calibration — does edge vary by entry time? | thousands of windows | pending Part 0 probe | history |
| Cost — spread/depth per rung | ~20–30 windows | **19**, days away | recorder |

Currently 19 recorded windows (6370 snapshots — count windows, not rows). If Part 0
returns no time-varying calibration, **Part 2 does not start at all.**

## Phase 2.1 — Instrument

`OutcomeRecord` ([learner.py:24](btcbot/learner.py:24)) does not record entry timing. Add
`seconds_remaining`. Key `Calibrator` on `(time_bucket, prob_bucket)` — but keep time
buckets **coarse** (4 rungs, not 15). 4 time × 10 prob × 30 obs = 1200 trades minimum to
fill; finer buckets never fill.

## Phase 2.2 — Collect

Cost layer only (~20–30 windows with real books). The calibration layer comes from Part 0,
not from waiting. If Part 0's granularity kill criterion fires, this reverts to the
original ~2-week continuous-recording requirement.

## Phase 2.3 — Derive the curve offline, before risking anything

The shadow replay already answers this. Output: net edge + LCB₉₅ per rung. Two things that
will bite:

- **Time and price are confounded.** Late *means* extreme price. Measure edge within
  `(time, price)` cells or you will credit the fee curve as a timing effect.
- **Multiple comparisons.** 4 rungs × 4 directions × 4 edge thresholds = 64 tests; |t|>2
  appears by chance in ~3. Pre-register the rungs and correct, or hold out windows.

## Phase 2.4 — The ladder

"Earliness" is just `max_seconds_remaining`, already gating at [risk.py:74](btcbot/risk.py:74).
Rungs as `(min_seconds_remaining, max_seconds_remaining)`:

| Rung | Window | Character |
|---|---|---|
| 0 | 30–120s | last 90s; cheap fees, fast learning |
| 1 | 30–240s | |
| 2 | 30–420s | |
| 3 | 30–900s | full window |

- **Promote** when the rung has n ≥ N_min settled trades **and** LCB₉₅ of net edge > 0.
- **Demote** when LCB₉₅ goes negative. Non-negotiable — a ratchet that only climbs is a
  one-way street into the regime with the worst fees and the least evidence.
- **Default with no data: rung 0.** Fail-safe, same principle as `learning.enabled: false`.
- Size on the posterior **LCB**, not the mean.

## Phase 2.5 — Paper-verify

Ladder holds at rung 0 on empty state; advances only on real evidence.

## Config conflicts to resolve first

- **`max_entry_price: 0.90` blocks the exact regime this thesis favours.** Late-window
  favourites trade 0.92–0.97. As configured, rung 0 is unreachable.
- `min_seconds_remaining: 30` — probably keep (the book genuinely thins), but it means
  "the end" is 30s, not 5s.
- Stops cost a **second** taker fee. At rung 0 you hold 90 seconds; `exits.enabled` should
  likely be off there, or you pay the spread twice on noise.

## Weakest point in this design

Promotion-on-evidence has a selection problem: you advance precisely when recent luck was
good, so you systematically arrive at earlier rungs *after* a favourable run. The LCB
dampens this but does not remove it. Deriving the curve offline (Phase 2.3) is the main
defence — those windows are not selected on your P&L.

---

## Standing rules

Never enable live trading. Never commit `.env`. Never weaken or delete a test. Run
`python -m pytest tests/ -q` after every change. Ask before merging to `main`.
