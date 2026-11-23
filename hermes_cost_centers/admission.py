"""Admission: before a card's run starts, decide whether it may.

- The cost center's monthly budget is spent: **block** the card. The operator is
  told through whatever announces blocked cards.
- It's ahead of its pace line: **hold** the card (`scheduled`) without an alert.
  The evaluator releases it when the line is back on pace. Urgent cards skip this.
- Otherwise, or whenever the state is missing or stale: **allow**. Admission fails
  open; the backstop at the model endpoint is the boundary.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import state as state_mod
from .config import Config
from .kanban import BLOCK_MARKER, PACE_MARKER, Board

ALLOW, HOLD, BLOCK = "allow", "hold", "block"


@dataclass
class Decision:
    action: str
    reason: str = ""
    cost_center: Optional[str] = None
    urgent: Optional[str] = None


def _money(x) -> str:
    return "unset" if x is None else f"${x:,.2f}"


def _day(iso: Optional[str]) -> str:
    return iso[:10] if iso else "next month"


def urgent_reason(cfg: Config, card: dict) -> Optional[str]:
    """Why a card counts as urgent, or None."""
    task = card.get("task") or {}
    creator = task.get("created_by") or ""
    if any(fnmatch.fnmatchcase(creator, p) for p in cfg.urgent.created_by):
        return f"created by {creator}"
    title = task.get("title") or ""
    for marker in cfg.urgent.title_markers:
        if marker in title:
            return f"title carries {marker}"
    if cfg.urgent.answered_by:
        # The comment just before the latest unblock: the operator's answer to a
        # blocked card is a comment followed by the unblock.
        last_author = None
        answered = None
        for ev in card.get("events") or []:
            kind = ev.get("kind")
            if kind == "commented":
                last_author = (ev.get("payload") or {}).get("author")
            elif kind == "unblocked":
                answered = last_author
        if answered and any(fnmatch.fnmatchcase(answered, p) for p in cfg.urgent.answered_by):
            return f"resumed by {answered}'s answer"
    return None


def decide(cfg: Config, state: Optional[dict], profile: str,
           card_loader: Callable[[], dict], now: Optional[datetime] = None) -> Decision:
    if state is None or state_mod.is_stale(state, now):
        return Decision(ALLOW, "no fresh budget state: failing open")
    center = cfg.center_for(profile)
    if center is None:
        return Decision(ALLOW, f"{profile} is in no cost center")
    c = (state.get("cost_centers") or {}).get(center)
    if c is None:
        return Decision(ALLOW, f"{center} is not in the budget state yet", center)
    if c.get("state") == "exhausted":
        return Decision(BLOCK, (
            f"{BLOCK_MARKER} {center} has spent its {_money(c.get('budget_usd'))} for "
            f"{state['month']['label']}; resumes {_day(state['month'].get('end'))} "
            f"or when the operator raises its budget"), center)
    pace = c.get("pace") or {}
    if pace.get("ahead_of_pace"):
        why = urgent_reason(cfg, card_loader())
        if why:
            return Decision(ALLOW, f"ahead of pace, but urgent: {why}", center, why)
        return Decision(HOLD, (
            f"{PACE_MARKER} {center} is ahead of its pace line ({_money(c.get('spend_usd'))} spent, "
            f"{_money(pace.get('allowance_usd'))} allowed by now); eligible again "
            f"{pace.get('eligible_at') or 'next month'}"), center)
    return Decision(ALLOW, "within budget and pace", center)


def record(cfg: Config, entry: dict) -> None:
    """Append one decision to <state dir>/admission.jsonl. Never raises."""
    try:
        path = Path(cfg.state_path).parent / "admission.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:
        pass


def admit(cfg: Config, profile: str, board_name: str, task_id: str,
          board: Optional[Board] = None, now: Optional[datetime] = None) -> tuple[bool, Decision, bool]:
    """Decide and, if enforcing, act. Returns (may_start, decision, acted)."""
    board = board or Board(cfg.kanban)
    state = state_mod.read(cfg.state_path)
    d = decide(cfg, state, profile, lambda: board.show(board_name, task_id), now)
    acted = False
    if d.action != ALLOW and cfg.enforce.admission:
        if d.action == BLOCK:
            board.block(board_name, task_id, d.reason)
        else:
            board.schedule(board_name, task_id, d.reason)
        acted = True
    from .evaluator import utcnow
    record(cfg, {"at": (now or utcnow()).isoformat(timespec="seconds"), "profile": profile,
                 "board": board_name, "task": task_id, "enforced": cfg.enforce.admission,
                 "acted": acted, **asdict(d)})
    return (not acted), d, acted
