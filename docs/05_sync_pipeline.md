# 05 — Sync pipeline

Nightly batch pulls Systembolaget's catalog into the local DuckDB, maintains history, keeps embeddings fresh.

## Goals

- **Idempotent**: any failed run is safe to re-run; no duplicate history rows.
- **Incremental**: detail fetches and embeddings only for products that actually changed.
- **Atomic**: MCP readers never see half-written state (DuckDB snapshot isolation).
- **Replayable**: raw API responses archived to disk so DB can be rebuilt from them.
- **Polite**: ≤5 parallel HTTP requests, exponential backoff.
- **Single-writer**: one sync process holds the DuckDB write connection; MCP readers use snapshot isolation.

## Schedule

Systemd-style cron via **APScheduler** in-process:

- Default: `0 4 * * *` in **Europe/Stockholm** (04:00, after Systembolaget's own overnight refresh at ~01:00–02:00 based on observed `lastModified` timestamps).
- Configurable via `SB_SYNC_CRON` and `SB_SYNC_TIMEZONE`.
- Lockfile at `/data/state/lockfile` prevents concurrent runs. Stale-lock threshold: 6 hours.

## Six phases

```
 ┌────────────┐    ┌────────────┐    ┌─────────────┐    ┌──────────┐
 │  A. Fetch  │───▶│  B. Diff & │───▶│  C. Detail  │───▶│ D. Embed │
 │  (catalog, │    │   Persist  │    │  fetch for  │    │  changed │
 │   stores,  │    │   basic    │    │   changed/  │    │  rows    │
 │   stock,   │    │   tables + │    │   new       │    │  (GPU)   │
 │   taxonomy)│    │   history  │    │   products  │    │          │
 └────────────┘    └────────────┘    └─────────────┘    └──────────┘
                                                             │
                                      ┌──────────────────────┘
                                      ▼
                            ┌─────────────────┐    ┌─────────────┐
                            │  E. FTS rebuild │───▶│ F. Finalize │
                            │  (if products   │    │  (sync_runs,│
                            │   changed)      │    │   backup,   │
                            │                 │    │   checkpoint│
                            └─────────────────┘    └─────────────┘
```

Phases are sequential; each internally parallelizes where useful.

---

## Phase A — Fetch

Writes to `/data/raw/YYYY-MM-DD/` before any DB work.

| What | Endpoint | Calls |
|---|---|---|
| Catalog (list + search fields) | `sb-api-ecommerce/v1/productsearch/search?categoryLevel1=X&page=N&size=30` × 6 categories | ~910 |
| Home store metadata + opening hours | `sb-api-ecommerce/v1/site/stores` | 1 |
| Per-home-store stock + shelf | `sb-api-mobile/v1/productsearch/search?storeId=X&isInStoreAssortmentSearch=true&page=N&size=30` × 4 stores | ~300 |
| Taxonomy (filter values + upcoming launches) | `sb-api-mobile/v1/productsearch/filter` | 1 |

Total ~1,200 calls. At 5 concurrent × 300 ms avg, ~1 minute wall clock.

### Raw archive layout

```
raw/2026-04-19/
  catalog/
    Vin_page_0001.json.gz
    Vin_page_0002.json.gz
    Öl_page_0001.json.gz
    ...
  stock/
    store_1701_page_01.json.gz
    store_1702_page_01.json.gz
    store_1716_page_01.json.gz
    store_1718_page_01.json.gz
    ...
  details/
    642008_web.json.gz
    642008_mobile.json.gz
    ...
  stores.json.gz
  taxonomy.json.gz
  meta.json           -- run timestamps, counts, errors
```

Gzipped JSON, ~20 MB per day. Retained 365 days (`SB_RAW_RETENTION_DAYS`), so ~7 GB/year.

### Category partitioning

`productsearch/search` caps at page 333 (30 × 333 = 9990 rows). Total catalog is ~27k, so we partition by `categoryLevel1`:

- Vin: ~15,801 rows → needs partition by subcategory (Rött, Vitt, Mousserande, Rosé)
- Öl: ~4,889 rows → 1 partition fits (163 pages)
- Sprit: ~5,842 rows → 1 partition fits (195 pages)
- Cider & blanddrycker: ~487 rows → trivial
- Presentartiklar: ~30 rows → trivial
- Alkoholfritt: ~197 rows → trivial

For wine, we further partition by `categoryLevel2` ("Rött vin" 5,700, "Vitt vin" 5,600, etc.) — each fits in <333 pages.

---

## Phase B — Diff & persist

One DuckDB transaction wrapping all writes. MCP readers stay on pre-sync snapshot until commit.

### Per product

```
1. Compute field_hash of TRACKED fields.

2. Lookup existing row by product_number.

3. Three cases:
   - NOT FOUND           → INSERT products row, first_seen_at = now()
   - FOUND, hash unchanged → UPDATE last_fetched_at only
   - FOUND, hash changed → per-field diff; UPDATE products; INSERT one row
                           per changed field into product_history

4. If product is NOT in today's catalog BUT exists in DB and not already
   marked discontinued → UPDATE is_discontinued=true, discontinued_at=today.
   Don't delete.
```

### Tracked fields (whitelist)

```
price_incl_vat, price_excl_vat, recycle_fee, comparison_price,
assortment_text, is_discontinued, is_completely_out_of_stock,
is_temporary_out_of_stock, is_supplier_not_available,
is_news, is_new_in_assortment, is_limited_edition,
vintage, available_number_of_stores,
back_in_stock_at_supplier, supplier_not_available_date,
taste_clock_body, taste_clock_bitter, taste_clock_sweetness,
taste_clock_fruitacid, taste_clock_roughness,
taste_clock_smokiness, taste_clock_casque
```

Non-tracked fields (name, producer, country, category hierarchy, grape list) almost never change. We update `products` silently without writing history — noise not worth tracking.

Hash computed via deterministic JSON serialization (sorted keys, canonical whitespace) of the tracked field dict.

### Stock

```
For each (site_id, product_number) in today's stock responses:
  - UPSERT stock (observed_at = now())
  - If (stock, shelf, is_in_assortment) differs from previous value:
      INSERT stock_history

For each (site_id, product_number) in DB but NOT in today's response:
  - DELETE from stock
  - INSERT stock_history with stock=0, is_in_assortment=false
```

### Stores, opening hours

`UPSERT stores`, replace `store_opening_hours` rows for all home stores (21-day rolling window).

### Demand signal

Append one row per home store to `store_orders_daily` with today's `ordersToday`.

### Taxonomy

Append all filter-group snapshot rows to `filter_taxonomy` with `captured_at=today`.

### Scheduled launches

From taxonomy's `UpcomingLaunches` filter values:

```
For each (launch_date, count) pair:
  INSERT scheduled_launches (launch_date, observed_at=today, product_count)
```

Historical observations preserved so we can later detect moved/cancelled launches.

**Commit transaction.** MCP now sees new state.

---

## Phase C — Detail fetch (changed / new products only)

Only runs for products where Phase B detected `field_hash` change OR new row.

Per product, fetch both endpoints in parallel:
- `GET /sb-api-ecommerce/v1/product/productNumber/{n}`
- `GET /sb-api-mobile/v1/product/productNumber/{n}`

Merge: all ~170 fields end up on the `products` row. Where both endpoints have the same field, prefer web. Mobile-exclusive fields (`rating`, `price_excl_vat`, `vat_code`, `comment`, `need_crate_product_id`, `packaging_level_2`) fill their columns.

Also populates `product_variants` from each detail response's `sameProductDifferentSizes`.

Raw archived: `raw/YYYY-MM-DD/details/{productNumber}_{web|mobile}.json.gz`.

**First run:** all 27k products fetched. At 5 concurrent × 300 ms = ~30 minutes.
**Subsequent runs:** 50–500 products, ~1 minute.

---

## Phase D — Embeddings

Only embeds products whose `embed_text_hash` changed (category-specific template output). Calls the **sb-embed service** over HTTP (OpenAI-compatible `/v1/embeddings`) rather than loading the model in-process. See [09_embedding_service.md](./09_embedding_service.md).

```
Before phase:
  - Wait for sb-embed service to be ready (GET /health returns 200).
    Timeout 300 s; on timeout, mark sync 'partial' and skip phase.

For each product from Phase B/C (changed or new):
  1. Build embedding_text using category-specific template.
     See DISH_PAIRING_DESIGN.md §"Category-specific embedding templates"
     for per-category field lists.
  2. Compute source_hash = sha256(embedding_text).
  3. If source_hash == existing product_embeddings.source_hash → skip.
     Else → add to embed_queue.

POST embed_queue to sb-embed in batches of SB_EMBED_CLIENT_BATCH_SIZE (default 128).
UPSERT product_embeddings (product_number, embedding, source_hash,
                           model_name, template_version, embedded_at).
```

- Client: `sb_stack.embed.EmbeddingClient` (async httpx, retry with backoff).
- Server: `sb-embed` holds Qwen3-Embedding-4B in VRAM (fp16, 2560-dim default).
- `model_name`: e.g. `"Qwen/Qwen3-Embedding-4B@2560"` — lets future code re-embed when model changes.
- `template_version`: e.g. `"wine_v1"`, `"beer_v1"`, `"spirit_v1"` — lets us re-embed just one category after template edits.
- Server binds to `localhost:9000` inside the container (not exposed to host by default).
- Swappable: point `SB_EMBED_URL` at an Ollama/LM Studio/vLLM instance and disable sb-embed.

**First run:** 27k items × ~15 tokens ≈ 10–30 min end-to-end (bottleneck = GPU inference, not HTTP overhead at batch=128).
**Subsequent runs:** typically <100 products, seconds.

HNSW index auto-maintained by DuckDB's `vss` extension — no rebuild needed.

### Failure modes for Phase D specifically

| Failure | Behavior |
|---|---|
| sb-embed not ready after 300 s wait | Mark sync `partial`; other phases already done; retry next night |
| sb-embed returns 503 mid-phase | Client retries with backoff; if persistent → mark `partial` |
| Dimension mismatch (server serves a model with different dim than `SB_EMBED_DIM`) | Hard fail; log clear error; don't write corrupt vectors |
| Individual batch fails after retries | Skip that batch's products; they'll retry next night |

---

## Phase E — FTS rebuild

DuckDB's `fts` extension is a static index. Rebuild when any product was inserted or updated in Phase B/C.

```sql
PRAGMA drop_fts_index('products');
PRAGMA create_fts_index(
  'products', 'product_number',
  'name_bold', 'name_thin', 'producer_name', 'country',
  'taste', 'aroma', 'usage', 'producer_description',
  stemmer='swedish', stopwords='swedish',
  lower=1, strip_accents=0
);
```

A few seconds at 27k rows. Skip entirely if no products touched (rare on failed-and-resumed runs).

---

## Phase F — Finalize

```
UPDATE sync_runs
SET finished_at   = now(),
    status        = 'success' | 'partial' | 'failed',
    products_added, products_updated, products_discontinued,
    stock_rows_updated, embeddings_generated,
    error         = NULL | error_summary
WHERE run_id = current_run;

PRAGMA checkpoint;                   -- flush WAL
```

Then:
- **Backup**: copy `/data/sb.duckdb` to `/data/backup/sb.duckdb.YYYY-MM-DD`; delete backups older than `SB_BACKUP_RETENTION_DAYS` (default 7).
- **Raw retention**: delete `raw/YYYY-MM-DD/` dirs older than `SB_RAW_RETENTION_DAYS` (default 365).

---

## Change detection — the hashing scheme

Two hashes on each `products` row:

| Column | Over | Drives |
|---|---|---|
| `field_hash` | whitelist of ~22 tracked fields | Phase B: decide if `product_history` rows must be written + Phase C trigger |
| `embed_text_hash` | category-specific embedding template output | Phase D: decide if re-embed needed |

Both are sha256 of deterministic JSON (sorted keys, UTF-8, stable formatting).

If `field_hash` matches but `embed_text_hash` doesn't (e.g. we tweaked a template), only embeddings re-run. The converse is also fine.

---

## Failure handling

| Failure | Response |
|---|---|
| HTTP 5xx | Exponential backoff, max 5 retries: 1s → 2s → 4s → 8s → 16s |
| HTTP 429 | Honor `Retry-After` header; fall back to 30 s if absent |
| HTTP 401/403 | Force-refresh API key (re-extract), retry once. Persistent → halt, alert |
| Network timeout | Same as 5xx |
| Empty / malformed response | Retry once; on persistent emptiness, treat as real result |
| Partial Phase A | Phase B processes what was fetched; missing products aren't marked discontinued (neither seen nor unseen); sync status = `partial` |
| Phase B crash mid-transaction | DuckDB rolls back; next run re-reads raw files and retries |
| Phase D GPU failure | Log, continue. Products remain searchable via FTS; `semantic_search` just misses new ones until next run |
| DuckDB file corruption | Restore from `backup/` (up to 7 days back); re-run sync |
| Unknown field in API response | Log once, ignore silently (Systembolaget can add fields without breaking us) |

---

## CLI

One entrypoint, composable flags:

```
sb-stack sync                       # all phases, full catalog, incremental details/embeddings
sb-stack sync --full-refresh        # force Phase C + D for all products (first-run equivalent)
sb-stack sync --phase=fetch         # Phase A only
sb-stack sync --phase=persist       # Phases B+C from existing raw/
sb-stack sync --phase=embed         # Phase D only
sb-stack sync --phase=fts           # Phase E only
sb-stack sync --from-raw=2026-04-18 # replay a previous day's raw/ into DB
sb-stack sync --dry-run             # fetch + diff report, no writes

sb-stack sync-scheduler             # long-running; fires `sync` on SB_SYNC_CRON
```

`--from-raw` is the replay path: useful for schema migrations (rebuild DB from archived raw files).

---

## First run vs incremental

| Phase | First run | Daily run |
|---|---|---|
| A (catalog + stock) | ~1,200 calls, ~1 min | same |
| B (persist) | 27k INSERTs | 100–500 UPDATEs |
| C (details) | 27k detail calls, ~30 min | 50–500 calls, ~1 min |
| D (embeddings) | 27k items, ~20 min on 1080 Ti | <100 items, seconds |
| E (FTS) | few seconds | same |
| F (finalize) | sub-second | same |

**First run total:** ~50 minutes wall clock.
**Daily run total:** ~5 minutes.

First run triggers automatically on container bootstrap when `sync_runs` table is empty (see [07_deployment.md](./07_deployment.md) §Bootstrap).

---

## Observability

Emitted as structured logs + tables:

- **`sync_runs` table** — one row per run, the authoritative operational log. Queryable by MCP's `sync_status` tool.
- **structlog events** — one line per phase start/end with counts and duration. Format: JSON to stdout, tee'd to `/data/logs/sb-sync.log`.
- **Metrics file** — `/var/lib/sb/metrics.prom` (Prometheus text format) with: sync duration, products added/updated/discontinued, embeddings generated, consecutive-failure counter.
- **Alerts** fire on:
  - Two consecutive sync runs with `status != 'success'`
  - Most recent `started_at` > 30 h ago
  - Any 401/403 from Systembolaget API (key revocation)

---

## API config extraction

Runs at sync start (Phase A entry), after checking the 7-day cache. Behavior detailed in [02_systembolaget_api.md](./02_systembolaget_api.md) §Key extraction.

Summary:
1. Is `SB_API_KEY` env var set? → use it, skip extraction.
2. Is cached key < 7 days old and still valid? → use it.
3. Else scrape frontend, extract `NEXT_PUBLIC_*`, validate, cache.
4. If validation fails → hard error with a clear "extraction succeeded but API rejected the key" message.

Also opportunistically re-extracts on any 401 mid-sync, retrying once before halting.
