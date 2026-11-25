"""hermes-cost-centers, the agent half: an agent can see its own budget.

Install it in each budgeted profile. It adds:

- **``budget_status``**, a tool: the cost center's spend and budget for the
  month, what's available now under the pace line, and what this run has spent
  so far, subagents included.
- **A note at the start of a run**: one line, added to the first turn's
  context. Under ``wrap_up`` it asks the agent to checkpoint.
- **One note mid-run** if the cost center's state changes while the run is going
  (it reaches ``wrap_up``, falls ahead of pace or is exhausted), added to the
  next tool result. At most once per change.

**Guidance only.** Nothing here enforces anything: admission and the backstop
run outside the agent, where it can't reach them. This plugin only reads:

- the budget state the evaluator writes (``HERMES_COST_CENTERS_STATE``, default
  ``/var/lib/hermes-cost-centers/state.json``), which must be readable by the
  agent's user;
- the profile's own ``state.db``, for this run's tokens, priced with the table
  the evaluator publishes in the state.

It needs only the standard library, and every failure is silent: no state, a
stale state or an unreadable database means no note and a tool answer that says
so, never an error in the agent's run.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("hermes_cost_centers.agent")

STATE_ENV = "HERMES_COST_CENTERS_STATE"
SOURCE_ENV = "HERMES_COST_CENTERS_SOURCE"
DEFAULT_STATE = "/var/lib/hermes-cost-centers/state.json"
STALE_INTERVALS = 5
TOOLSET = "cost_centers"
GUIDANCE = ("Guidance only: budgets are enforced outside the agent. A spent budget pauses "
            "new runs, and model calls are refused with HTTP 402 'budget exhausted'.")


# --- reading ------------------------------------------------------------------

def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def profile_name(home: Optional[Path] = None) -> str:
    """The running profile: ``<root>/profiles/<name>`` is ``name``, else ``default``."""
    home = Path(home) if home else _hermes_home()
    if home.parent.name == "profiles":
        return home.name
    return "default"


def source() -> str:
    """This agent's ledger source: the profile name, unless the operator says otherwise."""
    return os.environ.get(SOURCE_ENV) or profile_name()


def state_path() -> Path:
    return Path(os.environ.get(STATE_ENV) or DEFAULT_STATE)


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def read_state(path: Optional[Path] = None) -> Optional[dict]:
    """The budget state, or None if it is missing or unreadable. Never raises."""
    try:
        with Path(path or state_path()).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def is_stale(state: dict, now: Optional[datetime] = None) -> bool:
    generated = _parse_time(state.get("generated_at"))
    if generated is None:
        return True
    now = now or datetime.now(timezone.utc)
    interval = state.get("interval_seconds") or 60
    return (now - generated).total_seconds() > STALE_INTERVALS * interval


def center_for(state: dict, src: str) -> tuple[Optional[str], Optional[dict]]:
    for name, c in (state.get("cost_centers") or {}).items():
        if src in (c.get("sources") or []):
            return name, c
    return None, None


_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _normalize_model(model: str) -> str:
    name = (model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    if name.startswith("anthropic."):
        name = name[len("anthropic."):]
    return _DATE_SUFFIX.sub("", name)


def _price(prices: dict, model: str, tokens: tuple[int, int, int, int]) -> Optional[float]:
    rates = prices.get(model) or prices.get(_normalize_model(model))
    keys = ("input", "output", "cache_read", "cache_write")
    if not rates or any(rates.get(k) is None for k in keys):
        return None
    return sum(n * rates[k] for n, k in zip(tokens, keys)) / 1_000_000


def run_cost(db: Path, session_id: str, prices: dict) -> Optional[dict]:
    """What this run has spent: the session and every session descended from it
    (subagents, and continuations after compression). None if unknown."""
    if not session_id or not Path(db).is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        has_parent = "parent_session_id" in cols
        total, unpriced, sessions = 0.0, 0, 0
        todo, seen = [session_id], set()
        while todo:
            sid = todo.pop()
            if sid in seen:
                continue
            seen.add(sid)
            row = conn.execute(
                """SELECT model, input_tokens, output_tokens, cache_read_tokens,
                          cache_write_tokens, actual_cost_usd, estimated_cost_usd, cost_status
                     FROM sessions WHERE id = ?""", (sid,)).fetchone()
            if row:
                sessions += 1
                model, i, o, cr, cw, actual, estimated, status = row
                tokens = (int(i or 0), int(o or 0), int(cr or 0), int(cw or 0))
                if status == "actual" and actual is not None:
                    total += float(actual)
                elif status == "estimated" and estimated is not None:
                    total += float(estimated)
                elif status == "included" or sum(tokens) == 0:
                    pass
                else:
                    cost = _price(prices, model or "", tokens)
                    if cost is None:
                        unpriced += 1
                    else:
                        total += cost
            if has_parent:
                todo.extend(r[0] for r in conn.execute(
                    "SELECT id FROM sessions WHERE parent_session_id = ?", (sid,)))
        return {"spend_usd": round(total, 4), "sessions": sessions, "incomplete": unpriced > 0}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# --- the answer ---------------------------------------------------------------

def status(session_id: str = "", now: Optional[datetime] = None,
           state: Optional[dict] = None, src: Optional[str] = None,
           db: Optional[Path] = None, with_run: bool = True) -> dict:
    """Everything the tool returns and the notes are made from. ``with_run``
    off skips this run's own cost, for a check that only wants the state."""
    src = src or source()
    state = state if state is not None else read_state()
    if state is None:
        return {"available": False, "source": src, "detail": "no budget state found", "note": GUIDANCE}
    if is_stale(state, now):
        return {"available": False, "source": src, "note": GUIDANCE,
                "detail": f"the budget state is stale (generated {state.get('generated_at')})"}
    name, c = center_for(state, src)
    if c is None:
        return {"available": False, "source": src, "note": GUIDANCE,
                "detail": f"{src} is in no cost center, so it has no budget"}
    pace = c.get("pace") or {}
    budget = c.get("budget_usd")
    spend = c.get("spend_usd") or 0.0
    out = {
        "available": True,
        "source": src,
        "cost_center": name,
        "month": (state.get("month") or {}).get("label"),
        "resets_at": (state.get("month") or {}).get("end"),
        "state": c.get("state"),
        "spend_usd": spend,
        "budget_usd": budget,
        "remaining_usd": c.get("remaining_usd"),
        "spend_is_a_floor": bool(c.get("incomplete")),
        "as_of": state.get("generated_at"),
        "note": GUIDANCE,
    }
    if pace:
        allowance = pace.get("allowance_usd")
        out["pace"] = {
            "ahead_of_pace": bool(pace.get("ahead_of_pace")),
            "allowance_usd": allowance,
            "available_now_usd": round(max(0.0, allowance - spend), 2) if allowance is not None else None,
            "eligible_at": pace.get("eligible_at"),
        }
    run = run_cost(db or _hermes_home() / "state.db", session_id, state.get("prices") or {}) if with_run else None
    if run is not None:
        out["this_run"] = run
    return out


def _money(x: Optional[float]) -> str:
    return "unset" if x is None else f"${x:,.2f}"


def note(s: dict) -> Optional[str]:
    """One line for the agent's context, or None when there's nothing to say."""
    if not s.get("available"):
        return None
    cc, budget, spend = s["cost_center"], s.get("budget_usd"), s.get("spend_usd") or 0.0
    floor = "+" if s.get("spend_is_a_floor") else ""
    run = s.get("this_run") or {}
    run_txt = f" This run so far: {_money(run['spend_usd'])}." if run.get("spend_usd") else ""
    if budget is None:
        return f"[budget] {cc}: {_money(spend)}{floor} spent this month; no budget is set.{run_txt}"
    if s.get("state") == "exhausted":
        resets = (s.get("resets_at") or "")[:10] or "next month"
        return (f"[budget] {cc} has spent its {_money(budget)} for {s.get('month')}. New runs are paused and "
                f"model calls may be refused (HTTP 402) until {resets} or until the operator raises the "
                f"budget. Stop starting new work: record where you are and what's left, then finish.{run_txt}")
    text = (f"[budget] {cc}: {_money(spend)}{floor} of {_money(budget)} spent this month, "
            f"{_money(s.get('remaining_usd'))} left.")
    pace = s.get("pace") or {}
    if pace.get("ahead_of_pace"):
        text += (f" Ahead of its pace line: new runs wait until {(pace.get('eligible_at') or '')[:16]} UTC,"
                 f" so keep this one to what the card needs.")
    elif pace.get("available_now_usd") is not None:
        text += f" {_money(pace['available_now_usd'])} available now under the pace line."
    text += run_txt
    if s.get("state") == "wrap_up":
        text += (" The budget is nearly spent: checkpoint your work, record what's left, and start no new"
                 " branches of work.")
    return text


def _signature(s: dict) -> tuple:
    """What a mid-run note reacts to: a change of state or of pace."""
    if not s.get("available"):
        return ()
    return (s.get("cost_center"), s.get("state"), bool((s.get("pace") or {}).get("ahead_of_pace")))


# --- the hooks ----------------------------------------------------------------

_lock = threading.Lock()
_seen: dict[str, tuple] = {}  # session id -> the signature last told to it
_MAX_SESSIONS = 512


def _remember(session_id: str, sig: tuple) -> None:
    with _lock:
        if len(_seen) >= _MAX_SESSIONS:
            _seen.pop(next(iter(_seen)))
        _seen[session_id] = sig


def _log_note(kind: str, session_id: str, s: dict) -> None:
    """One log line per note given, so an operator can see the agent was told.
    Hermes adds hook context to the model's input without saving it to the
    session's history, so this line is the only record that a note went out."""
    pace = s.get("pace") or {}
    log.info("budget note given: kind=%s session=%s cost_center=%s state=%s ahead_of_pace=%s",
             kind, session_id or "-", s.get("cost_center"), s.get("state"),
             bool(pace.get("ahead_of_pace")))


def _budget_status_tool(args: dict, **kwargs) -> str:
    try:
        s = status(kwargs.get("session_id") or "")
        if s.get("available") and kwargs.get("session_id"):
            _remember(kwargs["session_id"], _signature(s))
        return json.dumps(s)
    except Exception as exc:  # pragma: no cover - defensive
        return json.dumps({"available": False, "detail": f"budget status failed: {exc}", "note": GUIDANCE})


def _on_pre_llm_call(session_id: str = "", is_first_turn: bool = False, **_: Any) -> Optional[dict]:
    """The note at the start of a run, and again on a later turn if the state changed."""
    try:
        s = status(session_id)
        sig = _signature(s)
        with _lock:
            told = _seen.get(session_id)
        if not is_first_turn and told == sig:
            return None
        text = note(s)
        if session_id:
            _remember(session_id, sig)
        if text:
            _log_note("start" if is_first_turn else "changed", session_id, s)
        elif is_first_turn:
            log.info("budget note not given: session=%s %s", session_id or "-",
                     s.get("detail") or "nothing to say")
        return {"context": text} if text else None
    except Exception:
        return None


# Only a move to a worse state is worth interrupting a run for.
_WORSE = ("wrap_up", "exhausted")


def _on_transform_tool_result(session_id: str = "", result: Any = None, tool_name: str = "",
                              **_: Any) -> Optional[str]:
    """Add one note to a tool result when the budget's state worsened mid-run."""
    try:
        if not session_id or not isinstance(result, str) or tool_name == "budget_status":
            return None
        with _lock:
            told = _seen.get(session_id)
        if not told:
            return None  # no start note yet (pre_llm_call gives it), or nothing was known then
        sig = _signature(status(session_id, with_run=False))
        if not sig or sig == told:
            return None
        _remember(session_id, sig)
        worse = (sig[1] in _WORSE and sig[1] != told[1]) or (sig[2] and not told[2])
        if not worse:
            return None
        s = status(session_id)
        text = note(s)
        if not text:
            return None
        _log_note("mid_run", session_id, s)
        return f"{result}\n\n{text}"
    except Exception:
        return None


SCHEMA = {
    "name": "budget_status",
    "description": (
        "Your cost center's budget for this month: spent, left, what's available now under the pace "
        "line, and what this run has spent so far (subagents included). Check it before starting a "
        "large or open-ended piece of work, and size the work to what's available. " + GUIDANCE),
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def register(ctx) -> None:
    ctx.register_tool(name="budget_status", toolset=TOOLSET, schema=SCHEMA,
                      handler=_budget_status_tool, description=SCHEMA["description"], emoji="💰")
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
