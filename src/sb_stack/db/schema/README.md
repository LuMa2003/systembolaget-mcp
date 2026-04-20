# Schema migrations

Forward-only DuckDB migrations. Applied by `sb_stack.db.migrations.MigrationRunner`.

## File conventions

- Name files `NNN_short_description.sql` (3-digit zero-padded, no gaps).
- Start with a comment block: one-line purpose, author, ISO date.
- DDL only: `CREATE TABLE / INDEX / SEQUENCE / VIEW`, `ALTER TABLE`,
  `PRAGMA create_fts_index`. No `INSERT / UPDATE / DELETE` — data
  migrations live as separate Python scripts.
- Use `CREATE TABLE IF NOT EXISTS` (belt-and-suspenders; the runner
  already guards against re-application).
- Do **not** `INSTALL` / `LOAD` extensions here — they can't run inside
  a transaction. `db/connection.py` loads `vss` + `fts` at open.

## After merge

Each file is **immutable**. Need a change? Write a new migration file.
The runner enforces strict sha256 equality between the on-disk file and
the recorded hash in `schema_migrations`; a whitespace edit to an
applied migration will refuse to start the next run.

## Backups

Before applying pending migrations, the runner writes a snapshot to
`/data/backup/pre-migration/sb.duckdb.pre-NNN` where NNN is the first
pending version.

## Bulk UPDATEs in data migrations

When a data migration needs to bulk-UPDATE a table that has a user-
created secondary index (e.g. backfilling a timestamp column), **drop
the index first, run the UPDATE, then recreate it**. We hit a DuckDB
1.5.2 FATAL ("Failed to append to PRIMARY_products_0: duplicate key")
during a 10k-row UPDATE on `products` with `idx_products_detail_fetched`
present, inside a DB that had WAL residue from an interrupted sync.
The crash isn't reproducible on a clean DB, so we believe it's a state-
dependent interaction between DuckDB's transaction-revert path, the PK
index, and the user index — but the drop/update/recreate pattern
sidesteps it regardless:

```python
with db.writer() as conn:
    conn.execute("DROP INDEX IF EXISTS idx_products_detail_fetched")
    conn.execute("UPDATE products SET last_detail_fetched_at = ... WHERE ...")
    conn.execute(
        "CREATE INDEX idx_products_detail_fetched "
        "ON products(last_detail_fetched_at)"
    )
```

This applies to Python data-migration scripts, not `.sql` files — DDL
migrations can't span the drop/update/recreate sequence transactionally
anyway.
