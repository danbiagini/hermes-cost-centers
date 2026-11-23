import pytest

from hermes_cost_centers import config


def test_minimal_config_defaults(write_config):
    cfg = config.load(write_config("cost_centers:\n  alpha: {monthly_usd: 50}\n"))
    alpha = cfg.cost_centers["alpha"]
    assert alpha.sources == ("alpha",)  # a cost center defaults to one profile of its name
    assert alpha.monthly_usd == 50
    assert alpha.pace.on and alpha.pace.headroom_days == 5
    assert cfg.ledger == {"source": "hermes-sessions"}
    assert cfg.wrap_up == 0.80


def test_unset_budget_is_not_zero(write_config):
    cfg = config.load(write_config("cost_centers:\n  infra: {sources: [a, b], monthly_usd: null}\n"))
    assert cfg.cost_centers["infra"].monthly_usd is None
    assert not cfg.cost_centers["infra"].budgeted


def test_pace_off_and_default(write_config):
    cfg = config.load(write_config(
        "pace_default: {headroom_days: 3}\n"
        "cost_centers:\n  a: {monthly_usd: 10}\n  b: {monthly_usd: 10, pace: off}\n"))
    assert cfg.cost_centers["a"].pace.headroom_days == 3
    assert not cfg.cost_centers["b"].pace.on


def test_source_in_two_cost_centers_is_refused(write_config):
    with pytest.raises(config.ConfigError, match="in both"):
        config.load(write_config("cost_centers:\n  a: {sources: [x]}\n  b: {sources: [x]}\n"))


def test_group_writable_config_is_refused(write_config):
    path = write_config("cost_centers:\n  a: {monthly_usd: 1}\n", mode=0o664)
    with pytest.raises(config.ConfigError, match="writable"):
        config.load(path)


def test_negative_budget_is_refused(write_config):
    with pytest.raises(config.ConfigError, match="negative"):
        config.load(write_config("cost_centers:\n  a: {monthly_usd: -1}\n"))


def test_only_calendar_month(write_config):
    with pytest.raises(config.ConfigError, match="calendar-month"):
        config.load(write_config("period: weekly\ncost_centers:\n  a: {}\n"))


def test_center_for(write_config):
    cfg = config.load(write_config("cost_centers:\n  line: {sources: [p1, p2]}\n"))
    assert cfg.center_for("p2") == "line"
    assert cfg.center_for("nobody") is None


@pytest.mark.parametrize("text,message", [
    ("[]", "must be a mapping"),
    ("timezone: Mars/Olympus\ncost_centers: {a: {}}", "timezone"),
    ("cost_centers: {a: [1]}", "expected a mapping"),
    ("cost_centers: {a: {sources: [1]}}", "list of names"),
    ("cost_centers: {a: {monthly_usd: '50'}}", "expected a number"),
    ("cost_centers: {a: {monthly_usd: true}}", "expected a number"),
    ("cost_centers: {a: {pace: 5}}", "pace"),
    ("cost_centers: {a: {pace: {on: maybe}}}", "true or false"),
    ("thresholds: {wrap_up: 1.5}\ncost_centers: {a: {}}", "between 0 and 1"),
    ("interval_seconds: 1\ncost_centers: {a: {}}", "at least 5"),
    ("ledger: {url: x}\ncost_centers: {a: {}}", "ledger.source"),
])
def test_bad_values(write_config, text, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load(write_config(text))


def test_missing_file(tmp_path):
    with pytest.raises(config.ConfigError, match="no config"):
        config.load(tmp_path / "nope.yaml")


def test_sources_as_string_and_pace_false(write_config):
    cfg = config.load(write_config("cost_centers: {a: {sources: solo, pace: false}}\n"))
    assert cfg.cost_centers["a"].sources == ("solo",) and not cfg.cost_centers["a"].pace.on


def test_default_path_from_hermes_home(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_COST_CENTERS_CONFIG", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert config.default_config_path() == tmp_path / "hermes-cost-centers" / "budgets.yaml"


def test_bare_on_key_is_honoured(write_config):
    # YAML parses `on:` as the key True; `pace: {on: false}` must still turn pacing off.
    cfg = config.load(write_config("pace_default: {on: false}\ncost_centers: {a: {}, b: {pace: {on: true}}}\n"))
    assert not cfg.cost_centers["a"].pace.on
    assert cfg.cost_centers["b"].pace.on
