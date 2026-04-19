"""Unit tests for raw archive writer + reader + retention."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from sb_stack.raw_archive import RawArchiveReader, RawArchiveWriter, cleanup_old_raw


async def test_writer_and_reader_round_trip(tmp_path: Path) -> None:
    w = RawArchiveWriter(tmp_path, date(2026, 4, 19))
    await w.write_catalog_page("Vin", 1, {"products": [{"productNumber": "1"}]})
    await w.write_catalog_page("Vin", 2, {"products": [{"productNumber": "2"}]})
    await w.write_stock_page("1701", 1, {"products": [{"productNumber": "1"}]})
    await w.write_stores([{"siteId": "1701"}])
    await w.write_taxonomy({"filterGroups": []})
    await w.write_meta({"started_at": "2026-04-19T04:00:00Z"})

    r = RawArchiveReader(tmp_path, date(2026, 4, 19))
    assert r.exists()
    catalog = list(r.iter_catalog_pages())
    assert len(catalog) == 2
    assert catalog[0][0] == "Vin" and catalog[0][1] == 1
    stock = list(r.iter_stock_pages())
    assert len(stock) == 1 and stock[0][0] == "1701"
    assert r.load_stores() == [{"siteId": "1701"}]
    assert r.load_taxonomy() == {"filterGroups": []}
    assert r.load_meta() is not None


async def test_writer_handles_slash_in_category(tmp_path: Path) -> None:
    w = RawArchiveWriter(tmp_path, date(2026, 4, 19))
    # Slashes should be replaced so we don't create nested dirs accidentally.
    path = await w.write_catalog_page("Cider / blandad", 1, {"products": []})
    assert "/" not in path.name
    assert path.exists()


def test_retention_removes_old_dirs(tmp_path: Path) -> None:
    today = date(2026, 4, 19)
    # Keep three dated dirs + one hand-created 'test' dir.
    for d in (today, today - timedelta(days=1), today - timedelta(days=10)):
        (tmp_path / d.isoformat()).mkdir()
    (tmp_path / "test").mkdir()

    deleted = cleanup_old_raw(tmp_path, retention_days=5, today=today)

    assert deleted == 1
    assert (tmp_path / today.isoformat()).exists()
    assert (tmp_path / "test").exists()
    assert not (tmp_path / (today - timedelta(days=10)).isoformat()).exists()
