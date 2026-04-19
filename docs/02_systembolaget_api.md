# 02 — Systembolaget API (reverse-engineered)

Reverse-engineered as of **2026-04-19**. Systembolaget has no officially public product API; the frontend talks to an Azure API Management gateway and ships the subscription key in a public JS chunk.

## Two API namespaces

Both authenticate with the same public-frontend subscription key extracted from the website JS.

- **`sb-api-ecommerce/v1`** — the web frontend's primary backend
- **`sb-api-mobile/v1`** — the mobile app's backend; a superset in some places (GTIN lookup, faceted filter, per-product shelf+stock inline)

Both are at `https://api-extern.systembolaget.se/`.

## Authentication

Required headers for every request:

```
Ocp-Apim-Subscription-Key: <32-hex key, extracted at runtime>
Origin:                    https://www.systembolaget.se
Accept:                    application/json
```

Key values observed in the wild (both valid as of 2026-04-19):
- `8d39a7340ee7439f8b4c1e995c8f3e4a` (current, from frontend build today)
- `cfc702aed3094c86b92d6d4ff7a54c84` (previous, still accepted)

Multiple keys accepted in parallel suggests Systembolaget operates a grace window on rotations — good for our sync's resilience.

## Key extraction

The frontend is Next.js. `NEXT_PUBLIC_*` env vars get inlined into a single config chunk at build time. Algorithm:

1. Fetch `https://www.systembolaget.se/`.
2. Parse all `/_next/static/chunks/*.js` URLs from HTML + `_buildManifest.js`.
3. Fetch chunks in parallel (concurrency ~10).
4. Find the chunk matching `NEXT_PUBLIC_API_KEY_APIM:"<32 hex>"`.
5. Extract all `NEXT_PUBLIC_*` variables from that chunk (same chunk contains all of them).
6. Validate the key against `/sb-api-ecommerce/v1/productsearch/search?size=1`.
7. Cache for 7 days (configurable).

The extractor also discovers base URLs dynamically (API gateway, image CDN, CMS), so if Systembolaget ever moves an endpoint, sync adapts.

### Full `NEXT_PUBLIC_*` block (today)

```
NEXT_PUBLIC_API_KEY_APIM:           "8d39a7340ee7439f8b4c1e995c8f3e4a"
NEXT_PUBLIC_API_MANAGEMENT_URL:     "https://api-extern.systembolaget.se"
NEXT_PUBLIC_APP_IMAGE_STORAGE_URL:  "https://product-cdn.systembolaget.se/productimages"
NEXT_PUBLIC_CMS_URL:                "https://cms.systembolaget.se"
NEXT_PUBLIC_APP_BASE_URL:           "https://www.systembolaget.se"
NEXT_PUBLIC_APP_ENV:                "production"
NEXT_PUBLIC_APPINSIGHTS_CLOUD_ROLE_NAME: "sb-aks-ecommerce-web"
NEXT_PUBLIC_API_KEY_EPISERVER:      "0cc21a8690754a8089f9213689732f26"
NEXT_PUBLIC_ADYEN_ORIGINKEY:        "pub.v2...."
NEXT_PUBLIC_GOOGLE_API_KEY:         "AIzaSyA-..."
NEXT_PUBLIC_PIWIK_ID:               "eed75c96-..."
NEXT_PUBLIC_GENESYS_ID:             "a1092750-..."
```

Our sync stores only the fields it needs (APIM key, API URL, image CDN URL); the rest are discarded.

### Extraction resilience

| Situation | Behavior |
|---|---|
| Fresh cache (< 7 days), key still valid | return cache, zero network for key |
| Cache stale | re-scrape + validate, update cache |
| HTTP 401 from any API call mid-sync | force-refresh once, retry the failing call |
| Extraction succeeds but validation fails | hard error; don't cache a bad key |
| Extraction fails (site down, schema changed) | fall back to cached key if still valid, else hard error + alert |
| `SB_API_KEY` env var set | skip extraction entirely (manual override for debugging) |

## Verified endpoints

### Web (`sb-api-ecommerce/v1`)

| Endpoint | Purpose |
|---|---|
| `GET /productsearch/search?page=N&size=30&...` | Paged product catalog; 27k total; cap at page 333; many filter params |
| `GET /product/productNumber/{productNumber}` | Full product detail (~137 fields) |
| `GET /site/stores` | All 455 active stores |
| `GET /site/store/{siteId}` | One store |
| `GET /site/stores/{productId}` | All stores carrying a product + per-store stock (takes productId) |
| `GET /site/agents` | All 454 ombud (pickup points; not used in this project) |
| `GET /sitesearch/site?q=&includePredictions=true` | Store/city text search |
| `GET /stockbalance/store/{siteId}/{productId}` | Single stock row (includes shelf) |
| `GET /stockbalance/store?ProductId=X&StoreId=Y` | Alt query form |

### Mobile (`sb-api-mobile/v1`)

All of the above plus:

| Endpoint | Purpose |
|---|---|
| `GET /product/gtin/{gtin}` | GTIN → product (one-way; sparse index; most EANs 404) |
| `GET /product/productId/{productId}` | Product detail by internal id (same shape as productNumber) |
| `GET /productsearch/filter` | **All 22 filter groups with counts** (faceted taxonomy) |
| `GET /v2/productsearch/search` | v2 search (same schema as v1) |
| `GET /health` | Redis health probe (leaks infra) |
| `GET /settings` | App constants: CO2 thresholds, image CDN URL, "Multiplier=4" |

### Key mobile-only behavior

**`productsearch/search?storeId=X&isInStoreAssortmentSearch=true` returns `shelf` and `stock` on every product** on the mobile API (not the web API). This is our primary stock-sync endpoint — one pagination per store gives the entire assortment + live stock in one pass.

## What does NOT work

Probed but all returned 404/401/forbidden:
- `/product/{id}` (must use `/product/productNumber/{n}` or `/product/productId/{n}`)
- Cart, order, reservation, favorites, lists, taste profile endpoints
- Reverse GTIN lookup (productNumber → GTIN doesn't exist)
- Recipe / article / news API (CMS is private)
- Swagger / OpenAPI specs
- POST on productsearch/search (CORS advertises it but the route 404s)

These either require BankID-authenticated sessions or live under a different subscription key that we can't see. Out of scope regardless.

## Rate limits & CORS

- CORS `Access-Control-Allow-Origin: https://www.systembolaget.se` (strict, reflects origin).
- No observable per-IP rate limit at 10 parallel requests; latencies 0.3–1 s.
- No `ETag` / `Last-Modified` / `Cache-Control` headers — conditional GETs not possible.
- Our sync uses **5 concurrent requests max** with exponential backoff on 5xx/429.

## Response idiosyncrasies

- `productsearch/search` metadata reports `totalPages: 9999` as a fixed ceiling, not a real count. Pagination cap is **333 pages** (30 × 333 = 9990 rows), after which `nextPage` flips to -1 but the server still returns rows. Paginate by `categoryLevel1` to stay under the cap.
- `size` parameter accepts up to 50; values >50 silently clamp to 30.
- 404 responses on `sb-api-mobile/v1` sometimes leak .NET stack traces with build-agent paths (`C:\a\22\s\Sb.Mobile.API.Application\...`) — confirms CQRS/MediatR, useful for route discovery but don't build logic depending on it.
- Both web and mobile product endpoints lack `lastModified`. Only `/site/stores` and `/site/agents` include a batch `lastModified` timestamp (same value for every row — it's the timestamp of the overnight refresh, not a per-row change marker). So we can't do incremental sync via server-side modified-since; we hash-diff locally.

## Stores → home subset

`/site/stores` returns all 455 stores. We mark only the user's 4 home stores:

| siteId | alias | city | role |
|---|---|---|---|
| **1701** | **Duvan** | Karlstad | main (walkable) |
| 1702 | Bergvik-Karlstad | Karlstad | |
| 1718 | Välsviken | Karlstad | |
| 1716 | *(no alias)* | Skoghall / Hammarö | |

Stock is fetched only for these four stores.

## Image CDN

Pattern: `https://product-cdn.systembolaget.se/productimages/{productId}/{productId}_{size}.{ext}`

- Sizes: `20, 60, 100, 200, 300, 400, 500, 600, 800`
- Extensions: `avif, webp, jpg, png`
- `productId` (not `productNumber`) is the key
- URLs are deterministic — we derive them with a generated column; no separate fetch needed

Every product detail response also includes an `imageModules.thumbnail` base64 WebP LQIP (~100 B) for placeholder rendering.

## Full-assortment sitemap

As an alternative to API pagination, the public sitemap exposes every product URL:

```
https://www.systembolaget.se/sitemap.xml
  → sitemap-produkter-ol.xml
  → sitemap-produkter-vin.xml
  → sitemap-produkter-sprit.xml
  → sitemap-produkter-cider-blanddrycker.xml
  → sitemap-produkter-alkoholfritt.xml
  → sitemap-produkter-presentartiklar.xml
```

Not our primary path (API is faster and richer), but useful for cross-validation: if the sitemap lists a product our DB doesn't, we missed it.

## ToS note

Systembolaget has historically asked community projects that **re-host** scraped data (dcronqvist/systembolaget-api, bolaget.io) to take it down, but personal/local scraping has not been contested. Our project is personal home-server use, no re-hosting. Keep it that way.

If the project ever pivots to public, contact Systembolaget first — they may be open to formal licensing, or may not.

## Related frontend plumbing (nice-to-know)

- Next.js buildId exposed at `"buildId":"<40-hex>"` in every page's `__NEXT_DATA__`.
- `GET /_next/data/{buildId}/<path>.json` returns the SSG payload for any static page (sortiment, butiker, product detail) — includes a `fallback` map with SWR cache keys revealing the frontend's data-fetch structure.
- `_buildManifest.js` lists every route → chunks mapping.
- `_ssgManifest.js` enumerates all SSG route patterns.
- Backend infra: `sb-aks-ecommerce-web` = Azure Kubernetes Service app name.
- Tracking: Piwik Pro (not Google Analytics, by design — no ads policy).
- Payment: Adyen (client key exposed in build for their checkout flow).
- Mobile API reveals Redis in front of the backend (`/health` shows Redis connection status).
