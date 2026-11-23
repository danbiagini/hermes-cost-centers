"""The default ledger: every profile's Hermes session database, read-only.

Hermes keeps one ``state.db`` per profile: ``<HERMES_HOME>/state.db`` for the
default profile and ``<HERMES_HOME>/profiles/<name>/state.db`` for the rest. A
row's source is the profile name.

Where ``session_model_usage`` has rows for a session, those are used (one row
per model and task, so auxiliary calls such as compression are counted).
Otherwise the session's own totals are used.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..ledger import BILLED, PRICED, UNMETERED, Row, Tokens
from ..pricing import PriceTable

DEFAULT_PROFILE = "default"


def discover(hermes_home: Path) -> dict[str, Path]:
    """Map profile name to its state.db for every profile that has one."""
    found: dict[str, Path] = {}
    root_db = hermes_home / "state.db"
    if root_db.is_file():
        found[DEFAULT_PROFILE] = root_db
    profiles = hermes_home / "profiles"
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            db = child / "state.db"
            if db.is_file():
                found[child.name] = db
    return found


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ts(epoch: Optional[float]) -> datetime:
    return datetime.fromtimestamp(float(epoch or 0), tz=timezone.utc)


def _cost(status: Optional[str], actual: Optional[float], estimated: Optional[float],
          model: str, tokens: Tokens, prices: PriceTable) -> tuple[Optional[float], Optional[str]]:
    """Hermes's own cost where it is known; otherwise the price table.

    Hermes stores 0 (not NULL) for an unknown cost, so the status decides
    whether a number means anything.
    """
    if status == "actual" and actual is not None:
        return float(actual), BILLED
    if status == "estimated" and estimated is not None:
        return float(estimated), PRICED
    if status == "included":
        return 0.0, UNMETERED
    priced = prices.price(model, tokens)
    return (priced, PRICED) if priced is not None else (None, None)


def read_profile(source: str, db: Path, since: datetime, until: datetime,
                 prices: PriceTable) -> Iterator[Row]:
    lo, hi = since.timestamp(), until.timestamp()
    with _connect(db) as conn:
        cols = _columns(conn, "sessions")
        parent_col = "parent_session_id" if "parent_session_id" in cols else "NULL"
        sessions = conn.execute(
            f"""SELECT id, model, {parent_col} AS parent, started_at,
                       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                       estimated_cost_usd, actual_cost_usd, cost_status
                  FROM sessions WHERE started_at >= ? AND started_at < ?""",
            (lo, hi),
        ).fetchall()
        if not sessions:
            return
        by_session: dict[str, list[sqlite3.Row]] = {}
        if _has_table(conn, "session_model_usage"):
            ids = [s["id"] for s in sessions]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"""SELECT session_id, model, input_tokens, output_tokens, cache_read_tokens,
                               cache_write_tokens, estimated_cost_usd, actual_cost_usd, cost_status,
                               first_seen
                          FROM session_model_usage WHERE session_id IN ({marks})""",
                    chunk,
                ):
                    by_session.setdefault(r["session_id"], []).append(r)

    for s in sessions:
        parts = by_session.get(s["id"]) or [s]
        for p in parts:
            tokens = Tokens(
                int(p["input_tokens"] or 0), int(p["output_tokens"] or 0),
                int(p["cache_read_tokens"] or 0), int(p["cache_write_tokens"] or 0),
            )
            status = p["cost_status"]
            if tokens.total == 0 and status not in ("actual", "estimated"):
                continue  # a session that never called a model
            model = p["model"] or s["model"] or "unknown"
            cost, basis = _cost(status, p["actual_cost_usd"], p["estimated_cost_usd"],
                                model, tokens, prices)
            first_seen = p["first_seen"] if "first_seen" in p.keys() else None
            yield Row(
                ts=_ts(first_seen or s["started_at"]),
                source=source,
                model=model,
                tokens=tokens,
                cost_usd=cost,
                basis=basis,
                run_ref=s["id"],
                parent_ref=s["parent"],
            )


class HermesSessions:
    name = "hermes-sessions"

    def __init__(self, options: dict, prices: PriceTable):
        home = options.get("hermes_home") or os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
        self.hermes_home = Path(home)
        self.prices = prices

    def rows(self, since: datetime, until: datetime) -> Iterator[Row]:
        for source, db in discover(self.hermes_home).items():
            yield from read_profile(source, db, since, until, self.prices)
