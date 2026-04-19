"""DuckDB access layer: connection management + forward-only migrations."""

from sb_stack.db.connection import DB
from sb_stack.db.migrations import Migration, MigrationRunner

__all__ = ["DB", "Migration", "MigrationRunner"]
