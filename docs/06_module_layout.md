# 06 — Module layout & foundations

Python package structure, settings, logging, errors, DB connection, CLI entrypoint, schema migration runner, doctor healthcheck. Ratified 2026-04-19.

## Repo structure

```
systembolaget/
├── README.md
├── DISH_PAIRING_DESIGN.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── .python-version                 (3.12)
├── .gitignore
├── .dockerignore
├── .env.example
│
├── Dockerfile
├── docker-compose.yaml
│
├── src/
│   └── sb_stack/                   ← single package, src-layout
│
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   ├── fixtures/cassettes/         (vcr.py recordings of real API responses)
│   └── golden/pairing_scenarios.yaml
│
├── docs/                            ← you are here
│
└── deploy/
    ├── s6-rc.d/
    │   ├── sb-mcp/
    │   └── sb-sync-scheduler/
    ├── entrypoint.sh
    └── truenas/
        ├── README.md
        ├── compose.yaml
        └── env.example
```

Src-layout (`src/sb_stack/`) prevents accidental imports of dev-only modules, plays well with uv + hatchling.

Single package `sb_stack` (not separate `sb_sync` + `sb_mcp`) so shared code (db, models, settings, logging) has a natural home.

## Package tree (`src/sb_stack/`)

```
sb_stack/
├── __init__.py                     version string
├── __main__.py                     enables `python -m sb_stack`
│
├── settings.py                     pydantic-settings Settings class
├── logging.py                      structlog configuration
├── errors.py                       exception hierarchy
│
├── db/
│   ├── __init__.py
│   ├── connection.py               DB class with .writer() / .reader()
│   ├── migrations.py               MigrationRunner
│   └── schema/                     SQL DDL files (forward-only)
│       ├── README.md               migration conventions
│       ├── 001_initial.sql
│       └── (future 002_*.sql, 003_*.sql, ...)
│
├── models/
│   ├── __init__.py
│   ├── product.py                  Product, ProductDetail, ProductVariant
│   ├── store.py                    Store, OpeningHours, StoreOrdersDaily
│   ├── stock.py                    StockRow
│   ├── pairing.py                  PairingRequest, PairingResult, ScoreBreakdown
│   ├── sync.py                     SyncRun, RuntimeConfig
│   └── taxonomy.py                 FilterGroup, FilterValue
│
├── api_client/
│   ├── __init__.py
│   ├── client.py                   SBApiClient: async httpx, retry, backoff
│   ├── config_extractor.py         NEXT_PUBLIC_* scraper + key validator
│   ├── paths.py                    URL builders (don't hardcode paths)
│   └── rate_limit.py               asyncio.Semaphore wrapper
│
├── raw_archive/
│   ├── __init__.py
│   ├── writer.py                   gzip JSON → /data/raw/YYYY-MM-DD/
│   ├── reader.py                   replay mode (--from-raw)
│   └── retention.py                delete > SB_RAW_RETENTION_DAYS
│
├── embed/                          client-side: calls sb-embed over HTTP
│   ├── __init__.py
│   ├── client.py                   EmbeddingClient: async httpx, OpenAI-compat, retry
│   ├── templates.py                per-category embed-text templates
│   └── hashing.py                  stable source_hash
│
├── embed_server/                   server-side: the sb-embed FastAPI app
│   ├── __init__.py
│   ├── server.py                   FastAPI app, /v1/embeddings + /health + /v1/models
│   ├── models.py                   Pydantic request/response (OpenAI shape)
│   ├── loader.py                   SentenceTransformer loader + warmup
│   └── cli.py                      `sb-stack embed-server` subcommand
│
├── pairing/                        (see DISH_PAIRING_DESIGN.md)
│   ├── __init__.py
│   ├── engine.py
│   ├── scorer.py
│   ├── cultural.py
│   ├── diversity.py
│   ├── confidence.py
│   └── data/
│       ├── cultural_pairings.yaml
│       └── sauce_keywords.yaml
│
├── sync/
│   ├── __init__.py
│   ├── orchestrator.py             ties phases; handles partial failure
│   ├── scheduler.py                APScheduler wrapper
│   ├── cli.py                      sub-CLI for `sb-stack sync`
│   └── phases/
│       ├── __init__.py
│       ├── fetch_catalog.py        Phase A
│       ├── fetch_stores.py         Phase A
│       ├── fetch_stock.py          Phase A
│       ├── fetch_taxonomy.py       Phase A
│       ├── fetch_details.py        Phase C
│       ├── diff.py                 Phase B diff logic
│       ├── persist.py              Phase B writes
│       ├── embed.py                Phase D
│       ├── index.py                Phase E (FTS)
│       └── finalize.py             Phase F
│
├── mcp_server/
│   ├── __init__.py
│   ├── server.py                   fastmcp server entrypoint + HTTP transport
│   ├── auth.py                     bearer token middleware
│   ├── responses.py                Pydantic output models
│   └── tools/
│       ├── __init__.py             registers all tools
│       ├── search_products.py
│       ├── semantic_search.py
│       ├── find_similar_products.py
│       ├── pair_with_dish.py
│       ├── get_product.py
│       ├── compare_products.py
│       ├── list_home_stores.py
│       ├── get_store_schedule.py
│       ├── list_taxonomy_values.py
│       └── sync_status.py
│
├── doctor/
│   ├── __init__.py
│   ├── runner.py                   orchestrates checks
│   └── checks.py                   individual CheckResult functions
│
├── notifications/
│   ├── __init__.py
│   └── ntfy.py                     AlertManager; state-transition ntfy notifier
│
└── cli/
    ├── __init__.py
    └── main.py                     typer app; unified entrypoint
```

## Module dependency graph

Leaves → composites. No cycles.

```
                 settings ←── errors ←── logging
                    │            ↑          ↑
                    ↓            │          │
                 models ─────────┤          │
                    ↓            │          │
                    db ──────────┤          │
                 ↓  ↓  ↓         │          │
       api_client  │  raw_archive│          │
            ↓      │      ↓      │          │
           embed   │      │      │          │  (client — HTTP to embed_server)
            ↓      │      │      │          │
            ├──────┼──────┼──────┤          │
            │      │      │      │          │
         sync   pairing   │   mcp_server    │
            │      │      │      │          │
            └──────┴──────┴──────┤          │
                                 │          │
                             doctor         │
                                 │          │
                               cli ─────────┘

         embed_server (standalone FastAPI process, imports settings/logging only)
```

Rules:
- `settings`, `errors`, `logging` are leaves — no `sb_stack.*` imports between them.
- `models` only imports leaves.
- `db` imports `models`.
- `api_client`, `raw_archive`, `embed` (client) build on `db` (and optionally `models`).
- `embed_server` is an **independent subpackage** — only imports `settings` and `logging`. It must not import any other subpackage (in particular not `db`, `sync`, or `mcp_server`). This keeps it swappable with an external service.
- `sync` and `mcp_server` are siblings — neither imports the other.
- `pairing` used by one MCP tool + (future) one sync phase.
- `cli` is the only place that imports both `sync` and `mcp_server` (and `embed_server` via a subcommand).
- `doctor` imports everything it checks.

## Settings

Single `Settings` class in `settings.py`, pydantic-settings, env-var-driven, lazy singleton.

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SB_", env_file=".env", case_sensitive=False,
    )

    # Storage
    data_dir: Path = Path("/data")

    # Home stores (comma-separated in env)
    store_subset: list[str] = ["1701", "1702", "1716", "1718"]
    main_store: str = "1701"

    # API
    api_key: str | None = None
    api_key_cache_ttl_days: int = 7
    api_base_url: str = "https://api-extern.systembolaget.se"
    app_base_url: str = "https://www.systembolaget.se"

    # Embedding
    embed_url: str = "http://localhost:9000/v1/embeddings"
    embed_model: str = "Qwen/Qwen3-Embedding-4B"
    embed_dim: int = 2560
    embed_device: str = "cuda:0"          # used by embed_server only
    embed_port: int = 9000                # used by embed_server only
    embed_max_batch: int = 2048           # server-side hard limit
    embed_gpu_batch_size: int = 32        # server internal GPU batching
    embed_client_batch_size: int = 128    # client-side chunking

    # MCP
    mcp_port: int = 8000
    mcp_transport: Literal["http", "stdio"] = "http"
    mcp_token: str | None = None

    # Sync
    sync_cron: str = "0 4 * * *"
    sync_timezone: str = "Europe/Stockholm"
    first_run_on_bootstrap: bool = True
    sync_concurrency: int = 5

    # Retention
    backup_retention_days: int = 7
    raw_retention_days: int = 365

    # Phase timeouts (minutes)
    phase_fetch_timeout_minutes:    int = 20
    phase_persist_timeout_minutes:  int = 5
    phase_details_timeout_minutes:  int = 60
    phase_embed_timeout_minutes:    int = 60
    phase_index_timeout_minutes:    int = 2
    phase_finalize_timeout_minutes: int = 2

    # Ntfy notifier (optional; unset → silent)
    ntfy_url:             str | None = None
    ntfy_token:           str | None = None
    ntfy_cooldown_hours:  int = 6
    ntfy_min_priority:    int = 3

    # Logging
    log_level: str = "info"
    log_format: Literal["json", "text"] = "json"
    log_to_file: bool = True
    log_to_stdout: bool = True

    # Derived
    @property
    def db_path(self) -> Path:          return self.data_dir / "sb.duckdb"
    @property
    def raw_dir(self) -> Path:          return self.data_dir / "raw"
    @property
    def backup_dir(self) -> Path:       return self.data_dir / "backup"
    @property
    def models_cache_dir(self) -> Path: return self.data_dir / "models"
    @property
    def logs_dir(self) -> Path:         return self.data_dir / "logs"
    @property
    def state_dir(self) -> Path:        return self.data_dir / "state"
    @property
    def duckdb_ext_dir(self) -> Path:   return self.data_dir / "duckdb_extensions"

@functools.cache
def get_settings() -> Settings: return Settings()
```

`.env.example` at repo root lists every variable with its default.

Invariant: **anything a user might tune lives in `Settings`.** No scattered constants.

## Logging

structlog, configured once per process at startup.

```python
def configure_logging(settings: Settings, process_name: str):
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level.upper())
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    if settings.log_to_file:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.TimedRotatingFileHandler(
            settings.logs_dir / f"{process_name}.log",
            when="midnight", backupCount=30, encoding="utf-8",
        )
        fh.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger().addHandler(fh)
        logging.getLogger().setLevel(
            getattr(logging, settings.log_level.upper())
        )
```

Convention: every log emits a short `event` string (snake_case) + structured fields. Never unstructured strings. Example:

```python
log.info("catalog_page_fetched", category="Vin", page=17, items=30, duration_ms=312)
log.error("api_request_failed", url=url, status=resp.status_code, attempt=4)
```

Tee'd to stdout (Docker log collection) **and** `/data/logs/{process}.log` (rotated daily, 30-day retention).

## Error hierarchy

```python
class SBError(Exception):
    """Base for all sb-stack errors."""

class MigrationError(SBError): ...
class ChecksumMismatchError(MigrationError): ...
class ConfigExtractionError(SBError): ...

class SystembolagetAPIError(SBError):
    status_code: int
    url: str
class RateLimitedError(SystembolagetAPIError): ...
class AuthenticationError(SystembolagetAPIError): ...
class NotFoundError(SystembolagetAPIError): ...
class ServerError(SystembolagetAPIError): ...

class SyncError(SBError): ...
class PartialSyncError(SyncError): ...
class EmbeddingError(SyncError): ...

class MCPError(SBError): ...
class ProductNotFoundError(MCPError): ...
class InvalidInputError(MCPError): ...
class DataStalenessError(MCPError): ...
```

Every exception carries structured context in `__str__` (product_number, site_id, url).

## DB connection management

```python
class DB:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def writer(self):
        conn = duckdb.connect(str(self.settings.db_path), read_only=False)
        conn.execute(f"SET extension_directory='{self.settings.duckdb_ext_dir}'")
        conn.execute("INSTALL vss; LOAD vss;")
        conn.execute("INSTALL fts; LOAD fts;")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def reader(self):
        conn = duckdb.connect(str(self.settings.db_path), read_only=True)
        conn.execute(f"SET extension_directory='{self.settings.duckdb_ext_dir}'")
        conn.execute("LOAD vss; LOAD fts;")
        try:
            yield conn
        finally:
            conn.close()
```

Why `extension_directory` override: DuckDB defaults to `~/.duckdb/extensions/`. In Docker `$HOME` is `/root` — not mounted, extensions would re-download on every restart. Pointing it at `/data/duckdb_extensions/` persists them.

Why no pooling: DuckDB `connect()` is sub-millisecond. Pooling adds lifecycle complexity for no gain. Sync holds a single long-lived writer; MCP opens a fresh reader per request.

## CLI entrypoint (`cli/main.py`)

One binary via `[project.scripts]` entry:

```
sb-stack --help

  migrate              apply pending schema migrations (idempotent)
  sync [options]       trigger a sync run now
    --full-refresh       re-fetch details + re-embed all products
    --from-raw DATE      replay a previous day's raw/
    --phase PHASE        only run fetch|persist|details|embed|index|finalize
    --dry-run            fetch + diff report, no writes
  sync-scheduler       long-running; fires `sync` on SB_SYNC_CRON
  mcp [options]        run the MCP server (long-running)
    --transport (http|stdio)
    --port PORT
  embed-server         long-running; serves Qwen3 on SB_EMBED_PORT
  bootstrap            first-run flow: migrate + seed home stores + initial sync
  runs [options]       list recent sync runs
    --limit N            how many to show (default 20)
  run-info <run-id>    show full details (incl. phase breakdown) for one run
  extract-key          debug: print current NEXT_PUBLIC_* config
  doctor [options]     run healthchecks
    --json               machine-readable output
    --only NAMES         run specific checks
    --verbose            details + optional checks
    --exit-on-warn       treat warns as fails
  shell                open a read-only DuckDB shell against /data/sb.duckdb
```

Typer-based. Each subcommand is a function in a dedicated module imported by `cli/main.py`.

In the container, s6 spawns three of these as services: `sb-stack embed-server`, `sb-stack mcp`, and `sb-stack sync-scheduler`. See [09_embedding_service.md](./09_embedding_service.md) for startup ordering.

## Schema migration runner

### Design goals (locked)

1. **Forward-only**: no downgrades. Personal DB, reverse migrations over-engineered.
2. **Strict sha256 integrity**: byte-exact match required; whitespace edits forbidden.
3. **Atomic per migration**: each file in its own transaction, rollback on failure.
4. **Pre-migration backup**: snapshot DB before applying any pending migration.
5. **Self-documenting**: filenames encode intent; `schema/README.md` states conventions.

### File conventions

```
src/sb_stack/db/schema/
├── README.md
├── 001_initial.sql
├── 002_*.sql
└── ...
```

- Naming: `NNN_short_description.sql`, 3-digit zero-padded sequence, no gaps allowed.
- Content: DDL only. No INSERT/UPDATE (data migrations are separate Python scripts).
- Once merged: **immutable**. Edits → new migration file.
- Idempotent DDL: `CREATE TABLE IF NOT EXISTS` etc., belt-and-suspenders.
- Comments at top: one-line purpose, author, date.

### Runner

```python
class MigrationRunner:
    """Applies forward-only SQL migrations from schema/*.sql."""

    def __init__(self, db: DB, settings: Settings, logger):
        self.db = db
        self.settings = settings
        self.log = logger
        self.schema_dir = Path(__file__).parent / "schema"

    def run(self) -> int:
        """Apply all pending migrations. Returns count applied."""
        self._ensure_migrations_table()
        self._verify_applied_integrity()          # strict sha256 check
        pending = self._pending()
        if not pending:
            self.log.info("migrations_up_to_date")
            return 0
        self._backup_pre_migration(pending[0].version)
        for m in pending:
            self._apply(m)
        return len(pending)

    def verify(self) -> None:
        """Like run() but fails if anything pending. Used by sb-mcp."""
        self._ensure_migrations_table()
        self._verify_applied_integrity()
        if self._pending():
            raise MigrationError(
                "database schema is behind; run `sb-stack migrate` first "
                "(or start sb-sync, which migrates at startup)."
            )

    # internals: _discover(), _applied(), _verify_applied_integrity(),
    # _pending(), _backup_pre_migration(), _apply(), _ensure_migrations_table()
```

Strict integrity: if a previously applied migration's file sha256 doesn't match the recorded one, refuse to start. Forces discipline.

### DuckDB transactional caveat

DuckDB supports transactions, but `INSTALL` / `LOAD` extensions can't run inside a transaction. We keep migrations to `CREATE TABLE/INDEX/VIEW`, `ALTER TABLE`, and `PRAGMA create_fts_index` only. Extensions are loaded at connection open (not in migrations).

### When migrations run

- **sb-sync** calls `MigrationRunner.run()` at startup (before Phase A). Authoritative migrator. User explicitly chose sb-sync to own the schema.
- **sb-mcp** calls `MigrationRunner.verify()` at startup. If pending migrations exist, sb-mcp exits with a clear error. Prevents running against an unmigrated DB.

### The `schema_migrations` table

Defined in `001_initial.sql`:

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    INTEGER PRIMARY KEY,
  filename   VARCHAR NOT NULL,
  sha256     VARCHAR NOT NULL,
  applied_at TIMESTAMP DEFAULT now()
);
```

### Migration safety

- Never edit an applied migration — add a new one.
- No DML in schema `.sql` files — data backfills as separate Python scripts.
- Pre-migration backups live in `/data/backup/pre-migration/sb.duckdb.pre-NNN`.

## Doctor subcommand

Runs a battery of healthchecks; single command answers "is this stack healthy?".

### Structure

```python
@dataclass
class CheckResult:
    name: str
    status: Literal["pass", "warn", "fail"]
    duration_ms: int
    summary: str
    details: str | None
```

All checks run independently; one failing doesn't abort others.

Exit codes:
- **0** — all pass or warn
- **1** — at least one fail
- **2** — doctor itself crashed

### The 17 checks

| # | Check | Verifies | Fail condition |
|---|---|---|---|
| 1 | `settings` | `Settings()` loads | missing required env, validation error |
| 2 | `data_dir` | `/data` exists, writable, subdirs present | missing or unwritable |
| 3 | `db_reachable` | DuckDB file opens read-only | missing or corrupt |
| 4 | `migrations_current` | no pending migrations | pending exist (warn) |
| 5 | `migration_integrity` | applied sha256 match files | mismatch |
| 6 | `duckdb_extensions` | vss and fts both load | extension load failure |
| 7 | `product_count` | sensible row count | <1 (warn: never synced); >50k (warn) |
| 8 | `last_sync_freshness` | `MAX(started_at) FROM sync_runs` | >30h fail, >25h warn |
| 9 | `api_key_valid` | 1-row productsearch call | 401/403 fail, timeout/5xx warn |
| 10 | `api_key_extractable` | can re-scrape from frontend | verbose-only; pattern miss |
| 11 | `disk_space` | `/data` free bytes | <1 GB fail, <5 GB warn |
| 12 | `model_cache` | Qwen3 weights present in `/data/models/` | missing (warn; downloads on sb-embed start) |
| 13 | `gpu_available` | `torch.cuda.is_available()` | no CUDA (warn; CPU fallback) |
| 14 | `embed_service_reachable` | `GET SB_EMBED_URL/health` returns 200 | unreachable = fail, loading = warn |
| 15 | `embed_dim_match` | service's embedding dim == `SB_EMBED_DIM` | mismatch = fail |
| 16 | `home_stores_seeded` | `stores.is_home_store=true` matches env | mismatch |
| 17 | `raw_archive_state` | raw dir exists, newest <48h, retention observed | missing / stale |

### Flags

```
sb-stack doctor                      # default: pretty table, all checks
sb-stack doctor --json               # machine-readable
sb-stack doctor --only NAME,NAME     # specific checks
sb-stack doctor --verbose            # details blob + optional checks
sb-stack doctor --exit-on-warn       # CI mode
```

### Docker healthcheck integration

```dockerfile
HEALTHCHECK --interval=5m --timeout=30s --start-period=2m --retries=3 \
  CMD sb-stack doctor --only db_reachable,api_key_valid,disk_space --exit-on-warn
```

Heavier checks (GPU, model load, key extraction) run only on demand via CLI.

## Data directory layout (recap)

```
/data/
├── sb.duckdb
├── sb.duckdb.wal
├── duckdb_extensions/
├── raw/
│   └── YYYY-MM-DD/...
├── backup/
│   ├── sb.duckdb.YYYY-MM-DD        (rolling 7)
│   └── pre-migration/
│       └── sb.duckdb.pre-NNN
├── models/qwen3-embedding-4b/
├── logs/
│   ├── sb-sync.log
│   └── sb-mcp.log
└── state/
    ├── lockfile
    └── last_run_id
```

## pyproject.toml skeleton

```toml
[project]
name            = "sb-stack"
version         = "0.1.0"
description     = "Systembolaget assortment + pairing MCP server"
requires-python = ">=3.12"
dependencies = [
    "duckdb>=1.1",
    "httpx>=0.27",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "structlog>=24.4",
    "apscheduler>=3.10",
    "typer>=0.12",
    "fastmcp>=3.2",
    "uvicorn>=0.32",
    "sentence-transformers>=3.3",
    "torch>=2.5",
    "einops>=0.8",
    "pyyaml>=6.0",
    "tenacity>=9.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "pytest-vcr>=1.0",
    "ruff>=0.7",
    "mypy>=1.13",
    "types-pyyaml",
]

[project.scripts]
sb-stack = "sb_stack.cli.main:app"

[build-system]
requires      = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/sb_stack"]
```

Torch with CUDA installed via PyTorch's index URL in the Dockerfile (not pinned here — handled at build time).
