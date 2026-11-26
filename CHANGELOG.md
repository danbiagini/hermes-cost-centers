# Changelog

Releases are tagged `vX.Y.Z`. Pin a tag, not a branch: an install should change
only when you choose to take a release.

## 0.1.0 — 2026-11-26

The first release. Everything below is new.

### Accounting
- An evaluator, run every interval by a timer, is the only code that sums spend.
  It writes `state.json`, per-run detail files and, optionally, a `budget_state`
  table. Every other part reads what it writes.
- Ledger sources: `hermes-sessions` (default, no setup), `sql` (your own spend
  table) and `litellm` (experimental).
- Cost centers group sources under a monthly budget; an unset budget is tracked
  and never enforced.
- A pace line (`allowance = budget × (elapsed + headroom_days / days)`) and the
  states `ok`, `wrap_up`, `exhausted` and `unset`.
- The account's target and the date spend crosses it, at the month-to-date
  average or, with `account.projection_days`, a trailing window; a 7-day daily
  series.

### The Budgets tab
- A dashboard tab with each cost center's spend against budget and pace, a daily
  chart, and run detail. It reads only the files the evaluator writes.

### Enforcement (off until switched on)
- `admission`: before a kanban worker starts, block the card when its cost
  center has spent its budget, or hold it when the center is ahead of its pace
  line and the card isn't urgent.
- A resume pass that releases held cards once their center is back on pace, and
  blocked ones when the month turns.
- `backstop`: a check for the model endpoint that refuses calls from an
  exhausted cost center with HTTP 402. Never raises; fails open.
- Each point records what it would have done while its switch is off
  (`admission.jsonl`; `DRY RUN, would refuse` in the log), and the backstop logs
  `REFUSED` when it acts.

### Awareness (the `agent/` plugin)
- `budget_status`, a tool: the cost center's figures and this run's cost,
  subagents included.
- A one-line note at the start of each run, and one mid-run if the state worsens.
  Guidance only. Each note given is logged.
