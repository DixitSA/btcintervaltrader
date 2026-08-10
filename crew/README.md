# Research crew — CrewAI + local Ollama

A read-only research and ops layer. Three agents run btcbot's analysis
commands, quote the numbers, and write a dated digest to `reports/`.

**It does not trade.** There is no code path from an agent to an order, and
that is enforced in `btcbot_tools.py` rather than by instructing the model —
`tests/test_crew_tools.py` pins it in the main test suite.

## Why it is not a strategy

A language model cannot tell you whether a trading rule has edge. The sweep's
t-statistic and deflated Sharpe ratio can, and those are computed by
`btcbot/multiple_testing.py`.

Putting an LLM in the tick loop would also break the property the repo is built
on: `backtest.py` replays recorded snapshots through the *same strategy code*
the live runner executes, which is what makes a backtest here unable to see
information the live bot wouldn't have had. An LLM call is non-deterministic
and non-replayable, so an LLM strategy could never clear `goal.md` Phase 3 or 4
— you would have no way to distinguish it from the 76.6%-win-rate-in-a-no-edge-world
case that the README is about.

What the crew is genuinely good at is the tedious part: running the same checks
every day, quoting them accurately, noticing when the window count crosses 100,
and writing down why a 76% win rate is not a finding. A lab notebook, not an
oracle.

## Install

Separate virtualenv, deliberately. See `requirements.txt` for why.

```bash
cd /opt/btcintervaltrader
python3 -m venv crew/.venv
crew/.venv/bin/pip install -r crew/requirements.txt
```

CrewAI needs Python **>=3.10, <3.14** — fine on Ubuntu 22.04 (3.10) and
24.04 (3.12).

## Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:7b
ollama list
```

On 16–32 GB of RAM with no GPU, a 7–8B model at Q4 is the sweet spot: roughly
5 GB resident, and tens of seconds per agent turn. A full three-agent run takes
**several minutes**. That is fine for a daily digest and useless for anything
interactive — which is another reason this is not in the trading path.

Smaller alternatives if it is tight: `llama3.2:3b`, `qwen2.5:3b`.

## Run it

```bash
crew/.venv/bin/python crew/research_crew.py --model ollama/qwen2.5:7b
```

The `ollama/` prefix is load-bearing — LiteLLM routes on it, and without it the
call goes looking for a hosted provider. The script refuses to start without it.

Output lands in `reports/digest-YYYY-MM-DD.md`.

## Protecting the recorder

This is the real operational risk on a shared box. Ollama will saturate every
core it is given, and the recorder polls every 2 seconds — starve it and you
get gaps in the dataset you are trying to collect, which is the one thing that
is expensive to lose.

Cap Ollama's CPU so the recorder always wins:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo cp deploy/ollama-cpu-limits.conf /etc/systemd/system/ollama.service.d/
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

Then run the digest on a timer rather than continuously:

```bash
sudo cp deploy/btcbot-crew.service deploy/btcbot-crew.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now btcbot-crew.timer
systemctl list-timers btcbot-crew
```

After a run, confirm the recorder did not suffer:

```bash
tail -20 recorder-supervisor.log     # restarts during the crew window?
journalctl -u btcbot-record --since "1 hour ago"
```

If you see restarts lining up with crew runs, lower `CPUQuota` further.

## What the agents can reach

Allowlisted, default-deny, in `btcbot_tools.py`:

| Allowed | Refused |
|---|---|
| `backtest`, `sweep`, `compare-exits`, `hurst`, `calibrate`, `shadow-report` | `live`, `paper`, `record`, `serve`, `shadow-replay`, `simulate` |
| window/snapshot counts, recorder log tail, disk usage | anything not on the list |
| six named repo docs | `.env`, source files, arbitrary paths |

`verify-venue` is available but needs `allow_network=True` explicitly.

Argument handling: no shell, argv built from validated parameters, `--set`
keys and values regex-checked, and `--data-dir` resolved and confined to the
repo. The tests cover injection attempts through each of those.

## Adding web research

There is no web access by default — the crew reasons about *your* recorded
data. If you want genuine market research (news, funding rates, other venues),
add a fetch tool and treat everything it returns as untrusted input to the
model. Note the standing caution in `docs/systematic-trading.md`: a signal you
cannot backtest against your own recorded books is a signal you cannot
evaluate, whatever an agent says about it.
