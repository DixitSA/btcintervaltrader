# Quickstart — running this on your own machine

Works on Windows, macOS and Linux. You need **Python 3.10+** and git.

---

## 1. Get the code

The work lives on a branch that isn't merged yet, so clone and check it out:

```bash
git clone https://github.com/DixitSA/btcintervaltrader.git
cd btcintervaltrader
git checkout claude/btc-prediction-market-bot-q4wjej
```

<details>
<summary>Don't have git? (click)</summary>

Download the branch as a ZIP from GitHub — **Code → Download ZIP** with the
branch selected — then unzip it and `cd` into the folder. Git is the better
option since you'll want to pull updates.
</details>

---

## 2. Run setup

One command, same on every platform:

```bash
python scripts/setup.py
```

On macOS/Linux you may need `python3` instead of `python`.

It creates a virtualenv in `.venv`, installs dependencies, creates `.env` from
the template, and runs an **offline** end-to-end check (simulate → backtest) so
you know the pipeline works before any network is involved. Safe to re-run; it
won't overwrite an existing `.env`.

Then activate the environment:

| Platform | Command |
|---|---|
| Windows (PowerShell) | `.venv\Scripts\Activate.ps1` |
| Windows (cmd) | `.venv\Scripts\activate.bat` |
| macOS / Linux | `source .venv/bin/activate` |

> **Windows PowerShell blocking the script?** Run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, then retry.

You'll need to activate in each new terminal. You'll know it worked when your
prompt shows `(.venv)`.

---

## 3. Check you can reach Kalshi

```bash
python -m btcbot verify-venue
```

This is the step that failed in the cloud environment this was built in — that
sandbox blocked the venue. On your own machine it should just work.

Expect to see the fee model, a list of open `KXBTC15M` markets, a real
orderbook, and a `sanity: up_bid + down_ask = 1.000 OK` line. **No credentials
needed** — Kalshi market data is public.

While you're here, capture a fixture so the parsers get validated against real
payloads instead of documentation:

```bash
python -m btcbot verify-venue --dump fixtures/kalshi.json
python -m pytest tests/test_fixtures.py -q
```

Those 6 tests skip until the file exists. If they pass, the wire format is
confirmed. Commit the file — it's public market data only.

---

## 4. Record data — this is the long part

```bash
python scripts/record_forever.py
```

Leave it running for **days**, not hours. The supervisor restarts the recorder
through crashes and network drops with backoff, and logs every restart to
`recorder-supervisor.log` so gaps in your dataset are visible rather than
silent.

Snapshots land in `data/` as one JSONL file per UTC day. Check progress:

| Platform | Command |
|---|---|
| Windows (PowerShell) | `Get-ChildItem data` |
| macOS / Linux | `ls -la data/ && wc -l data/*.jsonl` |

### Keeping it running when you close the terminal

| Platform | How |
|---|---|
| macOS / Linux | `nohup python scripts/record_forever.py &` — or use `tmux` / `screen` |
| Windows | `pythonw scripts\record_forever.py` — or Task Scheduler with "Run whether user is logged on or not" |

Your machine must stay awake. Disable sleep, or the recording stops with it.

---

## 5. Test the rule on your own data

Once you have a few days:

```bash
python -m btcbot sweep            # the $500k volume rule, all four directions
python -m btcbot compare-exits    # what your stop loss actually costs
python -m btcbot backtest --strategy edge_threshold
```

Read the **t-statistic**, not the ROI and not the win rate. `|t| < 2` means the
result is indistinguishable from no edge — see the README for why win rate is
the most misleading number here.

---

## 6. Paper trade

```bash
python -m btcbot paper
```

Simulated money against live books, with full portfolio tracking and stop
losses. Run this for at least as long as you recorded before considering
anything else.

---

## 7. Live — only if 5 and 6 justified it

```bash
pip install cryptography          # already installed by setup.py
```

Add your Kalshi API key ID and private key path to `.env`, set
`execution.backend: venue` in `config.yaml`, then:

| Platform | Command |
|---|---|
| Windows (PowerShell) | `$env:BTCBOT_I_UNDERSTAND_REAL_MONEY="yes"` |
| macOS / Linux | `export BTCBOT_I_UNDERSTAND_REAL_MONEY=yes` |

```bash
python -m btcbot live
```

Live needs **both** `mode: live` and that environment variable, so no config
typo alone can move real money.

> **Place your first order manually through Kalshi's website, not the bot.**
> The order-placement path has never run against a real server. Confirm one
> minimum-size order appears correctly, then let the bot place one and check it
> lands in the UI before leaving it unattended.

---

## Common problems

| Symptom | Fix |
|---|---|
| `python: command not found` | Use `python3`, or install from python.org (tick "Add to PATH" on Windows) |
| `No module named btcbot` | Activate the venv, and run from the repo root |
| `No module named venv` (Linux) | `sudo apt install python3-venv` |
| PowerShell "running scripts is disabled" | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `verify-venue` says 403 / connection failed | Corporate VPN or firewall blocking Kalshi — try another network |
| `No open markets found` | Check `markets.slug_prefixes` in `config.yaml` matches a live series |
| Backtest says "No trades were taken" | Usually correct. Read the rejection reasons it prints |
| `cryptography` fails to build | Upgrade pip: `python -m pip install --upgrade pip` |

---

## Layout

```
btcintervaltrader/
├── btcbot/              the bot
├── scripts/setup.py     this setup
├── scripts/record_forever.py
├── config.yaml          trading parameters (safe to commit)
├── .env                 YOUR CREDENTIALS -- never commit
├── data/                recorded snapshots (gitignored)
└── fixtures/            captured API responses for parser tests
```

`.env` and `data/` are gitignored. Keep it that way — `.env` holds your private
key path, and a leaked Kalshi key means someone else can trade your account.
