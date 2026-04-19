"""Sync pipeline — nightly pull + diff + history + embed + FTS rebuild.

See docs/05_sync_pipeline.md for design; docs/10_sync_orchestration.md
for phase wiring, retry/failure semantics, lockfile, and scheduler.
"""

from sb_stack.sync.lockfile import Lockfile, LockfileBusyError
from sb_stack.sync.phase_types import (
    CatastrophicError,
    Phase,
    PhaseError,
    PhaseOutcome,
    PhaseResult,
)

__all__ = [
    "CatastrophicError",
    "Lockfile",
    "LockfileBusyError",
    "Phase",
    "PhaseError",
    "PhaseOutcome",
    "PhaseResult",
]
