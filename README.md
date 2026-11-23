# hermes-cost-centers

Cost accounting and budgets for a [Hermes](https://github.com/NousResearch/hermes-agent)
agent fleet.

A **cost center** is what a budget is set on: one Hermes profile by default, or a
group of profiles and other spend sources (proxy keys, spawned coding tools,
subscription runs) that bill to one budget.

**Status: early development (phase 1, accounting).** It measures and shows
spend. It doesn't enforce anything yet.

## What it does today

- **A Budgets tab** in the Hermes dashboard, after Analytics. It shows each
  cost center's spend for the month against its budget and pace line, its
  state (ok, wrap up, exhausted, no budget), cost per run with subagents rolled
  into the run that started them, and a daily trend. It also shows spend from
  sources no cost center claims, so nothing is dropped.
- **An evaluator** that sums spend once and writes a small budget state. The
  tab reads that state, and so will every enforcement point later, so they
  can't disagree.
- **Unpriced usage shows as unpriced, never as $0.** A cost center with
  unpriced usage is marked incomplete, and its figure is a floor.
- **A pace line.** By any point in the month, a cost center may have spent its
  pro-rata share of the budget plus some days of headroom (5 by default).
  Unused allowance carries forward. Today the tab only shows the line;
  holding new runs that are ahead of it comes in a later phase.

## Planned

1. ~~Accounting~~ (this release)
2. **A backstop.** A model-endpoint check that refuses calls from a cost center
   whose monthly budget is spent, with an error the agent treats as a billing
   limit.
3. **Admission and pacing.** New runs wait while a cost center is ahead of
   pace, so a large backlog can't spend the month in its first days.
4. **Spawned tools.** Coding agents a profile starts count against its budget.
5. **Awareness.** An agent can see what it has left and plan around it.

Enforcement will run outside the agent's process, so an agent can't turn off
its own budget.

## Install

Install it in the **launch (root) profile only**. The tab is the operator's
view: it shows every cost center, whichever profile is selected.

```sh
# 1. The plugin (dashboard tab and router)
hermes plugins install danbiagini/hermes-cost-centers
# say yes to enabling it (it must be in plugins.enabled for the tab's backend to load)

# 2. The evaluator, run as a user that can read every profile's state.db
pip install 'git+https://github.com/danbiagini/hermes-cost-centers'
```

Run the evaluator from a timer every minute, or let it loop:

```sh
hermes-cost-centers evaluate --loop      # or: evaluate (once) from cron/systemd
hermes-cost-centers status               # the last written state
hermes-cost-centers explain <cost-center>
```

If the evaluator stops, the tab shows a red "out of date" banner after five
intervals. It never goes quiet.

## Configure

`<HERMES_HOME>/hermes-cost-centers/budgets.yaml` (or set
`HERMES_COST_CENTERS_CONFIG`). Budgets are the operator's decision, so the
evaluator refuses a file that anyone but its owner can write.

```yaml
timezone: UTC                    # when "the 1st" begins
account:
  target_usd: 150                # the whole account's monthly target (shown, never enforced)
cost_centers:
  research:                      # one profile: sources defaults to [research]
    monthly_usd: 50
  support:                       # several profiles and sources under one budget
    sources: [support-triage, support-writer]
    monthly_usd: 80
    pace: { headroom_days: 3 }   # or `pace: off`
  shared:
    sources: [default]
    monthly_usd: null            # unset: tracked, never enforced (not the same as 0)
pace_default: { on: true, headroom_days: 5 }
thresholds: { wrap_up: 0.80 }
ledger: { source: hermes-sessions }
state: { path: /var/lib/hermes-cost-centers/state.json }
```

### Ledger sources

Exactly one is authoritative per install.

- **`hermes-sessions`** (default): reads every profile's session database,
  read-only. It uses Hermes's own cost where Hermes knows it, and otherwise
  prices tokens from the bundled price table. It works with no setup, but it
  only sees spend that Hermes sessions record.
- **`sql`**: your own spend table. Give a connection string and a query that
  takes `:since` and `:until` and returns `ts, source, model, input_tokens,
  output_tokens, cache_read_tokens, cache_write_tokens, cost_usd`. Supports
  `sqlite:///…` and `postgresql://…` (`pip install 'hermes-cost-centers[postgres]'`).
- **`litellm`** (experimental): LiteLLM's spend logs API, for a proxy that runs its
  database. So far it's tested only against a stubbed response.

```yaml
ledger:
  source: sql
  url_env: SPEND_DB_URL
  query: >
    SELECT ts, source, model, input_tokens, output_tokens,
           cached_in AS cache_read_tokens, cache_in AS cache_write_tokens, cost_usd
    FROM usage_log WHERE ts >= :since AND ts < :until
```

### Prices

The bundled table (`hermes_cost_centers/prices.json`) lists only models whose
input, output, cache-read and cache-write rates are all known. Add or override
models, including free local ones, in `budgets.yaml`:

```yaml
pricing:
  models:
    local: { input: 0, output: 0, cache_read: 0, cache_write: 0 }
```

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
# The Postgres tests run when HCC_TEST_PG_URL points at a scratch database:
HCC_TEST_PG_URL=postgresql://postgres:test@127.0.0.1:55432/postgres .venv/bin/pytest
```

## License

MIT
