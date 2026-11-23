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


def _held_via_cli(board: Board) -> list[dict]:
    """The slow path: one CLI call per board and status, and one per held card."""
    out = []
    for name in board.boards():
        for status in ("blocked", "scheduled"):
            for card in board.cards(name, status):
                task_id = card.get("id")
                if not task_id:
                    continue
                full = board.show(name, task_id)
                marker = held_by_us(last_hold_reason(full))
                if marker:
                    out.append({"board": name, "id": task_id, "status": status,
                                "assignee": (full.get("task") or {}).get("assignee") or card.get("assignee"),
                                "marker": marker})
    return out


# A marker only counts on the state it belongs to: budget blocks are `blocked`,
# pace holds are `scheduled`. Anything else was moved by someone else.
_STATUS_FOR = {BLOCK_MARKER: "blocked", PACE_MARKER: "scheduled"}


def resume(cfg: Config, state: dict, board: Optional[Board] = None) -> list[dict]:
    board = board or Board(cfg.kanban)
    centers = state.get("cost_centers") or {}
    held = board.held_cards()
    if held is None:
        held = _held_via_cli(board)
    released = []
    for card in held:
        if _STATUS_FOR.get(card["marker"]) != card["status"]:
            continue
        center = cfg.center_for(card.get("assignee") or "")
        if _releasable(card["marker"], centers.get(center) if center else None):
            board.unblock(card["board"], card["id"])
            released.append({"board": card["board"], "task": card["id"], "marker": card["marker"],
                             "cost_center": center})
    return released
