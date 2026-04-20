"""_mobile_auth_failed recognises a mobile-namespace 401 in Phase A errors."""

from __future__ import annotations

from sb_stack.errors import (
    AuthenticationError,
    RateLimitedError,
    SystembolagetAPIError,
)
from sb_stack.sync.orchestrator import _mobile_auth_failed
from sb_stack.sync.phase_types import Phase, PhaseError, PhaseOutcome, PhaseResult


def _phase_a(errors: list[PhaseError]) -> PhaseResult:
    return PhaseResult(
        phase=Phase.FETCH,
        outcome=PhaseOutcome.PARTIAL if errors else PhaseOutcome.OK,
        errors=errors,
    )


def test_mobile_401_detected() -> None:
    err = AuthenticationError(
        "auth failed",
        status_code=401,
        url="https://api-extern.systembolaget.se/sb-api-mobile/v1/productsearch/filter",
    )
    assert _mobile_auth_failed(_phase_a([PhaseError("taxonomy failed", cause=err)])) is True


def test_ecommerce_401_not_flagged_as_mobile() -> None:
    err = AuthenticationError(
        "auth failed",
        status_code=401,
        url="https://api-extern.systembolaget.se/sb-api-ecommerce/v1/site/stores",
    )
    assert _mobile_auth_failed(_phase_a([PhaseError("stores failed", cause=err)])) is False


def test_non_auth_errors_ignored() -> None:
    err = RateLimitedError(
        "429",
        status_code=429,
        url="https://api-extern.systembolaget.se/sb-api-mobile/v1/productsearch/search",
    )
    assert _mobile_auth_failed(_phase_a([PhaseError("rate-limited", cause=err)])) is False


def test_empty_phase_result() -> None:
    assert _mobile_auth_failed(_phase_a([])) is False


def test_generic_sb_error_without_cause_is_ignored() -> None:
    assert _mobile_auth_failed(_phase_a([PhaseError("cosmetic warn", cause=None)])) is False
    # And with a non-AuthenticationError cause:
    assert (
        _mobile_auth_failed(
            _phase_a(
                [
                    PhaseError(
                        "hmm",
                        cause=SystembolagetAPIError(
                            "x",
                            status_code=500,
                            url="https://api-extern.systembolaget.se/sb-api-mobile/v1/",
                        ),
                    )
                ]
            )
        )
        is False
    )
