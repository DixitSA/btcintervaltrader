# btcbot overlay (Chrome extension)

A **read-only** panel on `kalshi.com` showing what your local btcbot model
thinks about the open KXBTC15M window: market-implied probability, model
probability, the gap between them, and the fee-inclusive break-even.

It places no orders and fills in no forms. If you want to act on what it says,
you do that yourself in the Kalshi UI.

## Install

1. Start the bot's local server (the extension is useless without it):

   ```bash
   python -m btcbot serve
   ```

2. Open `chrome://extensions`, turn on **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Open `https://kalshi.com`. The panel appears bottom-right.

The dot in its header is grey when idle, green while paper trading is running,
amber when the server is up but idle, and red when it cannot reach the server.

## How it talks to the bot

The content script cannot fetch `http://127.0.0.1` from an `https://` page —
that is mixed content, and Kalshi's CSP would block it regardless. So the
service worker does the fetching (it holds the `host_permissions` and is not
subject to the page's CSP) and hands results back over `runtime.sendMessage`.

On the server side, CORS is granted only to `chrome-extension://` origins, so
an ordinary web page you happen to have open cannot read your positions off
localhost.

## What it deliberately does not do

- **Place, modify or cancel orders.** Driving a logged-in exchange session
  through the DOM is fragile and sits badly with Kalshi's terms when an
  official API exists. More to the point, live trading is disabled in this
  project on purpose.
- **Send anything anywhere.** The only network call is to `127.0.0.1:8787`.
- **Read your Kalshi account.** It never touches page state; everything shown
  comes from the bot's own public-market-data view.

## Reading the panel

`BE + fee` is the win rate you would need for the trade to break even *after*
Kalshi's taker fee. When it shows a figure at or above 100% the panel says so
loudly: entry price plus fee has exceeded the $1 payout, so the trade cannot
profit at any win rate. That happens routinely on the heavy favourite late in
a window, which is exactly when it looks most tempting.

`edge` is model minus market. It is only as trustworthy as the volatility
behind it — see `_vol()` in `btcbot/strategies/edge_threshold.py` for why the
model refuses to price at all until it has measured one.
