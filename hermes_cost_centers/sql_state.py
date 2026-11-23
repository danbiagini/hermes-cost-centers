"""The budget state as a table, one row per ledger source, for the backstop.

The backstop runs at the model endpoint, which can usually reach a database but
not the evaluator's files. It looks a call's source up here and needs nothing
else. The operator creates the table (see ``DDL``) and grants the evaluator
INSERT, UPDATE and DELETE on it; the evaluator never creates objects.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable

DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    source           text PRIMARY KEY,
    cost_center      text NOT NULL,
    state            text NOT NULL,          -- ok | wrap_up | exhausted | unset
    spend_usd        numeric(12,6) NOT NULL,
    budget_usd       numeric(12,2),           -- NULL = unset, never enforced
    month            text NOT NULL,           -- YYYY-MM
    resumes_at       timestamptz NOT NULL,    -- the next month's start
    ahead_of_pace    boolean NOT NULL DEFAULT false,
    eligible_at      timestamptz,
    enforce_backstop boolean NOT NULL DEFAULT false,
    generated_at     timestamptz NOT NULL,
    interval_seconds integer NOT NULL
)"""

COLUMNS = ("source", "cost_center", "state", "spend_usd", "budget_usd", "month", "resumes_at",
           "ahead_of_pace", "eligible_at", "enforce_backstop", "generated_at", "interval_seconds")


def rows(state: dict, enforce_backstop: bool) -> list[tuple]:
    out = []
    month = state["month"]
    for name, c in (state.get("cost_centers") or {}).items():
        pace = c.get("pace") or {}
        for source in c.get("sources") or []:
            out.append((source, name, c["state"], c["spend_usd"], c.get("budget_usd"), month["label"],
                        month["end"], bool(pace.get("ahead_of_pace")), pace.get("eligible_at"),
                        bool(enforce_backstop), state["generated_at"], state.get("interval_seconds") or 60))
    return out


def _connect(url: str):
    if url.startswith("sqlite:///"):
        import sqlite3

        return sqlite3.connect(url[len("sqlite:///"):]), "?"
    import psycopg  # the `postgres` extra

    return psycopg.connect(url, connect_timeout=10), "%s"


def write(options: dict, state: dict, enforce_backstop: bool) -> int:
    """Replace the table's contents with this cycle's rows, in one transaction."""
    url = options.get("url") or os.environ.get(options.get("url_env", ""), "")
    if not url:
        raise ValueError("state.sql: no url (set url, or the env var named by url_env)")
    table = options.get("table", "budget_state")
    if not table.replace("_", "").isalnum():
        raise ValueError(f"state.sql.table: {table!r} is not a plain table name")
    data: Iterable[tuple[Any, ...]] = rows(state, enforce_backstop)
    conn, mark = _connect(url)
    try:
        cols = ", ".join(COLUMNS)
        marks = ", ".join([mark] * len(COLUMNS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS[1:])
        cur = conn.cursor()
        sources = [r[0] for r in data]
        cur.executemany(
            f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT (source) DO UPDATE SET {updates}",
            list(data))
        if sources:
            cur.execute(f"DELETE FROM {table} WHERE source NOT IN ({', '.join([mark] * len(sources))})", sources)
        else:
            cur.execute(f"DELETE FROM {table}")
        conn.commit()
        return len(sources)
    finally:
        conn.close()


def parse_time(value) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))
