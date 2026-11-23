from datetime import datetime, timezone

import pytest

from hermes_cost_centers import config
from hermes_cost_centers.evaluator import Month, allowance, eligible_at, evaluate
from hermes_cost_centers.ledger import BILLED, Row, Tokens

UTC = timezone.utc


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def row(source, usd, when="2026-11-02T00:00:00", run="r1", basis=BILLED):
    return Row(ts=dt(when), source=source, model="m", tokens=Tokens(1), cost_usd=usd,
               basis=basis if usd is not None else None, run_ref=run)


def cfg(text):
    return config.parse(__import__("yaml").safe_load(text))


DEC = Month.containing(dt("2026-12-15T00:00:00"), UTC)  # 31 days


class TestPaceLine:
    def test_start_of_month_is_headroom_only(self):
        # $50 budget, 5 days of headroom, 31-day month: about $8.06 at the start.
        assert allowance(50, DEC, DEC.start, 5) == pytest.approx(50 * 5 / 31)

    def test_grows_by_a_daily_share(self):
        a1 = allowance(50, DEC, dt("2026-12-02T00:00:00"), 5)
        a0 = allowance(50, DEC, dt("2026-12-01T00:00:00"), 5)
        assert a1 - a0 == pytest.approx(50 / 31)  # about $1.61 a day

    def test_capped_at_budget(self):
        assert allowance(50, DEC, dt("2026-12-30T00:00:00"), 5) == 50

    def test_eligible_at_is_when_allowance_reaches_spend(self):
        t = eligible_at(50, DEC, 20.0, 5)
        assert allowance(50, DEC, t, 5) == pytest.approx(20.0)

    def test_spend_within_headroom_is_eligible_at_start(self):
        assert eligible_at(50, DEC, 1.0, 5) == DEC.start

    def test_spent_budget_never_eligible(self):
        assert eligible_at(50, DEC, 50.0, 5) is None


class TestEvaluate:
    C = cfg("""
account: {target_usd: 100}
cost_centers:
  line: {sources: [p1, p2], monthly_usd: 50}
  small: {sources: [s], monthly_usd: 10}
  free: {sources: [f], monthly_usd: null}
""")
    NOW = dt("2026-11-16T00:00:00")

    def test_groups_sources_into_one_cost_center(self):
        s = evaluate(self.C, [row("p1", 5), row("p2", 7)], self.NOW)
        assert s["cost_centers"]["line"]["spend_usd"] == 12
        assert s["account"]["spend_usd"] == 12

    def test_states(self):
        s = evaluate(self.C, [row("p1", 30), row("s", 8.5), row("f", 1000)], self.NOW)
        assert s["cost_centers"]["line"]["state"] == "ok"
        assert s["cost_centers"]["small"]["state"] == "wrap_up"
        assert s["cost_centers"]["free"]["state"] == "unset"  # never exhausted without a budget
        s = evaluate(self.C, [row("s", 10)], self.NOW)
        assert s["cost_centers"]["small"]["state"] == "exhausted"
        assert s["cost_centers"]["small"]["remaining_usd"] == 0

    def test_unpriced_rows_mark_incomplete_and_count_nothing(self):
        s = evaluate(self.C, [row("p1", 5), row("p1", None)], self.NOW)
        c = s["cost_centers"]["line"]
        assert c["spend_usd"] == 5 and c["incomplete"] and c["unpriced_rows"] == 1
        assert s["account"]["incomplete"]

    def test_unattributed_sources_are_shown_not_dropped(self):
        s = evaluate(self.C, [row("stranger", 3)], self.NOW)
        assert s["unattributed"] == {"stranger": {"spend_usd": 3, "rows": 1, "unpriced_rows": 0}}
        assert s["account"]["spend_usd"] == 3

    def test_rows_outside_the_month_are_ignored(self):
        s = evaluate(self.C, [row("p1", 99, when="2026-10-31T23:59:59")], self.NOW)
        assert s["cost_centers"]["line"]["spend_usd"] == 0

    def test_ahead_of_pace_is_held_not_exhausted(self):
        # Mid-month allowance for $50 at 5 days' headroom is about $33.33.
        s = evaluate(self.C, [row("p1", 40)], self.NOW)
        pace = s["cost_centers"]["line"]["pace"]
        assert s["cost_centers"]["line"]["state"] == "wrap_up"
        assert pace["ahead_of_pace"] is True
        assert pace["eligible_at"] > s["generated_at"]

    def test_on_pace(self):
        s = evaluate(self.C, [row("p1", 10)], self.NOW)
        assert s["cost_centers"]["line"]["pace"]["ahead_of_pace"] is False
        assert s["cost_centers"]["line"]["pace"]["eligible_at"] is None

    def test_exhausted_is_not_also_ahead_of_pace(self):
        s = evaluate(self.C, [row("s", 12)], self.NOW)
        assert s["cost_centers"]["small"]["pace"]["ahead_of_pace"] is False

    def test_account_crossing_projection(self):
        # $60 by the 16th of a 30-day month: $100 at about day 25.
        s = evaluate(self.C, [row("p1", 60)], self.NOW)
        crossing = datetime.fromisoformat(s["account"]["crosses_target_at"])
        assert dt("2026-11-25T00:00:00") <= crossing <= dt("2026-11-26T00:00:00")

    def test_no_crossing_when_rate_is_below_target(self):
        s = evaluate(self.C, [row("p1", 10)], self.NOW)
        assert s["account"]["crosses_target_at"] is None

    def test_runs_counted(self):
        s = evaluate(self.C, [row("p1", 1, run="a"), row("p1", 1, run="b"), row("p2", 1, run="a")], self.NOW)
        assert s["cost_centers"]["line"]["runs"] == 2
