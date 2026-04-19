"""Unit tests for the forward-only migration runner.

These exercise the runner's discovery, integrity, and apply loop against
an on-disk DuckDB file (DB.writer/reader use the configured path).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from sb_stack.db import DB, Migration, MigrationRunner
from sb_stack.errors import ChecksumMismatchError, MigrationError
from sb_stack.settings import Settings


def _log() -> logging.Logger:
    # structlog isn't required for these tests; a stdlib logger is enough
    # since the runner only calls .info/.error. A test-only shim.
    class _L:
        def info(self, *_a: object, **_k: object) -> None: ...
        def error(self, *_a: object, **_k: object) -> None: ...

    return _L()  # type: ignore[return-value]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def schema_dir(tmp_path: Path) -> Path:
    d = tmp_path / "schema"
    d.mkdir()
    return d


def _write(dir: Path, name: str, sql: str) -> Path:
    p = dir / name
    p.write_text(sql, encoding="utf-8")
    return p


# ── Discovery / filename parsing ─────────────────────────────────────────────


def test_migration_from_path_parses_version(tmp_path: Path) -> None:
    p = tmp_path / "001_initial.sql"
    p.write_text("SELECT 1;", encoding="utf-8")
    m = Migration.from_path(p)
    assert m.version == 1
    assert m.filename == "001_initial.sql"
    assert m.sha256 == hashlib.sha256(b"SELECT 1;").hexdigest()


def test_rejects_badly_named_file(tmp_path: Path) -> None:
    p = _write(tmp_path, "bad_name.sql", "SELECT 1;")
    with pytest.raises(MigrationError, match="does not match NNN_name.sql"):
        Migration.from_path(p)


def test_discovery_detects_gap(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    _write(schema_dir, "003_c.sql", "CREATE TABLE c(x INTEGER);")  # gap: missing 002
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    with pytest.raises(MigrationError, match="gap"):
        runner.run()


def test_discovery_detects_duplicate(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    # craft a second file also starting with 001 by giving it a different name
    _write(schema_dir, "001_dup.sql", "CREATE TABLE dup(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    with pytest.raises(MigrationError, match="duplicate"):
        runner.run()


# ── Apply flow ───────────────────────────────────────────────────────────────


def test_apply_creates_tables_and_records(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)

    applied = runner.run()
    assert applied == 1

    with DB(settings).reader() as conn:
        rows = conn.execute(
            "SELECT version, filename FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert rows == [(1, "001_a.sql")]
        # Table a exists
        conn.execute("SELECT * FROM a")


def test_apply_is_idempotent(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)

    assert runner.run() == 1
    assert runner.run() == 0  # nothing pending the second time


def test_ordered_apply_of_multiple(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    _write(schema_dir, "002_b.sql", "CREATE TABLE b(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    assert runner.run() == 2

    with DB(settings).reader() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2]


def test_failed_migration_rolls_back(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    _write(schema_dir, "002_bad.sql", "THIS IS NOT VALID SQL;")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)

    with pytest.raises(MigrationError):
        runner.run()

    # First migration did apply and was recorded; second did not.
    with DB(settings).reader() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1]


# ── Integrity ────────────────────────────────────────────────────────────────


def test_integrity_violation_refuses_to_run(settings: Settings, schema_dir: Path) -> None:
    f = _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    runner.run()

    # Now mutate the applied migration (even whitespace edits are forbidden).
    f.write_text("CREATE TABLE a(x INTEGER);  -- edited", encoding="utf-8")

    with pytest.raises(ChecksumMismatchError):
        runner.run()


def test_missing_applied_file_refuses_to_run(settings: Settings, schema_dir: Path) -> None:
    f = _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    runner.run()
    f.unlink()

    with pytest.raises(MigrationError, match="missing from schema"):
        runner.run()


# ── Verify ───────────────────────────────────────────────────────────────────


def test_verify_passes_when_up_to_date(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    runner.run()
    runner.verify()  # should not raise


def test_verify_raises_when_pending(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    runner.run()
    _write(schema_dir, "002_b.sql", "CREATE TABLE b(x INTEGER);")
    with pytest.raises(MigrationError, match="schema is behind"):
        runner.verify()


def test_verify_raises_on_fresh_db(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    # No run() before verify(): DB file doesn't exist yet, so we create it
    # empty via a writer context and still expect verify() to fail because
    # schema_migrations is absent.
    with DB(settings).writer() as conn:
        conn.execute("CREATE TABLE sentinel(x INTEGER)")

    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    with pytest.raises(MigrationError, match="schema_migrations"):
        runner.verify()


# ── Pre-migration backup ─────────────────────────────────────────────────────


def test_pre_migration_backup_is_taken(settings: Settings, schema_dir: Path) -> None:
    _write(schema_dir, "001_a.sql", "CREATE TABLE a(x INTEGER);")
    runner = MigrationRunner(DB(settings), settings, _log(), schema_dir=schema_dir)
    runner.run()  # first run: DB doesn't exist yet; no backup needed.

    _write(schema_dir, "002_b.sql", "CREATE TABLE b(x INTEGER);")
    runner.run()
    backup = settings.pre_migration_backup_dir / "sb.duckdb.pre-002"
    assert backup.exists()
