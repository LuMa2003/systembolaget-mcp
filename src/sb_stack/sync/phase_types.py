"""Shared phase result types used across the orchestrator + phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    FETCH = "fetch"
    PERSIST = "persist"
    DETAILS = "details"
    EMBED = "embed"
    INDEX = "fts"
    FINALIZE = "finalize"


class PhaseOutcome(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"
    CATASTROPHIC = "catastrophic"


class CatastrophicError(Exception):
    """Raised by a phase to abort the entire run."""


@dataclass
class PhaseError:
    """One recoverable item-level failure inside a phase."""

    message: str
    cause: BaseException | None = None


@dataclass
class PhaseResult:
    phase: Phase
    outcome: PhaseOutcome
    duration_ms: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[PhaseError] = field(default_factory=list)
    summary: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "outcome": self.outcome.value,
            "duration_ms": self.duration_ms,
            "counts": self.counts,
            "error_summary": ("; ".join(e.message for e in self.errors))[:500] or None,
        }


def overall_status(results: list[PhaseResult]) -> str:
    if any(r.outcome == PhaseOutcome.CATASTROPHIC for r in results):
        return "failed"
    if any(r.outcome in (PhaseOutcome.FAILED, PhaseOutcome.PARTIAL) for r in results):
        return "partial"
    return "success"


def merge_counts(results: list[PhaseResult]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for r in results:
        for k, v in r.counts.items():
            merged[k] = merged.get(k, 0) + v
    return merged
