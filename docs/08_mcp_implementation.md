# 08 — MCP tool implementation

The underside of [04_mcp_surface.md](./04_mcp_surface.md): SQL queries, response schemas, sugar-param resolution, error mapping, freshness handling. Depends on schema in [03_data_schema.md](./03_data_schema.md).

## Shared patterns

### Sugar param resolution

Every tool that accepts a store reference takes a string that resolves consistently:

```python
# src/sb_stack/mcp_server/store_ref.py

def resolve_site_ids(value: str | None, settings: Settings) -> list[str]:
    """
    Resolve a 'site reference' to one or more concrete site_ids.

    Accepted:
      None or ""   → []  (no filter)
      "main"       → [settings.main_store]
      "home"       → settings.store_subset
      "<siteId>"   → [siteId]  (any 4-digit string; validated against DB)
      "<alias>"    → [matching siteId]  (e.g. "duvan" → "1701"; case-insensitive)
    """
    if not value:
        return []
    if value == "main":
        return [settings.main_store]
    if value == "home":
        return list(settings.store_subset)
    # literal siteId?
    if re.fullmatch(r"\d{4}", value):
        return [value]
    # alias lookup
    with db.reader() as conn:
        row = conn.execute(
            "SELECT site_id FROM stores WHERE LOWER(alias) = LOWER(?)",
            [value]
        ).fetchone()
    if row:
        return [row[0]]
    raise InvalidInputError(
        f"Unknown store reference '{value}'. "
        f"Expected 'main', 'home', a 4-digit siteId, or a store alias."
    )

def resolve_single_site_id(value: str | None, settings: Settings) -> str:
    """Same as resolve_site_ids but enforces exactly one; 'home' is an error here."""
    ids = resolve_site_ids(value or "main", settings)
    if len(ids) != 1:
        raise InvalidInputError(
            f"Tool requires a single store; got {len(ids)}. "
            f"Use 'main', a siteId, or an alias."
        )
    return ids[0]
```

Accepted stores and aliases locked in the DB, not in Python strings — so if Systembolaget renames "Duvan" we pick it up on next sync.

### Freshness meta

Every tool response carries a meta block. Populated only when data is actually stale (reduces noise in normal operation):

```python
class ResponseMeta(BaseModel):
    stale_warning: str | None = None
    hours_since_sync: float | None = None
    last_sync_at: datetime | None = None

def freshness_meta(conn) -> ResponseMeta | None:
    row = conn.execute("""
        SELECT started_at FROM sync_runs
        WHERE status = 'success'
        ORDER BY run_id DESC LIMIT 1
    """).fetchone()
    if not row:
        return ResponseMeta(
            stale_warning="Ingen lyckad synk ännu. Data kan saknas.",
            hours_since_sync=None
        )
    hours = (datetime.utcnow() - row[0]).total_seconds() / 3600
    if hours > 30:
        return ResponseMeta(
            stale_warning=f"Senaste synk var för {hours:.0f} timmar sedan. Data kan vara inaktuell.",
            hours_since_sync=round(hours, 1),
            last_sync_at=row[0],
        )
    if hours > 25:
        return ResponseMeta(
            stale_warning=f"Data är {hours:.1f} timmar gammal.",
            hours_since_sync=round(hours, 1),
            last_sync_at=row[0],
        )
    return None  # normal operation, no meta emitted
```

Tool responses include `meta: ResponseMeta | None`. LLM-side: if present, it's worth mentioning to the user; if null, data is fresh.

### Home stock fetcher

Shared by every tool that returns products:

```python
def fetch_home_stock(
    conn, product_numbers: list[str], settings: Settings
) -> dict[str, dict[str, HomeStockEntry]]:
    """
    Returns {product_number: {site_id: HomeStockEntry}}.
    Omits products with no home stock rows.
    """
    if not product_numbers:
        return {}
    rows = conn.execute("""
        SELECT s.product_number, s.site_id, st.alias,
               s.stock, s.shelf, s.is_in_assortment, s.observed_at
        FROM stock s
        JOIN stores st USING (site_id)
        WHERE s.product_number = ANY(?)
          AND st.is_home_store = true
    """, [product_numbers]).fetchall()
    result: dict[str, dict[str, HomeStockEntry]] = defaultdict(dict)
    for pn, sid, alias, stock, shelf, in_assort, obs in rows:
        result[pn][sid] = HomeStockEntry(
            site_id=sid, alias=alias, stock=stock, shelf=shelf,
            is_in_assortment=in_assort, observed_at=obs,
        )
    return dict(result)
```

### Error mapping

Each tool wraps in an adapter that translates SBError subclasses to FastMCP `ToolError`:

```python
# src/sb_stack/mcp_server/error_map.py

def map_error(e: Exception) -> ToolError:
    if isinstance(e, ProductNotFoundError):
        return ToolError(
            f"Produkten {e.product_number} hittades inte.",
            code="NOT_FOUND",
        )
    if isinstance(e, InvalidInputError):
        return ToolError(str(e), code="INVALID_PARAMS")
    if isinstance(e, DataStalenessError):
        return ToolError(str(e), code="PRECONDITION_FAILED")
    if isinstance(e, SBError):
        log.exception("tool_internal_error", error_class=type(e).__name__)
        return ToolError(
            "Ett internt fel uppstod. Försök igen senare.",
            code="INTERNAL_ERROR",
        )
    # Non-SB exceptions — log full trace, return safe message
    log.exception("tool_unhandled_exception")
    return ToolError(
        "Ett okänt fel uppstod. Kontrollera loggen.",
        code="INTERNAL_ERROR",
    )
```

Every tool is wrapped:

```python
@mcp.tool()
async def search_products(...) -> SearchResponse:
    try:
        with db.reader() as conn:
            ...
    except Exception as e:
        raise map_error(e) from e
```

Tool error messages are in **Swedish** so the LLM surfaces Swedish errors to the Swedish-speaking user.

### Shared response models

```python
# src/sb_stack/mcp_server/responses.py

class HomeStockEntry(BaseModel):
    site_id: str
    alias: str
    stock: int
    shelf: str | None
    is_in_assortment: bool
    observed_at: datetime

class TasteClocks(BaseModel):
    body: int | None = None
    bitter: int | None = None
    sweetness: int | None = None
    fruitacid: int | None = None
    roughness: int | None = None
    smokiness: int | None = None
    casque: int | None = None

class ProductSummary(BaseModel):
    """Compact product representation used by search/similarity/pairing."""
    product_number: str
    name_bold: str
    name_thin: str | None = None
    producer_name: str | None = None
    category_level_1: str | None = None
    category_level_2: str | None = None
    category_level_3: str | None = None
    country: str | None = None
    origin_level_1: str | None = None
    volume_ml: int | None = None
    alcohol_percentage: float | None = None
    price_incl_vat: float | None = None
    comparison_price: float | None = None
    taste_clocks: TasteClocks | None = None
    taste_symbols: list[str] | None = None
    grapes: list[str] | None = None
    vintage: str | None = None
    assortment_text: str | None = None
    is_organic: bool | None = None
    is_vegan_friendly: bool | None = None
    is_gluten_free: bool | None = None
    is_completely_out_of_stock: bool = False
    is_discontinued: bool = False
    image_url: str | None = None
    home_stock: dict[str, HomeStockEntry] | None = None

class ProductDetail(ProductSummary):
    """Extended product representation used by get_product."""
    supplier_name: str | None = None
    origin_level_2: str | None = None
    origin_level_3: str | None = None
    sugar_content_g_per_100ml: float | None = None
    usage: str | None = None
    taste: str | None = None
    aroma: str | None = None
    producer_description: str | None = None
    production: str | None = None
    cultivation_area: str | None = None
    harvest: str | None = None
    soil: str | None = None
    storage: str | None = None
    raw_material: str | None = None
    ingredients: str | None = None
    allergens: str | None = None
    additives: str | None = None
    additional_information: str | None = None
    did_you_know: str | None = None
    standard_drinks: float | None = None
    seal: str | None = None
    packaging: str | None = None
    packaging_co2_level: str | None = None
    packaging_co2_g_per_l: int | None = None
    ethical_label: str | None = None
    is_natural_wine: bool | None = None
    is_kosher: bool | None = None
    is_ethical: bool | None = None
    is_sustainable_choice: bool | None = None
    is_temporary_out_of_stock: bool = False
    available_number_of_stores: int | None = None
    product_launch_date: date | None = None
    images: list[dict] = []  # {size, url}
```

---

## Per-tool implementation

### `search_products`

**Query builder** (generated in Python, bindings for values):

```python
def build_search_query(params: SearchParams, settings: Settings) -> tuple[str, list, str, list]:
    """
    Returns (sql, binds, count_sql, count_binds).
    """
    conditions = []
    binds = []

    # FTS or no
    score_expr = ""
    order_expr = "p.product_launch_date DESC NULLS LAST"  # default when no text

    if params.text:
        score_expr = ", fts_main_products.match_bm25(p.product_number, ?) AS _score"
        binds.append(params.text)
        conditions.append("_score IS NOT NULL")
        order_expr = "_score DESC"

    def add(cond: str, *vals):
        conditions.append(cond)
        binds.extend(vals)

    if params.category:              add("p.category_level_1 = ?", params.category)
    if params.subcategory:
        # Match either level 2 or level 3
        add("(p.category_level_2 = ? OR p.category_level_3 = ?)",
            params.subcategory, params.subcategory)
    if params.country:               add("p.country = ?", params.country)
    if params.region:
        add("(p.origin_level_1 = ? OR p.origin_level_2 = ?)",
            params.region, params.region)
    if params.vintage:               add("p.vintage = ?", params.vintage)
    if params.price_min is not None: add("p.price_incl_vat >= ?", params.price_min)
    if params.price_max is not None: add("p.price_incl_vat <= ?", params.price_max)
    if params.abv_min is not None:   add("p.alcohol_percentage >= ?", params.abv_min)
    if params.abv_max is not None:   add("p.alcohol_percentage <= ?", params.abv_max)
    if params.volume_min_ml is not None: add("p.volume_ml >= ?", params.volume_min_ml)
    if params.volume_max_ml is not None: add("p.volume_ml <= ?", params.volume_max_ml)
    if params.sugar_min is not None: add("p.sugar_content >= ?", params.sugar_min)
    if params.sugar_max is not None: add("p.sugar_content <= ?", params.sugar_max)

    # Taste clocks (generate pairs dynamically)
    for clock in ("body", "bitter", "sweetness", "fruitacid",
                  "rough", "smoke"):
        col = f"taste_clock_{clock}"
        mn = getattr(params, f"taste_{clock}_min", None)
        mx = getattr(params, f"taste_{clock}_max", None)
        if mn is not None: add(f"p.{col} >= ?", mn)
        if mx is not None: add(f"p.{col} <= ?", mx)

    # List-type overlaps
    if params.grapes_any:
        add("list_has_any(p.grapes, ?)", params.grapes_any)
    if params.pairs_with_any:
        add("list_has_any(p.taste_symbols, ?)", params.pairs_with_any)

    # Structured enums
    if params.seal:            add("p.seal = ?", params.seal)
    if params.packaging:       add("p.packaging_level_1 = ?", params.packaging)
    if params.co2_impact:      add("p.packaging_co2_level = ?",
                                   {"Lägre": "Low", "Medel": "Medium",
                                    "Högre": "High"}.get(params.co2_impact, params.co2_impact))

    # Booleans
    if params.is_organic:         add("p.is_organic = true")
    if params.is_vegan:           add("p.is_vegan_friendly = true")
    if params.is_gluten_free:     add("p.is_gluten_free = true")
    if params.is_kosher:          add("p.is_kosher = true")
    if params.is_natural_wine:    add("p.is_natural_wine = true")
    if params.is_ethical:         add("p.is_ethical = true")
    if params.ethical_label:      add("p.ethical_label = ?", params.ethical_label)
    if params.assortment_text:    add("p.assortment_text = ?", params.assortment_text)

    # Dates
    if params.launched_since:
        add("p.product_launch_date >= ?", params.launched_since)

    # Discontinued filter
    if not params.include_discontinued:
        add("p.is_discontinued = false")

    # Stock filter
    site_ids = resolve_site_ids(params.in_stock_at, settings)
    if site_ids:
        placeholders = ",".join(["?"] * len(site_ids))
        add(
            f"EXISTS (SELECT 1 FROM stock s "
            f"WHERE s.product_number = p.product_number "
            f"AND s.site_id IN ({placeholders}) AND s.stock > 0)",
            *site_ids,
        )

    # Order override
    if params.order_by != "relevance":
        order_expr = {
            "price_asc":             "p.price_incl_vat ASC NULLS LAST",
            "price_desc":            "p.price_incl_vat DESC NULLS LAST",
            "launch_desc":           "p.product_launch_date DESC NULLS LAST",
            "body_asc":              "p.taste_clock_body ASC NULLS LAST",
            "body_desc":             "p.taste_clock_body DESC NULLS LAST",
            "comparison_price_asc":  "p.comparison_price ASC NULLS LAST",
        }.get(params.order_by, order_expr)

    where = " AND ".join(conditions) if conditions else "TRUE"

    sql = f"""
        SELECT p.product_number, p.name_bold, p.name_thin, p.producer_name,
               p.category_level_1, p.category_level_2, p.category_level_3,
               p.country, p.origin_level_1, p.volume_ml, p.alcohol_percentage,
               p.price_incl_vat, p.comparison_price,
               p.taste_clock_body, p.taste_clock_bitter, p.taste_clock_sweetness,
               p.taste_clock_fruitacid, p.taste_clock_roughness,
               p.taste_clock_smokiness, p.taste_clock_casque,
               p.taste_symbols, p.grapes, p.vintage,
               p.assortment_text, p.is_organic, p.is_vegan_friendly,
               p.is_gluten_free, p.is_completely_out_of_stock,
               p.is_discontinued, p.image_url
               {score_expr}
        FROM products p
        WHERE {where}
        ORDER BY {order_expr}
        LIMIT ? OFFSET ?
    """
    binds.extend([params.limit, params.offset])

    count_sql = f"SELECT COUNT(*) FROM products p WHERE {where}"
    # count_binds excludes the LIMIT/OFFSET binds; keep only the WHERE ones
    count_binds = binds[:-2]

    return sql, binds, count_sql, count_binds
```

**Tool body:**

```python
@mcp.tool()
async def search_products(params: SearchParams) -> SearchResponse:
    try:
        with db.reader() as conn:
            sql, binds, count_sql, count_binds = build_search_query(params, settings)
            rows = conn.execute(sql, binds).fetchall()
            total = conn.execute(count_sql, count_binds).fetchone()[0]
            products = [row_to_product_summary(r) for r in rows]
            stock_map = fetch_home_stock(
                conn, [p.product_number for p in products], settings
            )
            for p in products:
                p.home_stock = stock_map.get(p.product_number)
            return SearchResponse(
                results=products,
                total_count=total,
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `semantic_search`

```python
@mcp.tool()
async def semantic_search(params: SemanticSearchParams) -> SearchResponse:
    try:
        # Call sb-embed via HTTP (OpenAI-compat). See 09_embedding_service.md.
        vectors = await embedding_client.embed([params.query])
        qvec = vectors[0]

        with db.reader() as conn:
            # Reuse search_products' filter builder for the WHERE clause
            filter_sql, filter_binds = build_where_fragment(params.filters or {}, settings)

            site_binds = resolve_site_ids(params.in_stock_at, settings)
            stock_filter = ""
            if site_binds:
                placeholders = ",".join(["?"] * len(site_binds))
                stock_filter = f"""
                  AND EXISTS (SELECT 1 FROM stock s
                              WHERE s.product_number = p.product_number
                                AND s.site_id IN ({placeholders}) AND s.stock > 0)
                """

            sql = f"""
                SELECT {PRODUCT_SUMMARY_COLUMNS},
                       array_cosine_similarity(pe.embedding, ?::FLOAT[2560]) AS similarity
                FROM products p
                JOIN product_embeddings pe USING (product_number)
                WHERE {filter_sql}
                  {stock_filter}
                  AND p.is_discontinued = false
                ORDER BY similarity DESC
                LIMIT ?
            """
            rows = conn.execute(
                sql, [*filter_binds, qvec.tolist(), *site_binds, params.limit]
            ).fetchall()

            results = [
                (row_to_product_summary(r[:-1]), r[-1])  # last col is similarity
                for r in rows
            ]
            stock_map = fetch_home_stock(
                conn, [p.product_number for p, _ in results], settings
            )
            for p, sim in results:
                p.home_stock = stock_map.get(p.product_number)

            return SearchResponse(
                results=[SemanticResult(**p.model_dump(), similarity=round(sim, 4))
                         for p, sim in results],
                total_count=len(results),
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

HNSW index is used automatically by DuckDB when the query has `ORDER BY array_cosine_similarity(...) DESC LIMIT N` and the indexed column + metric match. We pre-filter to reduce the vector-scan set, but it's fast enough even without pre-filtering (27k × 2560-dim brute force cosine ≈ 50 ms).

**Error paths:**
- If the embedding service is unreachable, `embedding_client.embed()` raises after retries → mapped to `ToolError("INTERNAL_ERROR", "Semantisk sökning är inte tillgänglig just nu...")`.
- If sb-embed is still loading (503), the retry will pass once it's ready.
- The tool description (in [04_mcp_surface.md](./04_mcp_surface.md)) tells the LLM to recommend waiting if this tool fails at startup.

---

### `find_similar_products`

```python
@mcp.tool()
async def find_similar_products(
    product_number: str,
    limit: int = 10,
    same_category_only: bool = True,
    max_price: float | None = None,
    in_stock_at: str | None = None,
) -> SimilarProductsResponse:
    try:
        with db.reader() as conn:
            source_row = conn.execute("""
                SELECT p.*, pe.embedding
                FROM products p
                JOIN product_embeddings pe USING (product_number)
                WHERE p.product_number = ?
            """, [product_number]).fetchone()
            if not source_row:
                raise ProductNotFoundError(product_number=product_number)

            source = row_to_product_detail(source_row)
            source_vec = source_row[-1]

            conditions = ["p.product_number != ?", "p.is_discontinued = false"]
            binds: list = [product_number]
            if same_category_only:
                conditions.append("p.category_level_1 = ?")
                binds.append(source.category_level_1)
            if max_price is not None:
                conditions.append("p.price_incl_vat <= ?")
                binds.append(max_price)
            site_ids = resolve_site_ids(in_stock_at, settings)
            if site_ids:
                placeholders = ",".join(["?"] * len(site_ids))
                conditions.append(
                    f"EXISTS (SELECT 1 FROM stock s "
                    f"WHERE s.product_number = p.product_number "
                    f"AND s.site_id IN ({placeholders}) AND s.stock > 0)"
                )
                binds.extend(site_ids)

            where = " AND ".join(conditions)
            sql = f"""
                SELECT {PRODUCT_SUMMARY_COLUMNS},
                       array_cosine_similarity(pe.embedding, ?::FLOAT[2560]) AS similarity
                FROM products p
                JOIN product_embeddings pe USING (product_number)
                WHERE {where}
                ORDER BY similarity DESC
                LIMIT ?
            """
            rows = conn.execute(sql, [*binds, source_vec, limit]).fetchall()
            similar = [
                (row_to_product_summary(r[:-1]), r[-1]) for r in rows
            ]
            stock_map = fetch_home_stock(
                conn,
                [p.product_number for p, _ in similar] + [product_number],
                settings,
            )
            source.home_stock = stock_map.get(product_number)
            for p, _ in similar:
                p.home_stock = stock_map.get(p.product_number)

            return SimilarProductsResponse(
                source=source,
                similar=[
                    SemanticResult(**p.model_dump(), similarity=round(sim, 4))
                    for p, sim in similar
                ],
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `pair_with_dish`

The tool delegates scoring to the pairing engine (see `DISH_PAIRING_DESIGN.md`); the tool is responsible for:

1. Loading candidate products with full context for scoring.
2. Calling `pairing.engine.pair()`.
3. Enriching with home stock and packaging into the response.

```python
@mcp.tool()
async def pair_with_dish(params: PairWithDishParams) -> PairingResponse:
    try:
        with db.reader() as conn:
            # 1. Fetch candidates.
            # The engine wants structured pre-filtering to keep the set manageable.
            candidates_sql = """
                SELECT p.product_number, p.name_bold, p.name_thin, p.producer_name,
                       p.category_level_1, p.category_level_2, p.category_level_3,
                       p.country, p.origin_level_1,
                       p.volume_ml, p.alcohol_percentage,
                       p.price_incl_vat, p.comparison_price,
                       p.taste_clock_body, p.taste_clock_bitter, p.taste_clock_sweetness,
                       p.taste_clock_fruitacid, p.taste_clock_roughness,
                       p.taste_clock_smokiness, p.taste_clock_casque,
                       p.taste_symbols, p.grapes, p.vintage, p.color,
                       p.usage, p.taste, p.aroma,
                       p.assortment_text, p.available_number_of_stores,
                       p.is_organic, p.is_vegan_friendly, p.is_gluten_free,
                       p.is_completely_out_of_stock, p.is_temporary_out_of_stock,
                       p.is_discontinued,
                       pe.embedding, p.image_url
                FROM products p
                JOIN product_embeddings pe USING (product_number)
                WHERE p.is_discontinued = false
                  AND {category_filter}
                  AND {price_filter}
                  AND {dietary_filter}
                LIMIT 5000
            """
            # (category_filter / price_filter / dietary_filter compiled based on params)

            rows = conn.execute(candidates_sql, binds).fetchall()

            # 2. Call engine (pure Python over these candidates).
            engine_result: PairingEngineResult = pairing_engine.pair(
                dish=params.dish,
                meal_context=params.meal_context,
                style_preference=params.style_preference,
                cultural_tag=params.cultural_tag,
                dominant_component_hint=params.dominant_component_hint,
                dietary=params.dietary,
                candidates=rows,
                diversity=params.diversity,
                limit=params.limit,
                include_alternative_category=params.include_alternative_category,
            )

            # 3. Enrich with home stock.
            product_numbers = [rec.product.product_number
                               for rec in engine_result.recommendations]
            if engine_result.alternative_category:
                product_numbers.append(
                    engine_result.alternative_category.recommendation.product_number)
            site_ids = resolve_site_ids(params.in_stock_at, settings)
            stock_map = fetch_home_stock(conn, product_numbers, settings)
            for rec in engine_result.recommendations:
                rec.product.home_stock = stock_map.get(rec.product.product_number)
            if engine_result.alternative_category:
                alt = engine_result.alternative_category.recommendation
                alt.home_stock = stock_map.get(alt.product_number)

            return PairingResponse(
                interpretation=engine_result.interpretation,
                recommendations=engine_result.recommendations,
                alternative_category=engine_result.alternative_category,
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

The engine itself lives under `src/sb_stack/pairing/` and doesn't know about DuckDB — it takes a list of candidate rows. Keeps it testable in isolation and reusable if extracted into the standalone app later.

**Candidate cap (5000) logging.** When pre-filtering returns ≥5000 candidates, we log a structured warning so we can later tell whether the cap is too tight in practice:

```python
MAX_CANDIDATES = 5000

# Separate cheap count first (columnar scan, sub-ms)
full_count = conn.execute(
    f"SELECT COUNT(*) FROM products p "
    f"JOIN product_embeddings pe USING (product_number) "
    f"WHERE {where_sql}",
    where_binds,
).fetchone()[0]

if full_count > MAX_CANDIDATES:
    log.warning(
        "pairing_candidate_cap_hit",
        dish=params.dish,
        meal_context=params.meal_context,
        categories=params.categories,
        actual_count=full_count,
        cap=MAX_CANDIDATES,
        truncated_by=full_count - MAX_CANDIDATES,
    )

# Then fetch up to the cap
rows = conn.execute(
    candidates_sql + f" LIMIT {MAX_CANDIDATES}",
    binds,
).fetchall()
```

If this warning appears on > ~5% of production calls, we raise the cap.

**Embedding call from the engine.** Semantic scoring inside the engine requires embedding the dish text once. The engine is given an `embedding_client` dependency (injected) and calls it during scoring. One embedding per `pair_with_dish` invocation, not per candidate.

---

### `get_product`

```python
@mcp.tool()
async def get_product(
    product_number: str | None = None,
    query: str | None = None,
) -> ProductDetailResponse:
    try:
        if not product_number and not query:
            raise InvalidInputError("Ange antingen product_number eller query.")

        with db.reader() as conn:
            if product_number:
                row = conn.execute(
                    "SELECT * FROM products WHERE product_number = ?",
                    [product_number]
                ).fetchone()
                if not row:
                    raise ProductNotFoundError(product_number=product_number)
            else:
                row = conn.execute("""
                    SELECT p.*, fts_main_products.match_bm25(p.product_number, ?) AS _score
                    FROM products p
                    WHERE _score IS NOT NULL
                      AND p.is_discontinued = false
                    ORDER BY _score DESC
                    LIMIT 1
                """, [query]).fetchone()
                if not row:
                    raise ProductNotFoundError(
                        product_number=f"(query='{query}')"
                    )

            detail = row_to_product_detail(row)

            # Variants
            variants = conn.execute("""
                SELECT pv.product_number, pv.name_bold, pv.volume_ml,
                       pv.bottle_text, pv.price_incl_vat, pv.image_url
                FROM product_variants v
                JOIN products pv ON v.variant_product_number = pv.product_number
                WHERE v.product_number = ?
            """, [detail.product_number]).fetchall()

            # Home stock
            stock_rows = conn.execute("""
                SELECT s.site_id, st.alias, s.stock, s.shelf,
                       s.is_in_assortment, s.observed_at
                FROM stock s
                JOIN stores st USING (site_id)
                WHERE s.product_number = ? AND st.is_home_store = true
                ORDER BY st.is_main_store DESC, st.alias
            """, [detail.product_number]).fetchall()

            detail.home_stock = {
                r[0]: HomeStockEntry(site_id=r[0], alias=r[1], stock=r[2],
                                     shelf=r[3], is_in_assortment=r[4],
                                     observed_at=r[5])
                for r in stock_rows
            }

            # Image URLs at multiple sizes
            detail.images = [
                {"size": s, "url": (
                    f"https://product-cdn.systembolaget.se/productimages/"
                    f"{detail.product_id}/{detail.product_id}_{s}.webp"
                )}
                for s in (100, 200, 400, 800)
            ]

            return ProductDetailResponse(
                product=detail,
                variants=[row_to_product_summary(v) for v in variants],
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `compare_products`

```python
@mcp.tool()
async def compare_products(product_numbers: list[str]) -> CompareResponse:
    if not 2 <= len(product_numbers) <= 5:
        raise InvalidInputError("Ange mellan 2 och 5 produkter att jämföra.")
    try:
        with db.reader() as conn:
            placeholders = ",".join(["?"] * len(product_numbers))
            rows = conn.execute(
                f"SELECT * FROM products WHERE product_number IN ({placeholders})",
                product_numbers,
            ).fetchall()

            # Preserve input order
            by_num = {r["product_number"]: r for r in rows}
            missing = [n for n in product_numbers if n not in by_num]
            if missing:
                raise ProductNotFoundError(
                    product_number=",".join(missing),
                    message=f"{len(missing)} produkter hittades inte."
                )
            ordered = [by_num[n] for n in product_numbers]
            products = [row_to_product_detail(r) for r in ordered]

            # Compute comparison rows
            fields = [
                ("price_incl_vat",   "Pris"),
                ("comparison_price", "Pris/liter"),
                ("alcohol_percentage", "Alkoholhalt"),
                ("volume_ml",        "Volym (ml)"),
                ("country",          "Ursprungsland"),
                ("origin_level_1",   "Region"),
                ("vintage",          "Årgång"),
                ("taste_clock_body",       "Fyllighet"),
                ("taste_clock_sweetness",  "Sötma"),
                ("taste_clock_bitter",     "Beska"),
                ("taste_clock_fruitacid",  "Fruktsyra"),
                ("taste_clock_roughness",  "Strävhet"),
                ("taste_clock_smokiness",  "Rökighet"),
                ("is_organic",       "Ekologisk"),
                ("assortment_text",  "Sortiment"),
                ("is_completely_out_of_stock", "Slut online"),
            ]
            rows_out = [
                {"field": label, "values": [getattr(p, field) for p in products]}
                for field, label in fields
            ]

            # Stock summary per product
            stock_map = fetch_home_stock(conn, product_numbers, settings)
            for p in products:
                p.home_stock = stock_map.get(p.product_number)

            return CompareResponse(
                products=products,
                rows=rows_out,
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `list_home_stores`

```python
def haversine_km(lat1, lon1, lat2, lon2) -> float:
    R = 6371.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dp/2)**2 + cos(p1) * cos(p2) * sin(dl/2)**2
    return R * 2 * asin(sqrt(a))

@mcp.tool()
async def list_home_stores() -> list[HomeStoreInfo]:
    try:
        with db.reader() as conn:
            rows = conn.execute("""
                SELECT s.site_id, s.alias, s.address, s.postal_code, s.city,
                       s.county, s.is_main_store, s.latitude, s.longitude,
                       s.is_tasting_store, s.is_full_assortment_order_store,
                       oh.open_from, oh.open_to, oh.reason
                FROM stores s
                LEFT JOIN store_opening_hours oh
                  ON oh.site_id = s.site_id AND oh.date = CURRENT_DATE
                WHERE s.is_home_store = true
                ORDER BY s.is_main_store DESC, s.alias
            """).fetchall()

            if not rows:
                raise DataStalenessError("Inga hemmabutiker hittades i databasen.")

            # Find main store for distance calc
            main = next((r for r in rows if r[6]), rows[0])  # is_main_store
            main_lat, main_lon = main[7], main[8]

            return [
                HomeStoreInfo(
                    site_id=r[0], alias=r[1] or r[0], address=r[2],
                    postal_code=r[3], city=r[4], county=r[5],
                    is_main_store=r[6], latitude=r[7], longitude=r[8],
                    is_tasting_store=r[9],
                    is_full_assortment_order_store=r[10],
                    today_open_from=r[11],
                    today_open_to=r[12],
                    today_closed_reason=r[13],
                    distance_from_main_km=round(
                        haversine_km(main_lat, main_lon, r[7], r[8]), 1
                    ) if r[7] and r[8] else None,
                )
                for r in rows
            ]
    except Exception as e:
        raise map_error(e) from e
```

---

### `get_store_schedule`

```python
@mcp.tool()
async def get_store_schedule(
    site_id: str = "main",
    days_ahead: int = 14,
) -> StoreScheduleResponse:
    try:
        site_id = resolve_single_site_id(site_id, settings)
        with db.reader() as conn:
            store_row = conn.execute(
                "SELECT * FROM stores WHERE site_id = ?", [site_id]
            ).fetchone()
            if not store_row:
                raise InvalidInputError(
                    f"Butiken {site_id} finns inte i databasen."
                )

            schedule = conn.execute("""
                SELECT date, open_from, open_to, reason
                FROM store_opening_hours
                WHERE site_id = ?
                  AND date >= CURRENT_DATE
                  AND date < CURRENT_DATE + CAST(? AS INTEGER) * INTERVAL '1 day'
                ORDER BY date
            """, [site_id, days_ahead]).fetchall()

            return StoreScheduleResponse(
                store=row_to_store(store_row),
                schedule=[
                    ScheduleEntry(
                        date=r[0],
                        open_from=r[1],
                        open_to=r[2],
                        reason=r[3],
                        is_open=not (r[3] == "-" or (r[1] == r[2])),
                    )
                    for r in schedule
                ],
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `list_taxonomy_values`

```python
VALID_FILTER_NAMES = {
    "Country", "Seal", "TasteSymbols", "AssortmentText",
    "Grapes", "PackagingLevel1", "EthicalLabel", "CategoryLevel1",
    "PackagingCO2ImpactLevel", "NewArrivalType", "UpcomingLaunches",
}

@mcp.tool()
async def list_taxonomy_values(
    filter_name: str,
    min_count: int = 1,
) -> TaxonomyResponse:
    try:
        if filter_name not in VALID_FILTER_NAMES:
            raise InvalidInputError(
                f"Okänt filter '{filter_name}'. Giltiga: "
                f"{', '.join(sorted(VALID_FILTER_NAMES))}"
            )
        with db.reader() as conn:
            rows = conn.execute("""
                WITH latest AS (
                    SELECT MAX(captured_at) AS captured_at
                    FROM filter_taxonomy
                    WHERE filter_name = ?
                )
                SELECT ft.value, ft.count, ft.captured_at
                FROM filter_taxonomy ft, latest
                WHERE ft.filter_name = ?
                  AND ft.captured_at = latest.captured_at
                  AND ft.count >= ?
                ORDER BY ft.count DESC
            """, [filter_name, filter_name, min_count]).fetchall()

            if not rows:
                raise DataStalenessError(
                    f"Inga värden för filter '{filter_name}'. "
                    f"Har synken körts än?"
                )

            return TaxonomyResponse(
                filter_name=filter_name,
                values=[{"value": r[0], "count": r[1]} for r in rows],
                captured_at=rows[0][2],
                meta=freshness_meta(conn),
            )
    except Exception as e:
        raise map_error(e) from e
```

---

### `sync_status`

```python
@mcp.tool()
async def sync_status() -> SyncStatusResponse:
    try:
        with db.reader() as conn:
            last_any = conn.execute("""
                SELECT run_id, started_at, finished_at, status,
                       products_added, products_updated, products_discontinued,
                       stock_rows_updated, embeddings_generated, error
                FROM sync_runs
                ORDER BY run_id DESC LIMIT 1
            """).fetchone()

            last_success = conn.execute("""
                SELECT started_at FROM sync_runs
                WHERE status = 'success'
                ORDER BY run_id DESC LIMIT 1
            """).fetchone()

            product_count = conn.execute(
                "SELECT COUNT(*) FROM products WHERE is_discontinued = false"
            ).fetchone()[0]

            home_stock_count = conn.execute(
                "SELECT COUNT(*) FROM stock"
            ).fetchone()[0]

            key_ts_row = conn.execute(
                "SELECT value FROM sync_config WHERE key = 'api_key_last_validated'"
            ).fetchone()
            key_last_validated = (
                datetime.fromisoformat(key_ts_row[0]) if key_ts_row else None
            )

            hours_since = (
                (datetime.utcnow() - last_success[0]).total_seconds() / 3600
                if last_success else None
            )

            return SyncStatusResponse(
                last_run=SyncRunInfo(**last_any) if last_any else None,
                hours_since_last_success=(
                    round(hours_since, 1) if hours_since is not None else None
                ),
                product_count=product_count,
                home_stock_rows=home_stock_count,
                api_key_last_validated=key_last_validated,
                stale=(hours_since is None) or (hours_since > 30),
                meta=None,  # sync_status itself never emits staleness meta
            )
    except Exception as e:
        raise map_error(e) from e
```

`sync_status` is the one tool that doesn't carry `meta` — it *is* the freshness report.

---

## Shared SQL helpers

The search, semantic, and pairing tools all need overlapping WHERE fragments. Extract a `build_where_fragment(params, settings)` that emits the SQL + binds for the filter subset, used by all three.

```python
# src/sb_stack/mcp_server/sql.py

PRODUCT_SUMMARY_COLUMNS = """
    p.product_number, p.name_bold, p.name_thin, p.producer_name,
    p.category_level_1, p.category_level_2, p.category_level_3,
    p.country, p.origin_level_1, p.volume_ml, p.alcohol_percentage,
    p.price_incl_vat, p.comparison_price,
    p.taste_clock_body, p.taste_clock_bitter, p.taste_clock_sweetness,
    p.taste_clock_fruitacid, p.taste_clock_roughness,
    p.taste_clock_smokiness, p.taste_clock_casque,
    p.taste_symbols, p.grapes, p.vintage,
    p.assortment_text, p.is_organic, p.is_vegan_friendly,
    p.is_gluten_free, p.is_completely_out_of_stock,
    p.is_discontinued, p.image_url
"""

def build_where_fragment(
    filters: dict, settings: Settings,
    include_discontinued: bool = False,
) -> tuple[str, list]:
    """Pure WHERE-clause builder, no ORDER/LIMIT, no FTS."""
    ...
```

This keeps each tool's module focused on its unique logic (FTS scoring in search, vector in semantic, engine call in pair_with_dish).

---

## Row → Pydantic helpers

DuckDB returns tuples; we map to Pydantic via small adapters:

```python
# src/sb_stack/mcp_server/adapters.py

def row_to_product_summary(row: tuple) -> ProductSummary:
    return ProductSummary(
        product_number=row[0],
        name_bold=row[1],
        name_thin=row[2],
        producer_name=row[3],
        category_level_1=row[4],
        category_level_2=row[5],
        category_level_3=row[6],
        country=row[7],
        origin_level_1=row[8],
        volume_ml=row[9],
        alcohol_percentage=row[10],
        price_incl_vat=row[11],
        comparison_price=row[12],
        taste_clocks=TasteClocks(
            body=row[13], bitter=row[14], sweetness=row[15],
            fruitacid=row[16], roughness=row[17],
            smokiness=row[18], casque=row[19],
        ),
        taste_symbols=row[20],
        grapes=row[21],
        vintage=row[22],
        assortment_text=row[23],
        is_organic=row[24],
        is_vegan_friendly=row[25],
        is_gluten_free=row[26],
        is_completely_out_of_stock=row[27],
        is_discontinued=row[28],
        image_url=row[29],
    )
```

Column ordering in `PRODUCT_SUMMARY_COLUMNS` is the single source of truth for index positions. Keep them in sync or (preferred) use DuckDB's row factory to return dicts, accept a minor perf cost for readability.

**Preferred: use DuckDB's `fetchdf()` or row dict mode.** Eliminates index-mismatch bugs entirely:

```python
rows = conn.execute(sql, binds).fetch_arrow_table().to_pylist()
# or
rows = [dict(zip([d[0] for d in conn.description], row))
        for row in conn.execute(sql, binds).fetchall()]
```

At the scale we operate (at most a few thousand rows per response), the dict conversion cost is negligible.

---

## FTS specifics (DuckDB)

DuckDB's FTS creates a hidden schema `fts_main_<table>`. After:

```sql
PRAGMA create_fts_index(
  'products', 'product_number',
  'name_bold', 'name_thin', 'producer_name', 'country',
  'taste', 'aroma', 'usage', 'producer_description',
  stemmer='swedish', stopwords='swedish', lower=1, strip_accents=0
);
```

Query with:

```sql
SELECT p.*, fts_main_products.match_bm25(p.product_number, ?) AS score
FROM products p
WHERE fts_main_products.match_bm25(p.product_number, ?) IS NOT NULL
ORDER BY score DESC
```

Note: query string is bound twice (DuckDB doesn't let us reference the aliased `score` in WHERE). Two identical binds, same text.

No operator syntax is exposed — just free-text BM25 over concatenated fields. Stemming and stopwords are Swedish. `strip_accents=0` preserves å/ä/ö/Å/Ä/Ö as distinct tokens.

If FTS index is missing (shouldn't happen in steady state, but during Phase E rebuild for a few seconds it might be), `match_bm25` raises an error. **Strategy: client retries.** We wrap FTS-using tool bodies in a small retry helper:

```python
@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2.0),  # Phase E rebuild finishes in seconds
    retry=retry_if_exception(is_fts_missing_error),
)
async def _with_fts_retry(fn):
    return await fn()
```

`is_fts_missing_error` inspects the exception message for DuckDB's "no such function fts_main_products.match_bm25" pattern. If retries all fail, we surface `ToolError("INTERNAL_ERROR", "Textsökning tillfälligt otillgänglig; försök igen om några sekunder.")` — consistent with the tool-level error mapping.

No dual-index-swap machinery; the rebuild window is short and rare (once per daily sync, for seconds), so retries are sufficient.

---

## Concurrency

**Decision locked: simple per-request connections, no pool.** Each MCP tool call opens its own DuckDB reader, uses it, closes. DuckDB connections are ~1 ms to open; the overhead is invisible at the request volumes a home MCP server sees. Readers can run concurrently with each other and with the sync writer (snapshot isolation). If the server ever hosts more than a handful of concurrent clients, revisit — until then, no pool.

FastMCP runs request handlers on asyncio; our DuckDB calls are synchronous and would block the event loop if done naively. Wrap in `asyncio.to_thread()`:

```python
rows = await asyncio.to_thread(conn.execute, sql, binds)
```

Applies to all DB access. Embedding is not in-process here — it's a remote HTTP call via `embedding_client.embed()`, which is async-native (httpx) and needs no thread wrapping.

---

## Testing

Fixture strategy for unit tests:

```python
# tests/conftest.py
@pytest.fixture
def sample_db(tmp_path):
    """Create a DuckDB populated with ~100 representative products, 4 home stores."""
    path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(path))
    # Apply 001_initial.sql
    # Insert fixtures from tests/fixtures/products_sample.json
    conn.close()
    return path

@pytest.fixture
def reader(sample_db):
    conn = duckdb.connect(str(sample_db), read_only=True)
    yield conn
    conn.close()
```

Each tool has at least:
- Happy-path test (real-looking input, expected shape)
- Empty-result test (zero matches)
- Invalid-input test (sugar param fails to resolve, etc.)
- Staleness test (mock `sync_runs` with old timestamp, verify `meta.stale_warning` appears)

Integration tests run against VCR cassettes for the sync pipeline; MCP tests use the sample DB directly.

---

## Resolved decisions (from Step 2 open questions)

1. **Connection caching** → simple per-request (no pool). Documented in §Concurrency above.
2. **Embedding service ownership** → separate `sb-embed` HTTP service (OpenAI-compatible). Full design in [09_embedding_service.md](./09_embedding_service.md). Affects all three semantic tools plus Phase D.
3. **Pairing candidate budget** → keep cap at 5000, log when hit with full_count for observability. Raise cap only if the log appears in > ~5% of production calls.
4. **FTS rebuild window** → client-side retry in tool wrapper. Documented in §FTS specifics above.

Next: [Step 3 — sync orchestration](./10_sync_orchestration.md) (phase wiring, retry semantics, partial-failure recovery, raw-archive retention cleanup, scheduler lifecycle).
