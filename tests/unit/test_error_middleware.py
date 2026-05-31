"""Unit tests for the Swedish error middleware translation (#5).

Verifies pydantic validation failures render as clean Swedish (no `inp.`
prefix, no pydantic.dev URL) and that FastMCP's English tool wrapper is stripped.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from sb_stack.mcp_server.error_middleware import _PREFIX_RE, _to_swedish


class _Model(BaseModel):
    limit: int = Field(ge=1, le=200)
    product_numbers: list[str] = Field(min_length=2, max_length=5)
    query: str = "x"

    @field_validator("query")
    @classmethod
    def _q(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("sökfrågan får inte vara tom")
        return v

    @model_validator(mode="after")
    def _m(self) -> _Model:
        if self.limit > 100 and len(self.product_numbers) > 2:
            raise ValueError("för många produkter för stor limit")
        return self


def _err(**kw: object) -> str:
    full: dict[str, object] = {"limit": 5, "product_numbers": ["a", "b"], "query": "x"}
    full.update(kw)
    with pytest.raises(ValidationError) as exc:
        _Model(**full)
    return _to_swedish(exc.value)


def test_ge_le_bounds_render_swedish() -> None:
    assert _to_swedish_contains(_err(limit=0), "limit", "minst 1")
    assert _to_swedish_contains(_err(limit=999), "limit", "högst 200")


def test_list_length_renders_swedish() -> None:
    msg = _err(product_numbers=["a"])
    assert "kräver minst 2 värden" in msg
    msg2 = _err(product_numbers=["a", "b", "c", "d", "e", "f"])
    assert "högst 5 värden" in msg2


def test_value_error_message_not_double_prefixed() -> None:
    # A custom field_validator message is a complete Swedish sentence; it must
    # NOT be prefixed with the field label ("sökfråga sökfrågan ...").
    msg = _err(query="   ")
    assert "sökfrågan får inte vara tom" in msg
    assert "sökfråga sökfrågan" not in msg


def test_starts_with_swedish_lead_and_no_pydantic_url() -> None:
    msg = _err(limit=0)
    assert msg.startswith("Ogiltig inmatning:")
    assert "pydantic.dev" not in msg
    assert "inp." not in msg


def test_prefix_regex_strips_fastmcp_wrapper() -> None:
    raw = "Error calling tool 'get_product': hittar ingen produkt som matchar: 00000000"
    assert _PREFIX_RE.sub("", raw) == "hittar ingen produkt som matchar: 00000000"
    # leaves a non-wrapped message untouched
    assert _PREFIX_RE.sub("", "okänd butik: 9999") == "okänd butik: 9999"


def _to_swedish_contains(msg: str, *needles: str) -> bool:
    return all(n in msg for n in needles)
