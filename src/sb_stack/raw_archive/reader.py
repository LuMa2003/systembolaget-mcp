"""Read back archived raw responses (for Phase B, replay, and debugging)."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any


class RawArchiveReader:
    """Iterate over archived payloads for a given run_date."""

    def __init__(self, root: Path, run_date: date) -> None:
        self.root = Path(root)
        self.run_date = run_date
        self.dir = self.root / run_date.isoformat()

    def exists(self) -> bool:
        return self.dir.is_dir()

    # ── Iterators over raw pages ──────────────────────────────────────

    def iter_catalog_pages(self) -> Iterator[tuple[str, int, Any]]:
        """Yield (category, page_number, payload) for every archived catalog page."""
        catalog_dir = self.dir / "catalog"
        if not catalog_dir.exists():
            return
        for p in sorted(catalog_dir.glob("*.json.gz")):
            # Filename: {category}_page_{NNNN}.json.gz
            stem = p.name.replace(".json.gz", "")
            category, _, page_part = stem.rpartition("_page_")
            if not category:
                continue
            yield category.replace("_", " "), int(page_part), _load_gz(p)

    def iter_stock_pages(self) -> Iterator[tuple[str, int, Any]]:
        stock_dir = self.dir / "stock"
        if not stock_dir.exists():
            return
        for p in sorted(stock_dir.glob("store_*.json.gz")):
            stem = p.name.replace(".json.gz", "")
            parts = stem.split("_")
            # store_{siteId}_page_{NN}
            site_id = parts[1]
            page_num = int(parts[3])
            yield site_id, page_num, _load_gz(p)

    def iter_details(self) -> Iterator[tuple[str, Any]]:
        details_dir = self.dir / "details"
        if not details_dir.exists():
            return
        for p in sorted(details_dir.glob("*.json.gz")):
            product_number = p.name.replace(".json.gz", "")
            yield product_number, _load_gz(p)

    # ── Singleton reads ───────────────────────────────────────────────

    def load_stores(self) -> Any:
        p = self.dir / "stores.json.gz"
        return _load_gz(p) if p.exists() else None

    def load_taxonomy(self) -> Any:
        p = self.dir / "taxonomy.json.gz"
        return _load_gz(p) if p.exists() else None

    def load_meta(self) -> dict[str, Any] | None:
        p = self.dir / "meta.json"
        if not p.exists():
            return None
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data


def _load_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)
