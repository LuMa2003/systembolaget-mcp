"""Write gzipped JSON responses under `/data/raw/YYYY-MM-DD/`.

Layout (one subdir per category):
    raw/2026-04-19/catalog/Vin_page_0001.json.gz
    raw/2026-04-19/stock/store_1701_page_01.json.gz
    raw/2026-04-19/details/642008.json.gz
    raw/2026-04-19/stores.json.gz
    raw/2026-04-19/taxonomy.json.gz
    raw/2026-04-19/meta.json       (plain JSON; run timestamps, counts)
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any


class RawArchiveWriter:
    """One writer per sync run; scoped to a single date directory."""

    def __init__(self, root: Path, run_date: date) -> None:
        self.root = Path(root)
        self.run_date = run_date
        self.dir = self.root / run_date.isoformat()
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── Public write helpers ───────────────────────────────────────────

    async def write_catalog_page(self, category: str, page: int, payload: Any) -> Path:
        rel = Path("catalog") / f"{_safe_name(category)}_page_{page:04d}.json.gz"
        return await self._write_gz(rel, payload)

    async def write_stock_page(self, site_id: str, page: int, payload: Any) -> Path:
        rel = Path("stock") / f"store_{site_id}_page_{page:02d}.json.gz"
        return await self._write_gz(rel, payload)

    async def write_detail(self, product_number: str, payload: Any) -> Path:
        rel = Path("details") / f"{product_number}.json.gz"
        return await self._write_gz(rel, payload)

    async def write_stores(self, payload: Any) -> Path:
        return await self._write_gz(Path("stores.json.gz"), payload)

    async def write_taxonomy(self, payload: Any) -> Path:
        return await self._write_gz(Path("taxonomy.json.gz"), payload)

    async def write_meta(self, payload: dict[str, Any]) -> Path:
        dest = self.dir / "meta.json"
        await asyncio.to_thread(
            dest.write_text, json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
        return dest

    # ── Internals ─────────────────────────────────────────────────────

    async def _write_gz(self, rel: Path, payload: Any) -> Path:
        dest = self.dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _do_write() -> None:
            with gzip.open(dest, "wt", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)

        await asyncio.to_thread(_do_write)
        return dest


def _safe_name(category: str) -> str:
    """Make a category label filename-safe.

    Swedish characters are kept (utf-8 ext4 handles them cleanly); only
    path separators and whitespace are sanitised.
    """
    return category.replace("/", "_").replace(" ", "_").strip()
