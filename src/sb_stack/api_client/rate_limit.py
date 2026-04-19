"""Concurrency limiter — a thin asyncio.Semaphore wrapper.

Gives callers a context-manager API and a single place to centralise the
limit so sync phases don't each juggle their own Semaphore.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class ConcurrencyLimiter:
    """Cap the number of in-flight operations at `max_concurrent`."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        await self._sem.acquire()
        try:
            yield
        finally:
            self._sem.release()
