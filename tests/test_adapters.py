import sqlite3
from datetime import timedelta, timezone

import pytest

from hermes_cost_centers import runs
from hermes_cost_centers.adapters.hermes_sessions import HermesSessions, discover
from hermes_cost_centers.adapters.sql import Sql
from hermes_cost_centers.pricing import PriceTable

UTC = timezone.utc
from conftest import MONTH0, day

WINDOW = (MONTH0, MONTH0 + timedelta(days=31))


def test_discover_profiles(hermes_home):
    assert sorted(discover(hermes_home)) == ["alpha", "beta", "default"]


@pytest.fixture
def rows(hermes_home):
    return list(HermesSessions({"hermes_home": str(hermes_home)}, PriceTable()).rows(*WINDOW))


def by_run(rows, ref):
    return [r for r in rows if r.run_ref == ref]


def test_billed_cost_used_as_is(rows):
    (d1,) = by_run(rows, "d1")
    assert d1.source == "default" and d1.cost_usd == 1.25 and d1.basis == "billed"


def test_unknown_cost_is_priced_from_tokens(rows):
    (a1,) = by_run(rows, "a1")
    # 1M input at $3 + 100k output at $15. Hermes's 0.0 for an unknown cost is ignored.
    assert a1.cost_usd == pytest.approx(4.50) and a1.basis == "priced"


def test_unknown_model_stays_unpriced(rows):
    (a3,) = by_run(rows, "a3")
    assert a3.cost_usd is None and a3.basis is None


def test_window_and_empty_sessions(rows):
    assert not by_run(rows, "a0")  # October
    assert not by_run(rows, "a4")  # no model calls


def test_per_model_usage_replaces_session_totals(rows):
    b1 = sorted(by_run(rows, "b1"), key=lambda r: r.model)
    assert [r.model for r in b1] == ["claude-haiku-4-5", "claude-opus-5-5"]
    haiku, opus = b1
    assert haiku.cost_usd == pytest.approx(0.055) and haiku.basis == "priced"  # Hermes's estimate
    assert opus.cost_usd == pytest.approx(0.1 * 4 + 0.01 * 20)


def test_subagent_rolls_up_to_parent(rows):
    tops = runs.build([r for r in rows if r.source == "alpha"])
    a1 = next(t for t in tops if t.ref == "a1")
    assert [c.ref for c in a1.children] == ["a2"]
    assert a1.total_usd == pytest.approx(4.50 + 0.60)
    assert "a2" not in {t.ref for t in tops}


def test_sql_source_sqlite(tmp_path):
    db = tmp_path / "spend.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE usage_log (at TEXT, who TEXT, model TEXT, tin INT, tout INT, usd REAL)")
    conn.executemany("INSERT INTO usage_log VALUES (?,?,?,?,?,?)", [
        (day(2, 10).isoformat(), "alpha", "claude-sonnet-4-6", 10, 10, 0.5),
        (day(2, 11).isoformat(), "alpha", "claude-sonnet-4-6", 1_000_000, 0, None),
        (day(2, 12).isoformat(), "alpha", "mystery", 10, 10, None),
        (day(-5).isoformat(), "alpha", "claude-sonnet-4-6", 10, 10, 9.0),
    ])
    conn.commit()
    conn.close()
    src = Sql({
        "url": f"sqlite:///{db}",
        "query": "SELECT at AS ts, who AS source, model, tin AS input_tokens, tout AS output_tokens,"
                 " usd AS cost_usd FROM usage_log WHERE at >= :since AND at < :until",
    }, PriceTable())
    got = list(src.rows(*WINDOW))
    assert [(r.cost_usd, r.basis) for r in got] == [(0.5, "billed"), (3.0, "priced"), (None, None)]
