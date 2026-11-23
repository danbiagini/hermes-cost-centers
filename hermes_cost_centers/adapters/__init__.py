"""Ledger sources. Exactly one is authoritative per install."""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol

from ..config import Config
from ..ledger import Row
from ..pricing import PriceTable


class Adapter(Protocol):
    name: str

    def rows(self, since: datetime, until: datetime) -> Iterator[Row]: ...


def get_adapter(config: Config) -> Adapter:
    prices = PriceTable.from_config(config.pricing)
    kind = config.ledger["source"]
    if kind == "hermes-sessions":
        from .hermes_sessions import HermesSessions

        return HermesSessions(config.ledger, prices)
    if kind == "sql":
        from .sql import Sql

        return Sql(config.ledger, prices)
    if kind == "litellm":
        from .litellm import LiteLLM

        return LiteLLM(config.ledger, prices)
    raise ValueError(f"unknown ledger.source {kind!r} (hermes-sessions, sql or litellm)")
