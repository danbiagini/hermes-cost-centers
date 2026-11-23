"""A ledger from LiteLLM's own spend tracking.

This needs a LiteLLM proxy that runs its database, because the spend logs live
there. Options:

    ledger:
      source: litellm
      url: http://litellm:4000
      key_env: LITELLM_MASTER_KEY     # env var holding an admin key
      source_field: user              # which log field names the source:
                                      # user, api_key, team_id, or a metadata key

A proxy without a database has no spend logs; use the ``sql`` source on
whatever table it writes instead.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterator

from ..ledger import BILLED, PRICED, Row, Tokens
from ..pricing import PriceTable
from .sql import _as_datetime


class LiteLLM:
    name = "litellm"

    def __init__(self, options: dict, prices: PriceTable):
        self.url = (options.get("url") or "").rstrip("/")
        if not self.url:
            raise ValueError("ledger.url is required for the litellm source")
        self.key = os.environ.get(options.get("key_env", "LITELLM_MASTER_KEY"), "")
        self.source_field = options.get("source_field", "user")
        self.prices = prices

    def _get(self, path: str, params: dict) -> Any:
        url = f"{self.url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def _source(self, log: dict) -> str:
        if self.source_field in log and log[self.source_field]:
            return str(log[self.source_field])
        meta = log.get("metadata") or {}
        return str(meta.get(self.source_field) or "unknown")

    def rows(self, since: datetime, until: datetime) -> Iterator[Row]:
        logs = self._get("/spend/logs", {
            "start_date": since.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": until.strftime("%Y-%m-%d %H:%M:%S"),
        })
        for log in logs or []:
            ts = _as_datetime(log.get("startTime") or log.get("start_time"))
            if not since <= ts < until:
                continue
            usage = log.get("usage") or {}
            tokens = Tokens(
                int(log.get("prompt_tokens") or usage.get("prompt_tokens") or 0),
                int(log.get("completion_tokens") or usage.get("completion_tokens") or 0),
            )
            model = log.get("model") or "unknown"
            spend = log.get("spend")
            if spend is not None:
                cost, basis = float(spend), BILLED
            else:
                cost = self.prices.price(model, tokens)
                basis = PRICED if cost is not None else None
            yield Row(ts=ts, source=self._source(log), model=model, tokens=tokens,
                      cost_usd=cost, basis=basis, run_ref=log.get("request_id"))
