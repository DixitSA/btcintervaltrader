# btcintervaltrader

A research harness for Polymarket's BTC 15-minute Up/Down prediction markets.

It can trade. But it is built to make you **prove a rule works before it lets you
bet on it**, because the specific rule this repo started from — *"just bet when
volume is over $500k"* — does not survive contact with the evidence.

---

## Part 1: How these markets actually work

Polymarket runs rolling **BTC Up or Down** windows (15-minute, also 5-minute and
1-hour). Each one is a binary market:

- At the start of the window, a reference price is fixed — the **"price to beat"**.
- You buy **Up** shares or **Down** shares. Each share costs between $0.01 and $0.99.
- At the end of the window, whichever side is correct pays **$1.00 per share**.
  The other side pays $0.

So a share priced at $0.60 is the market saying "60% chance". Your profit on a
winning $0.60 share is $0.40; your loss on a losing one is $0.60.

Mechanically, running a bot means four things:

1. **Discover** open windows — Gamma API (`gamma-api.polymarket.com/markets`),
   filtering on the `btc-updown-15m` slug prefix. → `btcbot/markets.py`
2. **Read the book** — CLOB API (`clob.polymarket.com/book?token_id=...`) for the
   Up and Down token IDs. → `btcbot/clob.py`
3. **Decide** — apply a rule to the book + BTC spot. → `btcbot/strategies/`
4. **Execute** — sign an EIP-712 order with a Polygon wallet and post it.
   → `btcbot/execution.py` (needs the optional `py-clob-client`)

That's the plumbing, and it's all implemented here. The plumbing is the easy part.

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

```bash
pip install -r requirements.txt
```

### Step 0 — Sanity check the harness (no network needed)

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

### Step 3 — Paper trade against live books

```bash
python -m btcbot paper
```

Paper mode runs the full portfolio: cash, open positions marked to the bid,
realized vs unrealized P&L, equity curve, drawdown, and stop losses that fire
against real books. It prints a ledger on exit.

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

#### Option A — Bullpen CLI (`backend: bullpen`)

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

#### Option B — direct CLOB signing (`backend: clob`)

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

- **Neither live backend has been executed against the real venue.** The network
  policy where this was built blocks Polymarket and Bullpen entirely. The logic
  is tested against fakes; the wire format is not confirmed. Place one
  minimum-size order by hand and confirm it in the Polymarket UI first.
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
| `btcbot/markets.py` | Gamma API discovery, strike parsing |
| `btcbot/clob.py` | Order book reads, live order submission |
| `btcbot/spot.py` | BTC spot feed (**see the oracle warning above**) |
| `btcbot/signals.py` | Fair-value model, book imbalance, implied probability |
| `btcbot/strategies/` | `volume_threshold` (the video's rule), `edge_threshold` |
| `btcbot/risk.py` | Sizing, fee-aware edge check, caps, kill switch |
| `btcbot/portfolio.py` | Cash, positions, mark-to-market, P&L ledger, drawdown |
| `btcbot/exits.py` | Stop loss, take profit, trailing stop, drawdown guard |
| `btcbot/execution.py` | `PaperExecutor` / `BullpenExecutor` / `LiveExecutor` |
| `btcbot/backtest.py` | Replay + statistics |
| `btcbot/simulate.py` | Synthetic no-edge control world |
| `btcbot/runner.py` | Live loop |

**Separation of concerns that matters:** strategies propose a side and a
probability; they never choose size. `risk.py` alone decides whether and how much
to bet, so a strategy bug can't drain the bankroll. Every entry must clear a
fee-aware edge check, quarter-Kelly sizing, a max entry price of $0.90, a hard
book-depth check, an hourly trade cap, and a daily loss limit.

## Tests

```bash
python -m pytest tests/ -q     # 86 passing
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
