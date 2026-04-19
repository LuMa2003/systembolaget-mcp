"""Unit tests for ConcurrencyLimiter."""

from __future__ import annotations

import asyncio

import pytest

from sb_stack.api_client.rate_limit import ConcurrencyLimiter


def test_rejects_invalid_max_concurrent() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        ConcurrencyLimiter(0)


async def test_caps_parallelism() -> None:
    limiter = ConcurrencyLimiter(2)
    in_flight = 0
    peak = 0
    done = asyncio.Event()

    async def _work() -> None:
        nonlocal in_flight, peak
        async with limiter.acquire():
            in_flight += 1
            peak = max(peak, in_flight)
            # yield, let the scheduler try to pack more work in
            await asyncio.sleep(0.01)
            in_flight -= 1

    async def _fleet() -> None:
        await asyncio.gather(*[_work() for _ in range(10)])
        done.set()

    await asyncio.wait_for(_fleet(), timeout=2.0)
    assert peak <= 2
    assert done.is_set()
