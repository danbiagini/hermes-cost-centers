"""The CLI's failure paths: a failed cycle must leave the last state alone so it
goes stale, and must exit non-zero so a scheduler reports the fault."""

import json

import pytest

from hermes_cost_centers import cli, state
from hermes_cost_centers.adapters import get_adapter
from hermes_cost_centers import config as config_mod


@pytest.fixture
def setup(hermes_home, write_config, tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    path = write_config(f"""
cost_centers:
  alpha: {{monthly_usd: 50}}
ledger: {{source: hermes-sessions, hermes_home: {hermes_home}}}
state: {{path: {state_path}}}
interval_seconds: 30
""")
    monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(path))
    return state_path


def boom(*a, **k):
    raise RuntimeError("ledger unreachable")


def test_failed_cycle_exits_1_and_keeps_last_state(setup, monkeypatch, capsys):
    assert cli.main(["evaluate"]) == 0
    before = setup.read_text()
    monkeypatch.setattr(cli, "evaluate_once", boom)
    assert cli.main(["evaluate"]) == 1
    assert setup.read_text() == before  # left in place, to go stale
    assert "ledger unreachable" in capsys.readouterr().err


def test_loop_survives_a_failed_cycle(setup, monkeypatch):
    calls, sleeps = [], []
    real = cli.evaluate_once

    def flaky(cfg):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("transient")
        return real(cfg)

    class Stop(Exception):
        pass

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(cli, "evaluate_once", flaky)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        cli.main(["evaluate", "--loop"])
    assert len(calls) == 2 and sleeps == [30, 30]
    assert state.read(setup)["cost_centers"]["alpha"]["rows"] > 0  # the second cycle wrote


def test_stdout_does_not_write_state(setup, capsys):
    assert cli.main(["evaluate", "--stdout"]) == 0
    assert json.loads(capsys.readouterr().out)["cost_centers"]["alpha"]
    assert not setup.exists()


def test_status_before_any_state(setup, capsys):
    assert cli.main(["status"]) == 1
    assert "no state" in capsys.readouterr().err


def test_status_marks_stale_and_json(setup, capsys, monkeypatch):
    cli.main(["evaluate"])
    s = state.read(setup)
    s["generated_at"] = "2000-01-01T00:00:00+00:00"
    state.write(setup, s)
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    assert "STALE" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["generated_at"].startswith("2000")


def test_explain_unknown_and_json(setup, capsys):
    assert cli.main(["explain", "nope"]) == 1
    assert cli.main(["explain", "alpha", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any(r["ref"] == "a1" and r["subagents"] for r in out["runs"])


def test_bad_config_exits_2(write_config, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(write_config("cost_centers: {}\n")))
    assert cli.main(["status"]) == 2
    assert "at least one" in capsys.readouterr().err


@pytest.mark.parametrize("ledger,cls", [
    ("{source: sql, url: 'sqlite:///x.db', query: 'SELECT 1'}", "Sql"),
    ("{source: litellm, url: 'http://p'}", "LiteLLM"),
    ("{source: hermes-sessions}", "HermesSessions"),
])
def test_adapter_dispatch(ledger, cls):
    cfg = config_mod.parse(__import__("yaml").safe_load(f"cost_centers: {{a: {{}}}}\nledger: {ledger}\n"))
    assert type(get_adapter(cfg)).__name__ == cls


def test_unknown_ledger_source():
    cfg = config_mod.parse({"cost_centers": {"a": {}}, "ledger": {"source": "csv"}})
    with pytest.raises(ValueError, match="unknown ledger.source"):
        get_adapter(cfg)
