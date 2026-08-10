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

CrewAI needs Python **>=3.10, <3.14**. Check yours first — if the system Python
is 3.14 or newer, `pip install` will not resolve, and on a release that new
there is usually no `python3.12` apt package either.

```bash
python3 --version
```

**On 3.10–3.13:**

```bash
cd /opt/btcintervaltrader
python3 -m venv crew/.venv
crew/.venv/bin/pip install -r crew/requirements.txt
```

**On 3.14+**, use `uv` — it fetches a standalone interpreter without touching
system packages, and it is what CrewAI uses for dependency management anyway:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh && source $HOME/.local/bin/env
uv python install 3.12
cd /opt/btcintervaltrader
uv venv --python 3.12 crew/.venv
uv pip install --python crew/.venv/bin/python -r crew/requirements.txt
sudo chown -R btcbot:btcbot crew/.venv    # btcbot-crew.service runs as btcbot
```

btcbot itself is fine on 3.14. `btcbot_tools.py` invokes btcbot's *own*
interpreter rather than the crew's, so the two versions never meet — the
separation was for dependency isolation and buys version isolation for free.

## Ollama and per-agent models

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.1        # ~4.7 GB
ollama pull phi3            # ~2.2 GB
ollama list
```

Each agent gets its own model, because their jobs are not equally hard:

| Agent | Default | Why |
|---|---|---|
| Data Collection Steward | `ollama/phi3` (3.8B) | Reads three tool outputs and repeats the numbers. Almost no reasoning |
| Quantitative Analyst | `ollama/llama3.1` (8B) | Runs commands and quotes them; needs to follow a conditional instruction about sample size |
| Research Skeptic | `ollama/llama3.1` (8B) | Carries the argument and writes the digest. The one job worth spending tokens on |

Override per agent, or force one model everywhere:

```bash
crew/.venv/bin/python crew/research_crew.py \
    --model-steward ollama/phi3 \
    --model-analyst ollama/llama3.1 \
    --model-skeptic ollama/llama3.1

crew/.venv/bin/python crew/research_crew.py --model ollama/llama3.1
```

Also settable as `BTCBOT_CREW_MODEL_STEWARD` / `_ANALYST` / `_SKEPTIC`, which is
how `deploy/btcbot-crew.service` sets them.

### The caveat that matters

**Small models are unreliable at structured tool calling**, and every number in
the digest arrives through a tool call. If the steward starts reporting window
counts without calling `count_recorded_windows`, it is confabulating — and that
failure is silent, and it is the exact failure this crew exists to prevent.

Watch the first few runs with `verbose=True` output and confirm you see the tool
invocations. If phi3 is skipping them or malforming arguments, promote it:

```bash
--model-steward ollama/llama3.1
```

The cost is a few minutes per run. Worth it — a digest that quotes invented
numbers is worse than no digest.

### What about `qwen2.5-coder`?

It has no role in *this* crew. Nothing here generates code; the agents run
fixed commands and write prose. It is an excellent model for the Unity/Godot/JS
work it is usually recommended for, and pulling it costs you 4.5 GB of RAM this
crew will never use. If you later add an agent that writes code, the
`--model-*` flags are the place to wire it in.

### Speed on a CPU-only box

On a 6-core i5 with `CPUQuota=400%`, expect roughly 4–8 tokens/second on an 8B
model and noticeably faster on phi3. A full three-agent run is **several
minutes to half an hour**. Fine for a daily digest, useless interactively —
which is another reason this is not in the trading path.

Memory: llama3.1 + phi3 resident together is about 7 GB, comfortable in 24 GB
alongside the recorder. `OLLAMA_MAX_LOADED_MODELS=3` in the drop-in keeps both
loaded across agent handoffs; set it lower than the number of distinct models
and Ollama reloads between every agent, which on CPU is minutes of pure waste.

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
