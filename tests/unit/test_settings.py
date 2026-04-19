"""Unit tests for sb_stack.settings.Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from sb_stack.settings import Settings


def test_defaults_when_no_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Ensure none of our vars leak in from the host and .env isn't read
    for k in list(Settings.model_fields):
        monkeypatch.delenv(f"SB_{k.upper()}", raising=False)
    monkeypatch.chdir(tmp_path)

    s = Settings()

    assert s.data_dir == Path("/data")
    assert s.store_subset == ["1701", "1702", "1716", "1718"]
    assert s.main_store == "1701"
    assert s.embed_dim == 2560
    assert s.mcp_transport == "http"
    assert s.log_level == "info"
    assert s.log_format == "json"


def test_store_subset_from_csv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SB_STORE_SUBSET", "1701, 1702, 1718")

    s = Settings()

    assert s.store_subset == ["1701", "1702", "1718"]


def test_store_subset_passthrough_for_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SB_STORE_SUBSET", raising=False)

    s = Settings(store_subset=["1701", "9999"])

    assert s.store_subset == ["1701", "9999"]


def test_derived_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SB_DATA_DIR", str(tmp_path / "x"))

    s = Settings()

    assert s.db_path == tmp_path / "x" / "sb.duckdb"
    assert s.raw_dir == tmp_path / "x" / "raw"
    assert s.backup_dir == tmp_path / "x" / "backup"
    assert s.pre_migration_backup_dir == tmp_path / "x" / "backup" / "pre-migration"
    assert s.models_cache_dir == tmp_path / "x" / "models"
    assert s.logs_dir == tmp_path / "x" / "logs"
    assert s.state_dir == tmp_path / "x" / "state"
    assert s.duckdb_ext_dir == tmp_path / "x" / "duckdb_extensions"


def test_log_level_normalized(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SB_LOG_LEVEL", "DEBUG")

    s = Settings()

    assert s.log_level == "debug"


def test_case_insensitive_env_prefix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("sb_embed_dim", "384")

    s = Settings()

    assert s.embed_dim == 384
