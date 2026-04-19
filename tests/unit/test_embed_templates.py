"""Unit tests for embedding text templates + source_hash."""

from __future__ import annotations

from sb_stack.embed.hashing import source_hash
from sb_stack.embed.templates import TEMPLATES, render


def test_render_wine_includes_key_fields() -> None:
    product = {
        "category_level_1": "Vin",
        "category_level_2": "Rött vin",
        "category_level_3": "Kraftigt & fruktigt",
        "name_bold": "Apothic Red",
        "name_thin": "",
        "producer_name": "Apothic",
        "country": "USA",
        "origin_level_1": "Kalifornien",
        "origin_level_2": "",
        "grapes": ["Zinfandel", "Syrah", "Merlot"],
        "vintage": "2022",
        "color": "Mörkröd",
        "taste": "Smakrikt och mjukt",
        "aroma": "Hallon och vanilj",
        "usage": "Passar till grillat",
        "taste_symbols": ["Grillat", "Kött"],
    }
    result = render(product)
    assert result is not None
    text, version = result

    assert version == "wine_v1"
    assert "Apothic Red" in text
    assert "Zinfandel, Syrah, Merlot" in text
    assert "Passar till: Grillat, Kött" in text
    assert "Kraftigt & fruktigt" in text
    # Blank-only lines are collapsed:
    assert "\n\n" not in text


def test_render_unknown_category_returns_none() -> None:
    assert render({"category_level_1": "Presentartiklar"}) is None
    assert render({"category_level_1": ""}) is None
    assert render({}) is None


def test_render_beer_uses_alcohol_percentage() -> None:
    text_version = render(
        {
            "category_level_1": "Öl",
            "name_bold": "Oppigårds",
            "name_thin": "Indian IPA",
            "producer_name": "Oppigårds Bryggeri",
            "country": "Sverige",
            "origin_level_1": "Dalarna",
            "category_level_2": "Starköl",
            "category_level_3": "Ljus lager",
            "alcohol_percentage": "5.8",
            "taste": "Humle",
            "aroma": "Citrus",
            "usage": "Till grill",
            "taste_symbols": ["Grillat"],
        }
    )
    assert text_version is not None
    text, version = text_version
    assert version == "beer_v1"
    assert "Alkohol: 5.8%" in text


def test_render_passes_missing_fields_through_as_blank() -> None:
    # Only the minimum — everything else missing. Must still render without
    # throwing KeyError.
    text_version = render(
        {"category_level_1": "Sprit", "name_bold": "Absolut", "name_thin": "Vodka"}
    )
    assert text_version is not None
    text, _ = text_version
    assert "Absolut Vodka" in text


def test_source_hash_is_stable_and_distinct() -> None:
    h1 = source_hash("hello")
    h2 = source_hash("hello")
    h3 = source_hash("hello world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # sha256 hex digest


def test_templates_cover_expected_categories() -> None:
    assert set(TEMPLATES).issuperset({"Vin", "Öl", "Sprit", "Cider & blanddrycker", "Alkoholfritt"})
    for version, template in TEMPLATES.values():
        assert version.endswith("_v1")
        assert "{name_bold}" in template
