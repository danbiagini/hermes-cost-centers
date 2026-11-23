"""Pricing tokens from a price table.

The shipped table covers only models whose four rates are known. Anything else
prices to None (unpriced), never to 0.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .ledger import Tokens

_RATES = ("input", "output", "cache_read", "cache_write")
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _load_shipped() -> dict[str, dict[str, float]]:
    text = (Path(__file__).parent / "prices.json").read_text(encoding="utf-8")
    return json.loads(text)["models"]


def normalize_model(model: str) -> str:
    """Reduce a routed model name to the table key.

    ``anthropic/claude-sonnet-4-6``, ``anthropic.claude-sonnet-4-6`` and
    ``claude-sonnet-4-6-20250414`` all price as ``claude-sonnet-4-6``.
    """
    name = model.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[1]
    if name.startswith("anthropic."):
        name = name[len("anthropic."):]
    return _DATE_SUFFIX.sub("", name)


class PriceTable:
    def __init__(self, overrides: Optional[dict[str, Any]] = None, table_path: Optional[str] = None):
        self.models = _load_shipped()
        if table_path:
            data = json.loads(Path(table_path).read_text(encoding="utf-8"))
            self.models.update(data.get("models", data))
        if overrides:
            self.models.update(overrides)

    @classmethod
    def from_config(cls, pricing: dict[str, Any]) -> "PriceTable":
        return cls(overrides=pricing.get("models"), table_path=pricing.get("table"))

    def published(self) -> dict[str, dict[str, float]]:
        """The complete entries, as the state carries them for the agent plugin."""
        return {m: {k: float(r[k]) for k in _RATES} for m, r in sorted(self.models.items())
                if isinstance(r, dict) and all(r.get(k) is not None for k in _RATES)}

    def rates(self, model: str) -> Optional[dict[str, float]]:
        entry = self.models.get(model) or self.models.get(normalize_model(model))
        if not entry or any(entry.get(k) is None for k in _RATES):
            return None
        return entry

    def price(self, model: str, tokens: Tokens) -> Optional[float]:
        rates = self.rates(model)
        if rates is None:
            return None
        return (
            tokens.input * rates["input"]
            + tokens.output * rates["output"]
            + tokens.cache_read * rates["cache_read"]
            + tokens.cache_write * rates["cache_write"]
        ) / 1_000_000
