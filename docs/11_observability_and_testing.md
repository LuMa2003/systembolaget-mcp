# 11 — Observability, testing, and dev workflow

Cross-cutting concerns: logs, metrics, tests, fixtures, local development.

## Log event taxonomy

Every structured log line uses a short snake_case `event` name + typed fields. Never freeform strings. Past-tense verbs where appropriate.

### Naming conventions

- `<subsystem>_<action>_<state>` — e.g. `catalog_page_fetched`, `api_key_extraction_failed`.
- Severity at the Python logger level (`log.info/warning/error`), not in the event name.
- `alert=true` field marks events the ntfy notifier should consider.
- Durations always in `_ms` or `_s` suffix on the field name.
- Counts always `<thing>_count` (e.g. `pages_count`).

### Full catalog

#### `api_client/`

| Event | Level | Fields | Triggers alert? |
|---|---|---|---|
| `api_request_started` | debug | url, method | |
| `api_request_completed` | debug | url, status, duration_ms | |
| `api_request_retrying` | warning | url, attempt, error | |
| `api_request_failed` | error | url, status, final_error | |
| `api_key_extraction_started` | info | | |
| `api_key_extracted` | info | key_prefix (8 chars), source (cache\|fresh) | |
| `api_key_cache_hit` | debug | age_days | |
| `api_key_validated` | info | key_prefix | |
| `api_key_extraction_failed` | error | error, `alert=true` | yes |
| `api_key_invalid` | error | key_prefix, `alert=true` | yes (`api_key_invalid`) |

#### `sync/`

| Event | Level | Fields | Triggers alert? |
|---|---|---|---|
| `sync_run_started` | info | run_id, reason (cron\|manual\|first_run\|replay), full_refresh, from_raw | |
| `sync_run_finished` | info | run_id, status, duration_ms | |
| `sync_run_catastrophic` | error | run_id, error, phase, `alert=true` | yes |
| `sync_run_skipped_locked` | warning | lockfile_age_hours, lockfile_pid | |
| `sync_run_unhandled` | error | exception | |
| `stale_lockfile_taken_over` | warning | age_hours, old_pid | |
| `stale_data_warning` | warning | hours_since_sync, `alert=true` | yes (`data_very_stale`) |
| `phase_started` | info | run_id, phase | |
| `phase_finished` | info | run_id, phase, outcome, duration_ms, counts | |
| `phase_timeout` | error | run_id, phase, budget_s | |
| `phase_skipped` | info | run_id, phase, reason | |

#### `sync/phases/`

| Event | Level | Fields |
|---|---|---|
| `catalog_page_fetched` | debug | category, page, items_count, duration_ms |
| `catalog_page_failed` | warning | category, page, error |
| `stores_fetched` | info | stores_count, home_stores_count |
| `stock_fetched` | info | site_id, pages_count, items_count, duration_ms |
| `stock_fetch_failed` | warning | site_id, page, error |
| `taxonomy_fetched` | info | filter_groups_count, total_values_count |
| `detail_fetched` | debug | product_number, both_sources |
| `detail_fetch_failed` | warning | product_number, error |
| `product_persisted` | debug | product_number, op (added\|updated\|unchanged) |
| `product_discontinued` | info | product_number |
| `product_resurrected` | info | product_number |
| `stock_persisted` | info | site_id, changed_rows, added, removed |
| `history_written` | debug | table, rows |
| `fts_rebuild_started` | info | |
| `fts_rebuild_completed` | info | duration_ms |
| `fts_rebuild_failed_first_attempt` | warning | error |
| `fts_rebuild_failed_both_attempts` | error | error |
| `embed_batch_sent` | debug | batch_index, size |
| `embed_batch_completed` | debug | batch_index, duration_ms |
| `embed_batch_failed` | warning | batch_index, size, error |
| `embed_dim_mismatch` | error | expected, got, `alert=true` |
| `pairing_candidate_cap_hit` | warning | dish, actual_count, cap, truncated_by |

#### `embed/` (client)

| Event | Level | Fields |
|---|---|---|
| `embed_request_sent` | debug | size, url |
| `embed_request_completed` | debug | size, duration_ms |
| `embed_request_retrying` | warning | attempt, error |
| `embed_service_not_ready` | info | waited_s |
| `embed_service_ready` | info | |

#### `embed_server/`

| Event | Level | Fields |
|---|---|---|
| `embedding_model_loading` | info | name, device |
| `embedding_model_loaded` | info | dim, load_time_s |
| `embedding_request_served` | debug | batch_size, duration_ms |

#### `db/migrations/`

| Event | Level | Fields | Triggers alert? |
|---|---|---|---|
| `migrations_up_to_date` | info | version | |
| `applying_migration` | info | version, filename | |
| `migration_applied` | info | version, duration_ms | |
| `migration_failed` | error | version, error, `alert=true` | yes |
| `migration_integrity_violation` | error | version, expected_sha, got_sha, `alert=true` | yes |
| `pre_migration_backup` | info | path |

#### `mcp_server/`

| Event | Level | Fields |
|---|---|---|
| `mcp_server_starting` | info | transport, port |
| `mcp_server_started` | info | tools_registered_count |
| `mcp_tool_invoked` | debug | tool_name, latency_ms |
| `mcp_tool_error` | warning | tool_name, error_type, error_code |
| `mcp_auth_rejected` | warning | reason |

#### `notifications/`

| Event | Level | Fields |
|---|---|---|
| `ntfy_sent` | info | key, priority, title |
| `ntfy_suppressed_cooldown` | debug | key, age_hours, cooldown_hours |
| `ntfy_suppressed_low_priority` | debug | key, priority, min_priority |
| `ntfy_send_failed` | warning | key, error |

#### `doctor/`

| Event | Level | Fields |
|---|---|---|
| `doctor_check_started` | debug | name |
| `doctor_check_completed` | debug | name, status, duration_ms |
| `doctor_run_completed` | info | pass, warn, fail |

#### `raw_archive/`

| Event | Level | Fields |
|---|---|---|
| `raw_archive_written` | debug | date, file, size_bytes |
| `raw_archive_removed` | info | date |
| `raw_retention_cleaned` | info | deleted_count |

## Metrics file — finalized

Written to `/data/state/metrics.prom`, **overwritten** at end of each sync run (text-file collector style). Read-only from the consumer's perspective.

```
# HELP sb_sync_last_run_timestamp_seconds Unix time of the last sync attempt
# TYPE sb_sync_last_run_timestamp_seconds gauge
sb_sync_last_run_timestamp_seconds 1713463200

# HELP sb_sync_last_success_timestamp_seconds Unix time of the last successful sync
# TYPE sb_sync_last_success_timestamp_seconds gauge
sb_sync_last_success_timestamp_seconds 1713463200

# HELP sb_sync_last_status 0=success, 1=partial, 2=failed
# TYPE sb_sync_last_status gauge
sb_sync_last_status 0

# HELP sb_sync_duration_seconds Total wall time of the last run
# TYPE sb_sync_duration_seconds gauge
sb_sync_duration_seconds 287.15

# HELP sb_sync_consecutive_failures Count of consecutive non-success runs
# TYPE sb_sync_consecutive_failures gauge
sb_sync_consecutive_failures 0

# HELP sb_sync_phase_duration_seconds Duration by phase of the last run
# TYPE sb_sync_phase_duration_seconds gauge
sb_sync_phase_duration_seconds{phase="fetch"}     58.32
sb_sync_phase_duration_seconds{phase="persist"}   12.10
sb_sync_phase_duration_seconds{phase="details"}   84.51
sb_sync_phase_duration_seconds{phase="embed"}    127.92
sb_sync_phase_duration_seconds{phase="fts"}        3.14
sb_sync_phase_duration_seconds{phase="finalize"}   1.16

# HELP sb_sync_phase_outcome Phase outcome of the last run (0=ok..4=catastrophic)
# TYPE sb_sync_phase_outcome gauge
sb_sync_phase_outcome{phase="fetch"}    0
sb_sync_phase_outcome{phase="persist"}  0
sb_sync_phase_outcome{phase="details"}  0
sb_sync_phase_outcome{phase="embed"}    0
sb_sync_phase_outcome{phase="fts"}      0
sb_sync_phase_outcome{phase="finalize"} 0

# HELP sb_sync_products Latest run deltas
# TYPE sb_sync_products gauge
sb_sync_products_added              7
sb_sync_products_updated          143
sb_sync_products_discontinued       2
sb_sync_stock_rows_updated         28
sb_sync_embeddings_generated       12

# HELP sb_db_product_count Current product counts in the DB
# TYPE sb_db_product_count gauge
sb_db_product_count{state="active"}        27312
sb_db_product_count{state="discontinued"}    498

# HELP sb_db_stock_rows Current sparse stock rows
# TYPE sb_db_stock_rows gauge
sb_db_stock_rows 8234

# HELP sb_db_history_rows Cumulative history rows
# TYPE sb_db_history_rows gauge
sb_db_history_rows{table="stock_history"}   52841
sb_db_history_rows{table="product_history"}  9214

# HELP sb_db_size_bytes Size of sb.duckdb on disk
# TYPE sb_db_size_bytes gauge
sb_db_size_bytes 912384000

# HELP sb_embed_model_loaded_timestamp_seconds When sb-embed last loaded its model
# TYPE sb_embed_model_loaded_timestamp_seconds gauge
sb_embed_model_loaded_timestamp_seconds 1713459600

# HELP sb_api_key_last_validated_timestamp_seconds Last successful API key validation
# TYPE sb_api_key_last_validated_timestamp_seconds gauge
sb_api_key_last_validated_timestamp_seconds 1713463250
```

No Prometheus push or scrape endpoint in v1. A user who wants Grafana points the Prometheus text-file collector at `/data/state/metrics.prom`.

## Testing strategy

### Test pyramid

```
          ┌─────────────┐
          │   golden    │  <20 pairing scenarios
          │  (slow,     │
          │  meaningful)│
          └─────────────┘
        ┌───────────────────┐
        │   integration     │  <50 tests
        │ (VCR cassettes,   │
        │  sample DB,       │
        │  tiny embed)      │
        └───────────────────┘
     ┌─────────────────────────┐
     │         unit             │  >200 tests
     │ (pure logic, in-memory   │
     │  DuckDB, mocks)          │
     └─────────────────────────┘
```

Targets: unit ~1 s per test, integration ~1 s average with cassettes, golden ~5 s per scenario on a tiny model.

### Test layout

```
tests/
├── conftest.py                 shared fixtures
├── unit/                       fast, isolated
│   ├── test_settings.py
│   ├── test_config_extractor.py
│   ├── test_diff.py            hash + field diff
│   ├── test_migrations.py      sha256 integrity
│   ├── test_lockfile.py
│   ├── test_store_ref.py       sugar resolution
│   ├── test_freshness.py
│   ├── test_embedding_templates.py
│   ├── test_pairing_scorer.py  each signal function
│   ├── test_pairing_cultural.py
│   ├── test_pairing_engine.py  MMR + confidence
│   ├── test_alert_manager.py   state transitions
│   ├── test_dry_run.py
│   └── test_taxonomy.py
│
├── integration/                real-ish, slower
│   ├── test_sync_orchestrator.py   end-to-end with cassettes + sample DB
│   ├── test_mcp_tools.py           each tool against sample DB + mock embed
│   ├── test_embed_service.py       real sb-embed with tiny model
│   ├── test_phase_replay.py        --from-raw path
│   ├── test_doctor.py
│   └── test_api_client_retry.py    real httpx + respx mocks
│
├── contract/                   pinned response shapes
│   ├── test_sb_api_shape.py    snapshot of Systembolaget responses
│   └── test_openai_embed_shape.py  pinned OpenAI-compat shape
│
├── golden/                     regression checks on pairing quality
│   ├── pairing_scenarios.yaml
│   └── test_pairing_golden.py
│
├── fixtures/
│   ├── cassettes/              vcrpy recordings
│   ├── sample_products.json    ~100 curated products
│   ├── sample_stores.json      4 home stores
│   ├── sample_stock.json       ~500 stock rows
│   ├── sample_taxonomy.json
│   └── make_sample_db.py       builds tests/fixtures/sample.duckdb
│
└── benchmarks/                 not in CI; run on demand
    ├── bench_fts.py
    ├── bench_vector_search.py
    └── bench_pairing.py
```

### Fixtures

**In-memory DuckDB**: unit tests open `duckdb.connect(":memory:")` and apply `001_initial.sql`. No disk, no mocking, real DDL.

```python
@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    c.execute((SCHEMA_DIR / "001_initial.sql").read_text())
    yield c
    c.close()
```

**Sample DuckDB on disk**: integration tests use a pre-populated DB built from `fixtures/sample_*.json`. Built once by `tests/fixtures/make_sample_db.py`; committed to the repo if it's small enough, otherwise regenerated on demand:

```python
@pytest.fixture(scope="session")
def sample_db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("sb") / "sample.duckdb"
    build_sample_db(path)   # applies schema + loads fixtures
    return path
```

**VCR cassettes**: integration tests that exercise the real HTTP client use `vcrpy` to record/replay API interactions. Cassettes live in `tests/fixtures/cassettes/<endpoint>_<variant>.yaml`. Recorded once against the live API; scrubbed of the subscription key before commit (replaced with `REDACTED`).

```python
@pytest.mark.vcr("productsearch_vin_page_1.yaml")
async def test_fetch_catalog_page(api_client):
    result = await api_client.productsearch(category="Vin", page=1)
    assert len(result.products) == 30
```

Re-recording: `scripts/record-cassettes.sh` re-runs with `--record-mode=all` against the live API. Done rarely (quarterly, or when API breaks).

**Mock HTTP for unit tests**: `respx` mocks the `httpx.AsyncClient`. Faster than VCR; used for everything that doesn't need real response shape fidelity.

**Mock embedding service**: a pytest fixture spawns an in-process FastAPI app serving deterministic fake vectors so tests don't need a GPU or real model:

```python
@pytest.fixture
async def mock_embed():
    app = FastAPI()
    @app.post("/v1/embeddings")
    async def embed(req: dict):
        vectors = [[hash(t) % 1000 / 1000 for _ in range(2560)] for t in req["input"]]
        return {"data": [{"embedding": v, "index": i} for i, v in enumerate(vectors)],
                "model": "mock", "usage": {"prompt_tokens": 0, "total_tokens": 0}}
    async with httpx.AsyncClient(app=app, base_url="http://test") as client:
        yield client
```

Deterministic fake vectors mean pairing + semantic search tests produce stable rankings without needing real embeddings.

### Golden pairing scenarios

YAML-defined regression tests with loose-match assertions. Not "this exact bottle must rank #1" (brittle) — "a wine from Italy with body≥8 should appear in top 3" (stable).

`tests/golden/pairing_scenarios.yaml`:

```yaml
- name: fläskfile_i_gräddsås
  input:
    dish: "fläskfile i gräddsås"
    meal_context: main
    style_preference: balanced
  expects:
    confidence_at_least: medium
    top_3_all_in_categories: [Vin, Öl]
    at_least_one_top_3:
      - taste_symbols_contains: [Fläsk]
    at_least_one_top_3:
      - taste_clock_body: [6, 12]     # range [min, max]
        taste_clock_fruitacid: [5, 12]

- name: lax_kokt_potatis
  input:
    dish: "lax och kokt potatis"
    meal_context: main
    style_preference: classic
  expects:
    confidence: high
    top_3_category_1_distribution:
      Vin: {min: 2}       # at least 2 of top 3 are wine
    any_top_3:
      - category_level_2: [Vitt vin]
        taste_clock_fruitacid: [7, 12]

- name: julbord
  input:
    dish: "julbord"
    cultural_tag: julbord
    meal_context: buffet
  expects:
    top_5_category_1_distribution:
      Sprit: {min: 1}     # snaps should appear
      Vin: {min: 2}
    at_least_one_top_5:
      - category_level_2: [Kryddat brännvin]  # aquavit

- name: surströmming_cultural_hedge
  input:
    dish: "surströmming"
    cultural_tag: surströmming
  expects:
    confidence: low       # tool should hedge
    top_3_category_1_distribution:
      Sprit: {min: 1}
    diversity_categories_count: 2    # returns diverse options, not ranked winner
```

Test runner reads YAML, calls the engine against a mock embed client, validates each assertion. Failing assertions print what did rank so we can debug.

Goal: at least 50 scenarios covering the top Swedish dishes + cultural holidays. Fails if meaningful pairings regress; tolerates model changes.

### Contract tests

Pin the response shapes that the sync depends on. If Systembolaget changes their schema we want to know via a red test, not silent data corruption.

```python
def test_productsearch_response_shape():
    """Snapshot of a recorded productsearch/search response. Fails if
    top-level keys or product keys change."""
    cassette = load_cassette("productsearch_vin_page_1.yaml")
    body = json.loads(cassette.responses[0].body)
    assert set(body.keys()) == {
        "metadata", "products", "suggestedProducts",
        "filters", "filterMenuItems", "trackingUrlParameters"
    }
    product_keys = set(body["products"][0].keys())
    assert REQUIRED_PRODUCT_KEYS.issubset(product_keys)
    # new keys appearing is OK, required keys missing is a red flag
```

One contract test per exercised endpoint. When a test fails, fix happens in two steps: update the model/persister, then update the snapshot.

## Dev workflow

### Local setup

```bash
git clone <repo>
cd systembolaget
uv sync --all-extras
uv run pre-commit install
```

`uv sync --all-extras` installs dev dependencies (pytest, ruff, mypy, vcrpy, respx).

### Running tests

```bash
uv run pytest tests/unit                  # fast loop (<10 s)
uv run pytest tests/integration           # ~1 min with cassettes
uv run pytest tests/golden                # ~2 min with mock embed
uv run pytest tests/contract              # seconds

uv run pytest -k pair                     # only pairing tests
uv run pytest --cov=src/sb_stack          # coverage
uv run pytest tests/unit -x --pdb         # first failure drops to debugger
```

### Linting, formatting, type-checking

Pre-commit runs:
- `ruff check --fix` — lint + auto-fixes
- `ruff format` — formatting (Black-compatible)
- `mypy src/sb_stack` — strict mode on `src/`, relaxed on `tests/`

Pyproject config:

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM", "ARG", "RET", "PL"]
ignore = ["PLR0913"]  # too many arguments — we have wide input models

[tool.mypy]
strict = true
python_version = "3.12"
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-ra --strict-markers --tb=short"
testpaths = ["tests"]
markers = [
    "slow: takes > 1 s",
    "gpu: requires GPU",
    "network: requires live network",
]
```

CI (GitHub Actions, if used) runs: unit → integration → contract → golden. GPU-marked tests skipped on CI runners.

### Local dev compose (`docker-compose.dev.yaml`)

**Default: match production** — same Qwen3-Embedding-4B model, same 2560-dim schema. This preserves dev-prod parity, lets you seed directly from a prod backup, and makes pairing iteration meaningful (you see the ranking your users will see).

```yaml
services:
  sb-embed-dev:
    build: .
    runtime: nvidia
    command: sb-stack embed-server
    environment:
      - SB_DATA_DIR=/data
      - SB_EMBED_MODEL=Qwen/Qwen3-Embedding-4B
      - SB_EMBED_DIM=2560
      - SB_EMBED_DEVICE=cuda:0
      - SB_LOG_LEVEL=debug
      - SB_LOG_FORMAT=text
      - NVIDIA_VISIBLE_DEVICES=all
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
    ports:
      - "9000:9000"
    volumes:
      - ./dev-data:/data
      - ./src:/app/src

  sb-mcp-dev:
    build: .
    command: sb-stack mcp
    depends_on: [sb-embed-dev]
    environment:
      - SB_DATA_DIR=/data
      - SB_EMBED_URL=http://sb-embed-dev:9000/v1/embeddings
      - SB_EMBED_DIM=2560
      - SB_STORE_SUBSET=1701
      - SB_MAIN_STORE=1701
      - SB_MCP_TRANSPORT=http
      - SB_MCP_TOKEN=dev-token
      - SB_LOG_LEVEL=debug
      - SB_LOG_FORMAT=text
      - SB_FIRST_RUN_ON_BOOTSTRAP=false
    ports:
      - "8000:8000"
    volumes:
      - ./dev-data:/data
      - ./src:/app/src
```

Differences from prod (all non-model-related):
- Single home store (Duvan only) to keep data small during development.
- Source mounted live for hot iteration (`./src:/app/src`).
- `SB_FIRST_RUN_ON_BOOTSTRAP=false` — never triggers a 50-min sync unless you opt in.
- No sync-scheduler service in the compose file — manually invoke sync when desired.
- `SB_LOG_FORMAT=text` and `SB_LOG_LEVEL=debug` — human-readable output while iterating.

### When to diverge from the prod model

Three legitimate cases where you actually want a different embedding setup than prod:

#### 1. Share prod's embedding service (preferred if you already have a running prod)

Point dev's MCP at prod's `sb-embed` over the network. One model loaded on one GPU, shared by both. No dev-side model download, no dev-side VRAM use.

```yaml
# docker-compose.dev.yaml (alternate)
services:
  sb-mcp-dev:
    # ... same as above ...
    environment:
      - SB_EMBED_URL=http://truenas.local:9000/v1/embeddings
      # no sb-embed-dev service needed
```

Requires exposing port 9000 from the prod container (mapped to host, reachable on LAN):

```yaml
# prod docker-compose.yaml
  sb-stack:
    ports:
      - "9000:9000"      # add this
```

Since sb-embed has no auth (container-local trust), only do this on a trusted LAN. If you want it reachable beyond LAN, terminate through a reverse proxy with auth.

Tradeoff: small added latency per embed call (LAN hop ~1-2 ms); significant savings in dev setup.

#### 2. CI / no-GPU development — use MiniLM

Only when there is genuinely no CUDA available (GitHub Actions runner, a Mac laptop, a Raspberry Pi). Uses `sentence-transformers/all-MiniLM-L6-v2` (90 MB, 384-dim, CPU):

```yaml
# docker-compose.ci.yaml
services:
  sb-embed-ci:
    command: sb-stack embed-server
    environment:
      - SB_EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
      - SB_EMBED_DIM=384
      - SB_EMBED_DEVICE=cpu
    # no runtime: nvidia, no GPU env vars
```

**Important caveats:**
- Schema's `FLOAT[2560]` mismatches `SB_EMBED_DIM=384`. The CI/MiniLM variant needs a separate schema migration path or a build-time decision about embedding column width. Simplest: maintain two schema baselines (`001_initial_2560.sql`, `001_initial_384.sql`), pick one at container start based on `SB_EMBED_DIM`.
- **Cannot seed from a prod backup** — vectors are wrong shape. CI must start from empty DB and either run a full sync or load fixtures.
- **Pairing rankings will differ from prod.** Fine for code correctness (does the tool return valid JSON, do filters compose correctly), not fine for ranking-quality assertions. Golden scenarios that depend on specific rankings skip or relax their assertions under this profile.

Use only when forced by environment.

#### 3. Unit tests — mock the client, no model

Unit tests never load any real embedding model. A pytest fixture provides a mock `EmbeddingClient` that returns deterministic vectors derived from `hash(text)` seeds (see §"Mock embedding service" above). Doesn't care about dim — tests pass whatever dim their assertions need. This is the right path for fast tests regardless of whether you have a GPU.

### Choosing your dev setup

| Situation | Recommended setup |
|---|---|
| You have the TrueNAS prod box running and develop from another machine on the LAN | Point dev at prod's sb-embed (§1) |
| You have a local GPU and want a fully-offline dev loop | Dev compose as shown at the top (same model as prod) |
| No GPU available at all | MiniLM variant (§2), accept the schema fork and ranking drift |
| Writing unit tests | Mock client (§3), regardless of GPU |

**Default the dev compose to the first or second case.** MiniLM is the exception, not the default.

### Iterating on the pairing engine

Pairing is where you'll spend most design iteration time. Workflow:

1. **Seed dev DB from a prod backup** — avoids re-running the whole sync locally.

   ```bash
   # grab yesterday's backup from TrueNAS
   scp user@truenas:/mnt/tank/sb-data/backup/sb.duckdb.2026-04-18 ./dev-data/sb.duckdb
   ```

   Since dev uses the same Qwen3-4B / 2560-dim as prod, the embeddings in the backup load as-is. You're now developing against real prod data with real prod embeddings.

   If you're on the MiniLM/CI profile (§2 above), you can't use prod backups directly — the embeddings are the wrong shape. Either drop the `product_embeddings` table on import and re-embed, or stick with the default Qwen3 profile.

2. **Unit-test the scorer in isolation:**

   ```bash
   uv run pytest tests/unit/test_pairing_scorer.py -v
   ```

3. **Run golden scenarios against local DB:**

   ```bash
   uv run pytest tests/golden/ -v
   # or a single scenario
   uv run pytest tests/golden/ -k fläskfile
   ```

4. **Interactive debugging:**

   ```python
   # scripts/try_pairing.py
   from sb_stack.pairing.engine import pair
   from sb_stack.db import DB
   from sb_stack.embed.client import MockEmbeddingClient

   db = DB.local("./dev-data/sb.duckdb")
   result = pair(
       db=db, embed=MockEmbeddingClient(),
       dish="fläskfile med rödvinssås",
       meal_context="main",
       style_preference="balanced",
   )
   for rec in result.recommendations:
       print(rec.product.name_bold, rec.pairing_score, rec.why)
   ```

   Run: `uv run python scripts/try_pairing.py`

### Iterating on MCP tools

FastMCP supports hot-reload for development:

```bash
# terminal 1: run the mock embed
uv run sb-stack embed-server &

# terminal 2: run MCP with reload
uv run fastmcp dev src/sb_stack/mcp_server/server.py
```

`fastmcp dev` is FastMCP's built-in Inspector integration — opens a web UI at `http://localhost:6274` where you can invoke tools interactively and see responses. Hot-reloads on source changes.

### Seeding a dev DB from a TrueNAS backup

Helper script at `scripts/seed-from-backup.sh`:

```bash
#!/usr/bin/env bash
# Usage: seed-from-backup.sh [YYYY-MM-DD]
set -euo pipefail

DATE="${1:-$(date -d yesterday +%Y-%m-%d)}"
TRUENAS_USER="${TRUENAS_USER:-luma}"
TRUENAS_HOST="${TRUENAS_HOST:-truenas.local}"
TRUENAS_PATH="${TRUENAS_PATH:-/mnt/tank/sb-data/backup}"

mkdir -p ./dev-data
scp "${TRUENAS_USER}@${TRUENAS_HOST}:${TRUENAS_PATH}/sb.duckdb.${DATE}" \
    ./dev-data/sb.duckdb

echo "Seeded dev-data/sb.duckdb from ${DATE}"
```

### Re-recording VCR cassettes

`scripts/record-cassettes.sh`:

```bash
#!/usr/bin/env bash
# One-shot re-record of all cassettes against live API.
# Scrubs the API key from responses before commit.
set -euo pipefail

uv run pytest tests/integration \
    --vcr-record=all \
    -k "not test_embed_service"       # real embed recording is separate

# Scrub subscription key from recorded responses
uv run python scripts/scrub_cassettes.py tests/fixtures/cassettes/

echo "Cassettes re-recorded. Review diffs before committing."
```

Run quarterly or when a contract test fails.

### Dev reset

`scripts/dev-reset.sh`:

```bash
#!/usr/bin/env bash
# Nukes dev data. Prompts for confirmation.
read -p "Delete dev-data/? [y/N] " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] && rm -rf ./dev-data/*
```

### Benchmarks (on demand)

Not in CI. Run manually when tuning:

```bash
uv run python tests/benchmarks/bench_pairing.py
# 20 scenarios × 5 runs = 100 pairings
# p50: 120 ms, p95: 280 ms, p99: 420 ms
```

Covers:
- FTS search latency (10 varied queries)
- Vector search latency (10 embedded queries) at 27k rows
- Pairing engine end-to-end (20 scenarios)
- Sync Phase B diff throughput (simulated 500-product changeset)

## CI sketch

GitHub Actions `.github/workflows/ci.yaml` (if the user ever wants this):

```yaml
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync --all-extras
      - run: uv run ruff check
      - run: uv run ruff format --check
      - run: uv run mypy src/sb_stack
      - run: uv run pytest tests/unit tests/contract
      - run: uv run pytest tests/integration tests/golden
```

No CUDA on runners → `@pytest.mark.gpu` tests auto-skipped. No live-network calls → only `@pytest.mark.network` tests skipped. Cassettes + mock embed make integration tests self-contained.

## Open questions

1. **Should the MCP server expose metrics too?** It has latency/error data the sync metrics file doesn't include (per-tool-call latency, error rates). Add a `sb_mcp_tool_duration_seconds{tool=}` histogram? I'd lean yes — cheap to add, useful if the user adds Grafana. File path would be `/data/state/mcp-metrics.prom`, rewritten every N minutes.

2. **Pre-commit hook for doc consistency?** The env-var list appears in 3 docs (06, 07, 11). Drift is inevitable. One-off check via `scripts/check-env-vars.sh` that greps env vars from the `Settings` class and compares to the docs? I'd lean yes but as a manual script, not pre-commit (slow).

3. **Snapshot test tool?** Several places benefit from snapshot assertions (MCP responses, metrics file format, dry-run output). `syrupy` is the modern pytest plugin. Worth adding or use raw JSON comparisons? I'd lean syrupy — well-maintained, readable snapshots.

4. **How thorough are contract tests?** One per endpoint, or one per endpoint × noteworthy variant (e.g. product with/without vintage)? Lean: one per endpoint; add variants when we find an edge case that breaks.

5. **Cassette storage.** Cassettes can grow large (30 MB for 1200 recorded requests, gzipped). Commit them to the repo or store externally (LFS, release asset)? Lean: commit gzipped (vcrpy supports `.yaml.gz`), accept the git-LFS-ish overhead. If repo grows uncomfortable, split to LFS later.

Next step (Step 5): packaging — Dockerfile, s6 service definitions, entrypoint, first-run flow implementation. Ready or want to pin these first?
