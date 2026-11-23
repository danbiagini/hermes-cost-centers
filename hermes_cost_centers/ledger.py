"""The ledger row every adapter yields."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# How a row's dollar figure was obtained.
BILLED = "billed"        # the provider or proxy reported the cost
PRICED = "priced"        # computed from tokens and a price table
ESTIMATED = "estimated"  # API-equivalent dollars for usage not billed per token
UNMETERED = "unmetered"  # included in a plan; no per-call cost
BASES = (BILLED, PRICED, ESTIMATED, UNMETERED)


@dataclass(frozen=True)
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def __add__(self, other: "Tokens") -> "Tokens":
        return Tokens(
            self.input + other.input,
            self.output + other.output,
            self.cache_read + other.cache_read,
            self.cache_write + other.cache_write,
        )

    def as_dict(self) -> dict:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass(frozen=True)
class Row:
    """One unit of spend.

    ``cost_usd`` is None when the usage could not be priced. It is never 0 for
    that reason: an unpriced call must not read as free.
    """

    ts: datetime
    source: str
    model: str
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: Optional[float] = None
    basis: Optional[str] = None
    run_ref: Optional[str] = None
    parent_ref: Optional[str] = None
    task_ref: Optional[str] = None

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None
