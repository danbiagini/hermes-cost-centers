import pytest

from hermes_cost_centers.ledger import Tokens
from hermes_cost_centers.pricing import PriceTable, normalize_model


@pytest.mark.parametrize("name", [
    "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6", "anthropic.claude-sonnet-4-6",
    "claude-sonnet-4-6-20250414", "Claude-Sonnet-4-6",
])
def test_normalize(name):
    assert normalize_model(name) == "claude-sonnet-4-6"


def test_price_all_four_rates():
    t = PriceTable()
    cost = t.price("claude-sonnet-4-6", Tokens(1_000_000, 100_000, 1_000_000, 100_000))
    assert cost == pytest.approx(3.00 + 1.50 + 0.30 + 0.375)


def test_unknown_model_is_unpriced_not_free():
    assert PriceTable().price("mystery-model", Tokens(1000, 1000)) is None


def test_overrides_add_and_replace():
    t = PriceTable(overrides={
        "local-llama": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "claude-sonnet-4-6": {"input": 1, "output": 1, "cache_read": 1, "cache_write": 1},
    })
    assert t.price("local-llama", Tokens(10**6, 10**6)) == 0.0
    assert t.price("claude-sonnet-4-6", Tokens(10**6)) == pytest.approx(1.0)


def test_partial_rates_are_unpriced():
    t = PriceTable(overrides={"half": {"input": 1, "output": 1}})
    assert t.price("half", Tokens(10)) is None
