"""Map a Systembolaget API product payload to our `products` row shape.

The API returns camelCase JSON; our columns are snake_case. The mapping
is pragmatic rather than exhaustive — it covers every TRACKED field
(see docs/05_sync_pipeline.md §"Tracked fields (whitelist)") plus
identity, naming, category, and a handful of commonly-used descriptive
fields. Unmapped keys pass through untouched and are ignored by the
INSERT/UPDATE statements.

The `TRACKED_FIELDS` list is authoritative — changing it requires a
migration bump since embedded text (and thus source_hash) would shift.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# API camelCase → DB snake_case. Only fields we persist.
FIELD_MAP: dict[str, str] = {
    # Identity
    "productNumber": "product_number",
    "productId": "product_id",
    "productNumberShort": "product_number_short",
    # Naming
    "productNameBold": "name_bold",
    "productNameThin": "name_thin",
    "producerName": "producer_name",
    "supplierName": "supplier_name",
    # Category hierarchy
    "categoryLevel1": "category_level_1",
    "categoryLevel1Id": "category_level_1_id",
    "categoryLevel2": "category_level_2",
    "categoryLevel2Id": "category_level_2_id",
    "categoryLevel3": "category_level_3",
    "categoryLevel3Id": "category_level_3_id",
    "categoryLevel4": "category_level_4",
    "categoryLevel4Id": "category_level_4_id",
    "customCategoryTitle": "custom_category_title",
    # Origin
    "country": "country",
    "originLevel1": "origin_level_1",
    "originLevel2": "origin_level_2",
    "originLevel3": "origin_level_3",
    "brandOrigin": "brand_origin",
    # Physical
    "volume": "volume_ml",
    "volumeInMilliliters": "volume_ml",
    "bottleCode": "bottle_code",
    "bottleText": "bottle_text",
    "bottleTextShort": "bottle_text_short",
    "bottleTypeGroup": "bottle_type_group",
    "packaging": "packaging",
    "packagingLevel1": "packaging_level_1",
    "packagingLevel2": "packaging_level_2",
    "packagingTypeCode": "packaging_type_code",
    "packagingCo2Level": "packaging_co2_level",
    "packagingCo2GPerL": "packaging_co2_g_per_l",
    "seal": "seal",
    "parcelFillFactor": "parcel_fill_factor",
    "restrictedParcelQuantity": "restricted_parcel_qty",
    # Wine-specific
    "vintage": "vintage",
    "grapes": "grapes",
    "isNewVintage": "is_new_vintage",
    # Composition
    "alcoholPercentage": "alcohol_percentage",
    "sugarContent": "sugar_content",
    "sugarContentGramPer100ml": "sugar_content_g_per_100ml",
    "standardDrinks": "standard_drinks",
    "color": "color",
    # Pricing
    "price": "price_incl_vat",
    "priceInclVat": "price_incl_vat",
    "priceInclVatExclRecycleFee": "price_incl_vat_excl_recycle",
    "priceExclVat": "price_excl_vat",
    "recycleFee": "recycle_fee",
    "comparisonPrice": "comparison_price",
    "priorPrice": "prior_price",
    "vatCode": "vat_code",
    # Taste clocks
    "tasteClockBody": "taste_clock_body",
    "tasteClockBitter": "taste_clock_bitter",
    "tasteClockSweetness": "taste_clock_sweetness",
    "tasteClockFruitAcid": "taste_clock_fruitacid",
    "tasteClockRoughness": "taste_clock_roughness",
    "tasteClockSmokiness": "taste_clock_smokiness",
    "tasteClockCasque": "taste_clock_casque",
    "tasteClockGroup": "taste_clock_group",
    "tasteClockGroupBitter": "taste_clock_group_bitter",
    "tasteClockGroupSmokiness": "taste_clock_group_smokiness",
    "hasCasqueTaste": "has_casque_taste",
    # Food pairings
    "tasteSymbols": "taste_symbols",
    # Text content
    "usage": "usage",
    "taste": "taste",
    "aroma": "aroma",
    "producerDescription": "producer_description",
    "production": "production",
    "cultivationArea": "cultivation_area",
    "harvest": "harvest",
    "soil": "soil",
    "storage": "storage",
    "rawMaterial": "raw_material",
    "ingredients": "ingredients",
    "allergens": "allergens",
    "additives": "additives",
    "additionalInformation": "additional_information",
    "didYouKnow": "did_you_know",
    # Labels / flags
    "isOrganic": "is_organic",
    "isSustainableChoice": "is_sustainable_choice",
    "isClimateSmartPackaging": "is_climate_smart_packaging",
    "isLightWeightBottle": "is_light_weight_bottle",
    "isEcoFriendlyPackage": "is_eco_friendly_package",
    "isEthical": "is_ethical",
    "ethicalLabel": "ethical_label",
    "isKosher": "is_kosher",
    "isNaturalWine": "is_natural_wine",
    "isVeganFriendly": "is_vegan_friendly",
    "isGlutenFree": "is_gluten_free",
    "isManufacturingCountry": "is_manufacturing_country",
    "isRegionalRestricted": "is_regional_restricted",
    # Assortment
    "assortment": "assortment",
    "assortmentText": "assortment_text",
    "isBsAssortment": "is_bs_assortment",
    "isPaAssortment": "is_pa_assortment",
    "isFsAssortment": "is_fs_assortment",
    "isTsAssortment": "is_ts_assortment",
    "isTseAssortment": "is_tse_assortment",
    "isTslsAssortment": "is_tsls_assortment",
    "isTssAssortment": "is_tss_assortment",
    "isTstAssortment": "is_tst_assortment",
    "isTsvAssortment": "is_tsv_assortment",
    "isFstsAssortment": "is_fsts_assortment",
    "isWebLaunch": "is_web_launch",
    "isNews": "is_news",
    "isNewInAssortment": "is_new_in_assortment",
    "isLimitedEdition": "is_limited_edition",
    # Availability
    "isCompletelyOutOfStock": "is_completely_out_of_stock",
    "isTemporaryOutOfStock": "is_temporary_out_of_stock",
    "completelyOutOfStockDate": "completely_out_of_stock_date",
    "isSupplierNotAvailable": "is_supplier_not_available",
    "isSupplierTempNotAvailable": "is_supplier_temp_not_available",
    "supplierNotAvailableDate": "supplier_not_available_date",
    "supplierTempNotAvailableDate": "supplier_temp_not_available_date",
    "backInStockAtSupplierDate": "back_in_stock_at_supplier",
    "isOutOfStockAtDepot": "is_out_of_stock_at_depot",
    "isDepotDelivered": "is_depot_delivered",
    "customerOrderSupplySource": "customer_order_supply_source",
    "availableNumberOfStores": "available_number_of_stores",
    "isDiscontinued": "is_discontinued",
    "discontinuedAt": "discontinued_at",
    "isStoreOrderApplicable": "is_store_order_applicable",
    "isHomeOrderApplicable": "is_home_order_applicable",
    "isAgentOrderApplicable": "is_agent_order_applicable",
    # Mobile-only
    "needCrateProductId": "need_crate_product_id",
    "rating": "rating",
    # Dates
    "productLaunchDate": "product_launch_date",
    "originalSellStartDate": "original_sell_start_date",
    "sellStartTime": "sell_start_time",
    "tastingDate": "tasting_date",
}

# Tracked fields (whitelist) — change → append to product_history.
# Authority: docs/05_sync_pipeline.md §Tracked fields.
TRACKED_FIELDS: tuple[str, ...] = (
    "price_incl_vat",
    "price_excl_vat",
    "recycle_fee",
    "comparison_price",
    "assortment_text",
    "is_discontinued",
    "is_completely_out_of_stock",
    "is_temporary_out_of_stock",
    "is_supplier_not_available",
    "is_news",
    "is_new_in_assortment",
    "is_limited_edition",
    "vintage",
    "available_number_of_stores",
    "back_in_stock_at_supplier",
    "supplier_not_available_date",
    "taste_clock_body",
    "taste_clock_bitter",
    "taste_clock_sweetness",
    "taste_clock_fruitacid",
    "taste_clock_roughness",
    "taste_clock_smokiness",
    "taste_clock_casque",
)


# Columns that the DB stores as VARCHAR[]. The API is inconsistent: the
# search endpoint returns proper JSON arrays (`["Fisk", "Kött"]`), but the
# detail endpoint returns semicolon-joined strings (`"Fisk;Kött"`) in the
# same-named field, sometimes alongside a `*List` companion that is an
# array. We coerce to a list on the way in so Phase B never hands a string
# to DuckDB for a VARCHAR[] column.
_LIST_COLUMNS: frozenset[str] = frozenset({"taste_symbols", "grapes"})

# API aliases for list fields: when the primary camelCase key is a string
# but a `*List` sibling is an array, prefer the sibling.
_LIST_ARRAY_ALIASES: dict[str, str] = {
    "tasteSymbols": "tasteSymbolsList",
    "grapes": "grapesList",
}


def map_product(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate one API product payload into a `products` row dict.

    Coerces list-valued columns to Python lists regardless of whether the
    API sent a list or a semicolon-joined string.
    """
    # Build the array-sibling lookup once per call.
    list_arrays = {
        primary: payload[sibling]
        for primary, sibling in _LIST_ARRAY_ALIASES.items()
        if isinstance(payload.get(sibling), list)
    }
    row: dict[str, Any] = {}
    for k, v in payload.items():
        col = FIELD_MAP.get(k)
        if col is None:
            continue
        value = _coerce_list(v, array_sibling=list_arrays.get(k)) if col in _LIST_COLUMNS else v
        row[col] = value
    return row


def _coerce_list(value: Any, *, array_sibling: list[Any] | None) -> Any:
    """Return a list for VARCHAR[] columns, handling the API's dual encoding."""
    if array_sibling is not None:
        return list(array_sibling)
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part.strip() for part in value.split(";") if part.strip()]
    # Unknown shape — let DuckDB complain if this ever happens in prod.
    return value


def field_hash(row: dict[str, Any]) -> str:
    """sha256 of a canonical JSON dump of the tracked fields."""
    subset = {f: _normalize(row.get(f)) for f in TRACKED_FIELDS}
    encoded = json.dumps(subset, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize(v: Any) -> Any:
    """Make values comparable across sync runs (lists → sorted tuples etc.)."""
    if isinstance(v, list):
        return sorted(_normalize(x) for x in v)
    return v
