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


class TestMonthEnd:
    """The last days of a month, where the allowance reaches its cap."""

    def test_cap_reached_headroom_days_before_the_end(self):
        # With 5 days of headroom the allowance equals the budget from day 27 on.
        assert allowance(50, DEC, dt("2026-12-27T00:00:00"), 5) == pytest.approx(50)
        assert allowance(50, DEC, dt("2026-12-26T23:00:00"), 5) < 50

    def test_under_budget_after_the_cap_is_never_ahead(self):
        c = cfg("cost_centers:\n  a: {monthly_usd: 50}\n")
        s = evaluate(c, [row("a", 49.99, when="2026-12-02T00:00:00")], dt("2026-12-28T00:00:00"))
        assert s["cost_centers"]["a"]["pace"]["ahead_of_pace"] is False
        assert s["cost_centers"]["a"]["state"] == "wrap_up"

    def test_last_second_of_the_month_still_counts(self):
        c = cfg("cost_centers:\n  a: {monthly_usd: 50}\n")
        s = evaluate(c, [row("a", 1, when="2026-12-31T23:59:59")], dt("2026-12-31T23:59:59"))
        assert s["cost_centers"]["a"]["spend_usd"] == 1

    def test_december_rolls_into_january(self):
        assert DEC.end == dt("2027-01-01T00:00:00") and DEC.days == 31

    def test_zero_headroom_allows_only_the_elapsed_share(self):
        assert allowance(31, DEC, dt("2026-12-11T00:00:00"), 0) == pytest.approx(10)


class TestTimezone:
    C = cfg("timezone: America/New_York\ncost_centers:\n  a: {monthly_usd: 30}\n")

    def test_month_starts_at_local_midnight(self):
        m = Month.containing(dt("2026-12-01T03:00:00"), self.C.timezone)
        assert m.label == "2026-11"  # 22:00 on Nov 30 in New York
        assert m.start == dt("2026-11-01T04:00:00")  # EDT
        assert m.end == dt("2026-12-01T05:00:00")  # EST

    def test_rows_assigned_by_local_month(self):
        rows = [row("a", 1, when="2026-12-01T03:00:00"),   # Nov 30, local
                row("a", 2, when="2026-12-01T06:00:00")]   # Dec 1, local
        nov = evaluate(self.C, rows, dt("2026-12-01T04:00:00"))
        dec = evaluate(self.C, rows, dt("2026-12-01T07:00:00"))
        assert nov["month"]["label"] == "2026-11" and nov["cost_centers"]["a"]["spend_usd"] == 1
        assert dec["month"]["label"] == "2026-12" and dec["cost_centers"]["a"]["spend_usd"] == 2

    def test_dst_month_pace_uses_real_time(self):
        # November in New York is 30 days plus the hour gained when DST ends.
        m = Month.containing(dt("2026-11-15T12:00:00"), self.C.timezone)
        assert (m.end - m.start).total_seconds() == (30 * 24 + 1) * 3600
        assert allowance(30, m, m.end, 5) == 30
        assert allowance(30, m, m.start, 0) == 0


class TestEdges:
    def test_zero_budget_is_exhausted(self):
        c = cfg("cost_centers:\n  a: {monthly_usd: 0}\n")
        s = evaluate(c, [], dt("2026-11-16T00:00:00"))
        assert s["cost_centers"]["a"]["state"] == "exhausted" and s["cost_centers"]["a"]["pace"] is None

    def test_unpriced_unattributed_row(self):
        c = cfg("cost_centers:\n  a: {}\n")
        s = evaluate(c, [row("x", None)], dt("2026-11-16T00:00:00"))
        assert s["unattributed"]["x"] == {"spend_usd": 0, "rows": 1, "unpriced_rows": 1}
        assert s["account"]["incomplete"]

    def test_no_crossing_at_the_first_instant(self):
        c = cfg("account: {target_usd: 10}\ncost_centers:\n  a: {}\n")
        s = evaluate(c, [row("a", 1, when="2026-11-01T00:00:00")], dt("2026-11-01T00:00:00"))
        assert s["account"]["crosses_target_at"] is None


class TestTrailingProjection:
    """The account's crossing date runs at the last N days' rate when the
    operator sets account.projection_days: an early heavy week must not keep
    projecting a crossing after spend has slowed, nor a late surge be averaged away."""

    C = cfg("account: {target_usd: 100, projection_days: 7}\ncost_centers:\n  a: {sources: [a]}\n")
    NOW = dt("2026-11-16T00:00:00")

    def test_a_quiet_last_week_does_not_cross_after_a_heavy_start(self):
        # $60 on the 2nd, nothing since: the month average would cross on ~the 25th.
        s = evaluate(self.C, [row("a", 60, when="2026-11-02T00:00:00")], self.NOW)
        assert s["account"]["crosses_target_at"] is None
        assert s["account"]["projection"]["rate_usd_per_day"] == 0

    def test_a_late_surge_crosses_sooner_than_the_average_says(self):
        # $63 in the last 7 days = $9/day, against a month average of about $3.94.
        rows = [row("a", 14, when="2026-11-10T00:00:00"), row("a", 49, when="2026-11-15T00:00:00")]
        s = evaluate(self.C, rows, self.NOW)
        # $37 left at $9/day: about 4.1 days, the 20th.
        crossing = datetime.fromisoformat(s["account"]["crosses_target_at"])
        assert dt("2026-11-20T00:00:00") <= crossing <= dt("2026-11-20T06:00:00")
        assert s["account"]["projection"]["basis"] == "trailing 7 days"
        assert s["account"]["projection"]["at_month_end_usd"] == pytest.approx(63 + 9 * 15)  # 15 days left

    def test_the_window_reaches_into_last_month_but_spend_does_not(self):
        now = dt("2026-12-03T00:00:00")
        s = evaluate(self.C, [row("a", 70, when="2026-11-28T00:00:00")], now)
        assert s["account"]["spend_usd"] == 0 and s["cost_centers"]["a"]["spend_usd"] == 0
        assert s["account"]["projection"]["rate_usd_per_day"] == pytest.approx(10)

    def test_unset_means_the_month_average(self):
        c = cfg("account: {target_usd: 100}\ncost_centers:\n  a: {sources: [a]}\n")
        s = evaluate(c, [row("a", 60, when="2026-11-02T00:00:00")], self.NOW)
        assert s["account"]["projection"]["basis"] == "month to date"
        assert s["account"]["crosses_target_at"] is not None

    def test_zero_days_is_refused(self):
        with pytest.raises(config.ConfigError):
            cfg("account: {projection_days: 0}\ncost_centers:\n  a: {}\n")

    def test_daily_series_is_seven_days_with_the_quiet_ones(self):
        rows = [row("a", 5, when="2026-11-15T10:00:00"), row("a", 2, when="2026-11-09T23:00:00"),
                row("a", 99, when="2026-11-08T12:00:00")]  # outside the 7 days
        daily = evaluate(self.C, rows, dt("2026-11-15T12:00:00"))["account"]["daily"]
        assert [d["date"] for d in daily] == [f"2026-11-{n:02d}" for n in range(9, 16)]
        assert [d["spend_usd"] for d in daily] == [2, 0, 0, 0, 0, 0, 5]

    def test_the_ledger_is_read_far_enough_back(self):
        from hermes_cost_centers.evaluator import lookback_start
        month = Month.containing(dt("2026-12-03T00:00:00"), UTC)
        assert lookback_start(self.C, month, dt("2026-12-03T00:00:00")) == dt("2026-11-26T00:00:00")
        assert lookback_start(self.C, month, dt("2026-12-20T00:00:00")) == month.start
