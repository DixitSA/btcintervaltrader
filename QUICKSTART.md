# Quickstart — running this on your own machine

Works on Windows, macOS and Linux. You need **Python 3.10+** and git.

---

## 1. Get the code

```bash
git clone https://github.com/DixitSA/btcintervaltrader.git
cd btcintervaltrader
```

**Running headlessly on a Linux server?** Do steps 1–3 here, then jump to
[Running on a headless server](#running-on-a-headless-server) — `nohup` in
step 4 is not what you want on a machine you will disconnect from.

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

## Running on a headless server

Ubuntu/Debian. The goal is the multi-day `record` run surviving disconnects,
crashes and reboots without you watching it.

### Prerequisites

Ubuntu ships Python without `venv`, and the setup script needs it:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip git
python3 --version          # needs 3.10+
```

### Check the clock before anything else

This bot reasons about seconds-to-expiry and writes one data file per **UTC**
day. A server whose clock has drifted produces snapshots timestamped wrongly
relative to the windows they describe, and nothing downstream will flag it:

```bash
timedatectl                        # want "System clock synchronized: yes"
sudo timedatectl set-ntp true      # if it is not
```

Leave the server on UTC. Don't set a local timezone — the ticker names are ET
and the data files are UTC, and adding a third timezone helps nobody.

### Check the server can actually reach Kalshi

Do this **before** setting up the service. Run it from the server itself:

```bash
python -m btcbot verify-venue
```

Kalshi is a US-regulated venue, and datacenter IP ranges are a plausible thing
for it to refuse even when your laptop works fine. If this returns 403 or a
connection failure from the server but works at home, that is the port blocked
at the first step — find out now, not after you've configured systemd.

### Install

```bash
sudo useradd --system --create-home --home-dir /opt/btcintervaltrader btcbot
sudo -u btcbot git clone https://github.com/DixitSA/btcintervaltrader.git /opt/btcintervaltrader
cd /opt/btcintervaltrader
sudo -u btcbot python3 scripts/setup.py
```

### Run the recorder as a service

```bash
sudo cp deploy/btcbot-record.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcbot-record
```

`enable` is the part that matters — it survives reboots. Then:

```bash
systemctl status btcbot-record
journalctl -u btcbot-record -f              # live output
journalctl -u btcbot-record --since today   # what happened overnight
```

Use `systemd`, not `nohup`. `nohup` dies with the reboot you will eventually do
and gives you nothing to check afterwards.

Note the two layers of supervision, which is deliberate rather than redundant:
`record_forever.py` restarts the recorder with backoff and logs each restart to
`recorder-supervisor.log`, so **gaps in your dataset stay visible**; systemd
restarts the supervisor, covering reboots and the supervisor itself dying.

### Watch it accumulate

```bash
ls -la /opt/btcintervaltrader/data/
wc -l /opt/btcintervaltrader/data/*.jsonl
tail -5 /opt/btcintervaltrader/recorder-supervisor.log
```

The number that actually gates your analysis is **windows**, not snapshots —
they differ by ~300x, and it is the mistake that sends people into parameter
tuning on 19 windows:

```bash
cd /opt/btcintervaltrader && sudo -u btcbot .venv/bin/python -c \
  "from btcbot.recorder import load_dataset; from btcbot.backtest import group_windows; \
   print(len(group_windows(load_dataset('data'))), 'windows')"
```

You want **≥100** before tuning anything. At 4 windows/hour that is a bit over a
day of clean uptime; budget several days for restarts and gaps.

### Disk

At `poll_seconds: 2.0`, expect roughly **300–500 MB/day** — call it **10–15
GB/month**. Real Kalshi orderbooks are deep, so a snapshot runs to several KB,
not the ~750 bytes the synthetic control dataset suggests. Measure your own
rather than trusting that range:

```bash
S1=$(du -sb data | cut -f1); sleep 600; S2=$(du -sb data | cut -f1)
echo "$(( (S2-S1)*144/1000000 )) MB/day"
```

`data/` is **never pruned** and a full disk stops recording silently from your
perspective — the service stays green while collecting nothing. On a small VPS
this is the constraint that bites first. Check `df -h` periodically, and
consider archiving older `snapshots-*.jsonl` once they have been backtested.

### Reaching the web panel

`python -m btcbot serve` binds `127.0.0.1` deliberately. **Do not change that
to 0.0.0.0 to reach it remotely** — the panel has no authentication and drives a
trading bot. Tunnel over SSH instead, from your laptop:

```bash
ssh -L 8787:127.0.0.1:8787 youruser@yourserver
```

Then open <http://127.0.0.1:8787> locally. The browser extension and native
messaging host are desktop-only and have no role on a headless server.

### Optional: the research crew

`crew/` runs three CrewAI agents on a local Ollama that execute btcbot's
read-only analysis commands and write a daily digest to `reports/`. It reads
and reports; it never trades, and the boundary is enforced in code rather than
by prompt. Setup, model sizing and the CPU limits that keep inference from
starving the recorder are in [crew/README.md](crew/README.md).

The one thing to get right: cap Ollama's CPU before enabling the timer. The
recorder polls every 2 seconds, and a saturated box means gaps in the dataset
you are trying to collect.

### Credentials

`record` and `paper` need **no API key** — Kalshi market data is public. Only
live trading does. If you do add credentials later, `.env` is gitignored and
must stay that way; `chmod 600 .env` and keep the RSA private key readable only
by the `btcbot` user.

---

## Common problems

| Symptom | Fix |
|---|---|
| `python: command not found` | Use `python3`, or install from python.org (tick "Add to PATH" on Windows) |
| `No module named btcbot` | Activate the venv, and run from the repo root |
| `No module named venv` (Linux) | `sudo apt install python3-venv` |
| systemd: `status=217/USER` | The `btcbot` user doesn't exist. `sudo useradd --system --shell /usr/sbin/nologin --home-dir /opt/btcintervaltrader btcbot` then `sudo chown -R btcbot:btcbot /opt/btcintervaltrader` |
| systemd: `status=203/EXEC` | Wrong path in `ExecStart=`, or `.venv` was never created — run `scripts/setup.py` **as `btcbot`** |
| `PermissionError: 'data'` running by hand | You're running as yourself against a `btcbot`-owned tree. Use the service, or `sudo usermod -aG btcbot $USER && sudo chmod -R g+rwX /opt/btcintervaltrader && newgrp btcbot` |
| systemd: `Read-only file system` writing `data/` | `ReadWritePaths=` doesn't match your install dir |
| Service runs but `data/` stays empty | Check `journalctl -u btcbot-record` — usually `verify-venue` would have failed too |
| `Unable to locate package python3.12` (for the crew) | Your release is newer than 3.12. Don't fight apt — use `uv`, see [crew/README.md](crew/README.md) |
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
