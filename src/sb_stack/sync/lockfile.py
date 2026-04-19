"""Filesystem lockfile that prevents concurrent sync runs.

Lockfile at `/data/state/lockfile`. Stores `{pid}\n{iso_timestamp}`.
Stale-lock threshold defaults to 6 hours (longer than any expected
first-run sync).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sb_stack.errors import SBError


class LockfileBusyError(SBError):
    """Another sync run holds the lock."""


class Lockfile:
    def __init__(
        self,
        path: Path,
        *,
        stale_after_hours: int = 6,
        logger: Any | None = None,
    ) -> None:
        self.path = Path(path)
        self.stale_after_hours = stale_after_hours
        self._log = logger

    def acquire(self) -> None:
        """Write the lock file, or raise if someone else holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            pid, started = self._read()
            if pid is not None and started is not None:
                age_h = (_now() - started).total_seconds() / 3600
                if age_h < self.stale_after_hours and _pid_alive(pid):
                    raise LockfileBusyError(
                        f"another sync is running (pid={pid}, started {age_h:.1f}h ago)"
                    )
                if self._log is not None:
                    self._log.warning(
                        "stale_lockfile_taken_over",
                        age_hours=round(age_h, 2),
                        old_pid=pid,
                    )
        self._write(os.getpid(), _now())

    def release(self) -> None:
        try:
            if not self.path.exists():
                return
            pid, _ = self._read()
            if pid == os.getpid():
                self.path.unlink()
        except OSError as e:
            if self._log is not None:
                self._log.warning("lockfile_release_error", error=str(e))

    # ── Internals ───────────────────────────────────────────────────

    def _read(self) -> tuple[int | None, datetime | None]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None, None
        if len(lines) < 2:
            return None, None
        try:
            pid = int(lines[0])
            ts = datetime.fromisoformat(lines[1])
        except ValueError:
            return None, None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return pid, ts

    def _write(self, pid: int, started: datetime) -> None:
        self.path.write_text(f"{pid}\n{started.isoformat()}\n", encoding="utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


__all__ = ["Lockfile", "LockfileBusyError"]
