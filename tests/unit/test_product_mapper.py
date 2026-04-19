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
