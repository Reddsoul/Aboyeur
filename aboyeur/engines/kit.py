"""Macro Kit engine — build a mix-and-match grocery template from macros.

A kit is not a menu, and this is not the menu optimizer. A menu answers
"what do I cook on Tuesday"; a kit answers "what do I buy so that any bowl I
throw together lands on my macros". Different deliverable, different data
(hand-curated whole foods, not the recipe corpus), different math (a
closed-form portion solve, not a greedy combinatorial search). The two paths
share nothing but the USDA lookup, the allergen expander, and macro_fit's
scoring shape — deliberately.

Three stages:

  A. roster  — filter the component library by diet / allergies / excludes,
               resolve per-100 g macros, shortlist N per column.
  B. solve   — closed-form per-meal portion solve: veg anchored, carb base
               set from the carb target, protein from the protein target,
               fat from what's left, everything clamped to its gram band.
  C. emit    — columns, assembly rule, example bowls, daily math.

Honesty rule, same as the menu optimizer's under-days: when a gram band
blocks the target, the solve stops and reports the residual with an
under/over status. It never quietly claims a target was hit.
"""

from __future__ import annotations

import logging
import sqlite3

from aboyeur.config import Config
from aboyeur.data.components import COMPONENTS, ROLES
from aboyeur.engines import optimizer
from aboyeur.engines.allergens import expand_all
from aboyeur.engines.nutrition import (
    fold_plural,
    lookup_ingredient_macros,
    validated_per_100g,
)

logger = logging.getLogger("aboyeur")

# How many items to offer per column by default. carb_base varies with
# emphasis: an endurance athlete wants carbohydrate variety, a cut barely
# wants a carb base at all (the BWS diet this is modelled on has none).
DEFAULT_KIT_SIZE = {
    "protein": 3, "dairy_protein": 1, "carb_base": 3,
    "veg_volume": 5, "fruit": 2, "fat": 3, "sauce": 4,
}
CARB_BASE_BY_EMPHASIS = {"endurance": 4, "balanced": 3, "cut": 1}

# "Fill the bowl" — veg is anchored, not solved. Volume is the point.
VEG_ANCHOR_G = 300.0

# A bowl is on target when its calories land within this fraction of the
# per-meal target. Tighter than the menu path's 15% because the solve is
# closed-form against real per-100 g macros, not recipe estimates.
KIT_KCAL_TOLERANCE = 0.10

# Calories can land dead on while a macro is badly off — a gram band forces
# too much rice, and protein gets nudged down to pay for it. `status` stays
# calorie-only (it parallels the menu path's daily_totals status); this is
# the separate check that stops an on-calorie bowl from being presented as
# on-target when it isn't.
KIT_MACRO_TOLERANCE = 0.20

# Cap on how many components get a USDA lookup per column: shortlisting
# needs confidence, confidence needs the lookup, so a cold cache would
# otherwise mean one HTTP call per row in the whole library. Resolving twice
# the requested count leaves room for misses without a 90-call first run.
_RESOLVE_FACTOR = 2

# Shortlisting sorts on whether a component RESOLVED, not on how. A
# curated override ("high") and a clean USDA hit ("medium") are equally
# trustworthy numbers; ranking overrides above hits would silently
# reorder every column around which foods happen to need one.
_CONFIDENCE_RANK = {"high": 0, "medium": 0, "low": 1}

_DIET_PRIORITY = ("vegan", "vegetarian", "pescatarian", "omnivore")

_CUT_TIPS = [
    "Carbonated zero-calorie drinks with a meal add stomach volume and blunt "
    "a sweet craving for no calories.",
    "A cup of clear broth before a meal reliably takes calories off the meal "
    "that follows it.",
    "Eat with a smaller utensil — more bites, slower meal, more fullness by "
    "the end of it.",
]


# ---------------------------------------------------------------------------
# Stage A — roster
# ---------------------------------------------------------------------------

def effective_diet(diet_modes: list[str] | None) -> str:
    """The strictest recognized diet in the profile's diet_modes.

    diet_modes is free-form ("keto", "low-fodmap", "vegetarian"); only the
    four the component library tags for are meaningful here, and the
    strictest one wins. Anything unrecognized falls through to omnivore —
    the kit never silently drops foods over a mode it doesn't model.
    """
    modes = {str(m).strip().lower() for m in (diet_modes or [])}
    for diet in _DIET_PRIORITY:
        if diet in modes:
            return diet
    if "plant-based" in modes:
        return "vegan"
    return "omnivore"


def _matches_any(component: dict, terms: list[str]) -> bool:
    """Does a term hit this component's name or its USDA query?

    Word-fold both sides so "eggs" matches "egg whites" and "nuts" (expanded
    to almond/walnut/...) matches "almonds".
    """
    haystack = f"{component['name']} {component['usda_query']}"
    words = {fold_plural(w) for w in haystack.lower().replace("-", " ").split()}
    for term in terms:
        t = fold_plural(term.strip().lower())
        if not t:
            continue
        if " " in t:
            if t in fold_plural(haystack):
                return True
        elif t in words:
            return True
    return False


def filter_components(
    diet: str,
    allergies: list[str] | None = None,
    exclude_foods: list[str] | None = None,
) -> list[dict]:
    """The component library minus anything this member can't or won't eat.

    Allergies are expanded through engines/allergens first — a member who
    said "shellfish" must lose shrimp, and "dairy" must lose the yogurt
    column, even though neither food's name contains the category word.
    """
    expanded = [a for a in expand_all(allergies or []) if a.strip()]
    excludes = [e for e in (exclude_foods or []) if e and e.strip()]
    out = []
    for comp in COMPONENTS:
        if diet not in comp["diet_ok"]:
            continue
        if expanded and _matches_any(comp, expanded):
            continue
        if excludes and _matches_any(comp, excludes):
            continue
        out.append(comp)
    return out


def resolve_component(conn: sqlite3.Connection, config: Config, comp: dict) -> dict:
    """Attach per-100 g macros + a confidence flag to one component.

    A curated macros_override wins (and is sanity-checked the same way a
    USDA hit is); otherwise the normal cached USDA lookup runs. An override
    that fails the gate is a data bug, not a food fact — it's logged and the
    row falls back to USDA rather than poisoning a kit with bad numbers.
    """
    override = comp.get("macros_override")
    if override:
        validated = validated_per_100g(override)
        if validated is None and not any(
            float(override.get(k) or 0) for k in ("kcal", "protein_g", "carb_g", "fat_g")
        ):
            # A curated all-zero row (herbs, sugar-free syrup) is a real
            # answer. USDA's all-zero hits are missing data, which is why
            # the shared gate rejects them — that reasoning doesn't apply
            # to a value a human deliberately entered as zero.
            validated = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0}
        if validated is not None:
            return {**comp, "per_100g": {k: float(validated[k]) for k in
                                         ("kcal", "protein_g", "carb_g", "fat_g")},
                    "confidence": "high", "status": "override"}
        logger.warning(
            "component %r has an invalid macros_override %r — falling back to USDA",
            comp["name"], override,
        )

    macros = lookup_ingredient_macros(conn, config, comp["usda_query"])
    resolved = macros.get("status") == "success" and (
        macros["kcal"] or macros["protein_g"] or macros["carb_g"] or macros["fat_g"]
    )
    return {
        **comp,
        "per_100g": {k: float(macros.get(k) or 0.0) for k in
                     ("kcal", "protein_g", "carb_g", "fat_g")},
        "confidence": "medium" if resolved else "low",
        "status": macros.get("status", "failed"),
    }


def build_roster(
    conn: sqlite3.Connection,
    config: Config,
    available: list[dict],
    kit_size: dict,
) -> tuple[dict[str, list[dict]], list[str]]:
    """Shortlist per column: resolve macros, sort by confidence then curation
    order, take N. Returns (columns, low_confidence_names).

    Selection is deliberately a deterministic shortlist, not an optimization
    — the same member asking twice gets the same kit, and "why is quinoa in
    here" has a one-line answer (it's high in the curated order and it
    resolved).
    """
    columns: dict[str, list[dict]] = {}
    low_confidence: list[str] = []
    for role in ROLES:
        want = max(0, int(kit_size.get(role, DEFAULT_KIT_SIZE.get(role, 0))))
        pool = [c for c in available if c["role"] == role]
        if not want or not pool:
            columns[role] = []
            continue
        resolved = [
            resolve_component(conn, config, c) for c in pool[: want * _RESOLVE_FACTOR]
        ]
        low_confidence.extend(c["name"] for c in resolved if c["confidence"] == "low")
        resolved.sort(key=lambda c: _CONFIDENCE_RANK.get(c["confidence"], 2))
        picked = resolved[:want]
        # Drop an unresolved pick only while the column still has something
        # in it — an all-low column (no USDA key, say) is reported with its
        # confidence flags rather than emptied.
        usable = [c for c in picked if c["confidence"] != "low"]
        columns[role] = usable if usable else picked[:1]
    return columns, sorted(set(low_confidence))


# ---------------------------------------------------------------------------
# Stage B — portion solve
# ---------------------------------------------------------------------------

def _per_g(comp: dict | None, key: str) -> float:
    if not comp:
        return 0.0
    return float(comp["per_100g"].get(key) or 0.0) / 100.0


def _clamp(grams: float, comp: dict) -> float:
    return max(float(comp["min_g"]), min(float(comp["max_g"]), grams))


def _clamp_or_drop(grams: float, comp: dict) -> float:
    """Clamp into the band, except that a solve landing well under the band's
    floor drops the item instead.

    Forcing a minimum serving of something the target doesn't have room for
    is how a "cut" bowl ends up 200 kcal over: at ~450 kcal/meal there is no
    room for 40 g of dry rice on top of 300 g of vegetables. Dropping the
    column is the honest answer, and it's what the BWS diet this is modelled
    on does — it has no carb base at all.
    """
    if grams <= 0:
        return 0.0
    if grams < float(comp["min_g"]):
        return 0.0 if grams < float(comp["min_g"]) / 2.0 else float(comp["min_g"])
    return min(float(comp["max_g"]), grams)


def _totals(items: list[tuple[dict, float]]) -> dict:
    out = {"kcal": 0.0, "protein_g": 0.0, "carb_g": 0.0, "fat_g": 0.0}
    for comp, grams in items:
        for key in out:
            out[key] += _per_g(comp, key) * grams
    return out


# Relative weight per term when choosing between two candidate solves.
# Same priority resolve_macro_targets uses when a budget doesn't balance:
# carbohydrate gives way first, then fat, then protein. Under-delivering
# protein is the miss that actually matters; a carb overshoot is not.
_SELECTION_WEIGHTS = {"kcal": 3.0, "protein_g": 3.0, "fat_g": 2.0, "carb_g": 1.0}


def _selection_error(totals: dict, target: dict) -> float:
    """Weighted relative miss across calories and macros. Lower is better."""
    error = weight = 0.0
    for key, w in _SELECTION_WEIGHTS.items():
        t = float(target.get(key) or 0)
        if t <= 0:
            continue
        error += w * abs(float(totals.get(key) or 0) - t) / t
        weight += w
    return error / weight if weight else 0.0


def solve_bowl(
    target: dict,
    protein: dict | None,
    carb: dict | None,
    veg: dict | None,
    fat: dict | None,
    fruit: dict | None = None,
    fruit_g: float = 0.0,
    veg_g: float = VEG_ANCHOR_G,
) -> dict:
    """One meal's grams for one protein x carb x fat combo. Closed form.

    target: {kcal, protein_g, carb_g, fat_g} for a single meal.

    Order matters and follows what each food is actually for: vegetables are
    anchored (volume is the point, not a variable), the carb base is the
    carbohydrate lever, protein closes the protein gap net of what the carb
    base already brought, and fat takes whatever is left of the fat budget
    after the incidental fat in everything else. Only then are calories
    trued up, by nudging the carb base and then the protein inside their
    bands. Whatever the bands wouldn't allow comes back as a residual.

    When the carb solve lands under the carb base's minimum serving, both
    answers are worked out in full — no carb base at all, or the minimum
    serving — and the closer one wins. Forcing the minimum is how a low
    calorie target ends up over on carbohydrate and short on protein;
    dropping it is what the BWS diet this is modelled on does.
    """
    veg_g = _clamp(veg_g, veg) if veg else 0.0
    fruit_g = _clamp_or_drop(fruit_g, fruit) if (fruit and fruit_g > 0) else 0.0
    fixed = _totals([(veg, veg_g), (fruit, fruit_g)])

    carb_per_g = _per_g(carb, "carb_g")
    if carb and carb_per_g > 0:
        wanted = (float(target["carb_g"]) - fixed["carb_g"]) / carb_per_g
        if wanted < float(carb["min_g"]):
            carb_options = [0.0, float(carb["min_g"])]
        else:
            carb_options = [min(float(carb["max_g"]), wanted)]
    else:
        carb_options = [0.0]

    attempts = [
        _solve_from_carb(target, protein, carb, veg, fat, fruit, veg_g, fruit_g,
                         fixed, g, carb_wanted=carb_options[0] if len(carb_options) == 1 else None)
        for g in carb_options
    ]
    return min(attempts, key=lambda a: _selection_error(a, target))


def _solve_from_carb(
    target: dict,
    protein: dict | None,
    carb: dict | None,
    veg: dict | None,
    fat: dict | None,
    fruit: dict | None,
    veg_g: float,
    fruit_g: float,
    fixed: dict,
    g_carb: float,
    carb_wanted: float | None = None,
) -> dict:
    """Finish a solve with the carb base already decided. See solve_bowl."""
    blocked: list[str] = []
    if carb and carb_wanted is not None and abs(g_carb - carb_wanted) > 0.5:
        blocked.append(carb["name"])

    protein_per_g = _per_g(protein, "protein_g")
    g_protein = 0.0
    if protein and protein_per_g > 0:
        need = (
            float(target["protein_g"])
            - fixed["protein_g"]
            - g_carb * _per_g(carb, "protein_g")
        )
        wanted_protein = need / protein_per_g
        g_protein = _clamp(wanted_protein, protein)
        if abs(g_protein - wanted_protein) > 0.5:
            blocked.append(protein["name"])

    fat_per_g = _per_g(fat, "fat_g")
    g_fat = 0.0
    if fat and fat_per_g > 0:
        incidental = (
            fixed["fat_g"]
            + g_carb * _per_g(carb, "fat_g")
            + g_protein * _per_g(protein, "fat_g")
        )
        g_fat = _clamp_or_drop(
            max(0.0, float(target["fat_g"]) - incidental) / fat_per_g, fat
        )

    grams = {"carb": g_carb, "protein": g_protein, "fat": g_fat}
    comps = {"carb": carb, "protein": protein, "fat": fat}
    for key in ("carb", "protein"):
        comp = comps[key]
        if not comp or grams[key] <= 0:
            continue
        kcal_per_g = _per_g(comp, "kcal")
        if kcal_per_g <= 0:
            continue
        current = _totals(
            [(veg, veg_g), (fruit, fruit_g)]
            + [(comps[k], grams[k]) for k in grams]
        )
        gap = float(target["kcal"]) - current["kcal"]
        if abs(gap) < 1.0:
            break
        wanted = grams[key] + gap / kcal_per_g
        grams[key] = _clamp(wanted, comp)
        if abs(grams[key] - wanted) > 0.5:
            blocked.append(comp["name"])

    items = [(veg, veg_g), (fruit, fruit_g)] + [(comps[k], grams[k]) for k in grams]
    totals = _totals(items)

    target_kcal = float(target["kcal"]) or 0.0
    if not target_kcal:
        status = "unknown"
    elif totals["kcal"] < target_kcal * (1 - KIT_KCAL_TOLERANCE):
        status = "under"
    elif totals["kcal"] > target_kcal * (1 + KIT_KCAL_TOLERANCE):
        status = "over"
    else:
        status = "ok"

    macros_on_target = all(
        float(target[key]) <= 0
        or abs(totals[key] - float(target[key])) <= KIT_MACRO_TOLERANCE * float(target[key])
        for key in ("protein_g", "carb_g", "fat_g")
    )

    fit = optimizer.macro_fit(
        {"calories": totals["kcal"], "protein_g": totals["protein_g"],
         "carb_g": totals["carb_g"], "fat_g": totals["fat_g"]},
        target_kcal,
        {k: target[k] for k in ("protein_g", "carb_g", "fat_g")},
    )

    ordered = [
        (comps["protein"], grams["protein"]), (comps["carb"], grams["carb"]),
        (veg, veg_g), (comps["fat"], grams["fat"]), (fruit, fruit_g),
    ]
    return {
        "items": [
            {"name": c["name"], "role": c["role"], "grams": round(g)}
            for c, g in ordered if c and g > 0
        ],
        "grams_by_role": {
            "protein": round(grams["protein"]), "carb_base": round(grams["carb"]),
            "veg_volume": round(veg_g), "fat": round(grams["fat"]),
            "fruit": round(fruit_g),
        },
        "kcal": round(totals["kcal"]),
        "protein_g": round(totals["protein_g"], 1),
        "carb_g": round(totals["carb_g"], 1),
        "fat_g": round(totals["fat_g"], 1),
        "fit": round(fit, 2),
        # Calorie status, same vocabulary as the menu path's daily_totals.
        "status": status,
        # Calories can be right while a macro isn't — this is that check.
        "macros_on_target": macros_on_target,
        "residual": {
            "kcal": round(totals["kcal"] - target_kcal),
            "protein_g": round(totals["protein_g"] - float(target["protein_g"]), 1),
            "carb_g": round(totals["carb_g"] - float(target["carb_g"]), 1),
            "fat_g": round(totals["fat_g"] - float(target["fat_g"]), 1),
        },
        # Components whose gram band stopped the solve short of the target —
        # the reason a status is "under"/"over" instead of "ok".
        "blocked_by": sorted(set(blocked)),
    }


def _bowl_key(bowl: dict) -> tuple:
    return tuple(i["name"] for i in bowl["items"])


# ---------------------------------------------------------------------------
# Stage C — emit
# ---------------------------------------------------------------------------

def _assembly_text(bowl: dict, columns: dict[str, list[dict]]) -> str:
    parts = []
    g = bowl["grams_by_role"]
    if g["protein"]:
        parts.append(f"1 protein ~{g['protein']} g raw")
    if g["carb_base"]:
        parts.append(f"1 carb base ~{g['carb_base']} g raw / dry")
    if g["veg_volume"]:
        parts.append(f"vegetables ~{g['veg_volume']} g")
    if g["fat"]:
        parts.append(f"1 fat serving ~{g['fat']} g")
    if g["fruit"]:
        parts.append(f"fruit ~{g['fruit']} g")
    if columns.get("sauce"):
        parts.append("sauce and seasoning free")
    return (
        "Per meal, one bowl: " + " · ".join(parts) +
        ". Swap any item for another in the same column at the same weight."
    )


def build_kit(
    conn: sqlite3.Connection,
    config: Config,
    targets: dict,
    meals_per_day: int = 3,
    days: int = 7,
    emphasis: str = "balanced",
    kit_size: dict | None = None,
    diet_modes: list[str] | None = None,
    allergies: list[str] | None = None,
    exclude_foods: list[str] | None = None,
    include_foods: list[str] | None = None,
) -> dict:
    """Assemble a full Macro Kit. See the module docstring for the stages.

    targets: the output of nutrition.resolve_macro_targets — daily kcal and
    macro grams plus the strategy and its basis.
    """
    meals_per_day = max(1, int(meals_per_day or 1))
    days = max(1, int(days or 1))
    emphasis = (emphasis or "balanced").strip().lower()

    sizes = dict(DEFAULT_KIT_SIZE)
    sizes["carb_base"] = CARB_BASE_BY_EMPHASIS.get(emphasis, DEFAULT_KIT_SIZE["carb_base"])
    if kit_size:
        sizes.update({k: v for k, v in kit_size.items() if k in ROLES})

    diet = effective_diet(diet_modes)
    available = filter_components(diet, allergies, exclude_foods)
    if include_foods:
        # An explicit request re-adds a food the diet/exclude filters removed,
        # but never one an allergy removed — that filter is not negotiable.
        expanded_allergies = [a for a in expand_all(allergies or []) if a.strip()]
        forced = [
            c for c in COMPONENTS
            if _matches_any(c, include_foods)
            and not (expanded_allergies and _matches_any(c, expanded_allergies))
        ]
        # Front of the list: the shortlist keeps curation order within a
        # confidence tier, so a requested food leads its column.
        forced_names = {c["name"] for c in forced}
        available = forced + [c for c in available if c["name"] not in forced_names]

    columns, low_confidence = build_roster(conn, config, available, sizes)

    per_meal = {
        "kcal": round(float(targets["kcal"]) / meals_per_day, 1),
        "protein_g": round(float(targets["protein_g"]) / meals_per_day, 1),
        "carb_g": round(float(targets["carb_g"]) / meals_per_day, 1),
        "fat_g": round(float(targets["fat_g"]) / meals_per_day, 1),
    }

    proteins = columns.get("protein") or columns.get("dairy_protein") or []
    carbs = columns.get("carb_base") or []
    vegs = columns.get("veg_volume") or []
    fats = columns.get("fat") or []
    veg = vegs[0] if vegs else None

    def _solve(p, b, f):
        return solve_bowl(per_meal, p, b, veg, f)

    representative = _solve(
        proteins[0] if proteins else None,
        carbs[0] if carbs else None,
        fats[0] if fats else None,
    )

    # Example bowls: every combination of the first three items of each
    # column, ranked by fit, one per protein so the examples show the swap
    # rather than three variations of the same dinner.
    scored = []
    for p in (proteins or [None])[:3]:
        for b in (carbs or [None])[:3]:
            for f in (fats or [None])[:3]:
                bowl = _solve(p, b, f)
                if bowl["items"]:
                    scored.append((p["name"] if p else "", bowl))
    scored.sort(key=lambda pb: -pb[1]["fit"])
    examples: list[dict] = []
    seen_proteins: set[str] = set()
    seen_keys: set[tuple] = set()
    for pname, bowl in scored:
        if pname in seen_proteins or _bowl_key(bowl) in seen_keys:
            continue
        seen_proteins.add(pname)
        seen_keys.add(_bowl_key(bowl))
        examples.append(bowl)
        if len(examples) == 3:
            break
    for pname, bowl in scored:  # top up if there weren't 3 distinct proteins
        if len(examples) == 3:
            break
        if _bowl_key(bowl) not in seen_keys:
            seen_keys.add(_bowl_key(bowl))
            examples.append(bowl)

    daily = {
        key: round(representative[key] * meals_per_day, 1)
        for key in ("protein_g", "carb_g", "fat_g")
    }
    daily["kcal"] = round(representative["kcal"] * meals_per_day)
    target_kcal = float(targets["kcal"]) or 0.0
    # No protein at all (every option filtered out) means there is no bowl to
    # report on, not a bowl that missed — say unknown rather than under.
    if not target_kcal or not proteins:
        daily_status = "unknown"
    elif daily["kcal"] < target_kcal * (1 - KIT_KCAL_TOLERANCE):
        daily_status = "under"
    elif daily["kcal"] > target_kcal * (1 + KIT_KCAL_TOLERANCE):
        daily_status = "over"
    else:
        daily_status = "ok"
    daily_macros_ok = representative["macros_on_target"]

    notes = [
        "Weights are raw / as-purchased — dry rice, oats and pasta; raw meat and fish.",
        "Any item swaps for another in the same column at the same weight; "
        "the macros move a little, the bowl still lands.",
    ]
    if diet != "omnivore":
        notes.append(f"Foods filtered to a {diet} diet.")
    if allergies:
        notes.append(
            "Allergens excluded (category names expanded to the specific foods): "
            + ", ".join(sorted({a.lower().strip() for a in allergies if a.strip()}))
            + "."
        )
    if low_confidence:
        notes.append(
            "Some foods' macros didn't resolve against USDA and are estimated at "
            "zero — treat their numbers as unknown, not as free food."
        )
    if not daily_macros_ok:
        off = [
            key.replace("_g", "") for key in ("protein_g", "carb_g", "fat_g")
            if float(targets[key]) > 0
            and abs(representative[key] * meals_per_day - float(targets[key]))
            > KIT_MACRO_TOLERANCE * float(targets[key])
        ]
        lead = (
            "Calories land but the macro split doesn't: "
            if daily_status == "ok" else "The macro split is off: "
        )
        notes.append(
            lead + ", ".join(off)
            + " misses the target by more than "
            + f"{round(KIT_MACRO_TOLERANCE * 100)}%. The portion bands wouldn't "
            "stretch further."
        )
    if representative["status"] != "ok" and representative["blocked_by"]:
        notes.append(
            "Portion bands blocked an exact fit ("
            + ", ".join(representative["blocked_by"])
            + f"); the bowl comes out {representative['status']} by "
            f"{abs(representative['residual']['kcal'])} kcal."
        )

    return {
        "emphasis": emphasis,
        "meals_per_day": meals_per_day,
        "days": days,
        "diet": diet,
        "targets": {
            "strategy": targets.get("strategy", emphasis),
            "basis": targets.get("basis", ""),
            "daily": {
                "kcal": round(float(targets["kcal"])),
                "protein_g": round(float(targets["protein_g"]), 1),
                "carb_g": round(float(targets["carb_g"]), 1),
                "fat_g": round(float(targets["fat_g"]), 1),
            },
            "per_meal": per_meal,
        },
        "columns": {
            role: [
                {
                    "name": c["name"],
                    "serving_g": c["default_serving_g"],
                    "kcal": round(_per_g(c, "kcal") * c["default_serving_g"]),
                    "protein_g": round(_per_g(c, "protein_g") * c["default_serving_g"], 1),
                    "carb_g": round(_per_g(c, "carb_g") * c["default_serving_g"], 1),
                    "fat_g": round(_per_g(c, "fat_g") * c["default_serving_g"], 1),
                    "confidence": c["confidence"],
                    "tags": c["tags"],
                }
                for c in items
            ]
            for role, items in columns.items()
        },
        "assembly_rule": {
            "text": _assembly_text(representative, columns),
            "per_meal_grams": representative["grams_by_role"],
            "tips": list(_CUT_TIPS) if emphasis == "cut" else [],
        },
        "example_bowls": examples,
        "daily_totals": {
            **daily,
            "target_kcal": round(target_kcal),
            "target_protein_g": round(float(targets["protein_g"]), 1),
            "target_carb_g": round(float(targets["carb_g"]), 1),
            "target_fat_g": round(float(targets["fat_g"]), 1),
            "status": daily_status,
            "macros_on_target": daily_macros_ok,
        },
        "low_confidence_components": low_confidence,
        "notes": notes,
    }
