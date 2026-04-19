# 04 — MCP tool surface

Ten tools, read-only (no writes exposed to the LLM), bearer-token auth, HTTP transport on port 8000.

## Framework

**`fastmcp>=3.2`** (standalone, by Jeremiah Lowin) — not the FastMCP bundled in the official `mcp` SDK (which is frozen at 1.0 behavior for protocol conformance).

Reasons (verified against current releases as of 2026-04-19):
- `fastmcp` v3.2.4 is actively maintained; `mcp`'s `mcp.server.fastmcp` module is effectively frozen.
- Bearer token auth is a first-class documented feature in FastMCP 3.
- Streamable HTTP transport: one kwarg (`mcp.run(transport="streamable-http")`).
- Decorator ergonomics with Pydantic input/output give cleaner tool schemas for a 10-tool server.
- ~70% of MCP servers across all languages use some version of FastMCP; de facto standard.

`fastmcp` requires Python ≥ 3.10; we're on 3.12.

## Transport

**Streamable HTTP** on `SB_MCP_PORT` (default 8000). Exposed outside the container; reachable from Claude Desktop / IDE / any MCP-aware client on the home network.

## Authentication

**Bearer token** required when transport is `http`. Token in `SB_MCP_TOKEN` env var. Middleware rejects requests without `Authorization: Bearer <token>` header.

If `SB_MCP_TOKEN` is unset and transport is `http`, the server refuses to start (fail-closed; we never want an unauth'd HTTP server on a home LAN by accident).

For `stdio` transport no bearer token is needed.

## Principles

- **Tools = user intents, not raw SQL.** A good tool answers a question a Swedish user would actually ask.
- **Home-store stock inlined everywhere.** Every product-returning tool also reports stock at the user's home stores, because that's always the follow-up question.
- **`site_id` parameters accept sugar**: `"main"` → 1701, `"home"` → all four, a siteId string, or omitted (tool-specific default).
- **Tool descriptions in Swedish.** The LLM picks tools based on their descriptions; Swedish descriptions align with Swedish queries. Function names stay English.
- **Deterministic tools, ergonomic LLM.** Tools return structured data with score breakdowns; the LLM is responsible for human-quality Swedish phrasing.
- **Read-only.** No tool mutates. Sync is a separate process.

## The 10 tools

---

### A. Find products

#### `search_products`

**Description (Swedish, shown to the LLM):**
> "Sök i Systembolagets sortiment med filter: kategori, land, pris, alkoholhalt, smakklockor, matsymboler, förpackning, certifieringar, m.m. Använd denna när frågan kan uttryckas som strukturerade kriterier eller innehåller exakta namn och sökord."

**Input:**
```
text                          string?           FTS over name, producer, country, taste, aroma, usage, producer_description
category                      string?           "Vin" | "Öl" | "Sprit" | "Cider & blanddrycker" | "Presentartiklar" | "Alkoholfritt"
subcategory                   string?           matches categoryLevel2 or 3
country                       string?
region                        string?           matches originLevel1/2
grapes_any                    string[]?
vintage                       string?
price_min, price_max          number?
abv_min, abv_max              number?
volume_min_ml, volume_max_ml  integer?
sugar_min, sugar_max          number?
taste_body_min, _max          integer?          0-12
taste_sweet_min, _max         integer?          0-12
taste_bitter_min, _max        integer?          0-12
taste_fruitacid_min, _max     integer?          0-12
taste_rough_min, _max         integer?          0-12
taste_smoke_min, _max         integer?          0-12
pairs_with_any                string[]?         matches taste_symbols (any overlap)
seal                          string?
packaging                     string?
co2_impact                    string?           "Lägre" | "Medel" | "Högre"
is_organic                    boolean?
is_vegan                      boolean?
is_gluten_free                boolean?
is_kosher                     boolean?
is_natural_wine               boolean?
is_ethical                    boolean?
ethical_label                 string?
assortment_text               string?
launched_since                date?
in_stock_at                   string?           "main" | "home" | siteId | null
include_discontinued          boolean = false
order_by                      string = "relevance"  -- relevance | price_asc | price_desc | launch_desc | body_asc | body_desc | comparison_price_asc
limit                         integer = 20
offset                        integer = 0
```

**Output:**
```
{
  results: [
    {
      product_number, name_bold, name_thin, producer_name, category_level_1..3,
      country, origin_level_1, volume_ml, alcohol_percentage,
      price_incl_vat, comparison_price,
      taste_clocks: { body, bitter, sweetness, fruitacid, roughness, smokiness, casque },
      taste_symbols, grapes,
      is_organic, is_vegan_friendly, is_discontinued,
      home_stock: { "1701": {stock, shelf, is_in_assortment}, ... },
      image_url
    }
  ],
  total_count: number
}
```

**Reads:** `products`, `stock` (LEFT JOIN for home_stock).

This tool absorbs "new arrivals" (via `launched_since`), "food pairing" (via `pairs_with_any`), and all faceted search.

---

#### `semantic_search`

**Description:**
> "Hitta drycker genom fritext där beskrivningen är stämningsfull eller parafraserande, t.ex. 'en rökig whisky som passar en höstkväll' eller 'nåt lättdrucket och fräscht'. Använd när search_products inte räcker."

**Input:**
```
query          string
filters        SearchProductsFilters?   -- optional subset of search_products params, pre-filters before vector ranking
limit          integer = 10
in_stock_at    string?
```

**Output:**
```
{
  results: [
    { <Product fields>, home_stock, similarity: number (0-1) }
  ]
}
```

**Reads:** `product_embeddings` (HNSW), `products`, `stock`.

Implementation: embed `query` with Qwen3-Embedding-4B, run `WHERE <filters> ORDER BY array_cosine_distance(embedding, $q_vec) LIMIT N`. Pre-filter keeps the vector-rank set small.

---

#### `find_similar_products`

**Description:**
> "Hitta drycker som liknar en given produkt. Matchas på smakklockor, kategori och semantisk beskrivning."

**Input:**
```
product_number       string
limit                integer = 10
same_category_only   boolean = true
max_price            number?
in_stock_at          string?
```

**Output:**
```
{
  source: Product,
  similar: [ { <Product>, home_stock, similarity } ]
}
```

**Reads:** `product_embeddings`, `products`, `stock`.

---

#### `pair_with_dish`

**Description:**
> "Föreslår drycker som passar till en beskriven maträtt. Tolkar maten i tre lager: (1) semantisk matchning mot Systembolagets sommeliertexter om varje produkt, (2) smakklocke- och matsymbolanalys, (3) regional/traditionell affinitet. Bäst för vardagsmatlagning och klassiska rätter. För fusionsmat eller ovanliga kombinationer återges lägre confidence och flera alternativ."

**Input / output / behavior:** delegated to the pairing engine — see [`../DISH_PAIRING_DESIGN.md`](../DISH_PAIRING_DESIGN.md) for the full contract.

**Reads:** `product_embeddings`, `products`, `stock`, + in-memory cultural pairings YAML.

---

### B. Inspect

#### `get_product`

**Description:**
> "Hämta fullständig information om en specifik produkt (alla ~170 fält plus varianter i andra storlekar och lagerstatus i hemmabutikerna)."

**Input:**
```
product_number   string?       -- one of these
query            string?       -- fuzzy name lookup via FTS
```

**Output:**
```
{
  product: Product,                    -- all ~170 fields
  variants: Product[],                 -- same product, other sizes
  home_stock: [
    { site_id, alias, stock, shelf, is_in_assortment, observed_at }
  ],
  image_urls: [ { size, url } ]        -- 100, 200, 400, 800
}
```

**Reads:** `products`, `product_variants`, `stock`, `stores`.

If `query` given without `product_number`, returns the top FTS hit.

---

#### `compare_products`

**Description:**
> "Jämför 2–5 produkter sida vid sida (pris, smakprofil, ursprung, hållbarhet, lagerstatus)."

**Input:**
```
product_numbers  string[]    -- 2 to 5
```

**Output:**
```
{
  rows: [                    -- each row = one field across products
    { field, values: [value_per_product] }
  ],
  products: Product[]
}
```

Fields compared: price, comparison_price, ABV, volume, all 7 taste clocks, country, vintage, is_organic, assortment_text, home stock summary.

**Reads:** `products`, `stock`.

---

### C. Stores

#### `list_home_stores`

**Description:**
> "Lista användarens hemmabutiker (Duvan, Bergvik-Karlstad, Välsviken, Skoghall) med öppettider och position."

**Input:** (none)

**Output:**
```
[
  {
    site_id, alias, address, city, county,
    is_main_store, latitude, longitude,
    today_open_from, today_open_to,
    distance_from_main_km   -- computed Haversine
  }
]
```

**Reads:** `stores`, `store_opening_hours`.

---

#### `get_store_schedule`

**Description:**
> "Visa öppettider för en butik de kommande dagarna."

**Input:**
```
site_id     string = "main"
days_ahead  integer = 14
```

**Output:**
```
{
  store: Store,
  schedule: [ { date, open_from, open_to, reason, is_open } ]
}
```

**Reads:** `stores`, `store_opening_hours`.

---

### D. Meta

#### `list_taxonomy_values`

**Description:**
> "Lista giltiga värden för ett sökfilter, t.ex. alla länder i sortimentet, alla kapsyltyper, alla matsymboler. Använd innan search_products om du är osäker på exakta värden."

**Input:**
```
filter_name   string   -- "Country" | "Seal" | "TasteSymbols" | "AssortmentText" |
                       -- "Grapes" | "PackagingLevel1" | "EthicalLabel" | "CategoryLevel1"
min_count     integer = 1
```

**Output:**
```
{
  values: [ { value, count } ],
  captured_at: date
}
```

**Reads:** `filter_taxonomy` (latest snapshot).

Prevents the LLM from hallucinating filter values.

---

#### `sync_status`

**Description:**
> "Visa när databasen senast synkades mot Systembolagets API och hur aktuella siffrorna är."

**Input:** (none)

**Output:**
```
{
  last_run: {
    run_id, started_at, finished_at, status,
    products_added, products_updated, products_discontinued,
    stock_rows_updated, embeddings_generated,
    error
  },
  hours_since_last_success: number,
  product_count: number,
  home_stock_rows: number,
  api_key_last_validated: timestamp,
  stale: boolean            -- true if > 30h since last success
}
```

**Reads:** `sync_runs`, `products` (count), `stock` (count), `sync_config`.

Lets the LLM answer "is this data fresh?" and refuse confidently when it isn't.

---

## Intentionally missing

| Tool | Why skipped |
|---|---|
| `find_stores_with_product` (all 455 stores) | Sparse stock = only home-store data available locally; answering broader queries would require a live API call from MCP, breaking the offline model. |
| `get_product_history` / `get_stock_history` / `get_store_demand` | History is for external analytics only (per user decision). Tables still populated; read directly via DuckDB/Parquet for analytics. |
| Write tools (favorites, lists, add to cart) | User pinned: MCP is read-only. |
| Pairing-by-URL (scrape recipe from koket.se, pair it) | Out of scope for MCP v1; possible in a future standalone pairing app. |

## Error model

Each tool maps errors to structured MCP responses:

| Exception | MCP error |
|---|---|
| `ProductNotFoundError` | `NOT_FOUND` with the offending product_number |
| `InvalidInputError` | `INVALID_PARAMS` with which param failed |
| `DataStalenessError` | `PRECONDITION_FAILED` — sync hasn't run recently; includes hours_since |
| unexpected `SBError` | `INTERNAL_ERROR` with sanitized message (no stack trace leaked) |

Every response includes a short Swedish error message suitable for the LLM to surface directly.

## Healthcheck

`GET /health` returns 200 OK with JSON `{status: "ok"}`. Used by Docker HEALTHCHECK and monitoring. Separate from the `doctor` CLI — lighter, doesn't touch DB or API.

## Client configuration example

Client-side (Claude Desktop, Claude Code, Cursor, etc.), JSON config:

```json
{
  "mcpServers": {
    "systembolaget": {
      "transport": {
        "type": "http",
        "url": "http://truenas.local:8000",
        "headers": {
          "Authorization": "Bearer <SB_MCP_TOKEN>"
        }
      }
    }
  }
}
```

FastMCP v3 ships a `fastmcp install` CLI that can generate this for common clients.
