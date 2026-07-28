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

### Step 4 — Live (only if steps 2 and 3 justified it)

```bash
cp .env.example .env       # add POLYMARKET_PRIVATE_KEY
export BTCBOT_I_UNDERSTAND_REAL_MONEY=yes
python -m btcbot live
```

Live requires **both** `mode: live` and that environment variable, so no config
typo can move real money. Use a dedicated wallet funded with only what you can
afford to lose.

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
| `btcbot/execution.py` | `PaperExecutor` / `LiveExecutor` |
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
python -m pytest tests/ -q     # 40 passing
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
