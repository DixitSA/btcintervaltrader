# Handoff — Session 5

Written for a session picking this up **on the server** (`jarvis`). The previous
session ran in a cloud sandbox that could not reach Kalshi and had no view of
the server; everything about the deploy below came from pasted output, so
verify rather than trust.

## Where the code is

`main`, at the merge of PR #4. Two PRs landed this session, both merged.

New since Session 4:

| Path | What |
|---|---|
| `btcbot/multiple_testing.py` | Šidák-corrected critical t, family-wise p, Deflated Sharpe Ratio. Wired into `sweep` |
| `btcbot/hurst.py`, `btcbot hurst` | R/S analysis of the intra-window path, against a synthetic random-walk null |
| `signals.weighted_mid` / `microprice_up` | Size-weighted mid (Stoikov). `fair_value: mid\|microprice` on the `Strategy` base, default `mid` |
| `docs/systematic-trading.md` | What was taken from awesome-systematic-trading, what was rejected and why, open leads |
| `deploy/` | systemd units for the recorder and the daily digest, plus Ollama CPU limits |
| `crew/` | CrewAI + local Ollama research crew. Read-only, allowlisted, never trades |

Tests: **276 passed**. No existing test weakened. No default behaviour changed —
`fair_value` defaults to `mid` everywhere, so every recorded result still stands.

## Deploy state on jarvis, as of this handoff

Believed true, from pasted output:

- Repo at `/opt/btcintervaltrader`, owned `btcbot:btcbot`
- `saul` added to the `btcbot` group; tree `chmod -R g+rwX`
- `btcbot-record.service` **installed, enabled, running** as `btcbot`
- `.venv` built on **system Python 3.14**
- `verify-venue` **passes from the server** — `up_bid + down_ask = 1.000 OK`
- `data/snapshots-2026-08-10.jsonl` filling

Not yet done:

- [ ] **Capture the wire-format fixture.** Five seconds, closes a documented gap:
      `.venv/bin/python -m btcbot verify-venue --dump fixtures/kalshi.json`
      then `pytest tests/test_fixtures.py -q`. Six tests **skip** until it
      exists; a skip there means the wire format is unconfirmed, not that it
      passed. Public market data only — safe to commit.
- [ ] **Measure the real disk rate** (see correction below)
- [ ] Crew venv — needs Python 3.12, see below
- [ ] Ollama not installed

## Two things the previous session got wrong

**1. The disk estimate was ~10x low.** `QUICKSTART.md` said 30–60 MB/day. That
was derived from the *synthetic* control dataset at ~744 bytes/snapshot. Real
Kalshi books are far deeper — first measurement on jarvis was ~1.17 MB in about
four minutes, extrapolating to roughly **400+ MB/day**, i.e. >10 GB/month
against a directory that **never prunes**. The doc now says 300–500 MB/day
pending a proper measurement:

```bash
S1=$(du -sb data | cut -f1); sleep 600; S2=$(du -sb data | cut -f1)
echo "$(( (S2-S1)*144/1000000 )) MB/day"
```

Replace the figure in `QUICKSTART.md` once measured. A full disk stops
recording silently.

**2. CrewAI does not support Python 3.14.** It requires `>=3.10,<3.14`, and
jarvis's system Python is 3.14, so `pip install -r crew/requirements.txt` will
not resolve. There is no `python3.12` apt package on that release either. Use
`uv`, which fetches a standalone interpreter:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
uv python install 3.12
cd /opt/btcintervaltrader
uv venv --python 3.12 crew/.venv
uv pip install --python crew/.venv/bin/python -r crew/requirements.txt
sudo chown -R btcbot:btcbot crew/.venv     # the timer runs as btcbot
```

`btcbot` itself runs fine on 3.14. `crew/btcbot_tools.py` deliberately invokes
btcbot's own interpreter rather than the crew's, so the two Python versions
never meet.

## The actual critical path

**Window count, not snapshots.** They differ by ~300x and quoting snapshots is
how someone talks themselves into tuning on 19 windows.

```bash
cd /opt/btcintervaltrader && .venv/bin/python -c \
  "from btcbot.recorder import load_dataset; from btcbot.backtest import group_windows; \
   print(len(group_windows(load_dataset('data'))), 'windows')"
```

KXBTC15M runs one open window at a time → **4 windows/hour**. You need ≥100, so
**~25 hours of clean uptime minimum**; budget two to three days for restarts and
gaps. Gaps show up in `recorder-supervisor.log`, not in the data.

Until that count clears 100, `sweep`, `hurst` and the crew's digest will all
correctly report that there is nothing to analyse. That is the system working.

## Before adding Ollama to the box

The recorder polls every 2 seconds and its collection is the long pole. Ollama
will saturate every core it is given, and a starved recorder means gaps in the
dataset you are paying 25 hours to build. Install the CPU limits **first**:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp deploy/ollama-cpu-limits.conf /etc/systemd/system/ollama.service.d/
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

Then after the first crew run, check whether it cost you anything:

```bash
tail -20 recorder-supervisor.log            # restarts lining up with crew runs?
journalctl -u btcbot-record --since "1 hour ago"
```

## Carried over from Session 4 — still true

- Settlement resolves on BTC spot vs strike (`runner.py:_settle_expired`)
- Net-edge Kelly (`risk.py:147`)
- `learner.py` — Beta-Binomial calibrator + `outcomes.jsonl`; `learning.enabled:
  false` (opt-in). `python -m btcbot calibrate` inspects the curve
- Extension needs `python -m btcbot serve` + native host registered. **Desktop
  only — no role on the headless server**
- `goal.md` Phase 1b (fee formula vs real fills) still unverified
- `goal.md` Phase 1c (orderbook derivation) — **now verified on jarvis**, see above
- Phases 3–5 blocked on window count

## Rules

Never enable live trading. Never commit `.env`. Never weaken or delete a test.
Ask before merging to `main`.
