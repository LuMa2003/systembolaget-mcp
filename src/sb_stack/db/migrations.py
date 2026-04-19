"""Forward-only DuckDB schema migrations.

Design (ratified 2026-04-19 in docs/06_module_layout.md):

1. Forward-only. No downgrades.
2. Strict sha256 integrity: edit an applied migration, refuse to start.
3. Atomic per migration (DuckDB transaction).
4. Pre-migration DB backup snapshots before any pending migration applies.
5. Filenames `NNN_short_description.sql`, zero-padded, no gaps allowed.

Usage:
    runner = MigrationRunner(db, settings, log)
    runner.run()      # sync owns this at startup
    runner.verify()   # mcp calls this at startup (raises if pending)
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from sb_stack.db.connection import DB
from sb_stack.errors import ChecksumMismatchError, MigrationError
from sb_stack.settings import Settings

_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    """One migration file on disk."""

    version: int
    filename: str
    path: Path
    sha256: str

    @classmethod
    def from_path(cls, path: Path) -> Migration:
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise MigrationError(f"migration filename {path.name!r} does not match NNN_name.sql")
        return cls(
            version=int(m.group(1)),
            filename=path.name,
            path=path,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class _AppliedRow:
    version: int
    filename: str
    sha256: str


class MigrationRunner:
    """Applies forward-only SQL migrations from ``db/schema/*.sql``."""

    def __init__(
        self,
        db: DB,
        settings: Settings,
        logger: Any,
        *,
        schema_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.log = logger
        self.schema_dir = schema_dir or Path(__file__).parent / "schema"

    # ── Public API ───────────────────────────────────────────────────────

    def run(self) -> int:
        """Apply all pending migrations. Returns the count applied."""
        migrations = self._discover()
        with self.db.writer() as conn:
            self._ensure_migrations_table(conn)
            applied = self._applied(conn)
            self._verify_applied_integrity(applied, migrations)
            pending = self._pending(applied, migrations)
            if not pending:
                self.log.info("migrations_up_to_date", version=max(applied, default=0))
                return 0
            # Pre-migration backup runs outside the DB connection so we can
            # copy the file safely.
        self._backup_pre_migration(pending[0].version)

        with self.db.writer() as conn:
            for m in pending:
                self._apply(conn, m)
        return len(pending)

    def verify(self) -> None:
        """Like ``run`` but raises if any migration is pending.

        sb-mcp calls this at startup so it refuses to serve against an
        unmigrated DB.
        """
        migrations = self._discover()
        with self.db.reader() as conn:
            self._ensure_migrations_table_readonly(conn)
            applied = self._applied(conn)
            self._verify_applied_integrity(applied, migrations)
            pending = self._pending(applied, migrations)
        if pending:
            raise MigrationError(
                "database schema is behind; run `sb-stack migrate` first "
                "(or start sb-sync, which migrates at startup). "
                f"pending versions: {[m.version for m in pending]}"
            )

    # ── Discovery / integrity ────────────────────────────────────────────

    def _discover(self) -> list[Migration]:
        if not self.schema_dir.exists():
            raise MigrationError(f"schema directory not found: {self.schema_dir}")
        files = sorted(p for p in self.schema_dir.iterdir() if p.is_file() and p.suffix == ".sql")
        migrations = [Migration.from_path(p) for p in files]
        self._check_no_gaps(migrations)
        return migrations

    @staticmethod
    def _check_no_gaps(migrations: list[Migration]) -> None:
        versions = [m.version for m in migrations]
        if versions != sorted(versions):
            raise MigrationError(f"migrations out of order: {versions}")
        if len(set(versions)) != len(versions):
            raise MigrationError(f"duplicate migration versions: {versions}")
        for expected, actual in enumerate(versions, start=1):
            if expected != actual:
                raise MigrationError(
                    f"migration gap: expected version {expected:03d}, found {actual:03d}"
                )

    def _verify_applied_integrity(
        self, applied: dict[int, _AppliedRow], migrations: list[Migration]
    ) -> None:
        on_disk = {m.version: m for m in migrations}
        for version, row in applied.items():
            m = on_disk.get(version)
            if m is None:
                # Applied migration missing from disk: refuse (operator likely
                # deleted it; needs manual investigation).
                raise MigrationError(
                    f"applied migration {version:03d} ({row.filename!r}) is missing from schema/"
                )
            if row.sha256 != m.sha256:
                self.log.error(
                    "migration_integrity_violation",
                    version=version,
                    expected_sha=row.sha256,
                    got_sha=m.sha256,
                    alert=True,
                )
                raise ChecksumMismatchError(version, expected=row.sha256, got=m.sha256)

    def _pending(
        self, applied: dict[int, _AppliedRow], migrations: list[Migration]
    ) -> list[Migration]:
        return [m for m in migrations if m.version not in applied]

    # ── Schema-migrations table helpers ──────────────────────────────────

    def _ensure_migrations_table(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                filename    VARCHAR NOT NULL,
                sha256      VARCHAR NOT NULL,
                applied_at  TIMESTAMP DEFAULT now()
            )
            """
        )

    def _ensure_migrations_table_readonly(self, conn: duckdb.DuckDBPyConnection) -> None:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'schema_migrations'"
        ).fetchall()
        if not rows:
            raise MigrationError(
                "database has no schema_migrations table; run `sb-stack migrate` first."
            )

    def _applied(self, conn: duckdb.DuckDBPyConnection) -> dict[int, _AppliedRow]:
        rows = conn.execute(
            "SELECT version, filename, sha256 FROM schema_migrations ORDER BY version"
        ).fetchall()
        return {
            int(v): _AppliedRow(version=int(v), filename=str(f), sha256=str(s))
            for (v, f, s) in rows
        }

    # ── Apply ────────────────────────────────────────────────────────────

    def _apply(self, conn: duckdb.DuckDBPyConnection, m: Migration) -> None:
        self.log.info("applying_migration", version=m.version, filename=m.filename)
        sql = m.sql()
        t0 = time.monotonic()
        try:
            conn.execute("BEGIN TRANSACTION")
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, sha256) VALUES (?, ?, ?)",
                [m.version, m.filename, m.sha256],
            )
            conn.execute("COMMIT")
        except Exception as e:
            # Best-effort rollback; surface the original error.
            with contextlib.suppress(duckdb.Error):
                conn.execute("ROLLBACK")
            self.log.error(
                "migration_failed",
                version=m.version,
                error=str(e),
                alert=True,
            )
            raise MigrationError(f"migration {m.version:03d} ({m.filename}) failed: {e}") from e
        dt_ms = int((time.monotonic() - t0) * 1000)
        self.log.info("migration_applied", version=m.version, duration_ms=dt_ms)

    # ── Backup ───────────────────────────────────────────────────────────

    def _backup_pre_migration(self, first_version: int) -> None:
        src = self.settings.db_path
        if not src.exists():
            # Nothing to back up yet (brand-new DB). Still log for traceability.
            self.log.info(
                "pre_migration_backup",
                path=None,
                reason="db_not_created_yet",
            )
            return
        dest_dir = self.settings.pre_migration_backup_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"sb.duckdb.pre-{first_version:03d}"
        shutil.copy2(src, dest)
        # Copy WAL if present
        wal = src.with_suffix(src.suffix + ".wal")
        if wal.exists():
            shutil.copy2(wal, dest.with_suffix(dest.suffix + ".wal"))
        self.log.info("pre_migration_backup", path=str(dest))


__all__ = ["Migration", "MigrationRunner"]
