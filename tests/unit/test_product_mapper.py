"""Unit tests for the product mapper + field_hash."""

from __future__ import annotations

from sb_stack.sync.product_mapper import TRACKED_FIELDS, field_hash, map_product


def test_map_product_translates_camel_to_snake() -> None:
    row = map_product(
        {
            "productNumber": "642008",
            "productId": "1004489",
            "productNameBold": "Apothic Red",
            "categoryLevel1": "Vin",
            "priceInclVat": 119.00,
            "tasteClockBody": 8,
            "unknownField": "dropped",
        }
    )
    assert row["product_number"] == "642008"
    assert row["product_id"] == "1004489"
    assert row["name_bold"] == "Apothic Red"
    assert row["category_level_1"] == "Vin"
    assert row["price_incl_vat"] == 119.0
    assert row["taste_clock_body"] == 8
    assert "unknownField" not in row and "unknown_field" not in row


def test_field_hash_changes_only_on_tracked_field_change() -> None:
    base = map_product(
        {
            "productNumber": "1",
            "productNameBold": "X",
            "priceInclVat": 100,
            "tasteClockBody": 5,
        }
    )
    h1 = field_hash(base)

    # Change an UNTRACKED field → hash must stay the same.
    base2 = {**base, "name_bold": "Y"}
    assert field_hash(base2) == h1

    # Change a TRACKED field → hash must differ.
    base3 = {**base, "price_incl_vat": 101}
    assert field_hash(base3) != h1


def test_tracked_fields_nonempty() -> None:
    # Guard against a regression that silently empties the whitelist.
    assert set(TRACKED_FIELDS)
    assert "price_incl_vat" in TRACKED_FIELDS


def test_field_hash_is_deterministic() -> None:
    row = {"price_incl_vat": 100, "is_discontinued": False}
    assert field_hash(row) == field_hash(row)


# ── <N obfuscation coercion ──────────────────────────────────────────────


def test_html_encoded_lt_becomes_none_for_int_column() -> None:
    # availableNumberOfStores = "&lt;3" → None
    row = map_product({"productNumber": "1", "availableNumberOfStores": "&lt;3"})
    assert row["available_number_of_stores"] is None


def test_html_encoded_lt_becomes_none_for_decimal_column() -> None:
    # alcoholPercentage = "&lt;0,3" (Swedish decimal) → None
    row = map_product({"productNumber": "1", "alcoholPercentage": "&lt;0,3"})
    assert row["alcohol_percentage"] is None


def test_raw_lt_symbol_also_coerced() -> None:
    # Same behaviour if the API ever stops HTML-encoding.
    row = map_product({"productNumber": "1", "availableNumberOfStores": "<3"})
    assert row["available_number_of_stores"] is None


def test_greater_than_also_coerced() -> None:
    row = map_product({"productNumber": "1", "alcoholPercentage": "&gt;99,9"})
    assert row["alcohol_percentage"] is None


def test_normal_numeric_values_pass_through() -> None:
    row = map_product(
        {
            "productNumber": "1",
            "availableNumberOfStores": 12,
            "alcoholPercentage": 13.5,
            "priceInclVat": 149.0,
            "tasteClockBody": 8,
        }
    )
    assert row["available_number_of_stores"] == 12
    assert row["alcohol_percentage"] == 13.5
    assert row["price_incl_vat"] == 149.0
    assert row["taste_clock_body"] == 8


def test_null_numeric_stays_null() -> None:
    row = map_product({"productNumber": "1", "availableNumberOfStores": None})
    assert row["available_number_of_stores"] is None


def test_legitimate_numeric_string_passes_through_for_duckdb_cast() -> None:
    # "13.5" isn't obfuscated, just string-typed. Leave it; DuckDB's
    # implicit string→float cast handles it on INSERT.
    row = map_product({"productNumber": "1", "alcoholPercentage": "13.5"})
    assert row["alcohol_percentage"] == "13.5"


# ── API rename coverage ──────────────────────────────────────────────────


def test_new_and_legacy_api_key_names_both_land_on_same_column() -> None:
    """Aliases from the rename pass: new API name → new column, legacy
    name → same column. Whichever wins depends on iteration order; both
    must at least produce a non-null value on the target column."""
    for new, legacy, col in (
        ("packagingCO2ImpactLevel", "packagingCo2Level", "packaging_co2_level"),
        (
            "packagingCO2EquivalentPerLitre",
            "packagingCo2GPerL",
            "packaging_co2_g_per_l",
        ),
        ("tasteClockFruitacid", "tasteClockFruitAcid", "taste_clock_fruitacid"),
        ("didYouKnowInformation", "didYouKnow", "did_you_know"),
        ("isFsTsAssortment", "isFstsAssortment", "is_fsts_assortment"),
        ("isTsLsAssortment", "isTslsAssortment", "is_tsls_assortment"),
        (
            "backInStockAtSupplier",
            "backInStockAtSupplierDate",
            "back_in_stock_at_supplier",
        ),
        (
            "isSupplierTemporaryNotAvailable",
            "isSupplierTempNotAvailable",
            "is_supplier_temp_not_available",
        ),
        (
            "supplierTemporaryNotAvailableDate",
            "supplierTempNotAvailableDate",
            "supplier_temp_not_available_date",
        ),
    ):
        new_row = map_product({"productNumber": "1", new: "X"})
        legacy_row = map_product({"productNumber": "1", legacy: "X"})
        assert new_row.get(col) == "X", f"new name {new!r} didn't populate {col!r}"
        assert legacy_row.get(col) == "X", f"legacy name {legacy!r} didn't populate {col!r}"
