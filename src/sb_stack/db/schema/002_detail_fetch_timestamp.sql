-- 002_detail_fetch_timestamp.sql
-- Author: Lucas Mårtensson
-- Date:   2026-04-20
-- Purpose: add last_detail_fetched_at so Phase C can precisely re-heal
--          products whose detail merge previously failed without
--          re-fetching every product whose `usage` happens to be null
--          (some categories legitimately have no usage text).

ALTER TABLE products ADD COLUMN IF NOT EXISTS last_detail_fetched_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_products_detail_fetched
    ON products(last_detail_fetched_at);
