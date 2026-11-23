"""The resume pass: release the cards admission held, once they may run.

Hermes's `scheduled` state has no wake-up time, so this pass is the clock. It
runs each evaluator cycle and touches only cards whose latest hold reason starts
with one of this package's markers:

- `[cost-centers:pace]`: released when the cost center is no longer ahead of pace
  (or has stopped being paced), unless its monthly budget is now spent.
- `[cost-centers]`: released when the cost center is no longer exhausted (a new
  month, or the operator raised the budget).

A card held for any other reason is never touched.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .kanban import BLOCK_MARKER, PACE_MARKER, Board, held_by_us, last_hold_reason


def _releasable(marker: str, c: Optional[dict]) -> bool:
    if c is None:
        return True  # the cost center is gone: nothing holds the card any more
    if c.get("state") == "exhausted":
        return False
    if marker == BLOCK_MARKER:
        return True
    return not (c.get("pace") or {}).get("ahead_of_pace")


def resume(cfg: Config, state: dict, board: Optional[Board] = None) -> list[dict]:
    board = board or Board(cfg.kanban)
    centers = state.get("cost_centers") or {}
    released = []
    for name in board.boards():
        for status, marker_ok in (("blocked", (BLOCK_MARKER,)), ("scheduled", (PACE_MARKER,))):
            for card in board.cards(name, status):
                task_id = card.get("id")
                if not task_id:
                    continue
                full = board.show(name, task_id)
                marker = held_by_us(last_hold_reason(full))
                if marker not in marker_ok:
                    continue
                center = cfg.center_for((full.get("task") or {}).get("assignee") or card.get("assignee") or "")
                if _releasable(marker, centers.get(center) if center else None):
                    board.unblock(name, task_id)
                    released.append({"board": name, "task": task_id, "marker": marker, "cost_center": center})
    return released
