"""A thin wrapper over the `hermes kanban` CLI.

The CLI is the board's public interface, so admission and the resume pass use it
rather than writing the board's database. Every card this package holds carries
one of two markers at the start of its reason, and the resume pass touches only
cards that do.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

BLOCK_MARKER = "[cost-centers]"        # monthly budget spent: blocked, operator alerted
PACE_MARKER = "[cost-centers:pace]"    # ahead of pace: scheduled, no alert


def held_by_us(reason: Optional[str]) -> Optional[str]:
    """Which of our markers a hold reason carries, if any."""
    reason = (reason or "").lstrip()
    if reason.startswith(PACE_MARKER):
        return PACE_MARKER
    if reason.startswith(BLOCK_MARKER):
        return BLOCK_MARKER
    return None


def last_hold_reason(card: dict) -> Optional[str]:
    """The reason on the card's most recent block or schedule event."""
    for ev in reversed(card.get("events") or []):
        if ev.get("kind") in ("blocked", "scheduled"):
            return (ev.get("payload") or {}).get("reason")
    return None


# One query per board: the cards that are blocked or scheduled, with the reason
# on their latest block/schedule event. Read-only; Hermes's own schema.
HELD_SQL = """
SELECT t.id, t.status, t.assignee,
       (SELECT e.payload FROM task_events e
         WHERE e.task_id = t.id AND e.kind IN ('blocked', 'scheduled')
         ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS payload
  FROM tasks t
 WHERE t.status IN ('blocked', 'scheduled')
"""


def board_databases(home: Path) -> dict[str, Path]:
    """Board slug -> its kanban.db, skipping archived boards.

    Hermes keeps the default board at ``<home>/kanban.db`` and the rest at
    ``<home>/kanban/boards/<slug>/kanban.db``, each with a ``board.json``.
    """
    found: dict[str, Path] = {}
    if (home / "kanban.db").is_file():
        found["default"] = home / "kanban.db"
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        for d in sorted(boards.iterdir()):
            db = d / "kanban.db"
            if not db.is_file():
                continue
            try:
                meta = json.loads((d / "board.json").read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            if meta.get("archived"):
                continue
            found[d.name] = db
    return found


class Board:
    def __init__(self, options: Optional[dict] = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        options = options or {}
        self.hermes = options.get("hermes_bin", "hermes")
        self.env = dict(os.environ)
        self.env.update({k: str(v) for k, v in (options.get("env") or {}).items()})
        self.timeout = int(options.get("timeout_seconds", 30))
        self.run = runner
        home = options.get("home") or self.env.get("HERMES_KANBAN_HOME")
        self.home = Path(home) if home else None

    def _cli(self, *args: str) -> str:
        proc = self.run([self.hermes, "kanban", *args], capture_output=True, text=True,
                        env=self.env, timeout=self.timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"hermes kanban {args[:3]} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc.stdout

    def _json(self, *args: str) -> Any:
        return json.loads(self._cli(*args) or "null")

    def boards(self) -> list[str]:
        data = self._json("boards", "list", "--json")
        return [b["slug"] for b in data or [] if b.get("slug") and not b.get("archived")]

    def cards(self, board: str, status: str) -> list[dict]:
        data = self._json("--board", board, "list", "--status", status, "--json")
        return data if isinstance(data, list) else (data or {}).get("tasks", [])

    def show(self, board: str, task_id: str) -> dict:
        return self._json("--board", board, "show", task_id, "--json") or {}

    def block(self, board: str, task_id: str, reason: str) -> None:
        # `capability`: the alert reads "a grant or a manual step only you can do",
        # which is what raising a budget is.
        self._cli("--board", board, "block", "--kind", "capability", task_id, reason)

    def schedule(self, board: str, task_id: str, reason: str) -> None:
        self._cli("--board", board, "schedule", task_id, reason)

    def unblock(self, board: str, task_id: str) -> None:
        self._cli("--board", board, "unblock", task_id)

    def held_cards(self) -> Optional[list[dict]]:
        """Cards held with one of our markers, read straight from the boards'
        databases: one read-only query per board, no process starts.

        Returns None if the direct read isn't possible (no kanban home, or a
        schema this doesn't recognise), so the caller falls back to the CLI.
        Writes never come through here: releasing a card uses ``unblock``.
        """
        if self.home is None or not self.home.is_dir():
            return None
        out: list[dict] = []
        try:
            for slug, db in board_databases(self.home).items():
                conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                try:
                    rows = conn.execute(HELD_SQL).fetchall()
                finally:
                    conn.close()
                for task_id, status, assignee, payload in rows:
                    try:
                        reason = (json.loads(payload or "{}") or {}).get("reason")
                    except (TypeError, ValueError):
                        reason = None
                    marker = held_by_us(reason)
                    if marker:
                        out.append({"board": slug, "id": task_id, "status": status,
                                    "assignee": assignee, "marker": marker})
        except sqlite3.Error:
            return None
        return out
