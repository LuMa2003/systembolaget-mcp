"""Unit tests for the structured-rerank pairing logic (profile/scoring/voicing).

These cover the engine internals added for the audit fixes (#2/#3/#4/#8)
without needing a GPU, embed server, or DB — they operate on Product models
and Candidate dataclasses directly.
"""

from __future__ import annotations

from sb_stack.mcp_server.responses import Product, TasteClocks
from sb_stack.pairing.profile import infer_profile
from sb_stack.pairing.scoring import (
    Candidate,
    compute_confidence,
    diversify,
    score_candidates,
)
from sb_stack.pairing.voicing import build_dish_summary, build_why


def _prod(
    pn: str,
    *,
    name: str = "Provdryck",
    cat1: str = "Vin",
    cat2: str = "Rött vin",
    symbols: list[str] | None = None,
    body: int | None = None,
) -> Product:
    return Product(
        product_number=pn,
        name_bold=name,
        category_level_1=cat1,
        category_level_2=cat2,
        taste_symbols=symbols or [],
        taste_clocks=TasteClocks(body=body),
    )


def _cand(product: Product, *, usage: str | None = None, sim: float = 0.5) -> Candidate:
    return Candidate(product=product, usage_text=usage, similarity=sim)


# ── profile inference (#3) ──────────────────────────────────────────────────


def test_infer_profile_pork_symbol() -> None:
    p = infer_profile("fläskfilé med rotfrukter")
    assert "Fläsk" in p.symbols
    assert p.has_signal is True


def test_infer_profile_body_from_context() -> None:
    p = infer_profile("oxfilé", meal_context="jag vill ha något kraftigt")
    assert "Nöt" in p.symbols
    assert p.body_target == 10  # "kraftig" pushes body high


def test_infer_profile_nonsense_has_no_signal() -> None:
    p = infer_profile("betong och asfalt")
    assert p.symbols == []
    assert p.has_signal is False


def test_infer_profile_honours_hint() -> None:
    p = infer_profile("svamprisotto", taste_symbols_hint=["Fisk"])
    assert "Fisk" in p.symbols


# ── scoring (#2) ────────────────────────────────────────────────────────────


def test_scoring_promotes_symbol_match_over_offcategory_spirit() -> None:
    profile = infer_profile("oxfilé")
    wine = _cand(_prod("1", cat1="Vin", cat2="Rött vin", symbols=["Nöt"], body=10), sim=0.45)
    spirit = _cand(_prod("2", cat1="Sprit", cat2="Whisky", symbols=[], body=None), sim=0.60)
    ranked = score_candidates([spirit, wine], profile)
    # Despite a higher raw cosine, the off-category spirit ranks below the
    # Nöt-tagged wine (symbol match + category prior).
    assert ranked[0].product.product_number == "1"


def test_scoring_name_overlap_guard_flags_lexical_trap() -> None:
    profile = infer_profile("oxfilé")
    trap = _cand(_prod("9", name="Oxfilé Lager", cat1="Öl", cat2="Ljus lager", symbols=[]), sim=0.8)
    score_candidates([trap], profile)
    assert trap.name_overlap is True
    # guarded: total halved because name echoes the dish with no symbol support
    assert trap.total < 0.45 * 0.8


# ── confidence (#8) ─────────────────────────────────────────────────────────


def test_confidence_low_for_nonsense() -> None:
    profile = infer_profile("betong och asfalt")
    cand = _cand(_prod("1", symbols=[]), sim=0.9)
    score_candidates([cand], profile)
    assert compute_confidence([cand], profile) == "low"


def test_confidence_medium_for_single_symbol_match() -> None:
    profile = infer_profile("lax")
    cand = _cand(_prod("1", cat2="Vitt vin", symbols=["Fisk"]), sim=0.5)
    score_candidates([cand], profile)
    assert compute_confidence([cand], profile) == "medium"


def test_confidence_high_for_many_strong_matches() -> None:
    profile = infer_profile("fläskfilé")
    cands = [_cand(_prod(str(i), symbols=["Fläsk"]), sim=0.6) for i in range(5)]
    score_candidates(cands, profile)
    assert compute_confidence(cands, profile) == "high"


# ── diversity (#8b) ─────────────────────────────────────────────────────────


def test_diversify_respects_floor_and_dedups_top_picks() -> None:
    a = _cand(_prod("a", cat2="Rött vin"))
    b = _cand(_prod("b", cat2="Rött vin"))
    c = _cand(_prod("c", cat2="Vitt vin"))
    weak = _cand(_prod("d", cat2="Mousserande vin"))
    a.total, b.total, c.total, weak.total = 0.8, 0.7, 0.6, 0.1
    picked = diversify([a, b, c, weak], limit=3, relevance_floor=0.3)
    pns = [x.product.product_number for x in picked]
    assert "d" not in pns  # below the relevance floor
    buckets = [x.product.category_level_2 for x in picked]
    assert len(set(buckets[:2])) == 2  # two distinct buckets surface first


# ── voicing (#4) ────────────────────────────────────────────────────────────


def test_build_why_grounds_in_usage_and_hides_cosine() -> None:
    profile = infer_profile("fläskfilé")
    cand = _cand(
        _prod("1", symbols=["Fläsk"]),
        usage="Serveras vid 18°C till rätter av fläsk- eller lammkött.",
        sim=0.731,
    )
    why = build_why(cand, profile)
    assert "fläsk" in why.lower()
    assert "0.7" not in why and "0.73" not in why  # no cosine leak
    assert "serveras vid" not in why.lower()  # serving-temp clause trimmed


def test_build_dish_summary_signals_uninterpretable_dish() -> None:
    summary = build_dish_summary(infer_profile("betong och asfalt"))
    assert "Kunde inte tolka" in summary
