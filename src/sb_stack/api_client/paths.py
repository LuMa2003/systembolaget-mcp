"""URL builders for the Systembolaget API.

Two namespaces under the same host:
    sb-api-ecommerce/v1   — web frontend backend
    sb-api-mobile/v1      — mobile app backend (superset: GTIN, faceted
                            filter, and inline stock+shelf on search)

Everything is built from a single `base_url` so that the config extractor
can retarget the client at a new gateway if Systembolaget migrates.
"""

from __future__ import annotations

from typing import Literal

Namespace = Literal["ecommerce", "mobile"]

_NAMESPACE_PATHS: dict[Namespace, str] = {
    "ecommerce": "sb-api-ecommerce/v1",
    "mobile": "sb-api-mobile/v1",
}


def _join(base_url: str, namespace: Namespace, path: str) -> str:
    base = base_url.rstrip("/")
    ns = _NAMESPACE_PATHS[namespace]
    p = path.lstrip("/")
    return f"{base}/{ns}/{p}"


class Paths:
    """Small, stateful helper; stores the API base URL once."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    # ── Catalog ────────────────────────────────────────────────────────
    def productsearch_search(self) -> str:
        """GET — paginated catalog (web API). Query params supplied separately."""
        return _join(self.base_url, "ecommerce", "productsearch/search")

    def mobile_productsearch_search(self) -> str:
        """GET — same shape, mobile API. Returns shelf+stock when storeId set."""
        return _join(self.base_url, "mobile", "productsearch/search")

    def productsearch_filter(self) -> str:
        """GET — all 22 filter groups with counts (mobile API only)."""
        return _join(self.base_url, "mobile", "productsearch/filter")

    # ── Single product ─────────────────────────────────────────────────
    def product_by_number(self, product_number: str) -> str:
        return _join(self.base_url, "ecommerce", f"product/productNumber/{product_number}")

    def product_by_id(self, product_id: str) -> str:
        # Only on the mobile API.
        return _join(self.base_url, "mobile", f"product/productId/{product_id}")

    def product_by_gtin(self, gtin: str) -> str:
        # Only on the mobile API.
        return _join(self.base_url, "mobile", f"product/gtin/{gtin}")

    # ── Stores ─────────────────────────────────────────────────────────
    def site_stores(self) -> str:
        return _join(self.base_url, "ecommerce", "site/stores")

    def site_store(self, site_id: str) -> str:
        return _join(self.base_url, "ecommerce", f"site/store/{site_id}")

    def site_stores_for_product(self, product_id: str) -> str:
        return _join(self.base_url, "ecommerce", f"site/stores/{product_id}")

    def sitesearch_site(self) -> str:
        return _join(self.base_url, "ecommerce", "sitesearch/site")

    # ── Stock ──────────────────────────────────────────────────────────
    def stockbalance(self, site_id: str, product_id: str) -> str:
        return _join(self.base_url, "ecommerce", f"stockbalance/store/{site_id}/{product_id}")
