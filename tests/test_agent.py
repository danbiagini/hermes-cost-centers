"""The agent plugin: an agent sees its own budget, and nothing it reads can
break its run. Each test names what the plugin must do."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cost_centers import cli, config as config_mod
from conftest import make_db

_SPEC = importlib.util.spec_from_file_location(
    "hcc_agent", Path(__file__).resolve().parents[1] / "agent" / "__init__.py")
agent = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent)

NOW = datetime(2030, 3, 20, 12, 0, tzinfo=timezone.utc)
PRICES = {"model-a": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}


def make_state(state="ok", spend=20.0, budget=50.0, ahead=False, generated=NOW, **pace):
    c = {"sources": ["researcher"], "spend_usd": spend, "budget_usd": budget, "state": state,
         "remaining_usd": None if budget is None else max(0.0, budget - spend), "incomplete": False}
    if budget is not None:
        c["pace"] = {"allowance_usd": pace.get("allowance", 35.0), "ahead_of_pace": ahead,
                     "eligible_at": pace.get("eligible_at")}
    return {"generated_at": generated.isoformat(), "interval_seconds": 60,
            "month": {"label": "2030-03", "end": "2030-04-01T00:00:00+00:00"},
            "cost_centers": {"research": c}, "prices": PRICES}


@pytest.fixture(autouse=True)
def fresh_memory():
    agent._seen.clear()
    yield
    agent._seen.clear()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A state file and a profile home, wired through the plugin's env."""
    home = tmp_path / "root" / "profiles" / "researcher"
    home.mkdir(parents=True)
    state_file = tmp_path / "state.json"

    def put(**kw):
        kw.setdefault("generated", datetime.now(timezone.utc))
        state_file.write_text(json.dumps(make_state(**kw)))

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_COST_CENTERS_STATE", str(state_file))
    monkeypatch.delenv("HERMES_COST_CENTERS_SOURCE", raising=False)
    put()
    return home, put


# --- which budget is mine ---------------------------------------------------------

def test_profile_name_comes_from_hermes_home(tmp_path):
    assert agent.profile_name(tmp_path / ".hermes" / "profiles" / "researcher") == "researcher"
    assert agent.profile_name(tmp_path / ".hermes") == "default"


def test_operator_can_name_the_source(monkeypatch):
    monkeypatch.setenv("HERMES_COST_CENTERS_SOURCE", "lab-key")
    assert agent.source() == "lab-key"


# --- failing quietly --------------------------------------------------------------

def test_no_state_means_unavailable_and_no_note(tmp_path):
    s = agent.status(state=None, src="researcher", db=tmp_path / "none.db")
    assert s["available"] is False and agent.note(s) is None


def test_missing_state_file_reads_as_none(tmp_path):
    assert agent.read_state(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert agent.read_state(tmp_path / "bad.json") is None


def test_stale_state_is_not_presented_as_fact(tmp_path):
    s = agent.status(now=NOW, state=make_state(generated=NOW - timedelta(minutes=6)),
                     src="researcher", db=tmp_path / "none.db")
    assert s["available"] is False and "stale" in s["detail"] and agent.note(s) is None


def test_a_source_in_no_cost_center_has_no_budget(tmp_path):
    s = agent.status(now=NOW, state=make_state(), src="stranger", db=tmp_path / "none.db")
    assert s["available"] is False and "no cost center" in s["detail"]


def test_an_unreadable_database_gives_no_run_cost_but_still_answers(tmp_path):
    db = tmp_path / "state.db"
    db.write_text("this is not sqlite")
    s = agent.status("s1", now=NOW, state=make_state(), src="researcher", db=db)
    assert s["available"] is True and "this_run" not in s


# --- what the note says -----------------------------------------------------------

def _note(**kw):
    return agent.note(agent.status(now=NOW, state=make_state(**kw), src="researcher",
                                   db=Path("/nonexistent/state.db")))


def test_ok_note_gives_spend_left_and_available_now():
    text = _note(spend=20.0, allowance=35.0)
    assert "$20.00 of $50.00" in text and "$30.00 left" in text
    assert "$15.00 available now" in text and "checkpoint" not in text


def test_wrap_up_note_asks_for_a_checkpoint():
    assert "checkpoint your work" in _note(state="wrap_up", spend=42.0, allowance=48.0)


def test_exhausted_note_says_paused_refused_and_until_when():
    text = _note(state="exhausted", spend=50.0)
    assert "New runs are paused" in text and "402" in text and "2030-04-01" in text


def test_ahead_of_pace_note_says_new_runs_wait():
    text = _note(spend=40.0, allowance=35.0, ahead=True, eligible_at="2030-03-23T08:00:00+00:00")
    assert "new runs wait until 2030-03-23T08:00" in text and "available now" not in text


def test_unset_budget_note_reports_spend_only():
    assert "no budget is set" in _note(budget=None, spend=3.0)


def test_tool_answer_says_it_is_guidance_only():
    s = agent.status(now=NOW, state=make_state(), src="researcher", db=Path("/nonexistent"))
    assert "enforced outside the agent" in s["note"]


# --- this run's cost --------------------------------------------------------------

def _sessions(tmp_path):
    # run -> subagent -> its own subagent; plus an unrelated session.
    return make_db(tmp_path / "state.db", [
        ("run", "cli", "model-a", None, 1.0, 1_000_000, 0, 0, 0, None, None, "unknown"),
        ("sub", "cli", "model-a", "run", 2.0, 0, 100_000, 0, 0, None, None, "unknown"),
        ("subsub", "cli", "model-a", "sub", 3.0, 0, 0, 1_000_000, 0, None, None, "unknown"),
        ("other", "cli", "model-a", None, 4.0, 9_000_000, 0, 0, 0, None, None, "unknown"),
    ])


def test_run_cost_includes_every_descendant_and_nothing_else(tmp_path):
    run = agent.run_cost(_sessions(tmp_path), "run", PRICES)
    # 1M input x $3 + 100k output x $15 + 1M cache read x $0.30
    assert run == {"spend_usd": 4.8, "sessions": 3, "incomplete": False}


def test_run_cost_prefers_hermes_actual_cost(tmp_path):
    db = make_db(tmp_path / "state.db", [
        ("run", "cli", "model-a", None, 1.0, 1_000_000, 0, 0, 0, None, 1.25, "actual")])
    assert agent.run_cost(db, "run", PRICES)["spend_usd"] == 1.25


def test_unpriced_model_marks_the_run_cost_incomplete(tmp_path):
    db = make_db(tmp_path / "state.db", [
        ("run", "cli", "mystery", None, 1.0, 1000, 0, 0, 0, None, None, "unknown")])
    assert agent.run_cost(db, "run", PRICES) == {"spend_usd": 0.0, "sessions": 1, "incomplete": True}


def test_run_cost_shows_in_the_note(tmp_path):
    s = agent.status("run", now=NOW, state=make_state(), src="researcher", db=_sessions(tmp_path))
    assert "This run so far: $4.80" in agent.note(s)


# --- when it speaks -----------------------------------------------------------------

def test_first_turn_gets_the_note_and_a_repeat_turn_does_not(live):
    assert "[budget] research" in agent._on_pre_llm_call(session_id="s1", is_first_turn=True)["context"]
    assert agent._on_pre_llm_call(session_id="s1", is_first_turn=False) is None


def test_a_later_turn_hears_about_a_changed_state(live):
    _, put = live
    agent._on_pre_llm_call(session_id="s1", is_first_turn=True)
    put(state="wrap_up", spend=42.0, allowance=48.0)
    assert "checkpoint" in agent._on_pre_llm_call(session_id="s1", is_first_turn=False)["context"]


def test_mid_run_note_comes_once_when_the_state_worsens(live):
    _, put = live
    agent._on_pre_llm_call(session_id="s1", is_first_turn=True)
    assert agent._on_transform_tool_result(session_id="s1", result="out", tool_name="terminal") is None
    put(state="wrap_up", spend=42.0, allowance=48.0)
    noted = agent._on_transform_tool_result(session_id="s1", result="out", tool_name="terminal")
    assert noted.startswith("out\n\n[budget]") and "checkpoint" in noted
    assert agent._on_transform_tool_result(session_id="s1", result="out", tool_name="terminal") is None


def test_mid_run_note_when_the_line_falls_ahead_of_pace(live):
    _, put = live
    agent._on_pre_llm_call(session_id="s1", is_first_turn=True)
    put(spend=40.0, allowance=35.0, ahead=True, eligible_at="2030-03-23T08:00:00+00:00")
    assert "new runs wait" in agent._on_transform_tool_result(session_id="s1", result="x", tool_name="t")


def test_no_mid_run_note_when_things_improve(live):
    _, put = live
    put(state="wrap_up", spend=42.0, allowance=48.0)
    agent._on_pre_llm_call(session_id="s1", is_first_turn=True)
    put(state="ok", spend=42.0, budget=100.0, allowance=80.0)
    assert agent._on_transform_tool_result(session_id="s1", result="x", tool_name="t") is None


def test_no_mid_run_note_before_the_start_note(live):
    _, put = live
    put(state="exhausted", spend=50.0)
    assert agent._on_transform_tool_result(session_id="new", result="x", tool_name="t") is None


def test_hooks_never_raise(live, monkeypatch):
    monkeypatch.setattr(agent, "status", lambda *a, **k: 1 / 0)
    assert agent._on_pre_llm_call(session_id="s1", is_first_turn=True) is None
    agent._seen["s1"] = ("research", "ok", False)
    assert agent._on_transform_tool_result(session_id="s1", result="x", tool_name="t") is None


def test_tool_returns_json_with_this_run(live):
    home, _ = live
    make_db(home / "state.db", [("s1", "cli", "model-a", None, 1.0, 1_000_000, 0, 0, 0, None, None, "unknown")])
    out = json.loads(agent._budget_status_tool({}, session_id="s1"))
    assert out["cost_center"] == "research" and out["this_run"]["spend_usd"] == 3.0
    assert out["pace"]["available_now_usd"] == 15.0


def test_register_adds_the_tool_and_both_hooks():
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(("tool", kw["name"], kw["toolset"]))

        def register_hook(self, name, fn):
            calls.append(("hook", name))

    agent.register(Ctx())
    assert ("tool", "budget_status", "cost_centers") in calls
    assert ("hook", "pre_llm_call") in calls and ("hook", "transform_tool_result") in calls


# --- the evaluator's side -----------------------------------------------------------

def test_evaluator_publishes_the_price_table_with_overrides(hermes_home, write_config, tmp_path):
    path = write_config(f"""
cost_centers:
  alpha: {{monthly_usd: 50}}
ledger: {{source: hermes-sessions, hermes_home: {hermes_home}}}
state: {{path: {tmp_path / 'state.json'}}}
pricing:
  models:
    house-model: {{input: 1, output: 2, cache_read: 0, cache_write: 0}}
    half-known: {{input: 1}}
""")
    state = cli.evaluate_once(config_mod.load(path))
    assert state["prices"]["house-model"] == {"input": 1.0, "output": 2.0, "cache_read": 0.0, "cache_write": 0.0}
    assert "half-known" not in state["prices"] and "claude-sonnet-4-6" in state["prices"]
