# btcintervaltrader

A research harness for BTC 15-minute Up/Down prediction markets, on **Kalshi**
(default) or Polymarket.

It can trade. But it is built to make you **prove a rule works before it lets you
bet on it**, because the specific rule this repo started from — *"just bet when
volume is over $500k"* — does not survive contact with the evidence.

---

## Part 1: How these markets actually work

Kalshi runs a rolling **KXBTC15M** series — Bitcoin up or down, a new 15-minute
window every 15 minutes. Tickers look like `KXBTC15M-26JUL281745` (series, date,
window close in ET). Polymarket runs the same idea as `btc-updown-15m`.

- At the start of the window a reference price is fixed — the **"price to beat"**.
- You buy **YES** (up) or **NO** (down). Contracts trade between 1c and 99c.
- At the close, the correct side pays **$1.00**; the other pays $0.

A contract at 60c is the market saying "60% chance". Your profit on a winning
60c contract is 40c; your loss on a losing one is 60c.

Running a bot means four things:

1. **Discover** open windows — `GET /markets?series_ticker=KXBTC15M`
   → `btcbot/venues/kalshi.py`
2. **Read the book** — `GET /markets/{ticker}/orderbook` → same file
3. **Decide** — apply a rule to the book + BTC spot → `btcbot/strategies/`
4. **Execute** — `POST /portfolio/orders`, RSA-PSS signed → `btcbot/execution.py`

### Two Kalshi specifics that will silently corrupt results

**The orderbook contains only BIDS.** The `yes` and `no` arrays are both resting
bids; there is no ask side. A bid for YES at 42c *is* an ask for NO at 58c, so:

```
yes_ask = 100 - best_no_bid
no_ask  = 100 - best_yes_bid
```

Reading the `no` array as the yes-ask inverts every price you compute, and
nothing will crash to tell you. `verify-venue` asserts `up_bid + down_ask = 1.00`
against a live book as a check on this.

**Fees are charged up front on every fill**, not on profit at settlement:

```
fee = ceil(0.07 x contracts x P x (1 - P))   # rounded up to the cent
```

That peaks at **1.75c per contract at 50c** — 3.5% of a 50c stake, paid on entry
and *again* on exit if you stop out. It falls away toward the extremes, which is
why cheap longshots look deceptively cheap to trade. On the control dataset,
identical trades cost **$412 in Kalshi fees vs $116 under the Polymarket model**,
moving ROI from −1.06% to −2.47%.

**Results are not transferable between venues.** A rule tuned on Polymarket fees
can be nonsense on Kalshi. Re-record and re-backtest after switching.

---

## Part 2: Why the $500k volume rule can't work as stated

Three separate problems, and each is independently fatal.

**1. Volume has no direction.** This is the core issue. Volume tells you *how
much* was traded. It cannot tell you *which way the window resolves*. The rule
as usually stated doesn't even specify whether to buy Up or Down — there is no
version of "volume > $500k" that outputs a side. Any real rule needs a
directional component, and that component is where all the risk actually lives.

**2. The numbers don't match reality.** Individual 15-minute BTC windows
typically clear on the order of $5K–$50K, not $500K. A $500K per-window filter
would almost never fire. If a video showed it firing constantly, it was measuring
something else — cumulative daily volume across all windows, or spot volume on an
exchange — and that distinction changes the rule entirely.

**3. A 15-minute BTC window is close to a coin flip, and coin flips lose to
fees.** Over 15 minutes, BTC's drift is negligible against its volatility. The
market price already encodes spot's distance from the strike. You are paying
spread plus a fee on winnings to bet on approximately 50/50 — which is negative
expected value before you've made a single decision.

### The trap that makes bad rules look good

Run the sweep in this repo against its **synthetic control dataset** — a
simulated world where volume is generated *independently of the price path*, so
there is provably **zero** edge available:

```
direction      thresh  trades    win%     BE%       ROI       z
--------------------------------------------------------------
follow              0     800   57.6%   55.7%    +2.71%   +1.13
follow        100,000     680   67.1%   66.0%    +0.76%   +0.59
follow        500,000     209   76.6%   73.8%    +3.53%   +0.93
fade          500,000     220   22.3%   27.9%   -22.77%   -2.00
up            500,000     213   48.8%   50.3%    -8.09%   -0.44
down          500,000     216   48.6%   50.2%   -11.80%   -0.47
```

Look at row three. **A 76.6% win rate**, in a world with no edge whatsoever.

That is what a screenshot in a promotional video looks like. The number is real
and it is completely meaningless, because:

- **BE% (break-even) is 73.8%.** "Follow the favourite" buys shares at ~$0.74, so
  you *must* win 74% of the time just to break even. The high win rate is bought
  and paid for, not earned.
- **z is +0.93.** The gap between 76.6% and 73.8% is well within noise. Below
  |z| = 2, the result is statistically indistinguishable from no edge at all.

Note the `fade` row going the other way (z = −2.00): fading the favourite is
*reliably* worse than break-even. That is not a hidden edge in reverse — it's the
spread and fees being paid on every trade, which is exactly the cost that makes
coin-flip betting negative-sum in the first place.

**Win rate is the most misleading number in binary markets.** Any strategy that
buys favourites will post a gaudy win rate. The only numbers that matter are ROI
net of fees, and whether the sample is large enough to distinguish that ROI from
luck. This harness prints all three side by side, on purpose.

### About reflexbot.io

I could not verify it. This sandbox's network policy blocks the domain (403 at
the proxy), so there is no data here on what it does or whether it profits —
treat any claim about it as unverified. Worth noting as a prior, though: a bot
with a genuine edge in a market this competitive makes far more money trading
than selling subscriptions. That proves nothing about any specific service, but
it is the right way to weight the category.

---

## Part 3: What a real edge would have to look like

Edges in short-horizon crypto binaries are **microstructure and latency** edges,
not chart-pattern edges:

- **Oracle latency.** The window settles against a specific price feed at a
  specific instant. If you see that feed move before the book reprices, that's an
  edge — measured in milliseconds, against professionals with better
  infrastructure than you.
- **Stale quotes near expiry.** With 20 seconds left and spot clearly through the
  strike, resting orders on the wrong side are occasionally mispriced. This is
  real, and it is the most contested part of the window.
- **Market making.** Quote both sides, earn the spread, manage inventory. Doesn't
  need a directional view at all. Hardest to implement, most durable.

`edge_threshold` in this repo implements the honest version of the first idea: a
zero-drift lognormal model of P(spot > strike), trading only when the book
disagrees by more than fees can explain. It will decline most windows. **That is
the correct behaviour**, not a bug.

> **The single most important detail:** the spot feed in `btcbot/spot.py` is
> Binance, and it is almost certainly **not** the oracle Polymarket settles on.
> Before risking money, confirm the exact settlement source and timestamp. A feed
> that differs by a few dollars will flip precisely the trades you thought were
> safest.

---

## Part 4: Using it

**Running this on your own machine? See [QUICKSTART.md](QUICKSTART.md)** — one
setup command, Windows/macOS/Linux, plus a supervisor for the multi-day
recording run.

```bash
python scripts/setup.py     # venv + deps + .env + offline self-check
```

### Step 0 — Check the venue connection

```bash
python -m btcbot verify-venue
```

Confirms connectivity, prints the fee model, lists open markets, fetches a real
orderbook, and asserts `up_bid + down_ask = 1.00` so a broken ask derivation
cannot pass silently. Places no orders. Credentials are optional here — Kalshi
market data is public, so `record` and `paper` need no API key at all.

### Step 0b — Sanity check the harness (no network needed)

```bash
python -m btcbot simulate --data-dir data-sim --windows 800
python -m btcbot sweep --data-dir data-sim
```

Confirm z-scores hover near zero. If a strategy shows a big edge in the control
world, the harness is leaking future information and every result it produces is
worthless. There's a test pinning this: `test_volume_rule_shows_no_edge_in_a_no_edge_world`.

### Step 1 — Record real data (days, not hours)

```bash
python -m btcbot record
```

Writes one JSONL snapshot per tick per open window to `data/`. **You cannot
evaluate any rule without this.** There is no shortcut, and no downloadable
dataset that reflects the books you'd actually have traded against.

### Step 2 — Test the rule on YOUR data

```bash
python -m btcbot sweep                      # the $500k rule, all four directions
python -m btcbot backtest --strategy edge_threshold
python -m btcbot backtest --strategy volume_threshold --set min_volume_usd=50000 --set direction=fade
```

Read the **z** column, not the ROI column.

And read the block `sweep` prints *underneath* the table before you believe any
row of it. The default grid is 20 cells, and the bar for the best of 20 is
`|t| > 3.02`, not `|t| > 2`. That block prints the corrected bar, the family-wise
p-value, and a deflated Sharpe ratio that also accounts for how skewed binary
payoffs are — see [docs/systematic-trading.md](docs/systematic-trading.md).

### Step 2b — Check there is anything to find

```bash
python -m btcbot hurst
```

Measures whether the BTC path *inside* a window trends, mean reverts, or is a
random walk — against a synthetic random-walk control of the same shape, because
the R/S estimator reads ~0.63 on memoryless data and comparing it to 0.50 would
call that a trend. If the path is indistinguishable from a random walk, no rule
reading only price history can have a directional edge, and anything the sweep
turns up is selection.

### Step 3 — Paper trade against live books

```bash
python -m btcbot paper
```

Paper mode runs the full portfolio: cash, open positions marked to the bid,
realized vs unrealized P&L, equity curve, drawdown, and stop losses that fire
against real books. It prints a ledger on exit.

---

## Trade volume, and why DCA does not apply here

A common plan is "make lots of small trades and dollar-cost-average in." That
reasoning does not transfer to these markets, and the direction of the error is
expensive.

**DCA works on an accumulating asset with positive long-run drift.** You keep
buying, you hold through the dips, the drift eventually pays. These binaries do
not accumulate: each one resolves to $0 or $1 in fifteen minutes and is gone.
There is no position being averaged into and no drift to wait for.

**Volume is a multiplier on the sign of your edge.** Here is the same strategy
above (−1.06% per trade) resampled at different trade counts:

```
  trades   P(profit)   median ROI    5th pct   95th pct
      10      48.6%       -0.80%    -35.17%     31.75%
     100      45.1%       -0.79%    -11.60%      9.09%
    1000      29.3%       -1.08%     -4.44%      2.37%
    5000      13.4%       -1.02%     -2.54%      0.49%
```

The mean never improves. Volume just collapses the variance around it, so at 10
trades you are a coin flip and at 5,000 you have a 13% chance of being ahead.
Variance is the only thing that can rescue a negative-edge bettor, and volume is
what destroys variance. **Establish the edge first; scale volume second.**

### Raising trade count the honest way

One 15-minute family gives you 4 windows/hour, one position at a time. To trade
more without changing *what* you are betting on, trade more families:

```yaml
markets:
  slug_prefixes:
    - btc-updown-15m
    - btc-updown-5m
    - eth-updown-15m
    - sol-updown-15m
risk:
  max_concurrent_positions: 5
  max_total_exposure_fraction: 0.10
```

Three families instead of one took the control backtest from 163 to 495 trades.

> ⚠️ **Raise `max_concurrent_positions` and you must set
> `max_total_exposure_fraction`.** Kelly sizes every position as though it were
> your only one, so five concurrent positions is five times the intended risk
> unless total exposure is capped. The risk layer enforces the cap against live
> portfolio state and will shrink or refuse an order that would breach it.

The backtester replays snapshots in **time order**, not window order, so
overlapping markets compete for the same capital exactly as they would live.

---

## Stop losses, and what they actually cost

Thresholds are in **probability points**, not percent. Entered at `0.60` with
`stop_loss_drop: 0.15` exits when the **bid** hits `0.45`.

```yaml
exits:
  enabled: true
  stop_loss_drop: 0.15
  take_profit_rise: null
  trailing_stop_drop: null
  max_hold_seconds: null
  no_exit_within_seconds: 20.0   # don't churn as the window closes
  max_drawdown_pct: 0.25         # equity kill switch
```

Positions are marked at the **best bid** — the price someone will actually pay
you — not the mid. Marking at the mid overstates equity and lets a stop believe
it exited at a price it could not get.

**A stop here is not free protection.** It means selling back into the book, so
you cross the spread twice. On a near-coin-flip, noise stops you out of
positions that would have resolved in your favour. Measure it:

```bash
python -m btcbot compare-exits
```

On the synthetic control dataset:

```
              trades  profit%       ROI         P&L     maxDD       t
---------------------------------------------------------------------
no stops         421    72.0%    -1.06%     -223.92    887.00   -0.34
with stops       421    42.5%    -1.89%     -398.89    701.26   -1.06

Exit breakdown (with stops):
  stop_loss          241  $-3,596.09
  expiry             180  $+3,197.20
```

The stop cut max drawdown from **$887 to $701** — and cost **$175** in P&L,
dropping profitable trades from 72% to 42.5%. That is the real trade: stops buy
you a smaller drawdown and you pay for it in expectancy. Whether that's worth it
is your call, but make it with this table in front of you, on your own data.

> **Note on statistics:** once stops are on, payoffs are no longer binary, so
> the win-rate z-score stops being a valid test — a stopped-out trade can hold
> the winning side and still lose money. The report switches to a **t-statistic
> on per-trade returns**, which is valid either way. `|t| < 2` still means no
> demonstrable edge.

### Step 4 — Live (only if steps 2 and 3 justified it)

Live requires **both** `mode: live` and `BTCBOT_I_UNDERSTAND_REAL_MONEY=yes`, so
no config typo can move real money. Use a dedicated wallet funded with only what
you can afford to lose.

There are two execution backends. Pick one with `execution.backend`.

#### Option A — Kalshi (`backend: venue`, `venue: kalshi`)

```bash
pip install cryptography          # RSA-PSS request signing
cp .env.example .env              # KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH
```

Create an API key in Kalshi account settings; it gives you a key ID and
downloads an RSA private key. Orders are signed per request:
`timestamp_ms + METHOD + path` (path **without** the query string), signed
RSA-PSS/SHA256, sent as `KALSHI-ACCESS-*` headers. The timestamp is
**milliseconds** — seconds is the most common cause of signature rejection.

Orders go out as immediate-or-cancel: we price against the book we just read,
and a resting remainder in a window that expires in minutes is a liability.

```bash
export BTCBOT_I_UNDERSTAND_REAL_MONEY=yes
python -m btcbot live
```

#### Option B — Bullpen CLI (`backend: bullpen`, Polymarket only)

Shells out to the [Bullpen CLI](https://cli.bullpen.fi/), which handles auth,
signing and funding itself. Nothing wallet-related is needed in this repo.

**The flag syntax in `config.yaml` is a starting point, not confirmed syntax** —
the Bullpen docs were unreachable from the environment this was built in, so the
invocation is explicit configuration rather than a hardcoded guess. Verify it:

```bash
python -m btcbot verify-bullpen
```

That checks the binary exists, runs `bullpen polymarket buy --help`, prints the
exact command the bot would run, and fails if any flag in your template is
absent from the help output:

```
ok   binary: /usr/local/bin/bullpen
ok   `bullpen polymarket buy --help` succeeded

The bot would invoke:
  bullpen polymarket buy --token <TOKEN_ID> --shares 10.00 --limit-price 0.520 --yes --json

WARNING: these flags from buy_template do not appear in the help output: --size
```

Fix `execution.bullpen.buy_template` until it passes, then flip
`execution.bullpen.dry_run: false`. While `dry_run` is true the command is
logged and never executed.

#### Option C — Polymarket direct CLOB signing (`backend: clob`)

```bash
pip install py-clob-client
cp .env.example .env       # fill in the POLYMARKET_* values
```

⚠️ **The single most common failure:** if you funded your account through the
Polymarket website, your USDC is in a **proxy wallet**, not the EOA that owns
your private key. You must set `POLYMARKET_SIGNATURE_TYPE=1` (email/Magic login)
or `2` (browser wallet) **and** `POLYMARKET_FUNDER_ADDRESS` to that proxy
address. Leave it at `0` only if the private key itself holds the USDC.
Otherwise orders are signed from an address with no balance. The client raises a
clear error rather than letting you find out at order time.

```bash
export BTCBOT_I_UNDERSTAND_REAL_MONEY=yes
python -m btcbot live
```

### Known gaps in the live path

Be aware of these before running unattended:

- **No live backend has been executed against a real venue.** The network policy
  where this was built blocks Kalshi, Polymarket and Bullpen entirely. Request
  shapes follow published documentation and are tested against fakes; the wire
  format is not confirmed. Place one minimum-size order by hand and confirm it
  in the web UI first.

  You can close most of this gap in one command, from a machine that can reach
  the venue:

  ```bash
  python -m btcbot verify-venue --dump fixtures/kalshi.json
  ```

  That saves the raw API responses (public market data only — no account info,
  no credentials). Commit the file and `tests/test_fixtures.py` starts
  validating every parser against genuine payloads: market parsing, window
  durations, strike extraction, and the bid-only invariant
  `up_bid + down_ask == 1.00` on real books. Those tests **skip** until the
  fixture exists — a skip there means the wire format is still unconfirmed, not
  that it passed.
- **The Kalshi settlement source is unconfirmed.** Which BTC index KXBTC15M
  resolves against, and at exactly what instant, was not verifiable from here.
  Confirm it before trusting any model-based signal — see the spot.py warning.
- **Settlement is inferred, not confirmed.** `runner.py` settles an expired
  position from the last mark it saw (which converges to 0 or 1 as a window
  closes) rather than asking the venue what happened. It logs `APPROXIMATE` on
  every such settlement. Backtest P&L is exact; live P&L is close but should be
  reconciled against your actual account. Early exits (stop loss / take profit)
  *are* exact, because those fills are real.
- **No startup balance or allowance check.** Nothing verifies you hold USDC or
  that exchange allowances are set.
- **Fee constants are placeholders** and need verifying against real fills.

---

## Layout

| File | Role |
|---|---|
| `btcbot/models.py` | Core types: `Market`, `Book`, `Snapshot`, `Order`, `Fill` |
| `btcbot/venues/kalshi.py` | Kalshi REST, RSA auth, bid-only book handling |
| `btcbot/venues/polymarket.py` | Polymarket adapter (Gamma + CLOB) |
| `btcbot/fees.py` | Venue fee models (Kalshi formula vs Polymarket bps) |
| `btcbot/markets.py` | Polymarket Gamma discovery, strike parsing |
| `btcbot/clob.py` | Polymarket order book reads, order submission |
| `btcbot/spot.py` | BTC spot feed (**see the oracle warning above**) |
| `btcbot/signals.py` | Fair-value model, book imbalance, implied probability |
| `btcbot/strategies/` | `volume_threshold` (the video's rule), `edge_threshold` |
| `btcbot/risk.py` | Sizing, fee-aware edge check, caps, kill switch |
| `btcbot/portfolio.py` | Cash, positions, mark-to-market, P&L ledger, drawdown |
| `btcbot/exits.py` | Stop loss, take profit, trailing stop, drawdown guard |
| `btcbot/execution.py` | `PaperExecutor` / `BullpenExecutor` / `LiveExecutor` |
| `btcbot/backtest.py` | Replay + statistics |
| `btcbot/multiple_testing.py` | Corrections for having tried more than one strategy |
| `btcbot/hurst.py` | R/S analysis: is the intra-window path a random walk? |
| `btcbot/simulate.py` | Synthetic no-edge control world |
| `btcbot/runner.py` | Live loop |

| `crew/` | CrewAI + local Ollama research crew. Reads and reports; never trades |
| `deploy/` | systemd units for the recorder and the daily digest |

See [docs/systematic-trading.md](docs/systematic-trading.md) for what was taken
from [awesome-systematic-trading](https://github.com/wangzhe3224/awesome-systematic-trading),
what was deliberately left, and which of its entries are live leads against the
known gaps above.

**Separation of concerns that matters:** strategies propose a side and a
probability; they never choose size. `risk.py` alone decides whether and how much
to bet, so a strategy bug can't drain the bankroll. Every entry must clear a
fee-aware edge check, quarter-Kelly sizing, a max entry price of $0.90, a hard
book-depth check, an hourly trade cap, and a daily loss limit.

## Tests

```bash
python -m pytest tests/ -q     # 127 passing (+6 fixture tests, skipped until captured)
```

---

## Honest summary

The plumbing here is real and works. The `$500k volume` rule is not an edge, and
this repo is set up to let you demonstrate that to yourself with your own
recorded data rather than take anyone's word for it.

If after Step 2 the sweep shows |z| < 2 across every threshold and direction —
which is what the structure of these markets predicts — the correct action is to
not trade it. That outcome is a successful use of this repo, not a failed one.

Nothing here is financial advice. Most retail participants in short-horizon
binaries lose money net of fees.
