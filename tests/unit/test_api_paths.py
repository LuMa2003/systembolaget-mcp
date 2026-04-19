"""Unit tests for Paths URL builders."""

from __future__ import annotations

from sb_stack.api_client.paths import Paths

P = Paths("https://api-extern.systembolaget.se")


def test_productsearch_search_is_ecommerce() -> None:
    assert (
        P.productsearch_search()
        == "https://api-extern.systembolaget.se/sb-api-ecommerce/v1/productsearch/search"
    )


def test_mobile_search_is_mobile() -> None:
    assert (
        P.mobile_productsearch_search()
        == "https://api-extern.systembolaget.se/sb-api-mobile/v1/productsearch/search"
    )


def test_filter_is_mobile() -> None:
    assert (
        P.productsearch_filter()
        == "https://api-extern.systembolaget.se/sb-api-mobile/v1/productsearch/filter"
    )


def test_product_paths() -> None:
    assert P.product_by_number("642008").endswith(
        "/sb-api-ecommerce/v1/product/productNumber/642008"
    )
    assert P.product_by_id("1004489").endswith("/sb-api-mobile/v1/product/productId/1004489")
    assert P.product_by_gtin("7311210000000").endswith(
        "/sb-api-mobile/v1/product/gtin/7311210000000"
    )


def test_store_paths() -> None:
    assert P.site_stores().endswith("/sb-api-ecommerce/v1/site/stores")
    assert P.site_store("1701").endswith("/sb-api-ecommerce/v1/site/store/1701")
    assert P.site_stores_for_product("1004489").endswith("/sb-api-ecommerce/v1/site/stores/1004489")


def test_stockbalance_path() -> None:
    assert P.stockbalance("1701", "1004489").endswith(
        "/sb-api-ecommerce/v1/stockbalance/store/1701/1004489"
    )


def test_trailing_slash_in_base_url_is_normalized() -> None:
    p = Paths("https://api-extern.systembolaget.se/")
    assert (
        p.productsearch_search()
        == "https://api-extern.systembolaget.se/sb-api-ecommerce/v1/productsearch/search"
    )
