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
TEMPLATES: dict[str, tuple[str, str]] = {
    "Vin": ("wine_v1", TEMPLATE_WINE),
    "Öl": ("beer_v1", TEMPLATE_BEER),
    "Sprit": ("spirit_v1", TEMPLATE_SPIRIT),
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
    text = template.format_map(defaultdict(str, stringified))
    # Collapse runs of whitespace the templates introduce when fields are blank,
    # then strip. The embedding model doesn't need cosmetic whitespace.
    lines = [line.strip() for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    return cleaned, version
