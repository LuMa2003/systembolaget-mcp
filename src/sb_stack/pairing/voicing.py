"""Swedish, user-facing text for pairing output.

The engine returns deterministic Swedish copy grounded in the product's own
`usage` sentence plus the structured fit. A downstream voicing LLM may rewrite
it, but the engine must never emit English, never leak the cosine float, and
never just restate the product's taste_symbols verbatim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sb_stack.pairing.profile import DishProfile

if TYPE_CHECKING:
    from sb_stack.pairing.scoring import Candidate

# Human-readable Swedish phrasing for each taste_symbol the engine infers.
_SYMBOL_PHRASE: dict[str, str] = {
    "Fisk": "fisk",
    "Skaldjur": "skaldjur",
    "Fläsk": "fläskkött",
    "Nöt": "nötkött",
    "Lamm": "lammkött",
    "Fågel": "fågel och ljust kött",
    "Vilt": "vilt",
    "Ost": "ost",
    "Dessert": "dessert och sötsaker",
    "Grönsaker": "vegetariska rätter",
    "Kryddstarkt": "kryddstarka rätter",
    "Asiatiskt": "asiatiska smaker",
    "Aperitif": "aperitif",
    "Sällskapsdryck": "sällskap",
}


def _trim_usage(usage: str | None) -> str | None:
    if not usage:
        return None
    text = usage.strip()
    # Strip a leading serving-temperature clause so the pairing reason leads.
    lower = text.lower()
    if lower.startswith("serveras"):
        for sep in (" till ", " som "):
            idx = lower.find(sep)
            if idx != -1:
                rest = text[idx + 1 :].strip()
                return rest[0].upper() + rest[1:] if rest else text
    return text


def build_why(candidate: Candidate, profile: DishProfile) -> str:
    """A natural Swedish one-liner: why this drink fits the dish."""
    p = candidate.product
    usage = _trim_usage(candidate.usage_text)

    matched = [s for s in profile.symbols if s in set(p.taste_symbols)]
    fit_clause: str | None = None
    if matched:
        phrases = [_SYMBOL_PHRASE.get(s, s.lower()) for s in matched]
        fit_clause = f"passar bra till {_join_sv(phrases)}"

    if usage:
        if fit_clause:
            return f"{usage} {_capitalize(fit_clause)}."
        return usage
    if fit_clause:
        return f"{_capitalize(fit_clause)}."

    # Last resort: lean on body fit / a neutral note (still Swedish).
    if profile.body_target is not None and p.taste_clocks.body is not None:
        if p.taste_clocks.body >= 8:
            return "Ett fylligt val som matchar rättens kraft."
        return "Ett lättare val som inte tar över rätten."
    return "Ett mångsidigt val som fungerar till maträtten."


def build_dish_summary(profile: DishProfile) -> str:
    if not profile.has_signal:
        return f"Kunde inte tolka «{profile.raw_dish}» som en maträtt med tydlig profil."
    parts: list[str] = []
    if profile.symbols:
        phrases = [_SYMBOL_PHRASE.get(s, s.lower()) for s in profile.symbols]
        parts.append(f"Rätten kopplas till {_join_sv(phrases)}")
    if profile.body_target is not None:
        if profile.body_target >= 8:
            parts.append("och pekar mot en fyllig, kraftig dryck")
        elif profile.body_target <= 5:
            parts.append("och pekar mot en lättare, friskare dryck")
        else:
            parts.append("och pekar mot en medelfyllig dryck")
    return (" ".join(parts) + ".") if parts else f"«{profile.raw_dish}»."


def build_sommelier_reasoning(profile: DishProfile, confidence: str) -> str:
    if not profile.has_signal:
        return (
            "Rätten gick inte att tolka, så detta är tre breda och säkra val "
            "snarare än en träffsäker rekommendation."
        )
    base: str
    if profile.symbols:
        phrases = [_SYMBOL_PHRASE.get(s, s.lower()) for s in profile.symbols]
        base = (
            f"Förslagen är valda för att matcha {_join_sv(phrases)} "
            "med utgångspunkt i Systembolagets sommeliertexter."
        )
    else:
        base = (
            "Förslagen utgår från rättens karaktär och Systembolagets "
            "sommeliertexter."
        )
    if confidence == "low":
        base += " Träffsäkerheten är låg — se det som olika vägar att prova."
    elif confidence == "medium":
        base += " Träffsäkerheten är hyfsad — det kan passa, värt att prova."
    return base


def build_inferred_profile(profile: DishProfile) -> str:
    if profile.body_target is None:
        return "Ingen tydlig kroppsprofil kunde härledas."
    if profile.body_target >= 8:
        body = "fyllig kropp (8–12)"
    elif profile.body_target <= 5:
        body = "lätt kropp (0–5)"
    else:
        body = "medelfyllig kropp (5–8)"
    syms = ", ".join(profile.symbols) if profile.symbols else "inga"
    return f"Målprofil: {body}; matsymboler: {syms}."


def _capitalize(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _join_sv(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " och " + items[-1]
