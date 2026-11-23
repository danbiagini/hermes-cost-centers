import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cost_centers import cli, state

UTC = timezone.utc


def test_write_is_atomic_and_readable(tmp_path):
    p = tmp_path / "sub" / "state.json"
    state.write(p, {"generated_at": "2026-11-16T00:00:00+00:00", "interval_seconds": 60})
    assert state.read(p)["interval_seconds"] == 60
    assert [f.name for f in p.parent.iterdir()] == ["state.json"]


def test_read_never_raises(tmp_path):
    assert state.read(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert state.read(bad) is None


def test_stale_after_five_intervals():
    now = datetime(2026, 11, 16, tzinfo=UTC)
    fresh = {"generated_at": (now - timedelta(seconds=299)).isoformat(), "interval_seconds": 60}
    old = {"generated_at": (now - timedelta(seconds=301)).isoformat(), "interval_seconds": 60}
    assert not state.is_stale(fresh, now)
    assert state.is_stale(old, now)
    assert state.is_stale({}, now)


@pytest.fixture
def configured(hermes_home, write_config, tmp_path, monkeypatch):
    state_path = tmp_path / "state" / "state.json"
    path = write_config(f"""
account: {{target_usd: 100}}
cost_centers:
  alpha: {{monthly_usd: 50}}
  beta: {{monthly_usd: null}}
ledger: {{source: hermes-sessions, hermes_home: {hermes_home}}}
state: {{path: {state_path}}}
""")
    monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(path))
    return path, state_path


def test_cli_evaluate_then_status(configured, capsys):
    path, state_path = configured
    assert cli.main(["evaluate"]) == 0
    s = json.loads(state_path.read_text())
    # "default" maps to no cost center, so it shows as unattributed.
    assert "default" in s["unattributed"]
    assert s["cost_centers"]["alpha"]["incomplete"]  # the mystery model
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "unattributed" in out and "floor" in out


def test_cli_explain(configured, capsys):
    assert cli.main(["explain", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "claude-sonnet-4-6" in out and "1 subagents" in out


def _router():
    path = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hcc_plugin_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def client():
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()
    app.include_router(_router(), prefix="/api/plugins/hermes-cost-centers")
    return TestClient(app)


def test_api_state_before_and_after_evaluate(configured, client):
    r = client.get("/api/plugins/hermes-cost-centers/state").json()
    assert r["available"] is False and r["stale"] is True
    cli.main(["evaluate"])
    r = client.get("/api/plugins/hermes-cost-centers/state").json()
    assert r["available"] and not r["stale"]
    assert set(r["state"]["cost_centers"]) == {"alpha", "beta"}


def test_api_runs(configured, client):
    from conftest import MONTH0, day

    r = client.get(f"/api/plugins/hermes-cost-centers/cost-centers/alpha/runs?month={MONTH0:%Y-%m}")
    assert r.status_code == 200
    body = r.json()
    a1 = next(x for x in body["runs"] if x["ref"] == "a1")
    assert [s["ref"] for s in a1["subagents"]] == ["a2"]
    assert f"{day(2):%Y-%m-%d}" in body["daily_usd"]
    assert client.get("/api/plugins/hermes-cost-centers/cost-centers/nope/runs").status_code == 404
    assert client.get("/api/plugins/hermes-cost-centers/cost-centers/alpha/runs?month=bad").status_code == 400
    assert client.get("/api/plugins/hermes-cost-centers/cost-centers/alpha/runs").status_code == 200
