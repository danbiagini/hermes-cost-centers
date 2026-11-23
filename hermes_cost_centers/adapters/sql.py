"""A ledger from the operator's own spend table.

Configuration only: a connection string and a query. The query takes the
named parameters ``:since`` and ``:until`` and returns these columns:

    ts, source, model, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens, cost_usd

and optionally ``run_ref``, ``parent_ref`` and ``task_ref``. A NULL
``cost_usd`` is priced from the table; if that fails, the row stays unpriced.

Supported URLs: ``sqlite:///path/to/file.db`` (opened read-only) and
``postgresql://...`` (needs the ``postgres`` extra).
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator

from ..ledger import BILLED, PRICED, Row, Tokens
from ..pricing import PriceTable

_NAMED = re.compile(r"(?<!:):(since|until)\b")


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Sql:
    name = "sql"

    def __init__(self, options: dict, prices: PriceTable):
        url = options.get("url") or os.environ.get(options.get("url_env", ""), "")
        if not url:
            raise ValueError("ledger.url (or ledger.url_env) is required for the sql source")
        if not options.get("query"):
            raise ValueError("ledger.query is required for the sql source")
        self.url = url
        self.query = options["query"]
        self.prices = prices

    def _execute(self, since: datetime, until: datetime) -> tuple[list[str], list[tuple]]:
        params = {"since": since, "until": until}
        if self.url.startswith("sqlite:///"):
            path = self.url[len("sqlite:///"):]
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                cur = conn.execute(self.query, {k: v.isoformat() for k, v in params.items()})
                return [d[0] for d in cur.description], cur.fetchall()
            finally:
                conn.close()
        if self.url.startswith(("postgresql://", "postgres://")):
            import psycopg  # optional dependency

            # psycopg uses %-placeholders, so a literal % (LIKE 'x%') must be doubled first.
            query = _NAMED.sub(r"%(\1)s", self.query.replace("%", "%%"))
            with psycopg.connect(self.url, connect_timeout=10) as conn:
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    return [d.name for d in cur.description], cur.fetchall()
        raise ValueError(f"unsupported ledger.url scheme: {self.url.split(':', 1)[0]}")

    def rows(self, since: datetime, until: datetime) -> Iterator[Row]:
        names, records = self._execute(since, until)
        missing = {"ts", "source", "model"} - set(names)
        if missing:
            raise ValueError(f"ledger.query must return columns {sorted(missing)}")
        for record in records:
            r = dict(zip(names, record))
            tokens = Tokens(
                int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0),
                int(r.get("cache_read_tokens") or 0), int(r.get("cache_write_tokens") or 0),
            )
            model = r.get("model") or "unknown"
            cost = r.get("cost_usd")
            if cost is not None:
                cost, basis = float(cost), BILLED
            else:
                cost = self.prices.price(model, tokens)
                basis = PRICED if cost is not None else None
            yield Row(
                ts=_as_datetime(r["ts"]),
                source=str(r["source"]),
                model=model,
                tokens=tokens,
                cost_usd=cost,
                basis=basis,
                run_ref=r.get("run_ref"),
                parent_ref=r.get("parent_ref"),
                task_ref=r.get("task_ref"),
            )
