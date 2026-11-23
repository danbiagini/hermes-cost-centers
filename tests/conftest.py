from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# The subset of Hermes's sessions schema the adapter reads, plus the
# per-model table newer releases add.
SESSIONS = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, model TEXT, parent_session_id TEXT,
    started_at REAL NOT NULL, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT
)"""
MODEL_USAGE = """
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, task TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT, first_seen REAL, last_seen REAL
)"""


# Fixture sessions sit in the current month, so tests that run the CLI or the
# router against the real clock see them.
MONTH0 = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day(n: int, hour: int = 9, minute: int = 0) -> datetime:
    """Day ``n`` of the current month (n=0 is the last day of the previous month)."""
    return MONTH0 + timedelta(days=n - 1, hours=hour, minutes=minute)


def ts(n: int, hour: int = 9, minute: int = 0) -> float:
    return day(n, hour, minute).timestamp()


def make_db(path: Path, sessions: list[tuple], usage: list[tuple] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(SESSIONS)
    if usage is not None:
        conn.execute(MODEL_USAGE)
        conn.executemany(
            "INSERT INTO session_model_usage (session_id, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, estimated_cost_usd, actual_cost_usd,"
            " cost_status, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?)", usage)
    conn.executemany(
        "INSERT INTO sessions (id, source, model, parent_session_id, started_at, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, estimated_cost_usd,"
        " actual_cost_usd, cost_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", sessions)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes"
    # The default profile: one billed session.
    make_db(home / "state.db", [
        ("d1", "cli", "claude-sonnet-4-6", None, ts(1, 10), 0, 0, 0, 0, None, 1.25, "actual"),
    ])
    # A profile behind a custom endpoint: Hermes knows no price (status unknown, cost 0).
    make_db(home / "profiles" / "alpha" / "state.db", [
        ("a1", "kanban", "claude-sonnet-4-6", None, ts(2),
         1_000_000, 100_000, 0, 0, 0.0, None, "unknown"),
        # A subagent of a1.
        ("a2", "delegate", "claude-sonnet-4-6", "a1", ts(2, 9, 10),
         200_000, 0, 0, 0, 0.0, None, "unknown"),
        # A model the price table doesn't know: unpriced, never $0.
        ("a3", "kanban", "mystery-model", None, ts(3), 5000, 500, 0, 0, 0.0, None, "unknown"),
        # The previous month: outside the window.
        ("a0", "kanban", "claude-sonnet-4-6", None, ts(0), 9_000_000, 0, 0, 0, 0.0, None, "unknown"),
        # A session that never called a model.
        ("a4", "cli", "claude-sonnet-4-6", None, ts(1, 11), 0, 0, 0, 0, 0.0, None, "unknown"),
    ])
    # A profile on a release with per-model usage rows.
    make_db(home / "profiles" / "beta" / "state.db", [
        ("b1", "kanban", "claude-opus-5-5", None, ts(1, 12),
         999, 999, 0, 0, 0.0, None, "unknown"),
    ], usage=[
        ("b1", "claude-opus-5-5", 100_000, 10_000, 0, 0, 0.0, 0.0, "unknown", ts(1, 12)),
        ("b1", "claude-haiku-4-5", 50_000, 1_000, 0, 0, 0.055, 0.0, "estimated", ts(1, 12, 5)),
    ])
    return home


@pytest.fixture
def write_config(tmp_path: Path):
    def _write(text: str, mode: int = 0o644) -> Path:
        p = tmp_path / "budgets.yaml"
        p.write_text(text, encoding="utf-8")
        p.chmod(mode)
        return p
    return _write


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    """Late in the current month, after every fixture session."""
    from hermes_cost_centers import evaluator

    monkeypatch.setattr(evaluator, "utcnow", lambda: day(28, 9))
