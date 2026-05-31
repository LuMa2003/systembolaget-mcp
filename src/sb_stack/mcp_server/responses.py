"""Pydantic output models for every MCP tool.

Keeping them shared in one module lets tools reuse composition (e.g.
Product is returned by search_products, semantic_search, get_product).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TasteClocks(BaseModel):
    body: int | None = None
    bitter: int | None = None
    sweetness: int | None = None
    fruitacid: int | None = None
    roughness: int | None = None
    smokiness: int | None = None
    casque: int | None = None


class StockAtStore(BaseModel):
    stock: int
    shelf: str | None = None
    is_in_assortment: bool | None = None
    observed_at: datetime | None = None


class Product(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

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
    taste_clocks: TasteClocks = Field(default_factory=TasteClocks)
    taste_symbols: list[str] = Field(default_factory=list)
    grapes: list[str] = Field(default_factory=list)
    is_organic: bool | None = None
    is_vegan_friendly: bool | None = None
    is_discontinued: bool | None = None
    home_stock: dict[str, StockAtStore] = Field(default_factory=dict)
    image_url: str | None = None


class SearchProductsResult(BaseModel):
    results: list[Product]
    total_count: int


class SemanticSearchItem(Product):
    similarity: float


class SemanticSearchResult(BaseModel):
    results: list[SemanticSearchItem]


class SimilarProductsResult(BaseModel):
    source: Product
    results: list[SemanticSearchItem]


class Variant(BaseModel):
    variant_product_number: str
    variant_volume_ml: int | None = None
    variant_bottle_text: str | None = None


class ImageSize(BaseModel):
    size: int
    url: str


class HomeStockRow(BaseModel):
    site_id: str
    alias: str | None = None
    stock: int | None = None
    shelf: str | None = None
    is_in_assortment: bool | None = None
    observed_at: datetime | None = None


class GetProductResult(BaseModel):
    product: dict[str, Any]
    variants: list[Variant]
    home_stock: list[HomeStockRow]
    image_urls: list[ImageSize]


class CompareRow(BaseModel):
    field: str
    values: list[Any]


class CompareResult(BaseModel):
    rows: list[CompareRow]
    products: list[Product]
    notes: str | None = None


class HomeStore(BaseModel):
    site_id: str
    alias: str | None = None
    address: str | None = None
    city: str | None = None
    county: str | None = None
    is_main_store: bool = False
    latitude: float | None = None
    longitude: float | None = None
    today_open_from: str | None = None
    today_open_to: str | None = None
    distance_from_main_km: float | None = None


class ListHomeStoresResult(BaseModel):
    stores: list[HomeStore]


class ScheduleEntry(BaseModel):
    date: date
    open_from: str | None = None
    open_to: str | None = None
    reason: str | None = None
    is_open: bool


class StoreSchedule(BaseModel):
    store: HomeStore
    schedule: list[ScheduleEntry]
    notes: str | None = None


class TaxonomyEntry(BaseModel):
    value: str
    count: int


class TaxonomyResult(BaseModel):
    values: list[TaxonomyEntry]
    captured_at: date | None = None


class SyncLastRun(BaseModel):
    run_id: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str | None = None
    products_added: int | None = None
    products_updated: int | None = None
    products_discontinued: int | None = None
    stock_rows_updated: int | None = None
    embeddings_generated: int | None = None
    error: str | None = None


class SyncStatusResult(BaseModel):
    last_run: SyncLastRun
    hours_since_last_success: float | None = None
    product_count: int
    home_stock_rows: int
    api_key_last_validated: datetime | None = None
    stale: bool
    summary: str | None = None


class ScoreBreakdown(BaseModel):
    """Per-recommendation scoring decomposition for transparency / debugging.

    All terms are 0..1 where applicable; populated fields depend on what the
    engine could compute for a given dish. `total` is the blended score used
    for ranking.
    """

    usage_similarity: float | None = None
    taste_clock_fit: float | None = None
    symbol_match: float | None = None
    regional_fit: float | None = None
    total: float | None = None


class PairingRecommendation(BaseModel):
    product: Product
    similarity: float
    # Swedish, user-facing motivation. MUST NOT leak internal scores/floats.
    why: str
    score_breakdown: ScoreBreakdown | None = None


class PairingInterpretation(BaseModel):
    """How the engine understood the dish — surfaced so a voicing LLM can
    explain its reasoning in Swedish."""

    dish_summary: str | None = None
    inferred_taste_symbols: list[str] = Field(default_factory=list)
    inferred_profile: str | None = None
    sommelier_reasoning: str | None = None


class PairWithDishResult(BaseModel):
    recommendations: list[PairingRecommendation] = Field(default_factory=list)
    confidence: str = "low"
    dish: str = ""
    notes: str | None = None
    interpretation: PairingInterpretation | None = None
