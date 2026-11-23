"""A thin wrapper over the `hermes kanban` CLI.

The CLI is the board's public interface, so admission and the resume pass use it
rather than writing the board's database. Every card this package holds carries
one of two markers at the start of its reason, and the resume pass touches only
cards that do.
"""

from __future__ import annotations

import json
import os
import subprocess
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


class Board:
    def __init__(self, options: Optional[dict] = None,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        options = options or {}
        self.hermes = options.get("hermes_bin", "hermes")
        self.env = dict(os.environ)
        self.env.update({k: str(v) for k, v in (options.get("env") or {}).items()})
        self.timeout = int(options.get("timeout_seconds", 30))
        self.run = runner

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
