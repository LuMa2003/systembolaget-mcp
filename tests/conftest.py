"""Shared test fixtures for sb-stack.

More fixtures (in-memory DuckDB, sample DB on disk, VCR cassettes, mock embed)
land as later implementation steps add the systems they exercise.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sb_stack.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _reset_settings_cache_between_tests() -> Iterator[None]:
    """Make sure no test sees a stale Settings() from another test's env."""
    reset_settings_cache()
    yield
    reset_settings_cache()
