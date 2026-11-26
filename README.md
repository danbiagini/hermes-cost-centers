# hermes-cost-centers

Cost accounting and budgets for a [Hermes](https://github.com/NousResearch/hermes-agent)
agent fleet.

A **cost center** is what a budget is set on: one Hermes profile by default, or a
group of profiles and other spend sources (proxy keys, spawned coding tools,
subscription runs) that bill to one budget.

**Status: early development.** Accounting works; enforcement (admission, pacing
and the backstop) is built and ships switched off, in dry-run.

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
  Unused allowance carries forward. Admission holds new runs that are ahead of
  it (see Enforce).
- **Awareness.** An optional plugin per profile lets the agent see its own
  budget and this run's spend, so it can size its work to what's left.

## Planned

1. ~~Accounting~~ (this release)
2. ~~A backstop~~ and 3. ~~admission and pacing~~: built, shipped switched
   off (see Enforce).
4. **Spawned tools.** Coding agents a profile starts count against its budget.
5. ~~Awareness~~: built (see Awareness).

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

The evaluator writes two things next to each other: the budget state
(`state.path`) and each cost center's run detail (`<state dir>/runs/<YYYY-MM>/`).
The dashboard reads only those files. It never opens the ledger, so a ledger
credential (for the `sql` or `litellm` source) belongs to the evaluator alone,
and the dashboard needs read access to the state directory and nothing else.

## Enforce

Three enforcement points, all reading what the evaluator wrote. **Each one runs
in dry-run until you switch it on:** it records what it would have done, so you
can watch the decisions against the accounting first.

```yaml
enforce: {admission: false, backstop: false}   # flip each to true when ready
```

- **Admission**, before a kanban card's run starts:
  `hermes-cost-centers admit --profile P --board B --task T` exits 0 to let the run
  start, or 3 after it has **blocked** the card (the monthly budget is spent;
  reason `[cost-centers] …`, block kind `capability`) or **held** it (ahead of
  pace; `scheduled`, reason `[cost-centers:pace] …`, no alert). Call it from
  whatever spawns your workers, and on 3 exit without starting one. Every
  decision is appended to `<state dir>/admission.jsonl`. Any error means allow.
- **The resume pass**, each evaluator cycle (`kanban: {resume: true}`): Hermes's
  `scheduled` state has no wake-up time, so the evaluator releases the cards it
  held once their cost center is back on pace, and its budget blocks on the 1st
  or when you raise the budget. It touches only cards whose latest hold reason
  starts with one of its two markers.
- **The backstop**, at the model endpoint: with `state.sql` set, the evaluator
  also writes a `budget_state` table (one row per source; DDL in
  `hermes_cost_centers/sql_state.py`). `hermes_cost_centers.backstop.Backstop`
  looks a call's source up there and, if its cost center is exhausted, refuses
  with HTTP 402 and *"budget exhausted: … (hard billing limit)"*. It never raises
  and fails open on a missing, stale or unreadable row.

  ```python
  # LiteLLM custom_auth, after you've resolved the caller's source:
  from hermes_cost_centers.backstop import Backstop, HTTP_STATUS
  BACKSTOP = Backstop(lambda: psycopg2.connect(DB_URL))
  d = BACKSTOP.check(source)
  if not d.allow:
      raise ProxyException(message=d.message, type="budget_exceeded", param=None, code=HTTP_STATUS)
  ```

**Ship the backstop and admission together.** Hermes treats a 402 as a billing
error and doesn't retry the call, but a kanban worker that stops on one exits as
rate-limited, and its card is requeued about every 6 minutes without counting a
failure. Admission is what blocks that card at its next start.

**Urgent cards** may run ahead of pace, never past the monthly budget:

```yaml
urgent:
  created_by: ["chat:*", "operator"]    # card creators who are the operator (fnmatch)
  answered_by: [operator]               # a comment from these just before the latest
                                        # unblock = the operator answered the card
  title_markers: ["[time-critical]"]    # Hermes boards have no tags
```

## Awareness

`agent/` is a second, optional Hermes plugin, installed in each **budgeted
profile** (not the root profile). It adds:

- **`budget_status`**, a tool: the cost center's spend, budget and what's left,
  what's available now under the pace line, and what this run has spent so far,
  with subagents and compression continuations rolled in.
- **A one-line note** at the start of each run, for example
  `[budget] research: $20.00 of $50.00 spent this month, $30.00 left. $15.00
  available now under the pace line.` At `wrap_up` it asks the agent to
  checkpoint; when exhausted, to record where it is and stop starting new work.
- **One note mid-run**, added to the next tool result, if the state gets worse
  while the run is going (it reaches `wrap_up`, falls ahead of pace or is
  exhausted). Once per change.

It is **guidance only**, and says so to the model: enforcement is admission and
the backstop, outside the agent. It uses only the standard library and reads two
things, failing silently on either:

- the evaluator's state file (`HERMES_COST_CENTERS_STATE`, default
  `/var/lib/hermes-cost-centers/state.json`), which the profile's user must be
  able to read. The evaluator writes it world-readable, and publishes its price
  table in it so the plugin prices runs the same way;
- the profile's own `state.db`, for this run's tokens.

The agent's source is its profile name, or `HERMES_COST_CENTERS_SOURCE` when the
ledger knows it by another name.

Hermes adds a note to the model's input without saving it to the session's
history, so the plugin logs one line each time it gives one, to the profile's
`agent.log`:
`budget note given: kind=start session=<id> cost_center=research state=ok ahead_of_pace=False`
(`kind` is `start`, `changed` or `mid_run`). A first turn that gets no note logs
`budget note not given` with the reason, such as a stale state file.

Because it is guidance, the plugin needs no protection from the agent it runs
in. A profile that owns its own files can edit or disable it, just as it can
edit its own `config.yaml`. That changes only what the agent is told, never
what admission or the backstop allow.

```sh
cp -r agent <profile home>/plugins/hermes-cost-centers-agent
# in that profile's config.yaml:
#   plugins:
#     enabled: [hermes-cost-centers-agent]
#   platform_toolsets:
#     cli: [hermes-cli, cost_centers]
```

**Name the `cost_centers` toolset in the profile's `platform_toolsets.cli`.**
A kanban dispatcher pins each worker's toolsets from the plugins *its own*
process loaded; when the dispatcher runs in a profile without this plugin, the
tool is left out of every worker unless the profile lists it by name. Interactive
sessions in the profile get it either way.

## Configure

`<HERMES_HOME>/hermes-cost-centers/budgets.yaml` (or set
`HERMES_COST_CENTERS_CONFIG`). Budgets are the operator's decision, so the
evaluator refuses a file that anyone but its owner can write.

```yaml
timezone: UTC                    # when "the 1st" begins
account:
  target_usd: 150                # the whole account's monthly target (shown, never enforced)
  projection_days: 7             # project the crossing at the last 7 days' rate; omit for the month average
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

The state's `account` carries the projection (its basis, the daily rate and the
figure at month end) and spend for each of the last 7 days. A script that
reports spend can read these instead of summing the ledger a second time. To
fill them, the evaluator reads the ledger from 7 days (or `projection_days`, if
longer) before now, and only the current month counts toward spend.

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
