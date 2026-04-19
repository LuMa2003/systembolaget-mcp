"""Unit tests for the sync lockfile."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sb_stack.sync.lockfile import Lockfile, LockfileBusyError


def test_acquire_and_release(tmp_path: Path) -> None:
    lf = Lockfile(tmp_path / "lockfile")
    lf.acquire()
    assert (tmp_path / "lockfile").exists()
    lf.release()
    assert not (tmp_path / "lockfile").exists()


def test_release_idempotent_for_non_owner(tmp_path: Path) -> None:
    # Write a lock file with someone else's PID.
    lock_path = tmp_path / "lockfile"
    lock_path.parent.mkdir(exist_ok=True)
    lock_path.write_text(f"1\n{datetime.now(UTC).isoformat()}\n")

    Lockfile(lock_path).release()
    # File remains — we didn't own it.
    assert lock_path.exists()


def test_acquire_rejects_live_holder(tmp_path: Path) -> None:
    # Write a lock held by us (os.getpid) with current timestamp — fresh lock.
    lock_path = tmp_path / "lockfile"
    lock_path.parent.mkdir(exist_ok=True)
    lock_path.write_text(f"{os.getpid()}\n{datetime.now(UTC).isoformat()}\n")

    with pytest.raises(LockfileBusyError):
        Lockfile(lock_path).acquire()


def test_acquire_takes_over_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "lockfile"
    # PID 1 is almost certainly alive but the timestamp is ancient.
    lock_path.write_text("1\n2000-01-01T00:00:00+00:00\n")

    Lockfile(lock_path, stale_after_hours=1).acquire()
    # Our own PID should now be on disk.
    content = lock_path.read_text()
    assert content.splitlines()[0] == str(os.getpid())
