"""The evaluator: the only code that sums spend.

Every reader (the dashboard tab now; admission, the backstop and the agent's
budget tool later) reads the state this produces, so they can't disagree.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .config import Config, CostCenter
from .ledger import BASES, Row

STATE_VERSION = 1

OK = "ok"
WRAP_UP = "wrap_up"
EXHAUSTED = "exhausted"
UNSET = "unset"  # no budget: tracked, never enforced


@dataclass(frozen=True)
class Month:
    start: datetime
    end: datetime
    days: int

    @classmethod
    def containing(cls, t: datetime, tz) -> "Month":
        local = t.astimezone(tz)
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days = calendar.monthrange(local.year, local.month)[1]
        if local.month == 12:
            end = start.replace(year=local.year + 1, month=1)
        else:
            end = start.replace(month=local.month + 1)
        # Aware datetimes in the same zone subtract as wall-clock time, which
        # loses or gains the DST hour. Keep the boundaries in UTC.
        return cls(start.astimezone(timezone.utc), end.astimezone(timezone.utc), days, tz)

    tz: object = timezone.utc

    @property
    def label(self) -> str:
        return self.start.astimezone(self.tz).strftime("%Y-%m")

    def fraction(self, t: datetime) -> float:
        total = (self.end - self.start).total_seconds()
        return min(1.0, max(0.0, (t - self.start).total_seconds() / total))

    def at_fraction(self, f: float) -> datetime:
        return self.start + (self.end - self.start) * f


def allowance(budget: float, month: Month, t: datetime, headroom_days: float) -> float:
    """What a paced cost center may have spent by ``t``.

    Its pro-rata share of the month plus some days of headroom, capped at the
    budget. It depends only on time, so unused allowance carries forward.
    """
    return min(budget, budget * (month.fraction(t) + headroom_days / month.days))


def eligible_at(budget: float, month: Month, spend: float, headroom_days: float) -> Optional[datetime]:
    """When the allowance catches up with ``spend``. None if it never will this month."""
    if spend >= budget:
        return None
    f = spend / budget - headroom_days / month.days
    return month.at_fraction(max(0.0, f))


def utcnow() -> datetime:
    """The clock every caller uses, so tests can pin it in one place."""
    return datetime.now(timezone.utc)


def _iso(t: Optional[datetime]) -> Optional[str]:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds") if t else None


def _round(x: float) -> float:
    return round(x, 6)


def _center_state(center: CostCenter, rows: list[Row], month: Month, now: datetime,
                  wrap_up: float) -> dict:
    priced = [r for r in rows if r.priced]
    spend = sum(r.cost_usd for r in priced)
    unpriced = len(rows) - len(priced)
    basis = {b: 0.0 for b in BASES}
    for r in priced:
        basis[r.basis or "billed"] = basis.get(r.basis or "billed", 0.0) + r.cost_usd
    out = {
        "sources": list(center.sources),
        "spend_usd": _round(spend),
        "budget_usd": center.monthly_usd,
        "remaining_usd": None,
        "used_fraction": None,
        "state": UNSET,
        # With unpriced rows the spend is a floor: the true figure is higher.
        "incomplete": unpriced > 0,
        "unpriced_rows": unpriced,
        "rows": len(rows),
        "runs": len({r.run_ref for r in rows if r.run_ref}),
        "basis_usd": {k: _round(v) for k, v in basis.items()},
        "pace": None,
    }
    budget = center.monthly_usd
    if budget is None:
        return out
    out["remaining_usd"] = _round(max(0.0, budget - spend))
    if budget == 0:
        out["used_fraction"] = 1.0 if spend > 0 else 0.0
        out["state"] = EXHAUSTED
    else:
        used = spend / budget
        out["used_fraction"] = _round(used)
        out["state"] = EXHAUSTED if used >= 1 else WRAP_UP if used >= wrap_up else OK
    if center.pace.on and budget > 0:
        allow = allowance(budget, month, now, center.pace.headroom_days)
        ahead = spend >= allow and out["state"] != EXHAUSTED
        out["pace"] = {
            "headroom_days": center.pace.headroom_days,
            "allowance_usd": _round(allow),
            "ahead_of_pace": ahead,
            "eligible_at": _iso(eligible_at(budget, month, spend, center.pace.headroom_days)) if ahead else None,
        }
    return out


def _crossing(spend: float, target: Optional[float], month: Month, now: datetime) -> Optional[str]:
    """The date the account crosses ``target`` at this month's average rate."""
    if target is None or spend <= 0:
        return None
    if spend >= target:
        return "crossed"
    elapsed = (now - month.start).total_seconds()
    if elapsed <= 0:
        return None
    t = month.start + timedelta(seconds=elapsed * target / spend)
    return _iso(t) if t < month.end else None


def evaluate(config: Config, rows: Iterable[Row], now: Optional[datetime] = None,
             ledger_name: str = "") -> dict:
    now = now or utcnow()
    month = Month.containing(now, config.timezone)
    by_center: dict[str, list[Row]] = defaultdict(list)
    unattributed: dict[str, dict] = {}
    total = 0.0
    unpriced = 0
    for r in rows:
        if not month.start <= r.ts < month.end:
            continue
        if r.priced:
            total += r.cost_usd
        else:
            unpriced += 1
        name = config.center_for(r.source)
        if name is None:
            u = unattributed.setdefault(r.source, {"spend_usd": 0.0, "rows": 0, "unpriced_rows": 0})
            u["rows"] += 1
            if r.priced:
                u["spend_usd"] += r.cost_usd
            else:
                u["unpriced_rows"] += 1
            continue
        by_center[name].append(r)

    centers = {
        name: _center_state(c, by_center.get(name, []), month, now, config.wrap_up)
        for name, c in config.cost_centers.items()
    }
    for u in unattributed.values():
        u["spend_usd"] = _round(u["spend_usd"])
    return {
        "version": STATE_VERSION,
        "generated_at": _iso(now),
        "interval_seconds": config.interval_seconds,
        "ledger": ledger_name or config.ledger.get("source"),
        "month": {"label": month.label, "start": _iso(month.start), "end": _iso(month.end),
                  "days": month.days},
        "account": {
            "spend_usd": _round(total),
            "target_usd": config.target_usd,
            "hard_usd": config.hard_usd,
            "incomplete": unpriced > 0,
            "unpriced_rows": unpriced,
            "crosses_target_at": _crossing(total, config.target_usd, month, now),
        },
        "cost_centers": centers,
        "unattributed": unattributed,
    }
