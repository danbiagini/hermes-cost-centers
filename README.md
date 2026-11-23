# hermes-cost-centers

Cost accounting and budgets for a [Hermes](https://github.com/NousResearch/hermes-agent)
agent fleet.

A **cost center** is what a budget is set on: one Hermes profile by default, or a
group of profiles and other spend sources (proxy keys, spawned coding tools,
subscription runs) that bill to one budget.

**Status: early development.** Nothing here is released yet.

## Planned

1. **Accounting.** A *Budgets* tab in the Hermes dashboard: each cost center's
   spend for the month against its budget, cost per run, and the trend. Unpriced
   usage shows as unpriced, never as $0.
2. **A backstop.** A model endpoint check that refuses calls from a cost center
   whose monthly budget is spent, with an error the agent recognizes as a
   billing limit.
3. **Admission and pacing.** New runs wait when a cost center is ahead of a
   steady monthly pace, so a large backlog can't spend the month in its first
   days.
4. **Spawned tools.** Coding agents a profile starts count against its budget.
5. **Awareness.** An agent can see what it has left and plan around it.

Enforcement runs outside the agent's process: an agent can't turn off its own
budget.

## License

MIT
