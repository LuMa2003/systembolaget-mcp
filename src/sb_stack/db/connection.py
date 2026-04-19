"""DuckDB connection management.

Two entry points:
  - `DB(settings).writer()` for sync (single, long-lived connection).
  - `DB(settings).reader()` for MCP (fresh per-request connection).

Both load the DuckDB `vss` and `fts` extensions at connection open so that
HNSW index and FTS pragma work downstream. Extensions are cached in
`settings.duckdb_ext_dir` (inside the persistent volume) so restarts don't
re-download them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from sb_stack.settings import Settings


class DB:
    """Thin wrapper around duckdb.connect with vss + fts loaded."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- Public context managers ------------------------------------------------

    @contextmanager
    def writer(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Open a read-write connection. Creates the DB file if missing."""
        self._ensure_parent_dirs()
        conn = duckdb.connect(str(self.settings.db_path), read_only=False)
        try:
            self._bootstrap_connection(conn, writable=True)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Open a read-only connection. Fails if the DB file is missing."""
        conn = duckdb.connect(str(self.settings.db_path), read_only=True)
        try:
            self._bootstrap_connection(conn, writable=False)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def memory(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Open a :memory: connection with the same extensions loaded.

        Test-only convenience; production paths always use `writer` or
        `reader`. Doesn't set the persistent extension directory, so CI
        machines download extensions fresh (fast enough).
        """
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("INSTALL vss; LOAD vss;")
            conn.execute("INSTALL fts; LOAD fts;")
            conn.execute("SET hnsw_enable_experimental_persistence = true;")
            yield conn
        finally:
            conn.close()

    # -- Internals --------------------------------------------------------------

    def _ensure_parent_dirs(self) -> None:
        for p in (
            self.settings.data_dir,
            self.settings.duckdb_ext_dir,
        ):
            Path(p).mkdir(parents=True, exist_ok=True)

    def _bootstrap_connection(self, conn: duckdb.DuckDBPyConnection, *, writable: bool) -> None:
        ext_dir = str(self.settings.duckdb_ext_dir)
        conn.execute(f"SET extension_directory='{ext_dir}'")
        if writable:
            conn.execute("INSTALL vss; LOAD vss;")
            conn.execute("INSTALL fts; LOAD fts;")
        else:
            # Read-only connections can't INSTALL; the writer has already
            # persisted the extensions to `ext_dir`.
            conn.execute("LOAD vss;")
            conn.execute("LOAD fts;")
        # Persist-HNSW flag: required so HNSW indexes survive restarts.
        conn.execute("SET hnsw_enable_experimental_persistence = true;")
