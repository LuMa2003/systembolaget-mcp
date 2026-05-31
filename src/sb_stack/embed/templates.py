"""Category-specific embedding templates.

Each `category_level_1` has its own template so the embedding space
separates a wine's taste clocks from a spirit's raw material. The
returned text is hashed (`source_hash`) to drive the re-embed decision;
changing a template bumps its version so every row in that category
re-embeds on the next sync.

See docs/09_embedding_service.md §Template + hashing.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

# Each template references fields that exist on the `products` table.
# Missing fields render as empty strings via `defaultdict(str, ...)`.

TEMPLATE_WINE = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1} {origin_level_2}
{category_level_2} / {category_level_3}
{grapes}
Årgång: {vintage}
{color}
{taste}
{aroma}
{usage}
Passar till: {taste_symbols}"""

TEMPLATE_BEER = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1}
{category_level_2} / {category_level_3}
Alkohol: {alcohol_percentage}%
{taste}
{aroma}
{clock_tokens}
{usage}
Passar till: {taste_symbols}"""

TEMPLATE_SPIRIT = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1}
{category_level_2} / {category_level_3}
{raw_material}
Alkohol: {alcohol_percentage}%
{taste}
{aroma}
{clock_tokens}
{usage}
Passar till: {taste_symbols}"""

TEMPLATE_CIDER = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1}
{category_level_2} / {category_level_3}
{raw_material}
Alkohol: {alcohol_percentage}%
{taste}
{aroma}
{usage}
Passar till: {taste_symbols}"""

TEMPLATE_ALCOHOLFREE = """{name_bold} {name_thin}
{producer_name}
{country} {origin_level_1}
{category_level_2} / {category_level_3}
{raw_material}
{taste}
{aroma}
{usage}
Passar till: {taste_symbols}"""


# Maps category_level_1 → (template_version, template_string).
# beer/spirit versions bumped to *_v2 to inject structured taste-clock tokens
# (smokiness/body) so the embedder sees a strong lexical signal even when the
# free-text taste/aroma is sparse. Bump forces a re-embed of those categories.
TEMPLATES: dict[str, tuple[str, str]] = {
    "Vin": ("wine_v1", TEMPLATE_WINE),
    "Öl": ("beer_v2", TEMPLATE_BEER),
    "Sprit": ("spirit_v2", TEMPLATE_SPIRIT),
    "Cider & blanddrycker": ("cider_v1", TEMPLATE_CIDER),
    "Alkoholfritt": ("alcoholfree_v1", TEMPLATE_ALCOHOLFREE),
    # Presentartiklar deliberately omitted — we don't embed gift items.
}


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def _as_clock(value: Any) -> int | None:
    """Coerce a taste-clock cell to an int, or None when absent/unparseable.

    Clocks are stored on a roughly 1–12 scale (smokiness 1–11, body 2–12).
    """
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _clock_band(value: int, low: str, mid: str, high: str) -> str | None:
    """Map a 1–12 clock into a low/mid/high Swedish band (None for the floor)."""
    if value >= 9:
        return high
    if value >= 6:
        return mid
    if value >= 3:
        return low
    return None


def _clock_tokens(product: Mapping[str, Any]) -> str:
    """Render structured taste clocks as Swedish NL tokens.

    Free-text taste fields often omit the dominant character (e.g. an Islay
    whose `taste` is sparse but whose smokiness clock is 11). Surfacing the
    numeric clocks as words gives the embedder a hard lexical anchor.
    """
    tokens: list[str] = []
    smoke = _as_clock(product.get("taste_clock_smokiness"))
    if smoke is not None:
        band = _clock_band(smoke, "Lätt rökig", "Tydligt rökig", "Mycket rökig")
        if band is not None:
            tokens.append(band)
    body = _as_clock(product.get("taste_clock_body"))
    if body is not None:
        band = _clock_band(body, "Lätt", "Medelfyllig", "Fyllig")
        if band is not None:
            tokens.append(band)
    return ", ".join(tokens)


def render(product: Mapping[str, Any]) -> tuple[str, str] | None:
    """Render the embedding text for `product`.

    Returns (text, template_version) or None if the product's category has
    no template (e.g. Presentartiklar).
    """
    entry = TEMPLATES.get(str(product.get("category_level_1", "")))
    if entry is None:
        return None
    version, template = entry
    # defaultdict gives empty strings for any template placeholder not in the
    # product dict; str.format_map accepts a Mapping.
    stringified: dict[str, str] = {k: _stringify(v) for k, v in product.items()}
    stringified["clock_tokens"] = _clock_tokens(product)
    text = template.format_map(defaultdict(str, stringified))
    # Collapse runs of whitespace the templates introduce when fields are blank,
    # then strip. The embedding model doesn't need cosmetic whitespace.
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned, version
