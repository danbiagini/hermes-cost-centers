"""Budgets tab backend, mounted at /api/plugins/hermes-cost-centers/.

Every handler is read-only, and every figure comes from files the evaluator
writes: the budget state and each cost center's run detail. The dashboard never
reads the ledger itself, so it holds no ledger credential, and its numbers
can't disagree with the evaluator's.

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
from hermes_cost_centers import detail as detail_mod  # noqa: E402
from hermes_cost_centers import state as state_mod  # noqa: E402
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


def _month_label(label: str | None, cfg: config_mod.Config) -> str:
    if not label:
        return Month.containing(evaluator.utcnow(), cfg.timezone).label
    try:
        year, mon = (int(x) for x in label.split("-"))
        return Month.containing(datetime(year, mon, 15, tzinfo=cfg.timezone), cfg.timezone).label
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM") from exc


@router.get("/cost-centers/{name}/runs")
def get_runs(name: str, month: str | None = None, limit: int = 50) -> dict:
    """Run detail as the evaluator last wrote it. The dashboard never reads
    the ledger itself, so it needs no ledger credential."""
    cfg = _config()
    if name not in cfg.cost_centers:
        raise HTTPException(status_code=404, detail=f"no cost center {name!r}")
    label = _month_label(month, cfg)
    d = detail_mod.read(cfg.runs_dir, label, name)
    if d is None:
        raise HTTPException(status_code=404, detail=f"no run detail for {name} in {label} yet")
    d = dict(d)
    d["runs"] = d.get("runs", [])[: max(1, min(limit, detail_mod.MAX_RUNS))]
    return d


@router.get("/unattributed")
def get_unattributed() -> dict:
    cfg = _config()
    state = state_mod.read(cfg.state_path)
    return {"unattributed": (state or {}).get("unattributed", {})}
