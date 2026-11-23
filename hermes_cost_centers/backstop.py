"""The backstop: refuse a model call from a cost center whose monthly budget is spent.

Runs inside the model endpoint (for example a LiteLLM ``custom_auth``), in front
of every call, so it follows three rules:

- **It never raises.** Any error is an allow. A bug here must not become an outage.
- **It fails open.** No row, a stale row or an unreadable table: allow.
- **It reads one row** from the table the evaluator writes (``sql_state``) and
  caches it briefly. It never sums spend itself.

The refusal is HTTP 402 with a message an agent recognizes as a billing limit:
``budget exhausted: <cost center> has spent its $<n> for <month>; resumes <date>
(hard billing limit)``. Hermes treats a 402 as a billing error and doesn't retry
the call. A kanban worker then exits as rate-limited and its card is requeued,
so pair the backstop with admission, which blocks the card at its next start.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional

from .sql_state import COLUMNS, parse_time

log = logging.getLogger("hermes_cost_centers.backstop")

STALE_INTERVALS = 5
HTTP_STATUS = 402


@dataclass(frozen=True)
class Decision:
    allow: bool
    message: str = ""
    would_refuse: bool = False  # exhausted, but enforcement is off (dry run)


ALLOW = Decision(True)


def message(row: Mapping) -> str:
    budget = row.get("budget_usd")
    resumes = parse_time(row["resumes_at"]).astimezone(timezone.utc).date().isoformat()
    return (f"budget exhausted: {row['cost_center']} has spent its ${float(budget):,.2f} for "
            f"{row['month']}; resumes {resumes} (hard billing limit)")


def decide(row: Optional[Mapping], now: Optional[datetime] = None) -> Decision:
    """Pure decision for one source's row. Never raises."""
    try:
        if not row or row.get("state") != "exhausted" or row.get("budget_usd") is None:
            return ALLOW
        now = now or datetime.now(timezone.utc)
        age = (now - parse_time(row["generated_at"])).total_seconds()
        if age > STALE_INTERVALS * int(row.get("interval_seconds") or 60):
            return ALLOW  # the evaluator has stopped: fail open, it's reported elsewhere
        msg = message(row)
        if not row.get("enforce_backstop"):
            return Decision(True, msg, would_refuse=True)
        return Decision(False, msg)
    except Exception:  # pragma: no cover - defensive
        return ALLOW


class Backstop:
    """Looks a source up in the budget table, with a short cache. Never raises.

    ``connect`` returns a DB-API connection; ``paramstyle`` is ``"%s"`` for
    psycopg/psycopg2 or ``"?"`` for sqlite3.
    """

    def __init__(self, connect: Callable, table: str = "budget_state",
                 cache_seconds: float = 15.0, paramstyle: str = "%s"):
        self.connect = connect
        self.table = table
        self.cache_seconds = cache_seconds
        self.paramstyle = paramstyle
        self._cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._lock = threading.Lock()

    def _row(self, source: str) -> Optional[dict]:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(source)
            if hit and now - hit[0] < self.cache_seconds:
                return hit[1]
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT {', '.join(COLUMNS)} FROM {self.table} WHERE source = {self.paramstyle}",
                        (source,))
            rec = cur.fetchone()
        finally:
            conn.close()
        row = dict(zip(COLUMNS, rec)) if rec else None
        with self._lock:
            self._cache[source] = (now, row)
        return row

    def check(self, source: Optional[str], now: Optional[datetime] = None) -> Decision:
        if not source:
            return ALLOW
        try:
            d = decide(self._row(source), now)
        except Exception as exc:
            log.warning("budget check failed open for %s: %s", source, exc)
            return ALLOW
        if d.would_refuse:
            log.warning("DRY RUN, would refuse %s: %s", source, d.message)
        return d
