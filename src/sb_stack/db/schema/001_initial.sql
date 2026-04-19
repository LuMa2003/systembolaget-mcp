-- 001_initial.sql — initial sb-stack schema
-- Author: Lucas Mårtensson
-- Date:   2026-04-19
-- Purpose: All tables, indexes, and sequences described in docs/03_data_schema.md.
--          Vector (HNSW) and full-text indexes are built by the sync pipeline
--          (Phase E) once there is data to index; this migration stays pure DDL.

-- ──────────────────────────────────────────────────────────────────────────
-- Migration tracking (idempotent; MigrationRunner also creates this on demand)
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    filename    VARCHAR NOT NULL,
    sha256      VARCHAR NOT NULL,
    applied_at  TIMESTAMP DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────
-- Products — wide, denormalized, current-state only.
-- Field hashes drive change detection; see docs/05_sync_pipeline.md.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    product_number            VARCHAR PRIMARY KEY,
    product_id                VARCHAR UNIQUE,
    product_number_short      VARCHAR,

    -- Naming
    name_bold                 VARCHAR NOT NULL,
    name_thin                 VARCHAR,
    producer_name             VARCHAR,
    supplier_name             VARCHAR,

    -- Category hierarchy
    category_level_1          VARCHAR, category_level_1_id VARCHAR,
    category_level_2          VARCHAR, category_level_2_id VARCHAR,
    category_level_3          VARCHAR, category_level_3_id VARCHAR,
    category_level_4          VARCHAR, category_level_4_id VARCHAR,
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
    packaging_co2_level       VARCHAR,
    packaging_co2_g_per_l     INTEGER,
    seal                      VARCHAR,
    parcel_fill_factor        INTEGER,
    restricted_parcel_qty     INTEGER,

    -- Wine-specific
    vintage                   VARCHAR,
    grapes                    VARCHAR[],
    is_new_vintage            BOOLEAN,

    -- Composition
    alcohol_percentage        DECIMAL(4,1),
    sugar_content             INTEGER,
    sugar_content_g_per_100ml DECIMAL(4,1),
    standard_drinks           DECIMAL(4,1),
    color                     VARCHAR,

    -- Pricing
    price_incl_vat                 DECIMAL(10,2),
    price_incl_vat_excl_recycle    DECIMAL(10,2),
    price_excl_vat                 DECIMAL(10,2),
    recycle_fee                    DECIMAL(10,2),
    comparison_price               DECIMAL(10,2),
    prior_price                    DECIMAL(10,2),
    vat_code                       INTEGER,

    -- Taste clocks (0–12)
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
    taste_symbols                 VARCHAR[],

    -- Text content (candidates for embedding)
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
    assortment_text               VARCHAR,
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
    available_number_of_stores         INTEGER,
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

    -- Derived / operational
    image_url                     VARCHAR GENERATED ALWAYS AS (
        'https://product-cdn.systembolaget.se/productimages/'
        || product_id || '/' || product_id || '_400.webp'
    ),
    first_seen_at                 TIMESTAMP DEFAULT now(),
    last_fetched_at               TIMESTAMP,
    field_hash                    VARCHAR,
    embed_text_hash               VARCHAR
);

-- ──────────────────────────────────────────────────────────────────────────
-- product_variants — same-product-different-sizes graph.
-- NOTE: We deliberately omit FK constraints on product_number columns here
-- (and on stock/product_embeddings below). DuckDB rewrites UPDATEs on a
-- referenced table as DELETE+INSERT internally, which trips FK checks even
-- when the PK doesn't change. The sync pipeline only inserts child rows
-- for known products, so the app-level invariant stands without the DB FK.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_variants (
    product_number          VARCHAR NOT NULL,
    variant_product_number  VARCHAR NOT NULL,
    variant_volume_ml       INTEGER,
    variant_bottle_text     VARCHAR,
    PRIMARY KEY (product_number, variant_product_number)
);

-- ──────────────────────────────────────────────────────────────────────────
-- stores — all stores (4 flagged is_home_store=true for the project owner).
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stores (
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

-- ──────────────────────────────────────────────────────────────────────────
-- 21-day rolling opening-hours window per store. FKs omitted — see
-- product_variants note above.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS store_opening_hours (
    site_id     VARCHAR,
    date        DATE,
    open_from   TIME,
    open_to     TIME,
    reason      VARCHAR,
    PRIMARY KEY (site_id, date)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Demand proxy: daily ordersToday snapshots.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS store_orders_daily (
    site_id                                  VARCHAR,
    date                                     DATE,
    captured_at                              TIMESTAMP,
    orders_today                             INTEGER,
    full_assortment_orders_today             INTEGER,
    max_orders_per_day                       INTEGER,
    max_full_assortment_orders_per_day       INTEGER,
    PRIMARY KEY (site_id, date, captured_at)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Sparse current stock (home stores only).
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock (
    site_id           VARCHAR,
    product_number    VARCHAR,
    stock             INTEGER NOT NULL,
    shelf             VARCHAR,
    is_in_assortment  BOOLEAN,
    observed_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (site_id, product_number)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Change-only stock history.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS stock_history (
    site_id          VARCHAR,
    product_number   VARCHAR,
    observed_at      TIMESTAMP,
    stock            INTEGER,
    shelf            VARCHAR,
    is_in_assortment BOOLEAN,
    PRIMARY KEY (site_id, product_number, observed_at)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Change-only per-field product history. Tracked fields per docs/05.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_history (
    product_number  VARCHAR,
    observed_at     TIMESTAMP,
    field           VARCHAR,
    old_value       VARCHAR,
    new_value       VARCHAR,
    PRIMARY KEY (product_number, observed_at, field)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Scheduled launches (UpcomingLaunches filter snapshot).
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scheduled_launches (
    launch_date    DATE,
    observed_at    DATE,
    product_count  INTEGER,
    PRIMARY KEY (launch_date, observed_at)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Product embeddings. Vector index is created by the sync pipeline (Phase E)
-- once there are rows to index — leaving it out keeps this migration DDL-only.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS product_embeddings (
    product_number   VARCHAR PRIMARY KEY,
    embedding        FLOAT[2560],
    source_hash      VARCHAR,
    model_name       VARCHAR,
    template_version VARCHAR,
    embedded_at      TIMESTAMP
);

-- ──────────────────────────────────────────────────────────────────────────
-- Daily filter-facet snapshots — feeds the list_taxonomy_values MCP tool.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS filter_taxonomy (
    captured_at  DATE,
    filter_name  VARCHAR,
    value        VARCHAR,
    count        INTEGER,
    PRIMARY KEY (captured_at, filter_name, value)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Sync operational log.
-- ──────────────────────────────────────────────────────────────────────────
CREATE SEQUENCE IF NOT EXISTS sync_run_id_seq;

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id                BIGINT PRIMARY KEY,
    started_at            TIMESTAMP,
    finished_at           TIMESTAMP,
    status                VARCHAR,
    products_added        INTEGER,
    products_updated      INTEGER,
    products_discontinued INTEGER,
    stock_rows_updated    INTEGER,
    embeddings_generated  INTEGER,
    error                 VARCHAR
);

CREATE TABLE IF NOT EXISTS sync_run_phases (
    run_id        BIGINT NOT NULL,
    phase         VARCHAR NOT NULL,
    started_at    TIMESTAMP NOT NULL,
    finished_at   TIMESTAMP,
    outcome       VARCHAR,
    counts        JSON,
    error_summary VARCHAR,
    PRIMARY KEY (run_id, phase)
);

-- ──────────────────────────────────────────────────────────────────────────
-- Operational key-value store.
-- ──────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_config (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMP DEFAULT now()
);

-- ──────────────────────────────────────────────────────────────────────────
-- Indexes (columnar zonemaps cover most queries; these help hot paths).
-- ──────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_products_category_1 ON products(category_level_1);
CREATE INDEX IF NOT EXISTS idx_products_country    ON products(country);
CREATE INDEX IF NOT EXISTS idx_products_price      ON products(price_incl_vat);
CREATE INDEX IF NOT EXISTS idx_products_body       ON products(taste_clock_body);
CREATE INDEX IF NOT EXISTS idx_products_discont    ON products(is_discontinued);
CREATE INDEX IF NOT EXISTS idx_products_launch     ON products(product_launch_date);
CREATE INDEX IF NOT EXISTS idx_stock_product       ON stock(product_number);
CREATE INDEX IF NOT EXISTS idx_stock_hist_prod     ON stock_history(product_number, observed_at);
CREATE INDEX IF NOT EXISTS idx_stock_hist_site     ON stock_history(site_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_sync_run_phases_run ON sync_run_phases(run_id);
