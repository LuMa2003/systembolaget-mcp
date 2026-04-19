# 03 — Data schema (DuckDB)

Single `.duckdb` file at `/data/sb.duckdb`. DuckDB chosen over SQLite for: columnar compression (stock_history is repetitive), fast aggregations on time-series, Parquet export for analytics.

## Design principles

1. **Denormalized, wide tables.** Category/origin hierarchies, assortment flags, and packaging fields live inline on `products`. Columnar compression makes duplicate storage free.
2. **Change-based history, not daily snapshots.** Hash fields on ingest; append to `*_history` only when values change. Cuts storage by 10-50×.
3. **Sparse stock.** Only home stores × products they carry. (~10k rows vs 12M for all stores.)
4. **DuckDB native list types** for `grapes`, `taste_symbols` — `list_contains(taste_symbols, 'Fläsk')` is cleaner than a join table.
5. **Generated columns for derivable values** (image URL from product_id).
6. **Forward-only migrations** with sha256 integrity checking — see [06_module_layout.md](./06_module_layout.md).
7. **App-level referential integrity, no DB FKs on child → products/stores.** DuckDB rewrites UPDATEs on a referenced row as DELETE+INSERT internally, which raises an FK violation even when the PK isn't changing. Since Phase C re-updates `products` rows that `stock` and `product_embeddings` reference, we drop those FK constraints and rely on the sync pipeline's invariants (we only insert child rows for products that already exist in `products`). The FK on `sync_run_phases → sync_runs` is also removed for consistency.

## Tables overview

| Table | Row count (est.) | Purpose |
|---|---|---|
| `products` | 27k | Full product catalog, current state |
| `product_variants` | ~10k | Same product different sizes (Apothic 750ml ↔ 3L BiB) |
| `stores` | 455 | All stores (only 4 flagged `is_home_store=true`) |
| `store_opening_hours` | ~9k | 21 days × 455 stores, refreshed daily |
| `store_orders_daily` | ~455/day | `ordersToday` demand proxy |
| `stock` | ~10k | Current stock at home stores only |
| `stock_history` | 10k × 365 ÷ delta | Change-only stock events |
| `product_history` | <100k/year | Change-only product field events |
| `scheduled_launches` | ~2k | Upcoming launches from filter endpoint |
| `product_embeddings` | 27k | Qwen3-Embedding-4B vectors (2560-dim) |
| `filter_taxonomy` | ~500/day | Daily snapshots of filter facets |
| `sync_runs` | ~365/year | Operational log |
| `sync_config` | ~10 | Key-value config (api_key cache, last_run_id, etc.) |
| `schema_migrations` | ~10 | Migration tracking |

## The schema

### `products`

Wide table, ~120 columns. Every product here, current-state only. See §"Lifecycle" for how discontinued/new products are handled.

```sql
CREATE TABLE products (
  product_number            VARCHAR PRIMARY KEY,  -- "642008", public SKU
  product_id                VARCHAR UNIQUE,       -- "1004489", internal
  product_number_short      VARCHAR,

  -- Naming
  name_bold                 VARCHAR NOT NULL,
  name_thin                 VARCHAR,
  producer_name             VARCHAR,
  supplier_name             VARCHAR,

  -- Category hierarchy (string + id redundant, both kept for filter + display)
  category_level_1          VARCHAR,  category_level_1_id VARCHAR,
  category_level_2          VARCHAR,  category_level_2_id VARCHAR,
  category_level_3          VARCHAR,  category_level_3_id VARCHAR,
  category_level_4          VARCHAR,  category_level_4_id VARCHAR,
  custom_category_title     VARCHAR,

  -- Origin
  country                   VARCHAR,
  origin_level_1            VARCHAR,
  origin_level_2            VARCHAR,
  origin_level_3            VARCHAR,
  brand_origin              VARCHAR,

  -- Physical
  volume_ml                 INTEGER,
  bottle_code               VARCHAR,
  bottle_text               VARCHAR,
  bottle_text_short         VARCHAR,
  bottle_type_group         VARCHAR,
  packaging                 VARCHAR,
  packaging_level_1         VARCHAR,
  packaging_level_2         VARCHAR,
  packaging_type_code       VARCHAR,
  packaging_co2_level       VARCHAR,              -- Low/Medium/High
  packaging_co2_g_per_l     INTEGER,
  seal                      VARCHAR,
  parcel_fill_factor        INTEGER,
  restricted_parcel_qty     INTEGER,

  -- Wine-specific
  vintage                   VARCHAR,              -- string: non-vintage exists
  grapes                    VARCHAR[],            -- list of grape names
  is_new_vintage            BOOLEAN,

  -- Composition
  alcohol_percentage        DECIMAL(4,1),
  sugar_content             INTEGER,              -- g/L
  sugar_content_g_per_100ml DECIMAL(4,1),
  standard_drinks           DECIMAL(4,1),
  color                     VARCHAR,

  -- Pricing
  price_incl_vat                 DECIMAL(10,2),
  price_incl_vat_excl_recycle    DECIMAL(10,2),
  price_excl_vat                 DECIMAL(10,2),
  recycle_fee                    DECIMAL(10,2),
  comparison_price               DECIMAL(10,2),   -- kr/litre
  prior_price                    DECIMAL(10,2),
  vat_code                       INTEGER,

  -- Taste clocks (0-12)
  taste_clock_body              TINYINT,
  taste_clock_bitter            TINYINT,
  taste_clock_sweetness         TINYINT,
  taste_clock_fruitacid         TINYINT,
  taste_clock_roughness         TINYINT,
  taste_clock_smokiness         TINYINT,
  taste_clock_casque            TINYINT,
  taste_clock_group             VARCHAR,
  taste_clock_group_bitter      VARCHAR,
  taste_clock_group_smokiness   VARCHAR,
  has_casque_taste              BOOLEAN,

  -- Food pairings
  taste_symbols                 VARCHAR[],        -- list

  -- Text content (embedded for semantic search)
  usage                         TEXT,
  taste                         TEXT,
  aroma                         TEXT,
  producer_description          TEXT,
  production                    TEXT,
  cultivation_area              TEXT,
  harvest                       TEXT,
  soil                          TEXT,
  storage                       TEXT,
  raw_material                  TEXT,
  ingredients                   TEXT,
  allergens                     TEXT,
  additives                     TEXT,
  additional_information        TEXT,
  did_you_know                  TEXT,

  -- Labels / certifications / flags
  is_organic                    BOOLEAN,
  is_sustainable_choice         BOOLEAN,
  is_climate_smart_packaging    BOOLEAN,
  is_light_weight_bottle        BOOLEAN,
  is_eco_friendly_package       BOOLEAN,
  is_ethical                    BOOLEAN,
  ethical_label                 VARCHAR,
  is_kosher                     BOOLEAN,
  is_natural_wine               BOOLEAN,
  is_vegan_friendly             BOOLEAN,
  is_gluten_free                BOOLEAN,
  is_manufacturing_country      BOOLEAN,
  is_regional_restricted        BOOLEAN,

  -- Assortment classification
  assortment                    VARCHAR,
  assortment_text               VARCHAR,          -- "Fast sortiment" etc.
  is_bs_assortment              BOOLEAN,
  is_pa_assortment              BOOLEAN,
  is_fs_assortment              BOOLEAN,
  is_ts_assortment              BOOLEAN,
  is_tse_assortment             BOOLEAN,
  is_tsls_assortment            BOOLEAN,
  is_tss_assortment             BOOLEAN,
  is_tst_assortment             BOOLEAN,
  is_tsv_assortment             BOOLEAN,
  is_fsts_assortment            BOOLEAN,
  is_web_launch                 BOOLEAN,
  is_news                       BOOLEAN,
  is_new_in_assortment          BOOLEAN,
  is_limited_edition            BOOLEAN,

  -- Availability lifecycle
  is_completely_out_of_stock         BOOLEAN,
  is_temporary_out_of_stock          BOOLEAN,
  completely_out_of_stock_date       TIMESTAMP,
  is_supplier_not_available          BOOLEAN,
  is_supplier_temp_not_available     BOOLEAN,
  supplier_not_available_date        TIMESTAMP,
  supplier_temp_not_available_date   TIMESTAMP,
  back_in_stock_at_supplier          TIMESTAMP,
  is_out_of_stock_at_depot           BOOLEAN,
  is_depot_delivered                 BOOLEAN,
  customer_order_supply_source       VARCHAR,
  available_number_of_stores         INTEGER,     -- rarity signal
  is_discontinued                    BOOLEAN,
  discontinued_at                    DATE,

  is_store_order_applicable     BOOLEAN,
  is_home_order_applicable      BOOLEAN,
  is_agent_order_applicable     BOOLEAN,

  -- Mobile-only
  need_crate_product_id         VARCHAR,
  rating                        DECIMAL(3,2),

  -- Dates
  product_launch_date           DATE,
  original_sell_start_date      DATE,
  sell_start_time               TIMESTAMP,
  tasting_date                  DATE,

  -- Meta
  image_url                     VARCHAR GENERATED ALWAYS AS (
    'https://product-cdn.systembolaget.se/productimages/'
    || product_id || '/' || product_id || '_400.webp'
  ),
  first_seen_at                 TIMESTAMP DEFAULT now(),
  last_fetched_at               TIMESTAMP,
  field_hash                    VARCHAR,          -- sha256 of tracked fields
  embed_text_hash               VARCHAR           -- sha256 of embedding text
);
```

Two hash columns drive change detection:
- `field_hash`: over the TRACKED fields (see `05_sync_pipeline.md`); change → write history
- `embed_text_hash`: over category-specific embedding template output; change → re-embed

### `product_variants`

Product family graph from `sameProductDifferentSizes`.

```sql
CREATE TABLE product_variants (
  product_number          VARCHAR NOT NULL REFERENCES products(product_number),
  variant_product_number  VARCHAR NOT NULL,
  variant_volume_ml       INTEGER,
  variant_bottle_text     VARCHAR,
  PRIMARY KEY (product_number, variant_product_number)
);
```

### `stores`

```sql
CREATE TABLE stores (
  site_id                          VARCHAR PRIMARY KEY,
  alias                            VARCHAR,
  is_home_store                    BOOLEAN DEFAULT FALSE,
  is_main_store                    BOOLEAN DEFAULT FALSE,
  address                          VARCHAR,
  postal_code                      VARCHAR,
  city                             VARCHAR,
  county                           VARCHAR,
  phone                            VARCHAR,
  latitude                         DOUBLE,
  longitude                        DOUBLE,
  is_tasting_store                 BOOLEAN,
  is_full_assortment_order_store   BOOLEAN,
  depot_stock_id                   VARCHAR,
  parent_site_id                   VARCHAR,
  search_area                      VARCHAR,
  delivery_time_days               INTEGER,
  first_seen_at                    TIMESTAMP DEFAULT now(),
  last_fetched_at                  TIMESTAMP
);
```

Seeded at bootstrap with the user's 4 home stores flagged `is_home_store=true`, `1701` with `is_main_store=true`.

### `store_opening_hours`

```sql
CREATE TABLE store_opening_hours (
  site_id     VARCHAR REFERENCES stores(site_id),
  date        DATE,
  open_from   TIME,
  open_to     TIME,
  reason      VARCHAR,            -- "-" = closed (e.g. Sunday)
  PRIMARY KEY (site_id, date)
);
```

21-day rolling window refreshed daily. Old rows naturally pruned by UPSERT.

### `store_orders_daily`

```sql
CREATE TABLE store_orders_daily (
  site_id                                  VARCHAR REFERENCES stores(site_id),
  date                                     DATE,
  captured_at                              TIMESTAMP,
  orders_today                             INTEGER,
  full_assortment_orders_today             INTEGER,
  max_orders_per_day                       INTEGER,
  max_full_assortment_orders_per_day       INTEGER,
  PRIMARY KEY (site_id, date, captured_at)
);
```

Append-only. Demand proxy over time.

### `stock`

Current stock, sparse (home stores only).

```sql
CREATE TABLE stock (
  site_id           VARCHAR REFERENCES stores(site_id),
  product_number    VARCHAR REFERENCES products(product_number),
  stock             INTEGER NOT NULL,
  shelf             VARCHAR,
  is_in_assortment  BOOLEAN,
  observed_at       TIMESTAMP NOT NULL,
  PRIMARY KEY (site_id, product_number)
);
```

### `stock_history`

Change-only append.

```sql
CREATE TABLE stock_history (
  site_id          VARCHAR,
  product_number   VARCHAR,
  observed_at      TIMESTAMP,
  stock            INTEGER,
  shelf            VARCHAR,
  is_in_assortment BOOLEAN,
  PRIMARY KEY (site_id, product_number, observed_at)
);
```

### `product_history`

Change-only append. One row per changed field per observation.

```sql
CREATE TABLE product_history (
  product_number  VARCHAR,
  observed_at     TIMESTAMP,
  field           VARCHAR,
  old_value       VARCHAR,
  new_value       VARCHAR,
  PRIMARY KEY (product_number, observed_at, field)
);
```

Tracked fields (whitelist, see `05_sync_pipeline.md` §Change Detection):
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

### `scheduled_launches`

From `productsearch/filter` `UpcomingLaunches`. Captured daily; historical observations preserved so we can detect moved/cancelled launches.

```sql
CREATE TABLE scheduled_launches (
  launch_date    DATE,
  observed_at    DATE,
  product_count  INTEGER,
  PRIMARY KEY (launch_date, observed_at)
);
```

### `product_embeddings`

```sql
CREATE TABLE product_embeddings (
  product_number  VARCHAR PRIMARY KEY REFERENCES products(product_number),
  embedding       FLOAT[2560],
  source_hash     VARCHAR,              -- sha256 of embedding text
  model_name      VARCHAR,              -- e.g. "Qwen3-Embedding-4B@2560"
  template_version VARCHAR,             -- e.g. "wine_v1"
  embedded_at     TIMESTAMP
);
```

`model_name` + `template_version` together let us selectively re-embed subsets when we swap models or tweak templates.

### `filter_taxonomy`

```sql
CREATE TABLE filter_taxonomy (
  captured_at  DATE,
  filter_name  VARCHAR,        -- "Country", "Seal", "TasteSymbols", ...
  value        VARCHAR,
  count        INTEGER,
  PRIMARY KEY (captured_at, filter_name, value)
);
```

Daily snapshot. Lets `list_taxonomy_values` MCP tool answer "what countries exist?" without hitting the live API.

### `sync_runs`

```sql
CREATE TABLE sync_runs (
  run_id                BIGINT PRIMARY KEY,   -- auto-incremented
  started_at            TIMESTAMP,
  finished_at           TIMESTAMP,
  status                VARCHAR,    -- success / partial / failed
  products_added        INTEGER,
  products_updated      INTEGER,
  products_discontinued INTEGER,
  stock_rows_updated    INTEGER,
  embeddings_generated  INTEGER,
  error                 VARCHAR
);

-- Monotonic sequence (auto-increment via DuckDB sequence)
CREATE SEQUENCE sync_run_id_seq;
-- (Insert uses nextval('sync_run_id_seq') for run_id)
```

`run_id` is mapped to timestamp via `started_at` — the `sb-stack runs` and `sb-stack run-info` CLIs (see [06_module_layout.md](./06_module_layout.md)) surface this mapping.

### `sync_run_phases`

Per-phase timing and counts for each run. Populated by Phase F. Lets `run-info` explain "why was run N slow" (e.g. details phase took 30 min).

```sql
CREATE TABLE sync_run_phases (
  run_id        BIGINT NOT NULL REFERENCES sync_runs(run_id),
  phase         VARCHAR NOT NULL,  -- fetch | persist | details | embed | fts | finalize
  started_at    TIMESTAMP NOT NULL,
  finished_at   TIMESTAMP,
  outcome       VARCHAR,           -- ok | partial | skipped | failed | catastrophic
  counts        JSON,              -- phase-specific counters
  error_summary VARCHAR,           -- first N chars of joined error messages
  PRIMARY KEY (run_id, phase)
);
CREATE INDEX idx_sync_run_phases_run ON sync_run_phases(run_id);
```

Small: 6 rows per run × 365 runs/year ≈ 2k rows/year.

### `sync_config` (key-value)

Locked as single key-value table. One operational place; inspectable.

```sql
CREATE TABLE sync_config (
  key        VARCHAR PRIMARY KEY,
  value      VARCHAR NOT NULL,
  updated_at TIMESTAMP DEFAULT now()
);
```

Populated with:

| key | value |
|---|---|
| `sb_runtime_config` | JSON blob: extracted `NEXT_PUBLIC_*` values |
| `sb_runtime_config_at` | ISO timestamp of last successful extraction |
| `api_key_last_validated` | ISO timestamp of last validation probe |
| `last_run_id` | monotonic counter |
| `first_run_completed_at` | set once at bootstrap completion |

### `schema_migrations`

```sql
CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  filename    VARCHAR NOT NULL,
  sha256      VARCHAR NOT NULL,
  applied_at  TIMESTAMP DEFAULT now()
);
```

See [06_module_layout.md](./06_module_layout.md) for migration runner design.

## Indexes

DuckDB auto-creates zonemaps per column so columnar scans don't strictly need indexes. These help specific hot paths:

```sql
CREATE INDEX idx_products_category_1 ON products(category_level_1);
CREATE INDEX idx_products_country    ON products(country);
CREATE INDEX idx_products_price      ON products(price_incl_vat);
CREATE INDEX idx_products_body       ON products(taste_clock_body);
CREATE INDEX idx_products_discont    ON products(is_discontinued);
CREATE INDEX idx_products_launch     ON products(product_launch_date);
CREATE INDEX idx_stock_product       ON stock(product_number);
CREATE INDEX idx_stock_hist_prod     ON stock_history(product_number, observed_at);
CREATE INDEX idx_stock_hist_site     ON stock_history(site_id, observed_at);
```

## Search extensions

### HNSW (vss extension)

```sql
INSTALL vss; LOAD vss;
SET hnsw_enable_experimental_persistence = true;

CREATE INDEX products_vec_hnsw
    ON product_embeddings
    USING HNSW (embedding)
    WITH (metric = 'cosine');
```

Auto-maintained on INSERT/UPDATE of embedding rows.

### FTS (fts extension)

```sql
INSTALL fts; LOAD fts;

PRAGMA create_fts_index(
  'products', 'product_number',
  'name_bold', 'name_thin', 'producer_name', 'country',
  'taste', 'aroma', 'usage', 'producer_description',
  stemmer='swedish', stopwords='swedish',
  lower=1, strip_accents=0
);
```

FTS is static — must be rebuilt after bulk changes (Phase E of sync). Takes a few seconds at 27k rows.

## Lifecycle rules

### New product appears
- Not in `products` table → INSERT with `first_seen_at = now()`.

### Product changes
- Hash change detected → UPDATE `products`, compute per-field diff, append to `product_history`.

### Product disappears from catalog response
- Present in `products`, absent from today's response → UPDATE `is_discontinued=true`, `discontinued_at=today`.
- Never DELETE from `products` — history consumers and discontinued lookups need it.

### Product reappears
- `is_discontinued=true` but appears in catalog again → UPDATE `is_discontinued=false`, `discontinued_at=NULL`. Log the resurrection event to `product_history` with field `is_discontinued`.

### Stock disappears
- `(site_id, product_number)` was in `stock`, not in today's stock response → DELETE from `stock`, append a row to `stock_history` with `stock=0, is_in_assortment=false`.

## Size estimates (after 1 year)

| Table | Rows | Approx size |
|---|---|---|
| `products` | ~28k (+500/year churn) | ~250 MB (text-heavy) |
| `product_embeddings` | ~28k × 2560 × 4B | ~290 MB |
| `stock_history` | ~500k | ~20 MB (columnar RLE |
| `store_orders_daily` | ~170k (455 × 365) | ~8 MB |
| `product_history` | ~50k | ~3 MB |
| `filter_taxonomy` | ~180k | ~10 MB |
| FTS + HNSW indexes | — | ~200 MB combined |

Total DB after 1 year: ~800 MB–1 GB. Plus `raw/` archives (~7 GB/year at 1-year retention).
