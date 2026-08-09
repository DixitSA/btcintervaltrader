# awesome-systematic-trading, filtered through this repo

Source: <https://github.com/wangzhe3224/awesome-systematic-trading>

That list is a directory of roughly 300 links spanning every asset class, holding
period and language in systematic trading. This repo trades one thing: BTC
15-minute binary Up/Down windows on Kalshi, in ~700 lines of dependency-light
Python whose main job is to stop you betting on a rule you have not demonstrated.

Most of the list does not apply, and saying which parts and why is more useful
than reproducing the links. This file records three things:

1. What was **adopted**, and what it changed.
2. What was **deliberately not adopted**, and the reason.
3. The handful of entries that are **live leads against known gaps** in this
   repo — the most valuable thing the list contains for us.

---

## 1. Adopted

Three ideas were implemented. All three are stdlib-only reimplementations, not
dependency additions — see §2 for why.

### The microprice → `btcbot/signals.py`

*From: Alpha Collections → Orderbook → [The Microprice](https://github.com/sstoikov/microprice)
(Stoikov, 2018).*

The only entry in the entire Orderbook section, and the most directly relevant
link in the list for a repo whose signal is model-vs-market.

`market_implied_up()` read the market's probability off the **mid**. The mid
assumes the next trade is equally likely to hit either side, which is false
whenever resting size is lopsided. `weighted_mid()` and `microprice_up()` weight
by book imbalance instead:

```
I = bid_size / (bid_size + ask_size)
weighted_mid = I * ask + (1 - I) * bid
```

It reduces to the mid exactly when the book is balanced, so it is never *worse*
than the mid — only different on a skewed book.

This is better founded on Kalshi than it first looks. The book is bid-only and
the Up ask is **derived** from the best Down bid, so "Up ask size" literally is
the resting Down interest. The imbalance measured is genuinely "how much size
wants Up versus how much wants Down", not an artifact of a quoting convention.

**Opt-in, default unchanged, available everywhere.** `fair_value: mid |
microprice` lives on the `Strategy` base class, so all three strategies reach it
— including `always_trade`, which is what `config.yaml` actually runs. It
defaults to `mid`, because every recorded result in this repo was produced with
the mid and silently changing the estimator would invalidate the comparison
rather than improve it.

```bash
python -m btcbot backtest --strategy edge_threshold --set fair_value=microprice
```

```yaml
# config.yaml -- reaches `paper` and `live`, not just the backtester
strategy:
  name: always_trade
  params: {direction: follow, assumed_edge: 0.06, fair_value: microprice}
```

**The side and the price must come from the same estimator.** `always_trade` and
`volume_threshold` pick a direction with `favored_side()`, which read the mid
unconditionally. A strategy pricing with the microprice while picking its side
from the mid would, on a sufficiently skewed book, buy the side it had just
decided was the underdog — and nothing would crash to tell you. `Strategy` now
supplies both `market_up()` and `favored_side()` off the same setting, and
`tests/test_microprice.py` pins a book where the two estimators genuinely name
different favourites.

Two things it does not do. It does not create edge — it sharpens the estimate of
what the market thinks, which usually *shrinks* the gap a model-vs-market
strategy sees, and shrinking a spurious gap is the point. And it is computed from
resting size, the cheapest quantity on a book to fake; on a thin 15-minute binary
a single large resting order moves it several points.

**The control world cannot exercise it.** `simulate.py` builds both sides of
every book with identical size, so the imbalance is exactly 0.5 and the
microprice *is* the mid there — `backtest --data-dir data-sim` returns the same
t-statistic under either setting. That is the correct no-op, not evidence the
estimator does nothing; it only diverges on real recorded books with asymmetric
resting size. Judge it on `data/`, never on `data-sim/`.

### Multiple-testing correction and the Deflated Sharpe Ratio → `btcbot/multiple_testing.py`

*From: Resources → Books → [Advances in Financial Machine Learning](https://github.com/BlackArbsCEO/Adv_Fin_ML_Exercises)
and [Machine Learning for Asset Managers](https://github.com/emoen/Machine-Learning-for-Asset-Managers)
(López de Prado); Bailey & López de Prado, "The Deflated Sharpe Ratio" (2014).*

This closes the largest statistical hole in the repo. `sweep` runs a 20-cell grid
and prints a t-statistic per cell. It already *warned* in prose that judging the
best cell at `|t| > 2` is not a 5% test — it now computes the bar instead of
describing it:

| | |
|---|---|
| Šidák-corrected critical `\|t\|` | the bar that actually holds 5% across the grid — **3.02**, not 2.00, for the default 5×4 sweep |
| family-wise p-value | P(noise alone beats this result *somewhere* in the grid) |
| Deflated Sharpe Ratio | P(true edge > 0) after accounting for the search, the scatter across the grid, and non-normal payoffs |

The DSR is the one to read. Binary payoffs are violently non-normal — on the
control dataset the winning cell's per-trade returns come out at **skew −2.06,
kurtosis 5.29** — and the plain t-statistic assumes they are not. Buying
favourites produces many small wins and occasional large losses, which is exactly
the shape that flatters a t-statistic.

Grid cells are correlated (nested thresholds re-trade the same windows), so the
*effective* number of trials is below the nominal count. Passing the nominal
count makes the DSR conservative, which is the direction to err in.

On the 600-window synthetic control world, where edge is provably zero:

```
best cell by t: follow@100,000 (t=+1.07, n=496)
  single-test bar   |t| > 2.00   <- wrong bar for a swept result
  Sidak bar (n=20 )  |t| > 3.02   <- the bar that holds 5% across the grid
  family-wise p     0.999        <- P(noise alone beats this SOMEWHERE in the grid)
  deflated Sharpe   0.002        <- P(true edge > 0) after the search; want > 0.95
```

### Hurst exponent, with its own null → `btcbot/hurst.py`, `python -m btcbot hurst`

*From: Analytic tools → TimeSeries Analysis →
[hurst-calculator](https://github.com/Osamwonyi18/hurst-calculator).*

Rescaled-range (R/S) analysis on the recorded intra-window spot path. This tests
the claim the whole repo rests on — README Part 2, "a 15-minute BTC window is
close to a coin flip". If the path is a random walk, no rule reading only price
history can produce a directional edge, and whatever the sweep finds is
selection.

**The estimator's bias is the interesting part.** R/S is badly biased upward on
short samples: fed a few hundred points of pure random walk it returns ~0.63, not
0.50. Read against 0.50 that is a confident "trending" verdict on data with no
memory whatsoever — precisely the reading a hopeful person wants.

So `null_hurst()` generates synthetic random walks with the **same window count
and lengths as your data**, measures H on each, and reports the mean and spread.
That control is the comparison, not 0.50. It is `simulate.py`'s logic applied to
a different statistic, and it is why this module is worth having rather than a
one-line R/S formula.

On the control world, where the path is a random walk by construction:

```
Hurst exponent   : 0.631
random-walk null : 0.631 +/- 0.004  (200 synthetic datasets, same shape)
z vs null        : -0.05
```

The raw 0.631 looks like a trend. Against its own null it is nothing, which is
the correct answer.

---

## 2. Deliberately not adopted

The list's own framing is "practical, promising libraries with solid
engineering", and most of these are exactly that. They are still wrong here.

**Dataframe and numerics stacks** — pandas alternatives, Vaex, Modin, Polars,
ArcticDB, DuckDB, numba, Cython. This repo's entire dataset is ~6k JSONL
snapshots and a full backtest runs in under a second on stdlib types. The
constraint is *sample size*, not throughput. Adding a dataframe engine would buy
nothing and cost the property that `models.py` is dependency-free and the
strategy/risk/backtest layers unit-test with no network and no install.

**Metrics libraries** — quantstats, pyfolio, ffn, empyrical, alphalens. Genuinely
good, and aimed at a different problem: multi-year equity curves with drawdown
tables and rolling Sharpe. `backtest.py` already computes the four numbers that
matter for binaries (ROI net of fees, break-even win rate, t-statistic,
max drawdown), and the break-even calculation is Kalshi-fee-specific in a way no
general library will get right. What was missing was the *selection* correction,
which none of them provide — hence §1.

**TA indicator libraries** — TA-Lib, pandas-ta, finta, kand, and the rest of the
Indicators section, the largest single category in the list. A 15-minute binary
resolves on one comparison: spot versus strike at a fixed instant. There is no
trend to follow and no pattern to complete. README Part 3 is explicit that edges
here are microstructure and latency edges. An RSI on a 15-minute window is the
kind of thing that produces a 76.6% win rate in a world with no edge.

**Backtest frameworks** — backtrader, vectorbt, Nautilus Trader, Jesse,
Freqtrade, QuantConnect. All bar-based and position-oriented. This harness
replays *order book snapshots* in time order through the same strategy objects
the live runner uses, which is what makes a backtest here structurally unable to
see information the live bot would not have had. Porting to a bar framework would
trade that guarantee for features aimed at a different market shape.

**Portfolio optimizers** — PyPortfolioOpt, Riskfolio-Lib, skfolio, cvxportfolio.
These allocate across many assets with an estimated covariance matrix. This bot
holds one position at a time in an instrument that resolves to $0 or $1 in
fifteen minutes. Sizing here is quarter-Kelly on a fee-adjusted net edge with a
hard exposure cap, in `risk.py`; mean-variance machinery has nothing to optimize.

**ML/RL** — TradingGym, FinRL, the ML/RL section generally. With 19 recorded
windows, fitting anything with free parameters is curve-fitting with extra steps.
`goal.md` Phase 4 already refuses parameter tuning below 100 windows; a learned
policy is the same objection with more capacity to overfit. The existing
`learner.py` Beta-Binomial calibrator is deliberately the simplest thing that can
update on outcomes, and it ships disabled.

**Hummingbot / market making** — the most *interesting* rejection. README Part 3
names market making as the most durable of the three plausible edges here.
Hummingbot is a serious codebase for exactly that. It is out of scope because
this repo has never placed a live order on any venue and the Kalshi wire format
is unconfirmed (see §3); quoting two-sided in a 15-minute binary is not the place
to find out your order plumbing is wrong. Worth revisiting only after the live
path has been exercised.

---

## 3. Live leads against known gaps

These are the entries worth acting on. Each maps to a gap this repo already
documents, and none has been verified from here — the sandbox network policy
blocks the venues.

### Recorded data — the actual blocker

`handoff.md`: **19 windows recorded, ≥100 needed**, Phase 4 blocked. Everything
downstream is gated on this, and `record` has to run for days to fix it.

- **[polymarket-canary-tape](https://huggingface.co/datasets/oraclemangle/polymarket-canary-tape)**
  (Data Source) — CC-BY-4.0 prediction-market microstructure tape: 271M CEX
  trades and 61M Polymarket order-book WebSocket events, including a
  dual-vantage overlap window built for latency studies.

  This is the single most useful link in the list for this repo. Caveats before
  anyone gets excited: it is **Polymarket**, and README Part 1 is emphatic that
  results are not transferable between venues because the fee models differ
  enough to move ROI from −1.06% to −2.47% on identical trades. It would need
  loading through `recorder.py`'s schema and backtesting under the Polymarket fee
  model. What it is genuinely good for is the *latency* question — the
  dual-vantage overlap is aimed squarely at README Part 3's "oracle latency"
  edge, which is otherwise unmeasurable without infrastructure we do not have.

### Kalshi wire format — unconfirmed

README "Known gaps": no live backend has been executed against a real venue;
request shapes follow published docs and are tested against fakes.
`tests/test_fixtures.py` skips until `fixtures/kalshi.json` exists.

- **[pykalshi](https://github.com/ArshKA/kalshi-client)** (Prediction Markets) —
  a maintained Kalshi client with WebSocket streaming and local orderbook
  management. Read it as a **second opinion on the wire format**: RSA-PSS signing
  (README notes the millisecond-timestamp trap), the bid-only book derivation,
  and the `volume_fp` contracts-vs-dollars distinction that `models.py` documents
  at length. Cross-checking our parsers against an independent implementation is
  cheaper than discovering a sign error with real money. Not a dependency —
  `venues/kalshi.py` stays ours.
- **[pmxt](https://github.com/pmxt-dev/pmxt)** (Broker APIs) — "the ccxt for
  prediction markets". Same use: a normalization layer to check our venue
  abstraction against, particularly the Kalshi/Polymarket differences that
  `venues/base.py` papers over.
- **[Parsec](https://github.com/parsecular/parsec-mcp)** (Prediction Markets) —
  cross-exchange prediction market data and live streams.

### Fee constants — placeholders

README "Known gaps": fee constants are placeholders needing verification against
real fills. `goal.md` Phase 1b is still open. None of the list's entries settle
this — it needs a real fill on a real account. Noted here so the gap is not
mistaken for one the list closes.

### Volatility estimation — a real upgrade, not yet taken

- **[volest](https://github.com/jasonstrimpel/volatility-trading)** (Alpha
  Collections) — the estimator set from Sinclair's *Volatility Trading*:
  Parkinson, Garman-Klass, Rogers-Satchell, Yang-Zhang.

  `signals.py:realized_vol_from_series` uses close-to-close log returns, the
  least statistically efficient estimator available. For `edge_threshold` the
  volatility is not a tuning knob — it *is* the edge, and the docstring on
  `_vol()` records what a wrong σ did (assumed 60% against realized 24%, putting
  the model 17 points off fair value and fading every rally). A range-based
  estimator is several times more efficient at the same sample size, which
  matters exactly here, where σ is estimated from a few minutes of ticks.

  Not implemented because it needs OHLC bars and the recorder stores a tick
  series. Bucketing ticks into sub-bars is the obvious route and is a
  self-contained change. This is the best-value item left on the list.

---

## Attribution

`awesome-systematic-trading` is by [wangzhe3224](https://github.com/wangzhe3224)
and contributors. The Stoikov microprice, the Bailey & López de Prado deflated
Sharpe ratio, and R/S analysis are all reimplemented here from their published
descriptions rather than vendored; no code was copied from the linked projects.
