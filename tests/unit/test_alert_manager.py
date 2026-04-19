"""State-transition tests for AlertManager + ntfy send path."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from sb_stack.notifications import AlertManager
from sb_stack.settings import Settings

NTFY = "https://ntfy.test/sb"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        ntfy_url=NTFY,
        ntfy_cooldown_hours=6,
        ntfy_min_priority=2,
        log_to_file=False,
        log_to_stdout=False,
    )


@pytest.fixture
def settings_silent(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        ntfy_url=None,
        log_to_file=False,
        log_to_stdout=False,
    )


@respx.mock
async def test_success_streak_sends_nothing(settings: Settings) -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200))
    mgr = AlertManager(settings)
    await mgr.evaluate(status="success", run_id=1)
    await mgr.evaluate(status="success", run_id=2)
    assert route.call_count == 0


@respx.mock
async def test_first_failure_fires_sync_failing(settings: Settings) -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200))
    mgr = AlertManager(settings)
    await mgr.evaluate(status="success", run_id=1)
    await mgr.evaluate(status="failed", run_id=2)
    assert route.call_count == 1
    req = route.calls.last.request
    assert "Systembolaget sync: failed" in req.headers["Title"]
    assert req.headers["Priority"] == "3"


@respx.mock
async def test_repeated_failures_fires_urgent(settings: Settings) -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200))
    mgr = AlertManager(settings)
    await mgr.evaluate(status="failed", run_id=1)  # fires sync_failing
    await mgr.evaluate(status="failed", run_id=2)  # fires sync_repeatedly_failing
    assert route.call_count == 2
    latest = route.calls.last.request
    assert latest.headers["Priority"] == "5"
    assert "consecutive" in latest.headers["Title"]


@respx.mock
async def test_recovery_fires_check_mark(settings: Settings) -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200))
    mgr = AlertManager(settings)
    await mgr.evaluate(status="failed", run_id=1)
    await mgr.evaluate(status="success", run_id=2)
    assert route.call_count == 2
    latest = route.calls.last.request
    assert "recovered" in latest.headers["Title"]


@respx.mock
async def test_cooldown_suppresses_repeat_of_same_key(settings: Settings) -> None:
    route = respx.post(NTFY).mock(return_value=httpx.Response(200))
    mgr = AlertManager(settings)
    # Fire sync_failing.
    await mgr.evaluate(status="failed", run_id=1)
    # Recovered — clears cooldown for recovery, but sync_failing is still
    # within cooldown.
    await mgr.evaluate(status="success", run_id=2)
    # Fail again → should be suppressed by cooldown on sync_failing.
    await mgr.evaluate(status="failed", run_id=3)
    assert route.call_count == 2


async def test_silent_when_url_unset(settings_silent: Settings) -> None:
    mgr = AlertManager(settings_silent)
    # Should not raise, should not hit network.
    await mgr.evaluate(status="failed", run_id=1)
    await mgr.evaluate(status="success", run_id=2)


@respx.mock
async def test_send_failure_never_raises(settings: Settings) -> None:
    respx.post(NTFY).mock(return_value=httpx.Response(500))
    mgr = AlertManager(settings)
    await mgr.evaluate(status="failed", run_id=1)  # must not raise


async def test_state_persists_between_instances(settings: Settings) -> None:
    mgr1 = AlertManager(settings)
    await mgr1.evaluate(status="failed", run_id=1)

    mgr2 = AlertManager(settings)
    # Rehydration: internal counter should be 1, so another failure triggers
    # the repeatedly_failing alert.
    assert mgr2._state["consecutive_failures"] == 1
