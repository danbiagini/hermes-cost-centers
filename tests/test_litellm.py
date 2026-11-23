"""The litellm source against a stubbed spend-logs response."""

import io
import json
from datetime import timedelta

import pytest

from conftest import MONTH0, day
from hermes_cost_centers.adapters import litellm as litellm_mod
from hermes_cost_centers.adapters.litellm import LiteLLM
from hermes_cost_centers.pricing import PriceTable

WINDOW = (MONTH0, MONTH0 + timedelta(days=31))


def stamp(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")  # LiteLLM logs naive UTC times


LOGS = [
    {"request_id": "r1", "startTime": stamp(day(2, 10)), "model": "claude-sonnet-4-6",
     "user": "alpha", "spend": 0.42, "prompt_tokens": 100, "completion_tokens": 10},
    {"request_id": "r2", "startTime": stamp(day(2, 11)), "model": "claude-sonnet-4-6",
     "user": "", "metadata": {"user": "beta"}, "spend": None,
     "prompt_tokens": 1_000_000, "completion_tokens": 0},
    {"request_id": "r3", "startTime": stamp(day(-2)), "model": "claude-sonnet-4-6",
     "user": "alpha", "spend": 5.0},
    {"request_id": "r4", "startTime": stamp(day(3)), "model": "mystery", "spend": None},
]


@pytest.fixture
def requests(monkeypatch):
    seen = []

    def fake_urlopen(req, timeout):
        seen.append(req)
        return io.BytesIO(json.dumps(LOGS).encode())

    monkeypatch.setattr(litellm_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-test")
    return seen


def test_rows(requests):
    rows = list(LiteLLM({"url": "http://proxy:4000/"}, PriceTable()).rows(*WINDOW))
    assert [r.run_ref for r in rows] == ["r1", "r2", "r4"]  # r3 is last month
    r1, r2, r4 = rows
    assert (r1.source, r1.cost_usd, r1.basis) == ("alpha", 0.42, "billed")
    assert r1.ts == day(2, 10)
    assert (r2.source, r2.cost_usd, r2.basis) == ("beta", pytest.approx(3.0), "priced")
    assert (r4.source, r4.cost_usd) == ("unknown", None)


def test_request(requests):
    list(LiteLLM({"url": "http://proxy:4000/"}, PriceTable()).rows(*WINDOW))
    (req,) = requests
    assert req.full_url.startswith("http://proxy:4000/spend/logs?start_date=")
    assert req.get_header("Authorization") == "Bearer sk-test"


def test_source_field_option(requests):
    rows = list(LiteLLM({"url": "http://p", "source_field": "request_id"}, PriceTable()).rows(*WINDOW))
    assert rows[0].source == "r1"


def test_url_required():
    with pytest.raises(ValueError, match="url"):
        LiteLLM({}, PriceTable())
