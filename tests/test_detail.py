"""Run detail files: what the dashboard reads instead of the ledger."""

from datetime import timedelta

from conftest import MONTH0, day
from hermes_cost_centers import cli, config, detail, evaluator


def load(path):
    return config.load(path)


def make(write_config, hermes_home, tmp_path, extra=""):
    return load(write_config(f"""
cost_centers:
  alpha: {{monthly_usd: 50}}
  "../escape": {{sources: [beta]}}
ledger: {{source: hermes-sessions, hermes_home: {hermes_home}}}
state: {{path: {tmp_path}/state/state.json}}
{extra}"""))


def test_evaluate_writes_one_file_per_cost_center(write_config, hermes_home, tmp_path, monkeypatch):
    cfg = make(write_config, hermes_home, tmp_path)
    monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(cfg.path))
    assert cli.main(["evaluate"]) == 0
    month = f"{MONTH0:%Y-%m}"
    files = sorted(p.name for p in (tmp_path / "state" / "runs" / month).iterdir())
    assert files == ["..%2Fescape.json", "alpha.json"]  # names can't escape the directory
    a = detail.read(cfg.runs_dir, month, "alpha")
    assert a["complete"] is False
    assert [s["ref"] for s in next(r for r in a["runs"] if r["ref"] == "a1")["subagents"]] == ["a2"]
    assert f"{day(2):%Y-%m-%d}" in a["daily_usd"]


def test_previous_month_is_finished_once(write_config, hermes_home, tmp_path, monkeypatch):
    cfg = make(write_config, hermes_home, tmp_path)
    prev = evaluator.Month.containing(MONTH0 - timedelta(seconds=1), cfg.timezone)
    # First cycle of the month: the previous month gets a final, complete write.
    cli.evaluate_once(cfg, write_detail=True)
    p = detail.read(cfg.runs_dir, prev.label, "alpha")
    assert p["complete"] is True and p["run_count"] == 1  # session a0 is last month's
    assert not detail.needs_final_write(cfg.runs_dir, cfg, prev)

    # Later cycles leave it alone.
    calls = []
    real = cli.get_adapter

    def counting(c):
        a = real(c)
        orig = a.rows
        a.rows = lambda s, u: (calls.append((s, u)), orig(s, u))[1]
        return a

    monkeypatch.setattr(cli, "get_adapter", counting)
    cli.evaluate_once(cfg, write_detail=True)
    assert len(calls) == 1  # current month only


def test_runs_dir_override(write_config, hermes_home, tmp_path):
    cfg = make(write_config, hermes_home, tmp_path)
    assert cfg.runs_dir == tmp_path / "state" / "runs"
    cfg2 = load(write_config(f"cost_centers: {{a: {{}}}}\nstate: {{path: /x/s.json, runs_dir: {tmp_path}/r}}\n"))
    assert cfg2.runs_dir == tmp_path / "r"


def test_stdout_mode_writes_nothing(write_config, hermes_home, tmp_path, monkeypatch, capsys):
    cfg = make(write_config, hermes_home, tmp_path)
    monkeypatch.setenv("HERMES_COST_CENTERS_CONFIG", str(cfg.path))
    assert cli.main(["evaluate", "--stdout"]) == 0
    assert not (tmp_path / "state").exists()
