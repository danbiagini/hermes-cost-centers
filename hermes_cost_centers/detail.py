"""Per-cost-center run detail, written by the evaluator for the dashboard.

The dashboard reads these files and never the ledger, so the web-facing
process needs no ledger credential. One file per cost center per month:
``<runs_dir>/<YYYY-MM>/<cost-center>.json``.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import runs as runs_mod
from . import state as state_mod
from .config import Config
from .evaluator import Month, _iso
from .ledger import Row

MAX_RUNS = 500


def _file(runs_dir: Path, month_label: str, name: str) -> Path:
    # Cost center names come from the operator's config; quote them anyway so a
    # name can never address a path outside the month's directory.
    return Path(runs_dir) / month_label / (urllib.parse.quote(name, safe="") + ".json")


def build(config: Config, rows: Iterable[Row], month: Month, now: datetime) -> dict[str, dict]:
    by_center: dict[str, list[Row]] = {name: [] for name in config.cost_centers}
    for r in rows:
        if not month.start <= r.ts < month.end:
            continue
        name = config.center_for(r.source)
        if name is not None:
            by_center[name].append(r)
    out = {}
    for name, center_rows in by_center.items():
        daily: dict[str, float] = {}
        for r in center_rows:
            day = r.ts.astimezone(config.timezone).strftime("%Y-%m-%d")
            daily[day] = daily.get(day, 0.0) + (r.cost_usd or 0.0)
        built = runs_mod.build(center_rows)
        out[name] = {
            "cost_center": name,
            "month": month.label,
            "generated_at": _iso(now),
            "complete": now >= month.end,
            "daily_usd": {d: round(v, 6) for d, v in sorted(daily.items())},
            "run_count": len(built),
            "runs": [r.as_dict() for r in built[:MAX_RUNS]],
        }
    return out


def write(runs_dir: Path, details: dict[str, dict]) -> None:
    for name, d in details.items():
        state_mod.write(_file(runs_dir, d["month"], name), d)


def read(runs_dir: Path, month_label: str, name: str) -> Optional[dict]:
    return state_mod.read(_file(runs_dir, month_label, name))


def needs_final_write(runs_dir: Path, config: Config, month: Month) -> bool:
    """True until a closed month has been written once after its end.

    The last cycle of a month runs up to an interval before midnight, so the
    first cycle of the next month writes the previous month one final time.
    """
    for name in config.cost_centers:
        d = read(runs_dir, month.label, name)
        if d is None or not d.get("complete"):
            return True
    return False
