"""The sql source on a real Postgres.

Set HCC_TEST_PG_URL to a database the test may create a table in, e.g.
postgresql://postgres:test@127.0.0.1:55432/postgres. Skipped otherwise.
"""

import os
import uuid
from decimal import Decimal

import pytest

from conftest import MONTH0, day
from datetime import timedelta

from hermes_cost_centers.adapters.sql import Sql
from hermes_cost_centers.pricing import PriceTable

URL = os.environ.get("HCC_TEST_PG_URL")
psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.skipif(not URL, reason="HCC_TEST_PG_URL not set")

WINDOW = (MONTH0, MONTH0 + timedelta(days=31))


@pytest.fixture
def table():
    name = f"usage_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"""CREATE TABLE {name} (
            ts timestamptz NOT NULL, source text, model text,
            input_tokens integer, output_tokens integer, cached_in integer, cache_in integer,
            cost_usd numeric(12,6), session text)""")
        conn.cursor().executemany(f"INSERT INTO {name} VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", [
            (day(2, 10), "alpha", "claude-sonnet-4-6", 10, 20, 30, 40, Decimal("0.123456"), "s1"),
            (day(2, 11), "alpha", "claude-sonnet-4-6", 1_000_000, 0, 0, 0, None, "s1"),
            (day(3, 9), "beta", "mystery", 5, 5, 0, 0, None, None),
            (day(-3), "alpha", "claude-sonnet-4-6", 1, 1, 0, 0, Decimal("9"), None),
        ])
    yield name
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(f"DROP TABLE {name}")


def test_named_parameters_and_types(table):
    src = Sql({
        "url": URL,
        "query": f"""SELECT ts, source, model, input_tokens, output_tokens,
                            cached_in AS cache_read_tokens, cache_in AS cache_write_tokens,
                            cost_usd, session AS run_ref
                       FROM {table}
                      WHERE ts >= :since AND ts < :until
                        AND source::text !~ ':not_a_param'
                      ORDER BY ts""",
    }, PriceTable())
    rows = list(src.rows(*WINDOW))
    assert [r.source for r in rows] == ["alpha", "alpha", "beta"]  # last month excluded
    billed, priced, unpriced = rows
    assert billed.cost_usd == pytest.approx(0.123456) and isinstance(billed.cost_usd, float)
    assert billed.basis == "billed" and billed.run_ref == "s1"
    assert billed.tokens.cache_read == 30 and billed.tokens.cache_write == 40
    assert billed.ts.tzinfo is not None and billed.ts == day(2, 10)
    assert priced.cost_usd == pytest.approx(3.0) and priced.basis == "priced"
    assert unpriced.cost_usd is None


def test_connection_is_read_only(table):
    src = Sql({"url": URL, "query": f"DELETE FROM {table} WHERE ts >= :since AND ts < :until"
                                    " RETURNING ts, source, model"}, PriceTable())
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        list(src.rows(*WINDOW))


def test_literal_percent_in_query(table):
    src = Sql({"url": URL, "query": f"SELECT ts, source, model, cost_usd FROM {table}"
                                    " WHERE ts >= :since AND ts < :until AND source LIKE 'al%'"},
              PriceTable())
    assert {r.source for r in src.rows(*WINDOW)} == {"alpha"}


def test_budget_table_and_backstop_on_postgres():
    from datetime import datetime, timezone

    from hermes_cost_centers import backstop, sql_state
    from test_enforcement import NOW, budget_state

    table = f"budget_state_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(sql_state.DDL.format(table=table))
    try:
        assert sql_state.write({"url": URL, "table": table}, budget_state(research="exhausted"), True) == 3
        b = backstop.Backstop(lambda: psycopg.connect(URL), table=table, cache_seconds=0)
        d = b.check("researcher", now=NOW)
        assert not d.allow and "research has spent its $40.00 for 2026-11; resumes 2026-12-01" in d.message
        assert b.check("ops-bot", now=NOW).allow
        sql_state.write({"url": URL, "table": table}, budget_state(), True)   # upsert back to ok
        assert b.check("researcher", now=NOW).allow
        assert b.check("researcher", now=datetime(2026, 11, 16, 12, 6, tzinfo=timezone.utc)).allow
    finally:
        with psycopg.connect(URL, autocommit=True) as conn:
            conn.execute(f"DROP TABLE {table}")
