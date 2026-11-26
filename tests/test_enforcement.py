"""Admission, the resume pass, the budget table and the backstop."""

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from hermes_cost_centers import admission, backstop, cli, config, resume, sql_state, state
from hermes_cost_centers.kanban import BLOCK_MARKER, PACE_MARKER, Board, held_by_us, last_hold_reason

UTC = timezone.utc
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)

CFG_TEXT = """
cost_centers:
  research: {sources: [researcher], monthly_usd: 50}
  ops: {sources: [ops-bot], monthly_usd: 120}
  shared: {sources: [router], monthly_usd: null}
urgent:
  created_by: ["chat:*", "operator"]
  answered_by: [operator]
  title_markers: ["[time-critical]"]
"""


def cfg(extra="", path=None):
    c = config.parse(yaml.safe_load(CFG_TEXT + extra))
    if path:
        object.__setattr__(c, "state", {**c.state, "path": str(path)})
    return c


def budget_state(research="ok", ahead=False, generated=NOW, ops_state="ok"):
    pace = {"ahead_of_pace": ahead, "allowance_usd": 25.0,
            "eligible_at": "2026-11-20T00:00:00+00:00" if ahead else None}
    return {
        "generated_at": generated.isoformat(), "interval_seconds": 60,
        "month": {"label": "2026-11", "start": "2026-11-01T00:00:00+00:00", "end": "2026-12-01T00:00:00+00:00"},
        "cost_centers": {
            "research": {"sources": ["researcher"], "state": research, "spend_usd": 30.0, "budget_usd": 40.0, "pace": pace},
            "ops": {"sources": ["ops-bot"], "state": ops_state, "spend_usd": 10.0, "budget_usd": 120.0,
                    "pace": {"ahead_of_pace": False}},
            "shared": {"sources": ["router"], "state": "unset", "spend_usd": 4.0, "budget_usd": None, "pace": None},
        },
    }


def card(created_by="user", title="do a thing", events=()):
    return {"task": {"created_by": created_by, "title": title, "assignee": "researcher"}, "events": list(events)}


class TestDecide:
    def d(self, st, profile="researcher", c=None, now=NOW):
        return admission.decide(cfg(), st, profile, lambda: c or card(), now)

    def test_missing_or_stale_state_fails_open(self):
        assert self.d(None).action == "allow"
        assert self.d(budget_state(generated=NOW - timedelta(minutes=6))).action == "allow"

    def test_profile_in_no_cost_center(self):
        assert self.d(budget_state(), profile="stranger").action == "allow"

    def test_exhausted_blocks_with_the_marker(self):
        d = self.d(budget_state(research="exhausted"))
        assert d.action == "block" and d.reason.startswith(BLOCK_MARKER)
        assert "research has spent its $40.00 for 2026-11; resumes 2026-12-01" in d.reason

    def test_exhausted_blocks_even_urgent_cards(self):
        d = self.d(budget_state(research="exhausted"), c=card(created_by="chat:ask"))
        assert d.action == "block"

    def test_ahead_of_pace_holds(self):
        d = self.d(budget_state(ahead=True))
        assert d.action == "hold" and d.reason.startswith(PACE_MARKER)
        assert "eligible again 2026-11-20" in d.reason

    @pytest.mark.parametrize("c,why", [
        (card(created_by="chat:ask"), "created by chat:ask"),
        (card(created_by="operator"), "created by operator"),
        (card(title="[time-critical] call back"), "title carries [time-critical]"),
        (card(events=[{"kind": "blocked"}, {"kind": "commented", "payload": {"author": "operator"}},
                      {"kind": "unblocked"}]), "resumed by operator's answer"),
    ])
    def test_urgent_cards_run_ahead_of_pace(self, c, why):
        d = self.d(budget_state(ahead=True), c=c)
        assert d.action == "allow" and d.urgent == why

    def test_an_agent_comment_before_unblock_is_not_an_answer(self):
        c = card(events=[{"kind": "commented", "payload": {"author": "researcher"}}, {"kind": "unblocked"}])
        assert self.d(budget_state(ahead=True), c=c).action == "hold"

    def test_card_is_only_loaded_when_ahead_of_pace(self):
        def boom():
            raise AssertionError("card loaded")
        assert admission.decide(cfg(), budget_state(), "researcher", boom, NOW).action == "allow"


class FakeBoard:
    def __init__(self, cards=None):
        self.cards_by = cards or {}   # (board, status) -> [card dicts with id, assignee, events]
        self.calls = []

    def held_cards(self):
        return None   # no direct read: exercises the CLI fallback

    def boards(self):
        return sorted({b for b, _ in self.cards_by})

    def cards(self, board, status):
        return [{"id": c["id"], "assignee": c["assignee"]} for c in self.cards_by.get((board, status), [])]

    def show(self, board, task_id):
        for (b, _), cs in self.cards_by.items():
            for c in cs:
                if b == board and c["id"] == task_id:
                    return {"task": {"assignee": c["assignee"], "created_by": c.get("created_by", "user"),
                                     "title": "t"}, "events": c["events"]}
        return {}

    def block(self, board, task_id, reason):
        self.calls.append(("block", board, task_id, reason))

    def schedule(self, board, task_id, reason):
        self.calls.append(("schedule", board, task_id, reason))

    def unblock(self, board, task_id):
        self.calls.append(("unblock", board, task_id))


class TestAdmit:
    def setup(self, tmp_path, enforce):
        p = tmp_path / "state.json"
        state.write(p, budget_state(research="exhausted"))
        return cfg(f"enforce: {{admission: {str(enforce).lower()}}}\n", path=p)

    def test_dry_run_records_but_lets_the_run_start(self, tmp_path):
        c, b = self.setup(tmp_path, False), FakeBoard()
        may_start, d, acted = admission.admit(c, "researcher", "research", "t_1", board=b, now=NOW)
        assert may_start and d.action == "block" and not acted and b.calls == []
        entry = json.loads((tmp_path / "admission.jsonl").read_text().splitlines()[-1])
        assert entry["action"] == "block" and entry["enforced"] is False and entry["task"] == "t_1"

    def test_enforced_block(self, tmp_path):
        c, b = self.setup(tmp_path, True), FakeBoard()
        may_start, d, acted = admission.admit(c, "researcher", "research", "t_1", board=b, now=NOW)
        assert not may_start and acted
        assert b.calls[0][:3] == ("block", "research", "t_1")

    def test_enforced_hold(self, tmp_path):
        p = tmp_path / "state.json"
        state.write(p, budget_state(ahead=True))
        c, b = cfg("enforce: {admission: true}\n", path=p), FakeBoard({("research", "ready"): [
            {"id": "t_2", "assignee": "researcher", "events": []}]})
        may_start, d, _ = admission.admit(c, "researcher", "research", "t_2", board=b, now=NOW)
        assert not may_start and b.calls[0][0] == "schedule"


def held(id_, marker, assignee="researcher"):
    return {"id": id_, "assignee": assignee,
            "events": [{"kind": "scheduled" if marker == PACE_MARKER else "blocked",
                        "payload": {"reason": f"{marker} test"}}]}


class TestResume:
    def run(self, st, cards):
        b = FakeBoard(cards)
        out = resume.resume(cfg(), st, board=b)
        return [c[2] for c in b.calls if c[0] == "unblock"], out

    def test_pace_hold_released_when_back_on_pace(self):
        got, _ = self.run(budget_state(ahead=False), {("research", "scheduled"): [held("t_1", PACE_MARKER)]})
        assert got == ["t_1"]

    def test_pace_hold_kept_while_ahead(self):
        got, _ = self.run(budget_state(ahead=True), {("research", "scheduled"): [held("t_1", PACE_MARKER)]})
        assert got == []

    def test_pace_hold_kept_if_now_exhausted(self):
        got, _ = self.run(budget_state(research="exhausted"), {("research", "scheduled"): [held("t_1", PACE_MARKER)]})
        assert got == []

    def test_budget_block_released_on_reset_or_raise(self):
        got, _ = self.run(budget_state(research="ok"), {("research", "blocked"): [held("t_1", BLOCK_MARKER)]})
        assert got == ["t_1"]

    def test_budget_block_kept_while_exhausted(self):
        got, _ = self.run(budget_state(research="exhausted"), {("research", "blocked"): [held("t_1", BLOCK_MARKER)]})
        assert got == []

    def test_other_holds_are_never_touched(self):
        other = {"id": "t_9", "assignee": "researcher",
                 "events": [{"kind": "blocked", "payload": {"reason": "needs the operator's answer"}}]}
        mismatched = held("t_8", PACE_MARKER)  # a pace marker on a blocked card
        got, _ = self.run(budget_state(), {("research", "blocked"): [other, mismatched]})
        assert got == []

    def test_latest_hold_reason_wins(self):
        c = {"id": "t_7", "assignee": "researcher", "events": [
            {"kind": "blocked", "payload": {"reason": f"{BLOCK_MARKER} old"}},
            {"kind": "unblocked"},
            {"kind": "blocked", "payload": {"reason": "an agent asks the operator something"}}]}
        got, _ = self.run(budget_state(), {("research", "blocked"): [c]})
        assert got == []


def test_markers():
    assert held_by_us(f"{PACE_MARKER} x") == PACE_MARKER
    assert held_by_us(f"{BLOCK_MARKER} x") == BLOCK_MARKER
    assert held_by_us("[cost-centers]x") == BLOCK_MARKER
    assert held_by_us("blocked: [cost-centers] quoted") is None
    assert last_hold_reason({"events": []}) is None


class TestBoardCli:
    def test_commands(self):
        seen = []

        def runner(argv, **kw):
            seen.append(argv)
            out = '[{"slug": "research"}, {"slug": "old", "archived": true}]' if "boards" in argv else "[]"
            return subprocess.CompletedProcess(argv, 0, out, "")

        b = Board({"hermes_bin": "/x/hermes", "env": {"HERMES_KANBAN_HOME": "/k"}}, runner=runner)
        assert b.boards() == ["research"]
        b.block("research", "t_1", "r")
        b.schedule("research", "t_1", "r")
        b.unblock("research", "t_1")
        assert seen[1] == ["/x/hermes", "kanban", "--board", "research", "block", "--kind", "capability", "t_1", "r"]
        assert seen[2][4] == "schedule" and seen[3][4] == "unblock"
        assert b.env["HERMES_KANBAN_HOME"] == "/k"

    def test_failure_raises(self):
        b = Board(runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no such task"))
        with pytest.raises(RuntimeError, match="no such task"):
            b.unblock("research", "t_1")


class TestTableAndBackstop:
    def make_table(self, tmp_path):
        db = tmp_path / "b.db"
        conn = sqlite3.connect(db)
        conn.execute(sql_state.DDL.format(table="budget_state").replace("timestamptz", "text")
                     .replace("numeric(12,6)", "real").replace("numeric(12,2)", "real"))
        conn.commit(); conn.close()
        return db

    def test_write_upserts_and_drops_removed_sources(self, tmp_path):
        db = self.make_table(tmp_path)
        n = sql_state.write({"url": f"sqlite:///{db}"}, budget_state(research="exhausted"), True)
        assert n == 3
        st = budget_state()
        del st["cost_centers"]["shared"]
        sql_state.write({"url": f"sqlite:///{db}"}, st, False)
        rows = dict(sqlite3.connect(db).execute("select source, state from budget_state").fetchall())
        assert rows == {"researcher": "ok", "ops-bot": "ok"}

    def test_bad_table_name_refused(self):
        with pytest.raises(ValueError, match="plain table name"):
            sql_state.write({"url": "sqlite:///x", "table": "t; drop"}, budget_state(), False)

    def backstop_for(self, tmp_path, st, enforce):
        db = self.make_table(tmp_path)
        sql_state.write({"url": f"sqlite:///{db}"}, st, enforce)
        return backstop.Backstop(lambda: sqlite3.connect(db), paramstyle="?", cache_seconds=0)

    def test_enforced_refusal_leaves_a_log_line(self, tmp_path, caplog):
        b = self.backstop_for(tmp_path, budget_state(research="exhausted"), True)
        assert not b.check("researcher", now=NOW).allow
        assert "REFUSED researcher: budget exhausted: research" in caplog.text
        caplog.clear()
        assert b.check("ops-bot", now=NOW).allow and not caplog.text

    def test_enforced_refusal_message(self, tmp_path):
        b = self.backstop_for(tmp_path, budget_state(research="exhausted"), True)
        d = b.check("researcher", now=NOW)
        assert not d.allow
        assert d.message == ("budget exhausted: research has spent its $40.00 for 2026-11; "
                             "resumes 2026-12-01 (hard billing limit)")
        assert b.check("ops-bot", now=NOW).allow and b.check("router", now=NOW).allow

    def test_dry_run_allows_and_says_so(self, tmp_path, caplog):
        b = self.backstop_for(tmp_path, budget_state(research="exhausted"), False)
        d = b.check("researcher", now=NOW)
        assert d.allow and d.would_refuse
        assert "DRY RUN, would refuse researcher" in caplog.text

    def test_stale_row_fails_open(self, tmp_path):
        b = self.backstop_for(tmp_path, budget_state(research="exhausted"), True)
        assert b.check("researcher", now=NOW + timedelta(minutes=6)).allow

    def test_unknown_source_and_no_source_allow(self, tmp_path):
        b = self.backstop_for(tmp_path, budget_state(research="exhausted"), True)
        assert b.check("nobody", now=NOW).allow and b.check(None).allow

    def test_broken_database_never_raises(self, tmp_path):
        def connect():
            raise RuntimeError("db down")
        assert backstop.Backstop(connect).check("researcher").allow
        b = backstop.Backstop(lambda: sqlite3.connect(tmp_path / "empty.db"), paramstyle="?")
        assert b.check("researcher").allow   # no such table

    def test_cache(self, tmp_path):
        db = self.make_table(tmp_path)
        sql_state.write({"url": f"sqlite:///{db}"}, budget_state(), True)
        b = backstop.Backstop(lambda: sqlite3.connect(db), paramstyle="?", cache_seconds=60)
        assert b.check("researcher", now=NOW).allow
        sql_state.write({"url": f"sqlite:///{db}"}, budget_state(research="exhausted"), True)
        assert b.check("researcher", now=NOW).allow          # cached
        b.cache_seconds = 0
        assert not b.check("researcher", now=NOW).allow


class TestCli:
    def test_admit_fails_open_on_bad_config(self, write_config, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(write_config("cost_centers: {}\n")))
        assert cli.main(["admit", "--profile", "p", "--board", "b", "--task", "t"]) == cli.ADMIT_START
        assert "failed open" in capsys.readouterr().err

    def test_admit_exit_codes(self, write_config, tmp_path, monkeypatch):
        st = tmp_path / "s" / "state.json"
        state.write(st, budget_state(research="exhausted", generated=datetime.now(UTC)))
        for enforce, want in (("false", cli.ADMIT_START), ("true", cli.ADMIT_HELD)):
            monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(write_config(
                CFG_TEXT + f"enforce: {{admission: {enforce}}}\nstate: {{path: {st}}}\n")))
            monkeypatch.setattr(admission, "Board", lambda *a, **k: FakeBoard())
            from hermes_cost_centers import evaluator
            monkeypatch.setattr(evaluator, "utcnow", lambda: datetime.now(UTC))
            assert cli.main(["admit", "--profile", "researcher", "--board", "research", "--task", "t_1"]) == want

    def test_publish_reports_a_table_failure_as_a_fault(self, tmp_path):
        c = cfg(f"state: {{path: {tmp_path}/s.json, sql: {{url: 'sqlite:///{tmp_path}/none.db'}}}}\n")
        faults = cli.publish(c, budget_state())
        assert faults and "budget table not written" in faults[0]
        assert (tmp_path / "s.json").exists()   # the file is written first regardless


def make_board_db(path, cards):
    """A kanban.db in Hermes's layout: tasks + task_events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT)")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INT,"
                 " kind TEXT, payload TEXT, created_at INTEGER)")
    for task_id, status, assignee, events in cards:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", (task_id, "t", assignee, status))
        for i, (kind, reason) in enumerate(events):
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                         (task_id, kind, json.dumps({"reason": reason} if reason else {}), 1000 + i))
    conn.commit()
    conn.close()


class TestDirectRead:
    def home(self, tmp_path):
        home = tmp_path / "kanban-home"
        make_board_db(home / "kanban" / "boards" / "research" / "kanban.db", [
            ("t_pace", "scheduled", "researcher", [("scheduled", f"{PACE_MARKER} ahead")]),
            ("t_budget", "blocked", "researcher", [("blocked", f"{BLOCK_MARKER} spent")]),
            ("t_human", "blocked", "researcher", [("blocked", "waiting on the operator")]),
            # Ours once, then re-blocked for another reason: the latest reason decides.
            ("t_reblocked", "blocked", "researcher", [("blocked", f"{BLOCK_MARKER} spent"), ("unblocked", None),
                                                      ("blocked", "an agent asks something")]),
            ("t_done", "done", "researcher", [("blocked", f"{BLOCK_MARKER} spent")]),
        ])
        (home / "kanban" / "boards" / "research" / "board.json").write_text("{}")
        make_board_db(home / "kanban" / "boards" / "old" / "kanban.db", [
            ("t_old", "blocked", "researcher", [("blocked", f"{BLOCK_MARKER} spent")])])
        (home / "kanban" / "boards" / "old" / "board.json").write_text('{"archived": true}')
        make_board_db(home / "kanban.db", [
            ("t_default", "scheduled", "ops-bot", [("scheduled", f"{PACE_MARKER} ahead")])])
        return home

    def board(self, home, calls):
        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return Board({"env": {"HERMES_KANBAN_HOME": str(home)}}, runner=runner)

    def test_finds_only_our_holds_without_any_cli_call(self, tmp_path):
        calls = []
        held = self.board(self.home(tmp_path), calls).held_cards()
        got = sorted((h["board"], h["id"], h["status"], h["marker"]) for h in held)
        assert got == [
            ("default", "t_default", "scheduled", PACE_MARKER),
            ("research", "t_budget", "blocked", BLOCK_MARKER),
            ("research", "t_pace", "scheduled", PACE_MARKER),
        ]
        assert calls == []   # archived board skipped, done card skipped, no process started

    def test_resume_releases_through_the_cli_only(self, tmp_path):
        calls = []
        b = self.board(self.home(tmp_path), calls)
        out = resume.resume(cfg(), budget_state(ahead=False), board=b)
        assert sorted(r["task"] for r in out) == ["t_budget", "t_default", "t_pace"]
        assert all(c[:2] == ["hermes", "kanban"] and "unblock" in c for c in calls)
        assert len(calls) == 3

    def test_still_ahead_keeps_the_pace_hold(self, tmp_path):
        calls = []
        out = resume.resume(cfg(), budget_state(ahead=True), board=self.board(self.home(tmp_path), calls))
        assert sorted(r["task"] for r in out) == ["t_budget", "t_default"]   # ops-bot isn't ahead

    def test_falls_back_when_the_schema_is_unknown(self, tmp_path):
        home = tmp_path / "odd"
        (home / "kanban" / "boards" / "x").mkdir(parents=True)
        sqlite3.connect(home / "kanban" / "boards" / "x" / "kanban.db").execute("CREATE TABLE other (a)").connection.commit()
        assert Board({"home": str(home)}).held_cards() is None

    def test_no_home_means_fallback(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
        assert Board({}).held_cards() is None
