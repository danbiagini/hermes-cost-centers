"""Budgets tab backend, mounted at /api/plugins/hermes-cost-centers/.

Every handler is read-only. The figures come from the state the evaluator
writes; the per-run detail reads the ledger through the same adapter code the
evaluator uses, so the two can't disagree.

The plugin belongs in the launch (root) profile only. Its view is the
operator's: it always shows every cost center, whatever profile the request is
scoped to.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cost_centers import config as config_mod  # noqa: E402
from hermes_cost_centers import evaluator  # noqa: E402
from hermes_cost_centers import runs as runs_mod  # noqa: E402
from hermes_cost_centers import state as state_mod  # noqa: E402
from hermes_cost_centers.adapters import get_adapter  # noqa: E402
from hermes_cost_centers.evaluator import Month  # noqa: E402

router = APIRouter()


def _config() -> config_mod.Config:
    try:
        return config_mod.load()
    except config_mod.ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/state")
def get_state() -> dict:
    cfg = _config()
    state = state_mod.read(cfg.state_path)
    if state is None:
        return {"available": False, "stale": True, "state_path": str(cfg.state_path),
                "detail": "no budget state yet: is the evaluator running?"}
    return {"available": True, "stale": state_mod.is_stale(state),
            "age_seconds": state_mod.age_seconds(state), "state": state}


def _month(label: str | None, cfg: config_mod.Config) -> tuple[Month, datetime]:
    now = evaluator.utcnow()
    if not label:
        month = Month.containing(now, cfg.timezone)
    else:
        try:
            year, mon = (int(x) for x in label.split("-"))
            month = Month.containing(datetime(year, mon, 15, tzinfo=cfg.timezone), cfg.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc
    return month, min(now, month.end)


@router.get("/cost-centers/{name}/runs")
def get_runs(name: str, month: str | None = None, limit: int = 50) -> dict:
    cfg = _config()
    center = cfg.cost_centers.get(name)
    if center is None:
        raise HTTPException(status_code=404, detail=f"no cost center {name!r}")
    m, until = _month(month, cfg)
    rows = [r for r in get_adapter(cfg).rows(m.start, until) if r.source in center.sources]
    daily: dict[str, float] = {}
    for r in rows:
        day = r.ts.astimezone(cfg.timezone).strftime("%Y-%m-%d")
        daily[day] = daily.get(day, 0.0) + (r.cost_usd or 0.0)
    runs = runs_mod.build(rows)
    return {
        "cost_center": name,
        "month": m.label,
        "daily_usd": {d: round(v, 6) for d, v in sorted(daily.items())},
        "run_count": len(runs),
        "runs": [r.as_dict() for r in runs[: max(1, min(limit, 500))]],
    }


@router.get("/unattributed")
def get_unattributed() -> dict:
    cfg = _config()
    state = state_mod.read(cfg.state_path)
    return {"unattributed": (state or {}).get("unattributed", {})}
