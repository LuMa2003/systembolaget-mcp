"""Delete raw archive directories older than `raw_retention_days`."""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path
from typing import Any


def cleanup_old_raw(
    raw_dir: Path,
    *,
    retention_days: int,
    today: date | None = None,
    logger: Any | None = None,
) -> int:
    """Remove YYYY-MM-DD directories older than cutoff. Returns count removed."""
    today = today or date.today()
    cutoff = today - timedelta(days=retention_days)
    if not raw_dir.exists():
        return 0
    deleted = 0
    for entry in raw_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            entry_date = date.fromisoformat(entry.name)
        except ValueError:
            # Defensive: leave hand-created directories alone.
            continue
        if entry_date < cutoff:
            shutil.rmtree(entry)
            deleted += 1
            if logger is not None:
                logger.info("raw_archive_removed", date=entry.name)
    return deleted
