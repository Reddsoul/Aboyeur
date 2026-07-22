"""Shared, dependency-free helpers used by both backend/ and ingest/.

Stdlib only (re) so the lean ingest image can import it without extra deps.
"""
import re
import unicodedata

# ---------------------------------------------------------------------------
# Slugify
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 80) -> str:
    """ASCII, lowercase, hyphenated slug. Handles CJK/accented titles."""
    if not text:
        return "untitled"
    # Transliterate accents to ASCII where possible.
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if not text:
        return "untitled"
    return text[:max_len].strip("-")


# ---------------------------------------------------------------------------
# Technique classification
# ---------------------------------------------------------------------------
# Order matters: first match wins. Spec set: stir_fry|braise|roast|steam|raw|bake.
_TECHNIQUE_KEYWORDS = [
    ("stir_fry", ["stir-fry", "stir fry", "stir-fried", "wok", "stirfry"]),
    ("braise",   ["braise", "braised", "red-braise", "simmer", "stew", "stewed", "slow-cook"]),
    ("steam",    ["steam", "steamed", "steamer"]),
    ("roast",    ["roast", "roasted", "broil", "grill", "grilled", "barbecue"]),
    ("bake",     ["bake", "baked", "oven", "casserole"]),
    ("raw",      ["raw", "ceviche", "tartare", "salad", "no-cook", "uncooked", "sashimi"]),
]


def classify_technique(text: str) -> str:
    """Best-effort technique tag from recipe text. '' if undetermined."""
    if not text:
        return ""
    low = text.lower()
    counts = {}
    for tech, kws in _TECHNIQUE_KEYWORDS:
        c = sum(low.count(kw) for kw in kws)
        if c:
            counts[tech] = c
    if not counts:
        return ""
    return max(counts, key=counts.get)


# ---------------------------------------------------------------------------
# Grocery section categorization
# ---------------------------------------------------------------------------
_SECTION_KEYWORDS = {
    "protein": ["chicken", "beef", "pork", "lamb", "fish", "shrimp", "prawn", "tofu",
                "egg", "salmon", "tuna", "turkey", "bacon", "sausage", "duck", "crab",
                "scallop", "clam", "mussel", "anchovy", "ground", "mince", "steak",
                "tempeh", "seitan", "ham", "oyster"],
    "produce": ["garlic", "onion", "ginger", "scallion", "tomato", "potato", "carrot",
                "pepper", "chili", "chile", "cilantro", "parsley", "basil", "mint",
                "lettuce", "spinach", "cabbage", "cucumber", "lemon", "lime", "apple",
                "mushroom", "eggplant", "zucchini", "broccoli", "celery", "leek",
                "shallot", "lemongrass", "bok choy", "greens", "herb", "lime leaf",
                "coriander", "cucumbers", "bean sprout", "radish", "cauliflower",
                "squash", "corn", "avocado", "kale", "fennel", "asparagus"],
    "dairy": ["milk", "butter", "cream", "cheese", "yogurt", "yoghurt", "parmesan",
              "mozzarella", "ricotta", "mascarpone", "creme fraiche", "ghee", "feta"],
    "frozen": ["frozen", "ice cream", "peas frozen"],
    "spice": ["salt", "sugar", "pepper", "cumin", "coriander seed", "turmeric",
              "paprika", "cinnamon", "clove", "cardamom", "nutmeg", "star anise",
              "peppercorn", "sichuan pepper", "chili powder", "chile powder", "curry powder",
              "garam masala", "five spice", "bay leaf", "oregano", "thyme", "fennel seed",
              "mustard seed", "fenugreek", "saffron", "vanilla", "msg", "spice"],
    "pantry": ["oil", "vinegar", "soy sauce", "soy", "rice", "flour", "noodle",
               "pasta", "stock", "broth", "sauce", "paste", "doubanjiang", "miso",
               "sesame", "cornstarch", "starch", "wine", "sake", "mirin", "honey",
               "syrup", "bean", "lentil", "chickpea", "coconut milk", "tamarind",
               "fish sauce", "oyster sauce", "tomato paste", "canned", "tin", "dried",
               "nut", "almond", "peanut", "cashew", "tahini", "yeast", "baking",
               "chocolate", "cocoa", "gelatin", "tortilla", "bread", "panko", "breadcrumb"],
}

# Check spice/protein/dairy/produce before pantry (pantry is the catch-all-ish bucket).
_SECTION_ORDER = ["spice", "protein", "dairy", "produce", "frozen", "pantry"]


def categorize_ingredient(name: str) -> str:
    """Map an ingredient name to a grocery section. Defaults to 'pantry'."""
    if not name:
        return "pantry"
    low = name.lower()
    for section in _SECTION_ORDER:
        for kw in _SECTION_KEYWORDS[section]:
            if kw in low:
                return section
    return "pantry"


# ---------------------------------------------------------------------------
# Ingredient name extraction (quantity-stripped) from a recipe markdown chunk
# ---------------------------------------------------------------------------
_UNIT_WORDS = {
    "cup", "cups", "tbsp", "tablespoon", "tablespoons", "tsp", "teaspoon", "teaspoons",
    "g", "kg", "gram", "grams", "ml", "l", "liter", "litre", "oz", "ounce", "ounces",
    "lb", "lbs", "pound", "pounds", "pinch", "dash", "clove", "cloves", "slice", "slices",
    "piece", "pieces", "can", "cans", "package", "packet", "bunch", "handful", "stick",
    "sticks", "sprig", "sprigs", "head", "heads", "stalk", "stalks", "knob", "inch",
    "cm", "ml.", "qt", "quart", "pint", "fl",
}
_STOP_WORDS = {
    "of", "the", "a", "an", "and", "or", "to", "for", "with", "fresh", "freshly",
    "ground", "chopped", "minced", "diced", "sliced", "grated", "peeled", "large",
    "small", "medium", "finely", "roughly", "thinly", "about", "approximately",
    "plus", "more", "taste", "into", "optional", "such", "as", "your", "preferably",
    "good", "quality", "extra", "very", "well", "cut", "trimmed", "rinsed", "drained",
    "softened", "melted", "beaten", "cold", "warm", "hot", "room", "temperature",
    "cooked", "uncooked", "raw", "dried", "halved", "quartered", "crushed",
}
_INGREDIENT_LINE_RE = re.compile(
    r"^\s*[-*•]\s+|^\s*\d+[\d/\.\s]*\s*(?:" + "|".join(_UNIT_WORDS) + r")\b",
    re.IGNORECASE,
)
_QTY_PREFIX_RE = re.compile(
    r"^[\s\-*•]*[\d¼½¾⅓⅔⅛/\.\s]*\s*(?:" + "|".join(_UNIT_WORDS) + r")?s?\b\.?",
    re.IGNORECASE,
)


def _clean_ingredient(line: str) -> str:
    """Strip leading bullets/quantities/units, parentheticals, trailing notes."""
    s = line.strip()
    s = re.sub(r"\([^)]*\)", "", s)          # drop parentheticals
    s = s.split(",")[0]                       # drop ", finely chopped" tails
    s = _QTY_PREFIX_RE.sub("", s)
    # Drop residual leading numbers / fraction glyphs.
    s = re.sub(r"^[\s\d¼½¾⅓⅔⅛/\.\-]+", "", s)
    tokens = [t for t in re.split(r"\s+", s.lower()) if t]
    tokens = [t for t in tokens if t not in _STOP_WORDS and t not in _UNIT_WORDS]
    tokens = [re.sub(r"[^a-z\-]", "", t) for t in tokens]
    tokens = [t for t in tokens if len(t) > 1]
    name = " ".join(tokens).strip()
    return name


def extract_ingredients(text: str, max_items: int = 40) -> list[str]:
    """Heuristically pull ingredient names (no quantities) from a recipe chunk.

    Looks for an 'Ingredients' section first; falls back to scanning bullet /
    quantity-prefixed lines across the whole chunk. Deduped, order-preserved.
    """
    if not text:
        return []
    lines = text.splitlines()

    # Prefer an explicit ingredients block if present.
    block = []
    in_block = False
    for ln in lines:
        header = ln.strip().lower().strip("#* :")
        if header in ("ingredients", "for the sauce", "you will need"):
            in_block = True
            continue
        if in_block:
            if re.match(r"^\s*#{1,6}\s", ln) or header in ("method", "instructions", "directions", "steps"):
                break
            block.append(ln)
    scan = block if block else lines

    out, seen = [], set()
    for ln in scan:
        if not _INGREDIENT_LINE_RE.search(ln) and not block:
            continue
        name = _clean_ingredient(ln)
        if not name or len(name) < 2 or len(name) > 40:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= max_items:
            break
    return out
