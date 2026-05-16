-- 003_new_api_fields.sql
-- Author: Lucas Mårtensson
-- Date:   2026-05-01
-- Purpose: capture API fields discovered during E2E testing that the
--          original schema didn't include. All are simple scalar columns.

ALTER TABLE products ADD COLUMN IF NOT EXISTS origin_level_4 VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS origin_level_5 VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_default_product BOOLEAN;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_hidden BOOLEAN;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_searchable BOOLEAN;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_in_any_store_search_assortment BOOLEAN;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_in_selected_assortment BOOLEAN;
ALTER TABLE products ADD COLUMN IF NOT EXISTS preservable VARCHAR;
ALTER TABLE products ADD COLUMN IF NOT EXISTS is_dki BOOLEAN;
