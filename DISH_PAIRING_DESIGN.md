# Dish Pairing Engine — Design Document

Last revised: 2026-04-19. Self-contained; does not assume MCP hosting — can be lifted into a standalone web app, CLI, API, or mobile app later.

---

## 1. Vision

Give a non-expert home cook an expert-grade drink recommendation for any dish they describe, in their own words. Output includes the "why", not just the pick, so the user learns.

Primary differentiation vs. existing pairing tools (Vivino, Wine Searcher, generic LLMs):

- Grounded in **27,000+ real, locally-available products** with live stock at user's home stores. No fantasy recommendations for bottles they can't buy in Sweden.
- Leverages **Systembolaget's sommelier-written `usage` field** per product — most pairing tools invent pairings; this one retrieves human-written ones.
- Culturally fluent in Swedish food traditions (julbord, midsommar, surströmming, kräftskiva, fredagsmys).

---

## 2. The unfair advantage: Systembolaget's `usage` field

Every curated Systembolaget product has a `usage` field written by their sommeliers, typically 1–3 sentences in Swedish. Examples:

- *"Passar till lammsadel, vilt eller lagrad hårdost."*
- *"Till fet fisk, rökt lax eller asiatiska rätter med sötma och syra."*
- *"Serveras gärna som aperitif eller till ostron och fina skaldjur."*
- *"Till kraftigt tillagade kötträtter, lamm, vilt eller lagrad hårdost."*

**Semantic retrieval over this field outperforms formulaic rule-based pairing** because we're not inventing — we're finding products a human expert already paired with something similar to the user's dish.

This is the core architectural bet. Everything else is scaffolding to make this work well.

---

## 3. Pairing theory — the 8 axes

Good pairing balances eight dimensions simultaneously:

| Axis | What it does | Data signal |
|---|---|---|
| **Body match** | Dish weight ↔ drink weight. Mismatch = one bulldozes the other. | `taste_clock_body` 0–12 |
| **Acidity balance** | Acidic food needs wine with ≥ the food's acidity, else wine goes flat. | `taste_clock_fruitacid` 0–12 |
| **Sweetness balance** | Dessert wine must exceed dish sweetness. Sweet wine tames spicy heat. | `taste_clock_sweetness` 0–12 |
| **Tannin × protein/fat** | Tannic reds soften with fat and protein; clash metallically with fish. | `taste_clock_roughness` 0–12 |
| **Sauce dominance** | The sauce rules 70% of the time, not the protein. | LLM dish parsing + `usage` match |
| **Cooking method** | Grill → smoke/char; braise → richness; raw → delicacy. | `taste_clock_smokiness`, `usage` text |
| **Regional affinity** | "What grows together goes together" — a cheap, reliable shortcut. | `country`, `origin_level_1/2` |
| **Meal context** | Aperitif ≠ main ≠ cheese ≠ dessert ≠ digestif. Same ingredient, different answer. | `taste_symbols` (Aperitif, Dessert, Ost, Avec/digestif) |

---

## 4. The sauce-dominance principle (non-obvious, critical)

Most pairing tools treat a dish as `[protein] + [cooking method]` and miss that the *sauce* usually drives the pairing. Examples:

| Dish | Naive pairing | Correct pairing | Why |
|---|---|---|---|
| Oxfilé med rödvinssås | "beef → full red" | full red with tannin and fruit to match sauce depth | sauce signals richness beyond the beef |
| Kyckling med gräddsås | "chicken → light white" | rich/oaked white (Chardonnay, Viognier) | creamy sauce demands body |
| Torsk med brynt smör | "fish → lean white" | round, unoaked white (Chardonnay, Pinot Gris) | butter demands fat/mouthfeel |
| Pasta med tomatsås | "Italian red" | medium red with *fresh acidity* (Chianti) | tomato's acidity dictates |
| Lax, kokt potatis | "fish → white" | crisp, high-acid white (Riesling, Sancerre) | salmon fat needs cutting |

**Implementation:** delegate dish decomposition to the LLM rather than hardcoding a sauce taxonomy. The pairing engine exposes an optional `dominant_component_hint` parameter so the LLM (or a future rule-based caller) can explicitly override what drives the pairing.

---

## 5. Data inventory

Fields used (all available per-product from Systembolaget's API; see `project_systembolaget_scraper.md` for fetch details):

**Structured (for filtering and scoring):**
- `category_level_1/2/3` — Vin/Öl/Sprit and substyles
- `country`, `origin_level_1/2` — regional affinity
- `alcohol_percentage`, `volume_ml`, `price_incl_vat`, `comparison_price`
- `taste_clock_body`, `_bitter`, `_sweetness`, `_fruitacid`, `_roughness`, `_smokiness`, `_casque` — all 0–12
- `taste_symbols` — list of `[Aperitif, Asiatiskt, Avec/digestif, Buffémat, Dessert, Fisk, Fläsk, Fågel, Grönsaker, Kryddstarkt, Lamm, Nöt, Ost, Skaldjur, Sällskapsdryck, Vilt]`
- `grapes`, `vintage`, `color`
- `assortment_text` (Fast sortiment = widely curated; Bs = ordervara not tasted by Systembolaget)
- `is_organic`, `is_vegan_friendly`, `is_gluten_free`, `is_kosher`, dietary flags

**Text (for semantic match):**
- `usage` — **the primary pairing signal**
- `taste`, `aroma` — flavor descriptors
- `producer_description`, `production`, `cultivation_area`, `harvest`, `soil`, `storage`

**Availability:**
- `is_completely_out_of_stock`, `is_temporary_out_of_stock`, `is_discontinued`
- Per-store stock and shelf location (for "is it at my local store")

**Products to deprioritize:**
- Those with empty `usage` (mostly `is_bs_assortment` = ordervaror — Systembolaget hasn't tasted them)
- Those marked `is_discontinued` (keep but flag)
- Those where `is_completely_out_of_stock` is true (include with badge, don't filter)

---

## 6. Architecture

### Pipeline

```
User dish string (Swedish or English)
           │
           ▼
┌─────────────────────────────────────┐
│  LLM pre-analysis (external to      │
│  engine — the caller's job)         │
│                                      │
│  Decomposes dish into:               │
│  - dominant_component (e.g. sauce)   │
│  - likely taste_symbols              │
│  - flavor profile hints              │
│  - cultural/traditional tag          │
└──────────────┬───────────────────────┘
               │ (optional; engine works without it)
               ▼
┌──────────────────────────────────────┐
│  Pairing engine (this doc)           │
│                                      │
│  1. Build query embedding from       │
│     dish text + hints                │
│  2. Filter candidates by dietary,    │
│     price, availability              │
│  3. Score each candidate on 6        │
│     signals (see §7)                 │
│  4. Rank by weighted sum             │
│  5. Enforce diversity (MMR)          │
│  6. Compute confidence               │
│  7. Return structured breakdown      │
└──────────────┬───────────────────────┘
               │
               ▼
┌──────────────────────────────────────┐
│  Voicing layer (LLM again — caller)  │
│                                      │
│  Takes structured score_breakdown    │
│  and renders human-quality Swedish   │
│  explanations. Engine does NOT       │
│  generate final natural-language     │
│  copy.                               │
└──────────────────────────────────────┘
```

### Why this separation

- Engine is deterministic, testable, cheap, local.
- LLM handles the two tasks where it has a real edge: parsing free-text dishes (lots of world knowledge) and writing fluent Swedish explanations.
- Either layer can be replaced without touching the other. If embeddings improve, swap them. If a better LLM ships, swap it.

---

## 7. Scoring formula

Each candidate product gets six signal scores normalized to `[0, 1]`, then combined by a weighted sum whose weights depend on `style_preference`.

### Signal definitions

| Signal | Computation | Range |
|---|---|---|
| `usage_text_match` | cosine similarity between query embedding and product's `usage` field embedding | 0–1 |
| `taste_clock_fit` | Gaussian-distance fit between product's 7 taste-clock values and the target profile (inferred from dish) | 0–1 |
| `taste_symbol_match` | fraction of dish-inferred `taste_symbols` present in product's `taste_symbols` list | 0–1 |
| `regional_match` | 1.0 if dish cuisine's native country == product country, 0.5 if same continent/region, 0 otherwise | 0–1 |
| `style_match` | matches `style_preference`: classic → boost Fast sortiment, adventurous → boost `is_limited_edition` or rare, etc. | 0–1 |
| `price_fit` | Gaussian centered on user's price sweet spot if given; otherwise favors `comparison_price` (kr/L) as value proxy | 0–1 |

### Weights by style

| Style | usage | symbol | clock | regional | style | price |
|---|---|---|---|---|---|---|
| `classic` | 0.35 | 0.25 | 0.20 | 0.10 | 0.05 | 0.05 |
| `balanced` (default) | 0.30 | 0.20 | 0.25 | 0.10 | 0.10 | 0.05 |
| `adventurous` | 0.40 | 0.10 | 0.20 | 0.15 | 0.10 | 0.05 |
| `budget` | 0.25 | 0.20 | 0.20 | 0.05 | 0.05 | 0.25 |

Notes:
- `adventurous` also applies a small penalty (`-0.05`) for products appearing in the top 100 by `available_number_of_stores` (proxy for over-represented/common picks).
- `classic` applies a bonus (`+0.05`) when product is in `Fast sortiment` (Systembolaget's core curated range).
- `budget` uses `comparison_price` (kr/L) not raw price — accounts for box wine etc.

### Diversity (MMR)

After ranking, if `diversity=true`, apply Maximal Marginal Relevance:

```
result = []
while len(result) < limit:
    best = argmax over remaining candidates of:
        λ * pairing_score(c) − (1−λ) * max similarity(c, r) for r in result
    result.append(best)
```

With `λ = 0.7`. Enforces top-N has ≥3 distinct `category_level_3` values and ≥3 distinct countries where possible.

---

## 8. Tool contract (stable API)

Works whether called from MCP, HTTP, CLI, or library.

### Input

```
pair_with_dish

dish                       string            required
                                              -- free text, Swedish or English
                                              -- e.g. "oxfilé med rödvinssås"

meal_context               enum              default "main"
                                              -- aperitif | appetizer | main |
                                                 cheese_course | dessert |
                                                 digestif | casual_snack | buffet

categories                 string[]          default = context-appropriate
                                              -- ["Vin"] | ["Vin","Öl"] | ...

style_preference           enum              default "balanced"
                                              -- classic | balanced |
                                                 adventurous | budget

price_range                {min?, max?}      optional

dietary                    object            optional
                                              -- is_vegan, is_organic,
                                                 is_alcohol_free, is_gluten_free

dominant_component_hint    string            optional
                                              -- LLM's read of what drives
                                                 the pairing
                                                 e.g. "rödvinssåsen"

cultural_tag               string            optional
                                              -- "julbord" | "midsommar" |
                                                 "kräftskiva" | "surströmming" |
                                                 "fredagsmys" | "påsk"

in_stock_at                string            optional
                                              -- siteId | "main" | "home" | null

limit                      integer           default 8
diversity                  boolean           default true
include_alternative_category boolean         default true
                                              -- also suggest an off-category
                                                 alternative (e.g. a beer
                                                 when the main pick is wine)
```

### Output

```
{
  "interpretation": {
    "dish_summary": string,
      -- "Fet fisk, lätt tillagad, nordiskt."
    "dominant_component": string,
      -- "Laxens fetma"
    "inferred_profile": {
      "target_body": [min, max],
      "target_acidity": [min, max],
      "target_sweetness": [min, max],
      "target_tannin": [min, max],
      "target_smokiness": [min, max],
      "key_taste_symbols": string[],
      "regional_affinity": string[],
      "confidence": "high" | "medium" | "low"
    },
    "sommelier_reasoning": string,
      -- 2–3 sentence Swedish explanation
    "alternative_considerations": string[]
      -- "Man kan också gå mot en lätt rosé..."
  },
  "recommendations": [
    {
      "product": { ... standard Product object ... },
      "pairing_score": 0.82,
      "score_breakdown": {
        "usage_text_match": 0.91,
        "taste_clock_fit": 0.75,
        "taste_symbol_match": 1.0,
        "regional_match": 0.5,
        "style_match": 0.8,
        "price_fit": 0.7
      },
      "why": string
        -- human-friendly short explanation
        -- (engine returns basic template;
        --  caller's LLM rewrites to fluent Swedish)
    }
  ],
  "alternative_category": {
    "category": "Öl",
    "recommendation": { ... Product ... },
    "why": "En krispig pilsner är också ett klassiskt val..."
  } | null
}
```

---

## 9. Cultural / holiday pairings (curated lookup)

Maintained as a small data file. When `cultural_tag` is supplied or the dish text matches a trigger keyword, the engine injects pre-chosen pairings alongside the normal ranking.

Rationale: cultural pairings carry more weight than compositional theory. *"Snaps to surströmming"* isn't because of chemistry — it's tradition, and ignoring it makes the tool feel foreign.

Initial ~20 entries:

| Tag | Triggers (keywords) | Traditional drinks | Notes |
|---|---|---|---|
| `julbord` | julbord, julmat, julmiddag | Aquavit/snaps · julmust · off-dry Riesling · light red (Pinot Noir) · julöl | Serve a trio; snaps is the core. |
| `midsommar` | midsommar, midsommarafton, sillbord | Snaps · light beer · crisp dry Riesling · rosé | Herring-heavy, snaps is essential. |
| `kräftskiva` | kräftskiva, kräftor, räkor-och-dill | Snaps (especially dill-flavored) · Chardonnay · light lager | Crayfish is cultural, not just "skaldjur". |
| `surströmming` | surströmming | Snaps (Skåne akvavit traditional) · cold pilsner · brännvin | Low confidence badge always. |
| `fredagsmys` | fredagsmys, fredagskväll, mysigt hemma | Casual: Chianti/Rioja · pale ale/IPA · light cocktails | Sällskapsdryck taste_symbol. |
| `påsk` | påsk, påskmat, påskbord | Riesling · Grüner Veltliner · light red · påskmust | Egg/herring-led. |
| `valborg` | valborg, siste-april | Snaps · beer · rhubarb cider | Casual outdoor. |
| `skärtorsdag` | skärtorsdag | Riesling · cava · light red | Fish-heavy tradition. |
| `kanelbullens-dag` | kanelbullar, fika | Moscato d'Asti · sweet cider · vin santo | Fika context. |
| `ostbricka` | ostbricka, osttallrik, ostprovning | Port (blue cheese) · dry sherry (hard cheese) · fresh white (soft cheese) | Returns trio. |
| `tapas` | tapas, spansk meze | Fino/manzanilla sherry · cava · tempranillo | Regional. |
| `sushi` | sushi, nigiri, maki | Sake · Riesling Kabinett · dry Grüner Veltliner | Avoid tannic reds. |
| `ramen` | ramen, tonkotsu, shoyu | Dry/sparkling sake · Hefeweizen · off-dry Riesling | Umami-heavy. |
| `curry` | curry, indian mat | Off-dry Riesling · Gewürztraminer · IPA · lager | Sweetness tames heat. |
| `thai` | thai, pad-thai, tom-yum | Off-dry Riesling · Gewürztraminer · sparkling · pilsner | Avoid oaked wine. |
| `bbq` | bbq, grillat, grill | Zinfandel · Malbec · Syrah · porter · stout | Smoke-forward. |
| `pizza` | pizza, margherita, capricciosa | Chianti · Barbera · Lambrusco · Italian lager | Casual Italian. |
| `brunch` | brunch, frukost-sent | Cava · mimosa · Riesling · weissbier | Light, refreshing. |
| `choklad` | choklad, mörk-choklad | Port · PX sherry · bourbon · stout · Banyuls | Sweetness > chocolate. |
| `glass` | glass, sorbet, parfait | Moscato d'Asti · Sauternes · Icewine | Wine > dessert sweetness. |

**File format:** store as YAML or JSON in the app's `data/` directory so it's editable without redeploy. Schema:

```yaml
- tag: julbord
  triggers: [julbord, julmat, julmiddag]
  meal_context: buffet
  recommended:
    - taste_symbols_any: [Buffémat, Fisk, Fläsk]
      category_level_1: Sprit
      category_level_2: Kryddat brännvin
      note: "Snaps är kärnan i julbordet"
      weight: 1.0
    - category_level_1: Vin
      category_level_3: Torrt vitt, mycket syra och frisk  # Riesling-ish
      weight: 0.7
    - category_level_1: Vin
      category_level_3: Rött vin, fruktigt och lätt         # Pinot-ish
      weight: 0.5
```

The engine treats cultural matches as a pre-ranked list injected into the top-K before applying diversity.

---

## 10. Confidence model

Three declared levels. Always surfaced in the `interpretation.inferred_profile.confidence` field.

| Level | Criteria | UX behavior |
|---|---|---|
| **high** | Clean dish decomposition + ≥5 products with strong `usage` match (cosine > 0.7) + matching `taste_symbols` | Confident single ranked list. Top pick dominant. |
| **medium** | One signal is weak (either decomposition ambiguous, or low usage-text hits, or no clear symbol) | Ranked list but caller should soften language ("kan passa", "värt att prova"). |
| **low** | Fusion/rare/ambiguous dish, no strong signals, no cultural match | Returns 3 **diverse** options rather than a ranked winner. Caller should honestly say "här är tre olika vägar att gå". |

Caller (LLM) must respect confidence in phrasing. Engine never hallucinates confidence.

---

## 11. Edge cases to handle

| Input | Detection | Behavior |
|---|---|---|
| "nåt till fredagsmys" | meta-context, no specific food | Use `Sällskapsdryck` taste_symbol, casual context, broad selection |
| "middag för 10 med olika grejer" | buffet | `Buffémat` symbol, versatile wines |
| "vegansk curry" | dietary + spicy | Set dietary flag, off-dry white bias |
| "ostbricka" | cheese course | Returns 3 paired picks (soft/hard/blue) in one call |
| "pizza" | casual category | Italian-regional-affinity boost |
| "vin till tacos" | anglo-fusion | Off-dry Riesling or rosé; explain why not tempranillo |
| empty string | no input | Refuse politely; engine requires a dish |
| whisky query with meal_context=main | uncommon combo | Still honor request, but surface alternative_category |
| restriction conflict (no alcohol + julbord) | dietary override | Prefer alcohol-free alternatives, acknowledge tradition may not be possible |

---

## 12. Quality bar / definition of done

- 50 canonical Swedish dishes tested; ≥40 produce a recommendation the engine authors would serve at their own table.
- Confidence levels calibrated: `high` recommendations rated "good pick" by blind taste-tester ≥80%; `low` never presents a single confident winner.
- Cultural tags cover the top 15 Swedish holidays/traditions.
- P95 latency < 300 ms from dish to ranked list of 8 (embedding + DB filter + score + MMR).

---

## 13. What is deliberately out of scope (for now)

- **Multi-course meal orchestration.** One dish per call; the caller (LLM or UI) orchestrates flights.
- **Cocktail pairings beyond existing spirits.** No cocktail-recipe database.
- **User taste-profile learning.** No feedback loop yet; engine is stateless.
- **Cellar-aging recommendations** ("which of my picks would improve with 5 years?"). Out-of-scope data.
- **Non-Swedish markets.** Assumes Systembolaget's catalog. Extending to another market requires remapping `taste_symbols`, `taste_clocks`, cultural table.

---

## 14. Future evolution paths

If this becomes a standalone app:

**Short-term wins (months):**
- User accounts + feedback loop ("did this pairing work?") → personalized ranking weights.
- Image input: user photographs their plated dish; vision model describes it before pairing.
- Recipe-site integration: paste a URL (koket.se, ica.se, arla.se), scrape and pair.
- Shopping list: "pair this whole menu, then add all to shopping list" → output Systembolaget order.

**Medium-term (year):**
- Voice interface (Swedish STT → dish text → pairing → TTS explanation).
- Multi-course sommelier mode: user describes full menu, engine orchestrates wine flight.
- Cross-market: extend to Finland's Alko, Norway's Vinmonopolet (same monopoly-catalog data structure).
- Event planner: "I'm hosting 8 people, mixed preferences" → inventory suggestions.

**Longer-term (contemplative):**
- Learned pairing model: train a small ranker on "user chose X when we suggested X, Y, Z" data → personalized embeddings.
- Sommelier handoff: when confidence is low, surface option to ask a human sommelier (partner integration).
- Non-food contexts: pair drinks with occasions, moods, weather, music — similar engine, different inputs.

---

## 15. Open questions to re-examine if/when going standalone

1. **Monetization.** Affiliate model tricky given Systembolaget's no-ads policy. Subscription? Restaurant B2B? Data licensing?
2. **Data freshness SLA.** For MCP, daily sync is fine. For a public app, users expect live stock — need to decide if to call Systembolaget's API live per request or aggressively cache.
3. **Legal review.** Scraping via the public-frontend subscription key is currently tolerated for personal use. Any commercial launch requires conversation with Systembolaget (whose previous API takedowns suggest they may be receptive to licensing, or may not).
4. **Embedding model evolution.** Qwen3-Embedding-4B today; re-evaluate yearly. Having `model_name` and `source_hash` in the embeddings table (already specified in the main schema) makes swap cheap.
5. **Multi-language.** If expanding beyond Swedish users, translate `usage`/`taste` text once per product via LLM at sync time, store per-language embeddings, serve in user's language.

---

## 16. Minimal viable implementation checklist

For the MCP-embedded v1:

- [ ] Category-specific embedding templates applied during sync
- [ ] `pair_with_dish` tool with the full input/output contract above
- [ ] Score formula with all 6 signals, weighted per `style_preference`
- [ ] MMR diversity enforcement when `diversity=true`
- [ ] Cultural pairings data file with initial 20 entries
- [ ] Confidence level calculation
- [ ] Structured `score_breakdown` returned per product
- [ ] Engine never generates final natural-language "why" — caller does
- [ ] 50-dish regression test suite with expected-pairing assertions (fuzzy — category/country/body range, not specific product)
