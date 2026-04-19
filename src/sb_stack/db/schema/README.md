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
