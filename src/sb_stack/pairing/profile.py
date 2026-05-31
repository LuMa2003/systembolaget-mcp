"""Dish → taste-profile inference.

The pairing engine cannot re-embed, so it leans on a structured read of the
dish text to steer a re-rank over the embedding candidates: which
`taste_symbols` the dish implies, where the body target sits, and whether the
dish carries any usable signal at all (so we can fall back to diverse safe
options and an honest `low` confidence for nonsense input).

All keyword tables are Swedish-first because that is the primary use case;
a few common English food words are included as a courtesy.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# Canonical taste_symbols as they appear in the catalog.
SYMBOL_FISK = "Fisk"
SYMBOL_SKALDJUR = "Skaldjur"
SYMBOL_FLASK = "Fläsk"
SYMBOL_NOT = "Nöt"
SYMBOL_LAMM = "Lamm"
SYMBOL_FAGEL = "Fågel"
SYMBOL_VILT = "Vilt"
SYMBOL_OST = "Ost"
SYMBOL_DESSERT = "Dessert"
SYMBOL_GRONSAKER = "Grönsaker"
SYMBOL_KRYDDSTARKT = "Kryddstarkt"
SYMBOL_ASIATISKT = "Asiatiskt"
SYMBOL_APERITIF = "Aperitif"
SYMBOL_SALLSKAP = "Sällskapsdryck"

# Keyword → inferred taste_symbol(s). Keys are matched as whole-ish word stems
# against a normalised (accent-folded, lowercased) form of the dish text.
_KEYWORD_SYMBOLS: dict[str, tuple[str, ...]] = {
    # Pork
    "flask": (SYMBOL_FLASK,),
    "flaskfile": (SYMBOL_FLASK,),
    "kotlett": (SYMBOL_FLASK,),
    "karre": (SYMBOL_FLASK,),
    "skinka": (SYMBOL_FLASK,),
    "bacon": (SYMBOL_FLASK,),
    "revben": (SYMBOL_FLASK,),
    "pulled pork": (SYMBOL_FLASK,),
    # Beef
    "oxfile": (SYMBOL_NOT,),
    "oxkott": (SYMBOL_NOT,),
    "biff": (SYMBOL_NOT,),
    "not": (SYMBOL_NOT,),
    "notkott": (SYMBOL_NOT,),
    "entrecote": (SYMBOL_NOT,),
    "ryggbiff": (SYMBOL_NOT,),
    "hogrev": (SYMBOL_NOT,),
    "rostbiff": (SYMBOL_NOT,),
    "kottfars": (SYMBOL_NOT,),
    "hamburgare": (SYMBOL_NOT,),
    "beef": (SYMBOL_NOT,),
    "steak": (SYMBOL_NOT,),
    # Lamb
    "lamm": (SYMBOL_LAMM,),
    "lammstek": (SYMBOL_LAMM,),
    "lammsadel": (SYMBOL_LAMM,),
    "lammracks": (SYMBOL_LAMM,),
    # Fish
    "lax": (SYMBOL_FISK,),
    "torsk": (SYMBOL_FISK,),
    "fisk": (SYMBOL_FISK,),
    "sill": (SYMBOL_FISK,),
    "stromming": (SYMBOL_FISK,),
    "abborre": (SYMBOL_FISK,),
    "gos": (SYMBOL_FISK,),
    "tonfisk": (SYMBOL_FISK,),
    "rodspatta": (SYMBOL_FISK,),
    "salmon": (SYMBOL_FISK,),
    # Shellfish
    "rak": (SYMBOL_SKALDJUR,),
    "rakor": (SYMBOL_SKALDJUR,),
    "krabba": (SYMBOL_SKALDJUR,),
    "hummer": (SYMBOL_SKALDJUR,),
    "krafta": (SYMBOL_SKALDJUR,),
    "kraftor": (SYMBOL_SKALDJUR,),
    "skaldjur": (SYMBOL_SKALDJUR,),
    "musslor": (SYMBOL_SKALDJUR,),
    "ostron": (SYMBOL_SKALDJUR,),
    # Poultry
    "kyckling": (SYMBOL_FAGEL,),
    "fagel": (SYMBOL_FAGEL,),
    "kalkon": (SYMBOL_FAGEL,),
    "anka": (SYMBOL_FAGEL,),
    "chicken": (SYMBOL_FAGEL,),
    # Game
    "vilt": (SYMBOL_VILT,),
    "radjur": (SYMBOL_VILT,),
    "alg": (SYMBOL_VILT,),
    "hjort": (SYMBOL_VILT,),
    "ren": (SYMBOL_VILT,),
    "vildsvin": (SYMBOL_VILT,),
    "fasan": (SYMBOL_VILT,),
    # Cheese
    "ost": (SYMBOL_OST,),
    "ostbricka": (SYMBOL_OST,),
    "osttallrik": (SYMBOL_OST,),
    "ostprovning": (SYMBOL_OST,),
    # Dessert / sweets
    "choklad": (SYMBOL_DESSERT,),
    "dessert": (SYMBOL_DESSERT,),
    "tarta": (SYMBOL_DESSERT,),
    "kaka": (SYMBOL_DESSERT,),
    "glass": (SYMBOL_DESSERT,),
    "efterratt": (SYMBOL_DESSERT,),
    "pannacotta": (SYMBOL_DESSERT,),
    # Vegetables / vegetarian
    "vegetarisk": (SYMBOL_GRONSAKER,),
    "vegansk": (SYMBOL_GRONSAKER,),
    "gronsak": (SYMBOL_GRONSAKER,),
    "gronsaker": (SYMBOL_GRONSAKER,),
    "halloumi": (SYMBOL_GRONSAKER,),
    "svamp": (SYMBOL_GRONSAKER,),
    # Spicy / Asian
    "curry": (SYMBOL_KRYDDSTARKT, SYMBOL_ASIATISKT),
    "sushi": (SYMBOL_FISK, SYMBOL_ASIATISKT),
    "thai": (SYMBOL_KRYDDSTARKT, SYMBOL_ASIATISKT),
    "ramen": (SYMBOL_ASIATISKT,),
    "wok": (SYMBOL_ASIATISKT,),
    "asiatisk": (SYMBOL_ASIATISKT,),
    "chili": (SYMBOL_KRYDDSTARKT,),
    "stark": (SYMBOL_KRYDDSTARKT,),
}

# Adjectives that push the body target up or down. Matched against the combined
# normalised dish + meal_context text.
_HIGH_BODY_WORDS: tuple[str, ...] = (
    "kraftig",
    "mustig",
    "fyllig",
    "rik",
    "smakrik",
    "grillat",
    "grillad",
    "tung",
    "robust",
    "rodvinssas",
    "vilt",
)
_LOW_BODY_WORDS: tuple[str, ...] = (
    "latt",
    "fralsch",
    "frasch",
    "frisk",
    "delikat",
    "fin",
    "syrlig",
    "sommar",
)

# Symbols that legitimately call for spirits / liqueurs (so the category prior
# does not penalise them in those contexts).
_SPIRIT_FRIENDLY_SYMBOLS: frozenset[str] = frozenset(
    {SYMBOL_DESSERT, SYMBOL_APERITIF}
)


@dataclass
class DishProfile:
    """Structured read of a dish, used to re-rank embedding candidates."""

    raw_dish: str
    normalized: str
    symbols: list[str] = field(default_factory=list)
    body_target: int | None = None  # 0..12, or None when unknown
    spirit_friendly: bool = False
    has_signal: bool = False  # False ⇒ nonsense / no inferable food intent
    dish_words: set[str] = field(default_factory=set)


def _normalize(text: str) -> str:
    """Lowercase + strip diacritics so 'Fläskfilé' matches the 'flaskfile' key."""
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def _content_words(normalized: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", normalized) if len(w) >= 3}


def infer_profile(
    dish: str,
    meal_context: str | None = None,
    taste_symbols_hint: list[str] | None = None,
) -> DishProfile:
    """Infer taste_symbols + a body target from the dish and its context.

    `meal_context` is used purely for body/symbol inference here — it is NOT
    concatenated into the embedding query, which previously polluted ranking
    with stray words like "vänner".
    """
    norm_dish = _normalize(dish)
    norm_ctx = _normalize(meal_context) if meal_context else ""
    combined = f"{norm_dish} {norm_ctx}".strip()

    symbols: list[str] = []
    for sym in taste_symbols_hint or []:
        if sym not in symbols:
            symbols.append(sym)

    # Longer keys first so multi-word stems ("pulled pork") win over substrings.
    for key in sorted(_KEYWORD_SYMBOLS, key=len, reverse=True):
        if key in combined:
            for sym in _KEYWORD_SYMBOLS[key]:
                if sym not in symbols:
                    symbols.append(sym)

    high = any(w in combined for w in _HIGH_BODY_WORDS)
    low = any(w in combined for w in _LOW_BODY_WORDS)
    body_target: int | None = None
    if high and not low:
        body_target = 10
    elif low and not high:
        body_target = 4
    elif symbols:
        # Derive a sensible default body from the dominant protein.
        body_target = _default_body_for(symbols)

    spirit_friendly = any(s in _SPIRIT_FRIENDLY_SYMBOLS for s in symbols)

    has_signal = bool(symbols) or high or low
    return DishProfile(
        raw_dish=dish.strip(),
        normalized=norm_dish,
        symbols=symbols,
        body_target=body_target,
        spirit_friendly=spirit_friendly,
        has_signal=has_signal,
        dish_words=_content_words(norm_dish),
    )


def _default_body_for(symbols: list[str]) -> int:
    heavy = {SYMBOL_NOT, SYMBOL_VILT, SYMBOL_LAMM, SYMBOL_FLASK}
    light = {SYMBOL_FISK, SYMBOL_SKALDJUR}
    if any(s in heavy for s in symbols):
        return 9
    if any(s in light for s in symbols):
        return 4
    if SYMBOL_DESSERT in symbols:
        return 6
    return 6
