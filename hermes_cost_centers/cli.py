"""``hermes-cost-centers``: evaluate, status, explain."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import config as config_mod
from . import admission as admission_mod
from . import detail as detail_mod
from . import resume as resume_mod
from . import sql_state
from . import runs as runs_mod
from . import state as state_mod
from .adapters import get_adapter
from .pricing import PriceTable
from . import evaluator
from .evaluator import Month, evaluate


def _load(args) -> config_mod.Config:
    return config_mod.load(Path(args.config) if args.config else None,
                           check_perms=not args.skip_permission_check)


def evaluate_once(cfg: config_mod.Config, now: datetime | None = None,
                  write_detail: bool = False) -> dict:
    """Evaluate the current month. With ``write_detail``, also write each cost
    center's run detail, and finish the previous month if it isn't yet."""
    now = now or evaluator.utcnow()
    month = Month.containing(now, cfg.timezone)
    adapter = get_adapter(cfg)
    rows = list(adapter.rows(evaluator.lookback_start(cfg, month, now), now))
    state = evaluate(cfg, rows, now, ledger_name=adapter.name)
    # The agent plugin prices its own run's tokens with the same table, so the
    # operator's overrides reach it without a second copy.
    state["prices"] = PriceTable.from_config(cfg.pricing).published()
    if write_detail:
        detail_mod.write(cfg.runs_dir, detail_mod.build(cfg, rows, month, now))
        prev = Month.containing(month.start - timedelta(seconds=1), cfg.timezone)
        if detail_mod.needs_final_write(cfg.runs_dir, cfg, prev):
            prev_rows = list(adapter.rows(prev.start, prev.end))
            detail_mod.write(cfg.runs_dir, detail_mod.build(cfg, prev_rows, prev, now))
    return state


def publish(cfg: config_mod.Config, state: dict) -> list[str]:
    """Write the state everywhere its readers look, then release held cards.

    The file is written first, so the tab stays current even if a later step
    fails. Returns the failures; each is a fault, and the cycle exits non-zero.
    """
    state_mod.write(cfg.state_path, state)
    faults = []
    if cfg.state.get("sql"):
        try:
            sql_state.write(cfg.state["sql"], state, cfg.enforce.backstop)
        except Exception as exc:
            faults.append(f"budget table not written: {exc}")
    if cfg.kanban.get("resume", False):
        try:
            for r in resume_mod.resume(cfg, state):
                print(f"hermes-cost-centers: released {r['task']} on {r['board']} ({r['marker']})")
        except Exception as exc:
            faults.append(f"resume pass failed: {exc}")
    return faults


def cmd_evaluate(args) -> int:
    cfg = _load(args)
    while True:
        rc = 0
        try:
            if args.stdout:
                state = evaluate_once(cfg)
                json.dump(state, sys.stdout, indent=1, sort_keys=True)
                print()
            else:
                state = evaluate_once(cfg, write_detail=True)
                for fault in publish(cfg, state):
                    print(f"hermes-cost-centers: {fault}", file=sys.stderr)
                    rc = 1
        except Exception as exc:
            # A failed cycle leaves the last state in place; it goes stale and
            # readers fail open. Exit non-zero so a scheduler sees the fault.
            print(f"hermes-cost-centers: evaluate failed: {exc}", file=sys.stderr)
            rc = 1
        if not args.loop:
            return rc
        time.sleep(cfg.interval_seconds)


# Exit codes for `admit`, read by the spawn wrapper.
ADMIT_START, ADMIT_HELD = 0, 3


def cmd_admit(args) -> int:
    """May this card's run start? 0 = start it; 3 = the card was held or blocked,
    so don't start a worker. Fails open: any error prints and returns 0."""
    try:
        cfg = _load(args)
        may_start, d, acted = admission_mod.admit(cfg, args.profile, args.board, args.task)
        mode = "" if cfg.enforce.admission else " (dry run)"
        verb = {"allow": "allow", "hold": "hold", "block": "block"}[d.action]
        print(f"hermes-cost-centers admit{mode}: {verb} {args.task} for {args.profile}: {d.reason}")
        return ADMIT_START if may_start else ADMIT_HELD
    except Exception as exc:
        print(f"hermes-cost-centers admit: failed open: {exc}", file=sys.stderr)
        return ADMIT_START


def _money(x) -> str:
    return "—" if x is None else f"${x:,.2f}"


def cmd_status(args) -> int:
    cfg = _load(args)
    state = state_mod.read(cfg.state_path)
    if state is None:
        print(f"no state at {cfg.state_path}; run `hermes-cost-centers evaluate`", file=sys.stderr)
        return 1
    if args.json:
        json.dump(state, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    age = state_mod.age_seconds(state)
    stale = " STALE" if state_mod.is_stale(state) else ""
    acct = state["account"]
    print(f"{state['month']['label']}  ledger={state['ledger']}  "
          f"generated {int(age or 0)}s ago{stale}")
    print(f"account: {_money(acct['spend_usd'])} of target {_money(acct['target_usd'])}"
          + (" (incomplete)" if acct["incomplete"] else "")
          + (f", crosses target {acct['crosses_target_at']}" if acct.get("crosses_target_at") else ""))
    print()
    print(f"{'cost center':<20} {'spend':>10} {'budget':>10} {'used':>6}  {'state':<10} pace")
    for name, c in sorted(state["cost_centers"].items()):
        used = "" if c["used_fraction"] is None else f"{c['used_fraction']:.0%}"
        pace = ""
        if c["pace"]:
            pace = (f"held until {c['pace']['eligible_at']}" if c["pace"]["ahead_of_pace"]
                    else f"on pace (allowance {_money(c['pace']['allowance_usd'])})")
        flag = " *" if c["incomplete"] else ""
        print(f"{name:<20} {_money(c['spend_usd']):>10} {_money(c['budget_usd']):>10} {used:>6}  "
              f"{c['state']:<10} {pace}{flag}")
    if any(c["incomplete"] for c in state["cost_centers"].values()):
        print("\n* has unpriced usage: the spend shown is a floor")
    if state["unattributed"]:
        print("\nunattributed sources (map them to a cost center in budgets.yaml):")
        for src, u in sorted(state["unattributed"].items()):
            print(f"  {src:<20} {_money(u['spend_usd'])}  ({u['rows']} rows)")
    return 0


def cmd_explain(args) -> int:
    cfg = _load(args)
    center = cfg.cost_centers.get(args.cost_center)
    if center is None:
        print(f"no cost center {args.cost_center!r}; have {sorted(cfg.cost_centers)}", file=sys.stderr)
        return 1
    now = evaluator.utcnow()
    month = Month.containing(now, cfg.timezone)
    rows = [r for r in get_adapter(cfg).rows(month.start, now) if r.source in center.sources]
    state = evaluate(cfg, rows, now)["cost_centers"][center.name]
    if args.json:
        json.dump({"state": state, "runs": [r.as_dict() for r in runs_mod.build(rows)]},
                  sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    print(f"{center.name}: sources {', '.join(center.sources)}")
    print(f"  spend {_money(state['spend_usd'])} of {_money(state['budget_usd'])} "
          f"({state['state']}), {state['rows']} rows, {state['unpriced_rows']} unpriced")
    by_model: dict[str, list[float]] = {}
    for r in rows:
        m = by_model.setdefault(r.model, [0.0, 0])
        m[0] += r.cost_usd or 0.0
        m[1] += 0 if r.priced else 1
    for model, (usd, unpriced) in sorted(by_model.items(), key=lambda kv: -kv[1][0]):
        print(f"  {model:<36} {_money(usd):>10}" + (f"  ({unpriced} unpriced)" if unpriced else ""))
    print("  most recent runs:")
    for run in runs_mod.build(rows)[: args.limit]:
        sub = f", {len(run.children)} subagents" if run.children else ""
        print(f"    {run.started:%m-%d %H:%M}  {_money(run.total_usd):>9}  {run.ref}{sub}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hermes-cost-centers")
    p.add_argument("--config", help="path to budgets.yaml")
    p.add_argument("--skip-permission-check", action="store_true",
                   help="allow a config that others can write (testing only)")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("evaluate", help="compute the budget state and write it")
    e.add_argument("--loop", action="store_true", help="repeat every interval_seconds")
    e.add_argument("--stdout", action="store_true", help="print the state instead of writing it")
    e.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("status", help="show the last written state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    a = sub.add_parser("admit", help="admission check before a kanban card's run starts")
    a.add_argument("--profile", required=True)
    a.add_argument("--board", required=True)
    a.add_argument("--task", required=True)
    a.set_defaults(fn=cmd_admit)

    x = sub.add_parser("explain", help="where one cost center's spend went this month")
    x.add_argument("cost_center")
    x.add_argument("--limit", type=int, default=10)
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_explain)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except config_mod.ConfigError as exc:
        print(f"hermes-cost-centers: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
