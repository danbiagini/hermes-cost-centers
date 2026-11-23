"""Loading and validating ``budgets.yaml``."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import yaml

ENV_CONFIG = "HERMES_COST_CENTERS_CONFIG"
DEFAULT_STATE_PATH = "/var/lib/hermes-cost-centers/state.json"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Pace:
    on: bool = True
    headroom_days: float = 5.0


@dataclass(frozen=True)
class CostCenter:
    name: str
    sources: tuple[str, ...]
    monthly_usd: Optional[float]  # None = unset: tracked, never enforced
    pace: Pace = field(default_factory=Pace)

    @property
    def budgeted(self) -> bool:
        return self.monthly_usd is not None


@dataclass(frozen=True)
class Config:
    cost_centers: dict[str, CostCenter]
    timezone: ZoneInfo
    target_usd: Optional[float] = None
    hard_usd: Optional[float] = None
    wrap_up: float = 0.80
    ledger: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    interval_seconds: int = 60
    path: Optional[Path] = None

    def center_for(self, source: str) -> Optional[str]:
        for center in self.cost_centers.values():
            if source in center.sources:
                return center.name
        return None

    @property
    def state_path(self) -> Path:
        return Path(self.state.get("path", DEFAULT_STATE_PATH))


def default_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "hermes-cost-centers" / "budgets.yaml"


def _number(value: Any, where: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: expected a number or null, got {value!r}")
    if value < 0:
        raise ConfigError(f"{where}: must not be negative")
    return float(value)


def _pace(raw: Any, default: Pace, where: str) -> Pace:
    if raw is None:
        return default
    if raw is False or raw == "off":
        return Pace(on=False, headroom_days=default.headroom_days)
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a mapping, 'off' or false")
    on = raw.get("on", True)
    if not isinstance(on, bool):
        raise ConfigError(f"{where}.on: expected true or false")
    headroom = _number(raw.get("headroom_days", default.headroom_days), f"{where}.headroom_days")
    return Pace(on=on, headroom_days=headroom)


def parse(raw: Any, path: Optional[Path] = None) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("budgets.yaml must be a mapping")
    period = raw.get("period", "calendar-month")
    if period != "calendar-month":
        raise ConfigError(f"period: only 'calendar-month' is supported, got {period!r}")
    try:
        tz = ZoneInfo(raw.get("timezone", "UTC"))
    except Exception as exc:  # zoneinfo raises several types
        raise ConfigError(f"timezone: {exc}") from exc

    default_pace = _pace(raw.get("pace_default"), Pace(), "pace_default")

    centers_raw = raw.get("cost_centers")
    if not isinstance(centers_raw, dict) or not centers_raw:
        raise ConfigError("cost_centers: at least one cost center is required")
    centers: dict[str, CostCenter] = {}
    owner: dict[str, str] = {}
    for name, body in centers_raw.items():
        where = f"cost_centers.{name}"
        body = body or {}
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: expected a mapping")
        sources = body.get("sources", [name])
        if isinstance(sources, str):
            sources = [sources]
        if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
            raise ConfigError(f"{where}.sources: expected a list of names")
        for s in sources:
            if s in owner:
                # One source billing two budgets would count its spend twice.
                raise ConfigError(f"source {s!r} is in both {owner[s]!r} and {name!r}")
            owner[s] = str(name)
        centers[str(name)] = CostCenter(
            name=str(name),
            sources=tuple(sources),
            monthly_usd=_number(body.get("monthly_usd"), f"{where}.monthly_usd"),
            pace=_pace(body.get("pace"), default_pace, f"{where}.pace"),
        )

    account = raw.get("account") or {}
    thresholds = raw.get("thresholds") or {}
    wrap_up = _number(thresholds.get("wrap_up", 0.80), "thresholds.wrap_up")
    if not 0 < wrap_up <= 1:
        raise ConfigError("thresholds.wrap_up: must be between 0 and 1")
    interval = raw.get("interval_seconds", 60)
    if not isinstance(interval, int) or interval < 5:
        raise ConfigError("interval_seconds: must be an integer of at least 5")

    ledger = raw.get("ledger") or {"source": "hermes-sessions"}
    if not isinstance(ledger, dict) or "source" not in ledger:
        raise ConfigError("ledger.source is required")

    return Config(
        cost_centers=centers,
        timezone=tz,
        target_usd=_number(account.get("target_usd"), "account.target_usd"),
        hard_usd=_number(account.get("hard_usd"), "account.hard_usd"),
        wrap_up=wrap_up,
        ledger=dict(ledger),
        state=dict(raw.get("state") or {}),
        pricing=dict(raw.get("pricing") or {}),
        interval_seconds=interval,
        path=path,
    )


def check_permissions(path: Path) -> None:
    """Refuse a config that anyone but its owner can write.

    Budgets are the operator's decision. A config an agent's user can edit
    would let the agent raise its own budget.
    """
    st = path.stat()
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{path} is writable by group or others; budgets must be writable by their owner only "
            f"(chmod go-w {path})"
        )


def load(path: Optional[Path] = None, *, check_perms: bool = True) -> Config:
    path = Path(path) if path else default_config_path()
    if not path.exists():
        raise ConfigError(f"no config at {path} (set {ENV_CONFIG} or create it)")
    if check_perms:
        check_permissions(path)
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return parse(raw, path)
