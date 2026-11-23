"""Writing and reading the budget state.

Readers fail open: a missing, unreadable or stale state means "no
information", never "refuse". The evaluator's absence is reported as a fault
by the reader that notices it.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

STALE_INTERVALS = 5


def write(path: Path, state: dict) -> None:
    """Write atomically: a temp file in the same directory, then a rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def age_seconds(state: dict, now: Optional[datetime] = None) -> Optional[float]:
    try:
        generated = datetime.fromisoformat(state["generated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if now is None:
        from .evaluator import utcnow

        now = utcnow()
    return (now - generated).total_seconds()


def is_stale(state: dict, now: Optional[datetime] = None) -> bool:
    age = age_seconds(state, now)
    if age is None:
        return True
    interval = state.get("interval_seconds") or 60
    return age > STALE_INTERVALS * interval


def read(path: Path) -> Optional[dict]:
    """The state, or None if it is missing or unreadable. Never raises."""
    try:
        with Path(path).open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
