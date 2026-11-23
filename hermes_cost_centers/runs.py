"""Cost per run, with subagent sessions rolled up into the run that started them."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from .ledger import Row, Tokens


@dataclass
class Run:
    ref: str
    source: str
    started: datetime
    cost_usd: float = 0.0
    unpriced_rows: int = 0
    tokens: Tokens = field(default_factory=Tokens)
    models: set[str] = field(default_factory=set)
    task_ref: Optional[str] = None
    children: list["Run"] = field(default_factory=list)

    def add(self, row: Row) -> None:
        if row.priced:
            self.cost_usd += row.cost_usd
        else:
            self.unpriced_rows += 1
        self.tokens = self.tokens + row.tokens
        self.models.add(row.model)
        self.started = min(self.started, row.ts)
        self.task_ref = self.task_ref or row.task_ref

    @property
    def total_usd(self) -> float:
        return self.cost_usd + sum(c.total_usd for c in self.children)

    @property
    def incomplete(self) -> bool:
        return self.unpriced_rows > 0 or any(c.incomplete for c in self.children)

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "source": self.source,
            "started": self.started.isoformat(timespec="seconds"),
            "own_usd": round(self.cost_usd, 6),
            "total_usd": round(self.total_usd, 6),
            "incomplete": self.incomplete,
            "tokens": self.tokens.as_dict(),
            "models": sorted(self.models),
            "task_ref": self.task_ref,
            "subagents": [c.as_dict() for c in sorted(self.children, key=lambda c: c.started)],
        }


def build(rows: Iterable[Row]) -> list[Run]:
    """Top-level runs, newest first. Rows without a run id are grouped as one
    pseudo-run per source, so their spend is still visible."""
    runs: dict[str, Run] = {}
    parent_of: dict[str, Optional[str]] = {}
    for r in rows:
        ref = r.run_ref or f"(no run id: {r.source})"
        run = runs.get(ref)
        if run is None:
            run = runs[ref] = Run(ref=ref, source=r.source, started=r.ts)
        run.add(r)
        if r.parent_ref:
            parent_of[ref] = r.parent_ref

    def root(ref: str) -> str:
        seen = set()
        while parent_of.get(ref) in runs and ref not in seen:
            seen.add(ref)
            ref = parent_of[ref]
        return ref

    children: dict[str, list[Run]] = defaultdict(list)
    tops: list[Run] = []
    for ref, run in runs.items():
        top = root(ref)
        if top == ref:
            tops.append(run)
        else:
            children[top].append(run)
    for top in tops:
        top.children = children.get(top.ref, [])
    return sorted(tops, key=lambda r: r.started, reverse=True)
