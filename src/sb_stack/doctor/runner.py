"""Orchestrates individual checks defined in `checks.py`.

Each check runs independently; one failing doesn't short-circuit the
others. Exit codes match docs/06_module_layout.md §Doctor:

  0 — all pass or warn
  1 — at least one fail
  2 — doctor itself crashed (raised to caller)
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from sb_stack.settings import Settings

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    duration_ms: int = 0
    summary: str = ""
    details: str | None = None


CheckCallable = Callable[[Settings], CheckResult | Awaitable[CheckResult]]


@dataclass
class _Check:
    name: str
    fn: CheckCallable
    optional: bool = False


@dataclass
class DoctorResult:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == "pass")

    @property
    def warned(self) -> int:
        return sum(1 for r in self.results if r.status == "warn")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "fail")

    def exit_code(self, *, exit_on_warn: bool) -> int:
        if self.failed:
            return 1
        if exit_on_warn and self.warned:
            return 1
        return 0


async def _run_one(check: _Check, settings: Settings) -> CheckResult:
    t0 = time.monotonic()
    try:
        out = check.fn(settings)
        if inspect.isawaitable(out):
            out = await out
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            name=check.name,
            status="fail",
            duration_ms=int((time.monotonic() - t0) * 1000),
            summary=f"check crashed: {e!r}",
        )
    result: CheckResult = out
    if result.duration_ms == 0:
        result.duration_ms = int((time.monotonic() - t0) * 1000)
    return result


async def run_all_async(
    settings: Settings,
    *,
    only: Iterable[str] | None = None,
    include_optional: bool = False,
) -> DoctorResult:
    from sb_stack.doctor.checks import ALL_CHECKS  # noqa: PLC0415

    name_filter = set(only) if only else None
    selected: list[_Check] = []
    for c in ALL_CHECKS:
        if name_filter is not None and c.name not in name_filter:
            continue
        if c.optional and not include_optional:
            continue
        selected.append(c)
    # Run sequentially; checks are cheap and serialising keeps output stable.
    results: list[CheckResult] = []
    for c in selected:
        results.append(await _run_one(c, settings))
    return DoctorResult(results=results)


def run_all(
    settings: Settings,
    *,
    only: Iterable[str] | None = None,
    include_optional: bool = False,
) -> DoctorResult:
    return asyncio.run(run_all_async(settings, only=only, include_optional=include_optional))
