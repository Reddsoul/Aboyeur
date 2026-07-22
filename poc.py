#!/usr/bin/env python3
"""
Aboyeur — terminal culinary chat POC
Single-file. No Docker, no ChromaDB, no MCP. Just Python + Spark LLM.
"""
import sys
import json
import os
import re
import random
import string
import datetime
import uuid
from collections import Counter, defaultdict
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:
    print("Installing openai...")
    os.system(f"{sys.executable} -m pip install openai -q")
    from openai import OpenAI

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJ_DIR      = Path(__file__).parent
RECIPES_DIR   = Path("/Volumes/General/Cookbooks/Recipes")
DATA_DIR      = PROJ_DIR / "data"
LOG_DIR       = PROJ_DIR / "logs"
INDEX_PATH    = DATA_DIR / "recipe_index.json"
PANTRY_PATH   = DATA_DIR / "pantry.txt"
PROFILE_PATH  = DATA_DIR / "user_profile.json"
ILOG_PATH     = DATA_DIR / "interaction_log.jsonl"

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_BASE_URL = "http://spark-50a1.local:4000/v1"
LLM_MODEL    = "hard"
EXTRA_BODY   = {"chat_template_kwargs": {"enable_thinking": False}}
client       = OpenAI(base_url=LLM_BASE_URL, api_key="none")

# ── Backend utils (optional import — better categorization) ───────────────────
sys.path.insert(0, str(PROJ_DIR))
try:
    from backend.utils import categorize_ingredient as _cat_backend
    _HAS_BACKEND = True
except ImportError:
    _HAS_BACKEND = False

# ── Stopwords ─────────────────────────────────────────────────────────────────
STOPWORDS = {
    "a","an","the","and","or","of","with","for","in","on","to","is","i",
    "me","my","some","any","what","can","do","make","want","need","have",
    "get","find","recipes","recipe","food","dish","dishes","something","that",
    "from","using","use","would","like","please","show","give","suggest",
    "how","about","think","ideas","meals","meal","dinner","lunch","breakfast",
    # meta-descriptors — colloquial, never appear usefully in recipe titles
    "cheap","eats","fancy","easy","quick","simple","basic","budget","affordable",
    "healthy","hearty","light","heavy","classic","modern","traditional","rustic",
    "upcoming","next","this","week","plan","menu","looking","nothing","just",
    "only","really","very","nice","good","great","best","new","old",
    "dinners","lunches","breakfasts","snacks","everyday","weeknight","weekday",
    "tall","man","woman","person","lbs","weight","foot","feet","inches",
}

CUISINE_WORDS = {
    "chinese","italian","indian","french","japanese","korean","thai","mexican",
    "greek","spanish","american","british","english","vietnamese","turkish",
    "middle eastern","mediterranean","sichuan","cantonese","szechuan","portuguese",
    "moroccan","persian","lebanese","nordic","scandinavian","german","hungarian",
    # Latin American
    "peruvian","argentine","argentinian","brazilian","colombian","cuban","caribbean",
    "latin","south american","central american",
    # African
    "ethiopian","nigerian","west african","north african","egyptian",
    # Southeast/East Asian
    "filipino","indonesian","malaysian","singaporean","taiwanese",
    # Eastern European / Middle Eastern
    "polish","czech","austrian","russian","ukrainian","georgian","armenian","israeli",
    "jewish","sephardic","ashkenazi",
}

# Ingredients that appear in nearly every cookbook — not meaningful for overlap scoring.
# Using these to drive greedy selection causes same-book clustering.
UNIVERSAL_STAPLES = {
    "salt","pepper","butter","flour","water","oil","olive oil","vegetable oil",
    "sugar","vinegar","onion","garlic","eggs","egg","milk","cream","stock","broth",
    "boiled rice","rice","bread","bread crumbs","breadcrumbs","bay leaf","bay-leaf",
    "parsley","thyme","lemon juice","lemon",
}

DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]


# ── Timestamps ────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ── Session log (file in /logs/) ──────────────────────────────────────────────
_session_log = None


def setup_session_log(session_id: str):
    global _session_log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    path  = LOG_DIR / f"poc_{today}.log"
    _session_log = open(path, "a", buffering=1)
    _session_log.write(f"\n{'='*60}\nSession {session_id} started {_ts()}\n{'='*60}\n")


def _slog(msg: str):
    if _session_log:
        _session_log.write(f"[{_ts()}] {msg}\n")


# ── Interaction log (data/interaction_log.jsonl) ──────────────────────────────
def log_interaction(role: str, content: str, *, intent: str = "",
                    results_count: int = 0, session_id: str = ""):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _ts(),
        "session_id": session_id,
        "role": role,
        "content": content[:2000],          # cap to avoid huge log entries
        "intent": intent,
        "results_count": results_count,
    }
    with open(ILOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ── User profile ──────────────────────────────────────────────────────────────
def load_profile() -> dict:
    try:
        p = json.loads(PROFILE_PATH.read_text())
        # Backfill dietary field if missing (older profiles)
        p.setdefault("dietary", [])
        return p
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "household": [
                {"id": "user", "label": "Me", "age": None, "sex": None,
                 "height_cm": None, "weight_kg": None,
                 "activity": "moderate", "daily_kcal": None}
            ],
            "preferences": {
                "likes": [], "dislikes": [], "allergies": [],
                "cuisine_affinity": [], "fusion_tolerance": "medium",
            },
            "plan_days": 7,
            "meals_per_day": 3,
            "dietary": [],
        }


def save_profile(profile: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))


def profile_injection(profile: dict) -> str:
    """One-line summary injected into system prompt."""
    hh      = profile.get("household", [])
    prefs   = profile.get("preferences", {})
    mpd     = profile.get("meals_per_day", 1)
    dietary = profile.get("dietary", [])

    parts = []
    if hh:
        members = []
        for m in hh:
            label = m.get("label") or m.get("id", "?")
            kcal  = m.get("daily_kcal") or _tdee_from_member(m)
            if kcal and kcal > 0:
                members.append(f"{label}({round(kcal)}kcal)")
            else:
                members.append(label)
        parts.append("Eating: " + ", ".join(members))
    parts.append(f"Meals/day: {mpd}")
    if dietary:
        parts.append(f"Dietary: {', '.join(dietary)}")
    for key, label in [("likes", "Likes"), ("dislikes", "Dislikes"),
                       ("allergies", "Allergies"), ("cuisine_affinity", "Cuisine")]:
        vals = prefs.get(key, [])
        if vals:
            parts.append(f"{label}: {', '.join(vals)}")
    return ". ".join(parts) + "." if parts else ""


# ── BMR / TDEE math (ported from backend/macro_engine.py, no external deps) ──
_ACTIVITY_MUL = {
    "sedentary": 1.2, "light": 1.375, "moderate": 1.55,
    "active": 1.725, "very_active": 1.9,
}
_SPLIT = {"protein": 0.30, "carb": 0.40, "fat": 0.30}
_KCAL_PER_G = {"protein": 4.0, "carb": 4.0, "fat": 9.0}


def _bmr(sex: str, age: int, height_cm: float, weight_kg: float) -> float:
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + (5.0 if (sex or "").lower().startswith("m") else -161.0)


def _tdee_from_member(m: dict) -> float:
    if all(m.get(k) is not None for k in ("sex", "age", "height_cm", "weight_kg")):
        b = _bmr(m["sex"], m["age"], m["height_cm"], m["weight_kg"])
        return b * _ACTIVITY_MUL.get(m.get("activity", "moderate"), 1.55)
    return float(m.get("daily_kcal") or 2000)


def per_meal_targets(daily_kcal: float, meals: int = 1) -> dict:
    meals = max(1, meals)
    kcal  = daily_kcal / meals
    return {
        "kcal":      round(kcal, 1),
        "protein_g": round(_SPLIT["protein"] * kcal / _KCAL_PER_G["protein"], 1),
        "carb_g":    round(_SPLIT["carb"]    * kcal / _KCAL_PER_G["carb"],    1),
        "fat_g":     round(_SPLIT["fat"]     * kcal / _KCAL_PER_G["fat"],     1),
    }


# ── Ingredient normalization ───────────────────────────────────────────────────
_QTY_RE = re.compile(
    r"^\s*\d[\d\s/\-\.]*"
    r"(?:cups?|tablespoons?|tbsp?|teaspoons?|tsp?|pounds?|lbs?|oz|ounces?"
    r"|grams?|g|kg|ml|liters?|l|cloves?|pieces?|slices?|cans?|packages?|pkg"
    r"|bunches?|heads?|sprigs?|pinch|dash|handful|inch|cm|quarts?|pints?"
    r"|gallon|bunch|strip|strips|sheet|sheets|block|blocks|stick|sticks"
    r"|jar|jars|bag|bags|bottle|bottles|box|boxes)\.?\s*",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\([^)]*\)")
_LINK_RE  = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def normalize_ingredient(raw: str) -> str:
    raw = _LINK_RE.sub(r"\1", raw)
    raw = _PAREN_RE.sub("", raw)
    raw = raw.strip("-• \t")
    raw = _QTY_RE.sub("", raw)
    raw = raw.strip(",. \t")
    raw = raw.lower()
    raw = re.sub(r"^(of|fresh|dried|frozen|chopped|minced|sliced|diced|grated|"
                 r"peeled|crushed|ground|whole|large|small|medium|extra)\s+", "", raw)
    return raw.strip()


# ── Ingredient categorization ─────────────────────────────────────────────────
_CAT_MAP = {"produce": "Produce", "protein": "Protein", "dairy": "Protein",
            "pantry": "Pantry", "spice": "Pantry", "frozen": "Other"}

_PRODUCE_KW = {"garlic","onion","tomato","pepper","carrot","celery","potato","lemon",
               "lime","ginger","scallion","spinach","kale","lettuce","broccoli","zucchini",
               "mushroom","eggplant","cucumber","corn","pea","bean","basil","cilantro",
               "parsley","thyme","rosemary","sage","mint","chive","leek","shallot",
               "fennel","artichoke","asparagus","cabbage","cauliflower","squash","pumpkin",
               "beet","radish","turnip","apple","orange","banana","mango","avocado"}
_PROTEIN_KW = {"chicken","beef","pork","lamb","fish","salmon","tuna","shrimp","tofu",
               "tempeh","egg","cheese","milk","cream","butter","yogurt","bacon","turkey",
               "duck","veal","crab","lobster","scallop","clam","mussel","anchovy","sardine",
               "cod","halibut","tilapia","sausage","ham","prosciutto","lentil","chickpea"}
_PANTRY_KW  = {"oil","flour","sugar","salt","vinegar","soy","sauce","paste","broth","stock",
               "wine","rice","pasta","noodle","bread","cumin","paprika","turmeric","cinnamon",
               "nutmeg","clove","bay","star","cardamom","oregano","chili","sesame","tahini",
               "miso","mustard","honey","syrup","baking","cocoa","chocolate","vanilla","panko"}


def categorize(ing: str) -> str:
    if _HAS_BACKEND:
        return _CAT_MAP.get(_cat_backend(ing), "Other")
    low = ing.lower()
    for w in _PRODUCE_KW:
        if w in low: return "Produce"
    for w in _PROTEIN_KW:
        if w in low: return "Protein"
    for w in _PANTRY_KW:
        if w in low: return "Pantry"
    return "Other"


# ── Parsing ───────────────────────────────────────────────────────────────────
def parse_recipe(path: Path, book: str) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None

    title = ""
    cuisine = "unknown"
    ingredients = []
    frontmatter_lines = []
    body_lines = []
    lines = text.splitlines()

    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                body_lines = lines[i + 1:]
                break
            frontmatter_lines.append(line)

    if frontmatter_lines:
        in_ingredients = False
        for line in frontmatter_lines:
            stripped = line.strip()
            if stripped.startswith("title:"):
                title = stripped[6:].strip().strip('"\'')
            elif stripped.startswith("cuisine:"):
                cuisine = stripped[8:].strip().strip('"\'')
            elif stripped.startswith("ingredients:"):
                in_ingredients = True
            elif in_ingredients:
                if stripped.startswith("-"):
                    ing = normalize_ingredient(stripped[1:].strip().strip('"\''))
                    if ing:
                        ingredients.append(ing)
                elif stripped and not stripped.startswith(" ") and ":" in stripped:
                    in_ingredients = False
        if not body_lines:
            body_lines = lines
    else:
        body_lines = lines

    if not title:
        for line in body_lines:
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        title = path.stem.replace("_", " ")

    if not ingredients:
        in_ing = False
        for line in body_lines:
            if re.match(r"^##\s+Ingredients", line, re.IGNORECASE):
                in_ing = True
                continue
            if in_ing:
                if line.startswith("## ") or line.startswith("# "):
                    break
                stripped = line.strip()
                if stripped.startswith(("-", "•", "*")):
                    ing = normalize_ingredient(stripped)
                    if ing:
                        ingredients.append(ing)

    return {"title": title, "book": book, "cuisine": cuisine,
            "ingredients": ingredients, "path": str(path)}


# ── Index ─────────────────────────────────────────────────────────────────────
def build_index() -> list[dict]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    all_files = [
        p for p in RECIPES_DIR.rglob("*.md")
        if p.name not in ("00_Dashboard.md", "_test_write.md")
        and ".trash" not in p.parts
        and "_Cuisines" not in p.parts
        and "_Ingredients" not in p.parts
        and "_tags" not in str(p)
    ]
    total = len(all_files)
    print(f"Indexing {total:,} files...", flush=True)
    index = []
    for i, path in enumerate(all_files, 1):
        if i % 1000 == 0:
            print(f"  {i:,}/{total:,}", flush=True)
        book   = path.parent.name
        recipe = parse_recipe(path, book)
        if recipe and recipe["title"]:
            index.append(recipe)
    print(f"Done. {len(index):,} recipes indexed.")
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    return index


def load_index() -> list[dict]:
    if INDEX_PATH.exists():
        print("Loading index...", end=" ", flush=True)
        with open(INDEX_PATH, encoding="utf-8") as f:
            idx = json.load(f)
        print(f"{len(idx):,} recipes ready.")
        return idx
    return build_index()


# ── Pantry ────────────────────────────────────────────────────────────────────
def load_pantry() -> list[str]:
    if PANTRY_PATH.exists():
        return [l.strip().lower() for l in PANTRY_PATH.read_text().splitlines() if l.strip()]
    return []


def save_pantry(items: list[str]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PANTRY_PATH.write_text("\n".join(items))


# ── Search ────────────────────────────────────────────────────────────────────
def keywords(query: str) -> list[str]:
    q = query.lower().translate(str.maketrans("", "", string.punctuation))
    return [w for w in q.split() if w not in STOPWORDS and len(w) > 2]


def score_recipe(recipe: dict, kws: list[str], pantry: list[str], cuisine_hint: str) -> int:
    score = 0
    title_l   = recipe["title"].lower()
    book_l    = recipe["book"].lower().replace("_", " ")
    ings_l    = " ".join(recipe["ingredients"])
    cuisine_l = recipe["cuisine"].lower()

    for kw in kws:
        if kw in title_l: score += 3
        if kw in ings_l:  score += 2
        if kw in book_l:  score += 1

    if cuisine_hint and cuisine_hint in cuisine_l:
        score += 8

    pantry_set = set(pantry)
    for item in pantry_set:
        if any(item in ing or ing in item for ing in recipe["ingredients"]):
            score += 3

    return score


def dedup_results(results: list[dict]) -> list[dict]:
    seen, out = set(), []
    for r in results:
        key = r["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def search_recipes(query: str, index: list[dict], pantry: list[str],
                   n: int = 15) -> list[dict]:
    kws          = keywords(query)
    cuisine_hint = next((w for w in kws if w in CUISINE_WORDS), "")

    pool = index
    if cuisine_hint:
        cuisine_pool = [r for r in index if cuisine_hint in r["cuisine"].lower()]
        if len(cuisine_pool) >= n:
            pool = cuisine_pool

    scored = []
    for r in pool:
        s = score_recipe(r, kws, pantry, cuisine_hint)
        if s > 0:
            scored.append((s, r))

    if not scored and pool is not index:
        for r in index:
            s = score_recipe(r, kws, pantry, cuisine_hint)
            if s > 0:
                scored.append((s, r))

    scored.sort(key=lambda x: -x[0])
    if not scored:
        return []

    result, i = [], 0
    while i < len(scored) and len(result) < n * 3:
        ts = scored[i][0]
        tier = []
        while i < len(scored) and scored[i][0] == ts:
            tier.append(scored[i][1])
            i += 1
        random.shuffle(tier)
        result.extend(tier)

    return dedup_results(result)[:n]


def ingredient_search(ingredients: list[str], index: list[dict], pantry: list[str],
                      n: int = 15) -> list[dict]:
    scored = []
    for r in index:
        ings_l = " ".join(r["ingredients"])
        hits = sum(1 for ing in ingredients if ing.lower() in ings_l)
        if hits > 0:
            scored.append((hits, r))
    scored.sort(key=lambda x: -x[0])
    result, i = [], 0
    while i < len(scored) and len(result) < n * 2:
        ts = scored[i][0]
        tier = []
        while i < len(scored) and scored[i][0] == ts:
            tier.append(scored[i][1])
            i += 1
        random.shuffle(tier)
        result.extend(tier)
    return dedup_results(result)[:n]


def book_search(book_name: str, index: list[dict], n: int = 20) -> list[dict]:
    bn = book_name.lower().replace(" ", "_")
    matches = [r for r in index if bn in r["book"].lower()]
    random.shuffle(matches)
    return dedup_results(matches)[:n]


def diverse_random_pool(index: list[dict], size: int = 50) -> list[dict]:
    """Random sample capped at 2 recipes per book — prevents greedy from clustering."""
    shuffled = list(index)
    random.shuffle(shuffled)
    book_counts: dict = defaultdict(int)
    pool = []
    for r in shuffled:
        if book_counts[r["book"]] < 2:
            pool.append(r)
            book_counts[r["book"]] += 1
        if len(pool) >= size:
            break
    return pool


def detect_cuisine_hints(query: str) -> list[str]:
    """Return all cuisine keywords found in the query (may be multiple)."""
    q = query.lower().translate(str.maketrans("", "", string.punctuation))
    return [w for w in q.split() if w in CUISINE_WORDS]


def cuisine_search(cuisine: str, index: list[dict], pantry: list[str],
                   kws: list[str], n: int = 15) -> list[dict]:
    """Search within a single cuisine's recipes."""
    pool = [r for r in index if cuisine in r["cuisine"].lower()]
    if not pool:
        return []
    scored = [(score_recipe(r, kws, pantry, cuisine), r) for r in pool]
    scored = [(s, r) for s, r in scored if s > 0]
    if not scored:
        # cuisine found but no keyword matches — return random from that cuisine
        random.shuffle(pool)
        return pool[:n]
    scored.sort(key=lambda x: -x[0])
    result, i = [], 0
    while i < len(scored) and len(result) < n * 2:
        ts = scored[i][0]
        tier = []
        while i < len(scored) and scored[i][0] == ts:
            tier.append(scored[i][1])
            i += 1
        random.shuffle(tier)
        result.extend(tier)
    return dedup_results(result)[:n]


def multi_cuisine_week_search(query: str, index: list[dict], pantry: list[str],
                               n: int = 30) -> tuple[list[dict], list[str], list[str]]:
    """For week plans with multiple cuisines: search each, interleave, report missing.

    Returns (results, found_cuisines, missing_cuisines).
    """
    cuisines = detect_cuisine_hints(query)
    kws      = keywords(query)

    if len(cuisines) <= 1:
        results = search_recipes(query, index, pantry, n=n)
        found   = [c for c in cuisines if any(c in r["cuisine"].lower() for r in results)]
        missing = [c for c in cuisines if c not in found]
        return results, found, missing

    found, missing, per_cuisine = [], [], []
    per_n = max(10, n // len(cuisines))

    for cuisine in cuisines:
        hits = cuisine_search(cuisine, index, pantry, kws, n=per_n)
        if hits:
            found.append(cuisine)
            per_cuisine.append(hits)
        else:
            missing.append(cuisine)

    if not per_cuisine:
        return [], [], cuisines

    # interleave: Mon=cuisine1, Tue=cuisine2, Wed=cuisine1, ...
    interleaved: list[dict] = []
    max_len = max(len(r) for r in per_cuisine)
    for i in range(max_len):
        for cuisine_results in per_cuisine:
            if i < len(cuisine_results):
                interleaved.append(cuisine_results[i])

    return dedup_results(interleaved)[:n], found, missing


def get_available_cuisines(index: list[dict]) -> list[str]:
    """Cuisines with ≥5 recipes in the library, sorted by recipe count."""
    counts = Counter(
        r["cuisine"] for r in index
        if r["cuisine"] not in ("unknown", "International", "")
    )
    return [c for c, n in counts.most_common() if n >= 5]


def suggest_substitute_cuisine(missing: str, available: list[str]) -> str:
    """Ask the LLM which available cuisine is culinarily closest to the missing one.
    Non-streaming, low-temp, expects a single cuisine name back.
    """
    avail_str = ", ".join(available[:40])
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": (
                f"A user wants {missing} cuisine. "
                f"The cookbook library only contains: {avail_str}. "
                f"Which ONE cuisine from that list is most culinarily similar to {missing}? "
                f"Reply with the cuisine name ONLY — no explanation, no punctuation."
            )}],
            temperature=0.1,
            max_tokens=15,
            extra_body=EXTRA_BODY,
        )
        suggestion = (resp.choices[0].message.content or "").strip().lower().strip(".,;:")
        # validate it's actually in our available list
        for avail in available:
            if suggestion == avail.lower() or suggestion in avail.lower() or avail.lower() in suggestion:
                return avail
    except Exception:
        pass
    return available[0] if available else ""


def detect_ingredients_in_query(query: str) -> list[str]:
    q_words = set(query.lower().translate(str.maketrans("", "", string.punctuation)).split())
    common = {
        "chicken","beef","pork","lamb","fish","salmon","shrimp","tofu","eggs","egg",
        "mushroom","tomato","garlic","onion","carrot","potato","pepper","lemon",
        "ginger","rice","pasta","cheese","cream","butter","oil","spinach","broccoli",
        "cabbage","zucchini","eggplant","corn","beans","lentils","chickpeas",
        "bacon","turkey","duck","crab","tuna","anchovy","sausage","ham",
        "avocado","mango","apple","banana","orange","lime",
    }
    return [w for w in q_words if w in common]


def detect_book_in_query(query: str, index: list[dict]) -> str | None:
    q     = query.lower()
    books = set(r["book"].replace("_", " ").lower() for r in index)
    for book in sorted(books, key=len, reverse=True):
        if len(book) > 5 and book in q:
            return book
    return None


# ── Weekly menu optimizer (ported from backend/menu_engine.py) ────────────────
W_PANTRY    = 0.5
W_SEMANTIC  = 0.3
W_VARIETY   = 0.2


def _pantry_overlap_ratio(ingredients: list[str], pantry_set: set) -> float:
    if not ingredients:
        return 0.0
    hits = sum(1 for ing in ingredients if any(p in ing or ing in p for p in pantry_set))
    return hits / len(ingredients)


def _meaningful_ings(recipe: dict) -> set:
    """Ingredient set minus universal staples — used for meaningful overlap scoring."""
    return {ing for ing in recipe["ingredients"] if ing not in UNIVERSAL_STAPLES}


_DESSERT_TITLES = {
    "pie", "cake", "pudding", "tart", "tarte", "waffle", "cookie", "biscuit",
    "pastry", "custard", "ice cream", "sorbet", "mousse", "brownie", "muffin",
    "scone", "donut", "charlotte", "compote", "strudel", "torte", "clafouti",
    "syllabub", "blancmange", "blanc-mange", "flan", "praline", "macaroon",
    "meringue", "bavaroise", "junket", "jelly dessert",
}
_PROTEIN_WORDS = {
    "chicken", "beef", "pork", "lamb", "fish", "salmon", "shrimp", "tofu",
    "egg", "eggs", "cheese", "lentil", "chickpea", "turkey", "duck", "crab",
    "tuna", "anchovy", "sausage", "ham", "cod", "halibut", "bass", "tilapia",
    "clam", "mussel", "lobster", "scallop", "squid", "octopus", "veal",
    "venison", "rabbit", "quail", "tempeh", "seitan", "prawn", "steak",
}
_MEAT_WORDS = {
    "chicken", "beef", "pork", "lamb", "veal", "turkey", "duck", "bacon",
    "ham", "sausage", "prosciutto", "salami", "pepperoni", "venison", "rabbit",
    "quail", "goose", "pheasant", "mutton", "lard",
}
_ANIMAL_WORDS = _MEAT_WORDS | {
    "egg", "eggs", "milk", "cream", "cheese", "butter", "yogurt", "honey",
    "gelatin", "lard", "ghee",
}
_KETO_EXCLUDE = {
    "pasta", "rice", "bread", "flour", "sugar", "potato", "corn", "oats",
    "cereal", "crackers", "tortilla", "noodle", "waffle", "pancake", "biscuit",
    "rye", "wheat", "barley", "semolina", "couscous",
}
_LIGHT_MEAL_TITLES = {
    "soup", "broth", "consomme", "punch", "grog", "lemonade", "cocktail",
    "drink", "beverage", "juice", "salad", "slaw", "compote",
}

# Dietary mode detection
_DIETARY_TRIGGERS: dict = {
    "omad":        ["omad", "one meal a day", "one meal per day"],
    "keto":        ["keto", "ketogenic", "low carb", "low-carb"],
    "vegetarian":  ["vegetarian", "veggie", "no meat"],
    "vegan":       ["vegan", "plant based", "plant-based"],
    "gluten_free": ["gluten free", "gluten-free", "no gluten", "celiac"],
    "paleo":       ["paleo", "primal"],
    "dairy_free":  ["dairy free", "dairy-free", "no dairy", "lactose free"],
}

# Must-include dish type patterns
_MUST_INCLUDE_RE = re.compile(
    r'\b(?:a|some|want|have|having|eat|eating|include|with)\s+'
    r'(stew|soup|roast|curry|casserole|braise|grill|bake|pasta|noodle|'
    r'dumpling|sandwich|burger|salad|rice dish|fried rice|stir.?fry)\b',
    re.IGNORECASE,
)
_REPEAT_RE = re.compile(
    r'\b(?:one or two|1 or 2|couple(?:\s+of)?|two|2|three|3)\s+days?\b',
    re.IGNORECASE,
)
# "rice in every dish" / "chicken in all meals" pattern
_MUST_ALL_RE = re.compile(
    r'\b(\w+)\s+in\s+(?:every|all|each)\s+(?:dish|meal|recipe|day)\b',
    re.IGNORECASE,
)


def detect_dietary_modes(query: str, profile: dict) -> set:
    """Merge dietary modes from query with profile.dietary."""
    ql = query.lower()
    modes = set(profile.get("dietary", []))
    for mode, triggers in _DIETARY_TRIGGERS.items():
        if any(t in ql for t in triggers):
            modes.add(mode)
    return modes


def detect_must_include(query: str) -> dict:
    """Parse specific dish requests. Returns {dish: repeat_count}.
    'a stew i can have for two days' → {'stew': 2}
    """
    must: dict = {}
    for m in _MUST_INCLUDE_RE.finditer(query):
        dish = m.group(1).lower().rstrip("ed").strip()
        repeat = 2 if _REPEAT_RE.search(query) else 1
        must[dish] = repeat
    return must


def detect_must_in_all(query: str) -> list:
    """Parse 'rice in every dish' → ['rice']."""
    return [m.group(1).lower() for m in _MUST_ALL_RE.finditer(query)]


def apply_dietary_filter(pool: list, dietary: set) -> list:
    """Filter recipe pool based on active dietary restrictions."""
    if not dietary:
        return pool
    out = []
    for r in pool:
        ings  = " ".join(i.lower() for i in r.get("ingredients", []))
        title = r["title"].lower()
        if "keto" in dietary and any(k in ings or k in title for k in _KETO_EXCLUDE):
            continue
        if ("vegetarian" in dietary or "vegan" in dietary) and \
                any(m in ings for m in _MEAT_WORDS):
            continue
        if "vegan" in dietary and any(a in ings for a in _ANIMAL_WORDS):
            continue
        out.append(r)
    return out if out else pool  # never return empty


def update_profile_from_rejection(text: str, profile: dict):
    """Extract disliked items from a rejection message and persist to profile."""
    ql = text.lower()
    dislikes = set(profile.get("preferences", {}).get("dislikes", []))
    # Patterns: "no brains", "don't want offal", "hate liver", "avoid X"
    for pat in [
        r'\bno\s+([a-z]+(?:\s+[a-z]+)?)\b',
        r"\bdon'?t\s+want\s+([a-z]+(?:\s+[a-z]+)?)\b",
        r'\bhate\s+([a-z]+(?:\s+[a-z]+)?)\b',
        r'\bavoid\s+([a-z]+(?:\s+[a-z]+)?)\b',
        r'\bwithout\s+([a-z]+(?:\s+[a-z]+)?)\b',
    ]:
        for m in re.finditer(pat, ql):
            word = m.group(1).strip()
            _skip = {"to", "the", "a", "an", "it", "i", "we", "this", "that",
                     "there", "desserts", "happy", "idea", "clue", "way"}
            if word not in _skip and len(word) > 2:
                dislikes.add(word)
    profile.setdefault("preferences", {})["dislikes"] = list(dislikes)
    save_profile(profile)


def maybe_collect_profile_data(profile: dict) -> dict:
    """If profile user is missing caloric data, offer to collect anthropometrics."""
    user = next((m for m in profile.get("household", []) if m.get("id") == "user"), None)
    if not user:
        return profile
    # Already have what we need
    if user.get("daily_kcal") or all(
            user.get(k) for k in ("sex", "age", "height_cm", "weight_kg")):
        return profile
    if profile.get("onboarded"):
        return profile  # skipped before — don't re-ask

    print("\nTo give you accurate calorie targets, I need a few details.")
    print("(Press Enter to skip)\n")

    def _ask(prompt: str):
        try:
            return input(f"  {prompt}: ").strip() or None
        except (KeyboardInterrupt, EOFError):
            return None

    sex    = _ask("Sex (m/f)")
    age    = _ask("Age in years")
    weight = _ask("Weight  (kg or lbs, e.g. '85kg' or '185lbs')")
    height = _ask("Height  (cm or ft, e.g. '178cm' or '5ft10')")
    act    = _ask("Activity level (sedentary/light/moderate/active/very_active)")

    if sex:    user["sex"]      = sex.lower()[:1]
    if age:
        try:   user["age"]      = int(re.sub(r"[^0-9]", "", age))
        except ValueError: pass
    if weight:
        wl = weight.lower()
        num = re.sub(r"[^0-9.]", "", wl)
        try:
            kg = float(num) * 0.453592 if "lb" in wl else float(num)
            user["weight_kg"] = round(kg, 1)
        except ValueError: pass
    if height:
        hl = height.lower()
        num = re.sub(r"[^0-9.]", "", hl)
        try:
            if "ft" in hl or "'" in hl:
                parts = re.match(r'(\d+)\D+(\d*)', hl)
                if parts:
                    ft, inch = int(parts.group(1)), int(parts.group(2) or 0)
                    user["height_cm"] = round((ft * 12 + inch) * 2.54, 1)
            else:
                user["height_cm"] = float(num)
        except ValueError: pass
    if act:    user["activity"] = act.lower()

    if all(user.get(k) for k in ("sex", "age", "height_cm", "weight_kg")):
        user["daily_kcal"] = round(_tdee_from_member(user))
        print(f"\n  TDEE: {user['daily_kcal']} kcal/day")
    elif user.get("daily_kcal") is None:
        try:
            raw = _ask("Or just enter your daily calorie target directly")
            if raw:
                user["daily_kcal"] = int(re.sub(r"[^0-9]", "", raw))
        except ValueError: pass

    profile["onboarded"] = True
    save_profile(profile)
    print()
    return profile


def is_dessert(recipe: dict) -> bool:
    t = recipe["title"].lower()
    return any(kw in t for kw in _DESSERT_TITLES)


def has_protein(recipe: dict) -> bool:
    ings = " ".join(i.lower() for i in recipe["ingredients"])
    return any(p in ings for p in _PROTEIN_WORDS)


def pick_week(candidates: list[dict], pantry: list[str], days: int = 7,
             must_include: dict = None, dietary: set = None,
             must_in_all: list = None) -> list[dict]:
    """Greedy selection with diversity guards + dietary awareness.

    must_include: {dish_keyword: repeat_count} — e.g. {'stew': 2}
    dietary:      set of active modes — omad, keto, vegetarian, etc.
    must_in_all:  keywords every chosen recipe should contain — e.g. ['rice']
    """
    if not candidates:
        return []

    _omad   = dietary and "omad" in dietary
    pantry_set = set(pantry)

    # ── Pre-lock must_include dishes ────────────────────────────────────────
    pre_chosen: list = []
    remaining = list(candidates)
    if must_include:
        for dish, count in must_include.items():
            matches = [c for c in remaining
                       if (dish in c["title"].lower() or
                           any(dish in i.lower() for i in c.get("ingredients", [])))
                       and not is_dessert(c)]
            if matches:
                best = next((c for c in matches if has_protein(c)), matches[0])
                for _ in range(min(count, days - len(pre_chosen))):
                    pre_chosen.append(best)
                remaining = [c for c in remaining if c is not best]
        candidates = pre_chosen + remaining

    if len(candidates) <= days:
        return candidates[:days]

    n = len(candidates)

    first_scored = []
    for i, c in enumerate(candidates):
        rank_score   = 1.0 - (i / max(n - 1, 1))
        pantry_score = _pantry_overlap_ratio(c["ingredients"], pantry_set)
        s = W_PANTRY * pantry_score + W_SEMANTIC * rank_score + W_VARIETY * 1.0
        first_scored.append((s, c))
    first_scored.sort(key=lambda x: -x[0])

    pool   = [c for _, c in first_scored]
    chosen = list(pre_chosen)
    # Remove pre-chosen from pool
    pre_paths = {c["path"] for c in pre_chosen}
    pool = [c for c in pool if c["path"] not in pre_paths]
    if not chosen:
        chosen = [pool.pop(0)]

    sel_m_ing = set()
    for c in chosen:
        sel_m_ing.update(_meaningful_ings(c))
    cuisines = [c["cuisine"].lower() for c in chosen]
    book_cnt = Counter(c["book"] for c in chosen)

    while len(chosen) < days and pool:
        best_s, best_i = -1e9, 0
        dessert_count = sum(1 for r in chosen if is_dessert(r))
        protein_count = sum(1 for r in chosen if has_protein(r))
        for j, c in enumerate(pool):
            ing_ov      = len(_meaningful_ings(c) & sel_m_ing)
            variety_pen = 0.5 if c["cuisine"].lower() in cuisines else 0.0
            book_pen    = 2.0 * max(0, book_cnt.get(c["book"], 0) - 1)
            dessert_pen = 4.0 if is_dessert(c) and dessert_count >= 2 else 0.0
            protein_bon = 1.5 if has_protein(c) and protein_count < 4 else 0.0
            # OMAD: penalize light meals (soups, drinks, salads)
            light_pen   = 3.0 if _omad and any(
                kw in c["title"].lower() for kw in _LIGHT_MEAL_TITLES) else 0.0
            # must_in_all: every recipe must contain these keywords
            all_pen     = 5.0 if must_in_all and not all(
                kw in c["title"].lower() or
                any(kw in i.lower() for i in c.get("ingredients", []))
                for kw in must_in_all) else 0.0
            orig_rank   = next((k for k, x in enumerate(candidates) if x is c), n)
            rank_score  = 1.0 - (orig_rank / max(n - 1, 1))
            s = (ing_ov - variety_pen - book_pen - dessert_pen - light_pen - all_pen
                 + protein_bon + W_SEMANTIC * rank_score)
            if s > best_s:
                best_s, best_i = s, j
        nxt = pool.pop(best_i)
        chosen.append(nxt)
        sel_m_ing.update(_meaningful_ings(nxt))
        cuisines.append(nxt["cuisine"].lower())
        book_cnt[nxt["book"]] += 1

    return chosen


def overlap_score(recipes: list[dict]) -> tuple[int, set]:
    if not recipes:
        return 0, set()
    sets    = [set(r["ingredients"]) for r in recipes]
    all_ing = set().union(*sets)
    multi   = {ing for ing in all_ing if sum(1 for s in sets if ing in s) >= 2}
    if not all_ing:
        return 0, set()
    # formula mirrors backend/menu_engine.compute_overlap_score
    base = (1.0 - len(all_ing) / max(sum(len(s) for s in sets), 1)) * 100.0
    return max(0, min(100, round(base))), multi


# ── Verbatim recipe display ───────────────────────────────────────────────────
def get_recipe_sections(path_str: str) -> str:
    """Read MD file, return ## Ingredients + ## Instructions verbatim."""
    try:
        text  = Path(path_str).read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        out   = []
        in_sec = False
        for line in lines:
            if re.match(r"^##\s+(Ingredients|Instructions|Method|Directions)", line,
                        re.IGNORECASE):
                in_sec = True
                out.append(line)
                continue
            if in_sec:
                # stop before Notes / Tags / Nutrition / unrelated sections
                if re.match(r"^##\s+", line) and not re.match(
                        r"^##\s+(Ingredients|Instructions|Method|Directions)", line,
                        re.IGNORECASE):
                    out.append("")
                    break
                if line.startswith("# "):
                    break
                out.append(line)
        return "\n".join(out).strip()
    except Exception:
        return ""


def format_week_summary(week: list[dict]) -> str:
    """Week plan table + overlap stats — no recipe content."""
    score, shared = overlap_score(week)

    lines = ["\nWEEK PLAN", "─" * 45]
    for day, r in zip(DAYS, week):
        lines.append(f"{day:<12}{r['title']}")

    lines.append("")
    top5 = sorted(shared)[:5]
    rest = max(0, len(shared) - 5)
    shared_str = ", ".join(top5) + (f" (+{rest} more)" if rest else "")
    lines.append(f"Shared ingredients: {shared_str or 'none'}")
    lines.append(f"Ingredient overlap score: {score}/100")
    lines.append("\nType 'shopping' to get the list.")
    return "\n".join(lines)


def format_week_recipes(week: list[dict]) -> str:
    """Full verbatim recipe content for all 7 days."""
    lines = ["\n" + "═" * 45]
    for day, r in zip(DAYS, week):
        book    = r["book"].replace("_", " ")
        lines.append(f"\n{day.upper()} — {r['title']}")
        lines.append(f"Source: {book}")
        lines.append("─" * 45)
        content = get_recipe_sections(r["path"])
        if content:
            lines.append(content)
        else:
            lines.append("## Ingredients")
            for ing in r["ingredients"]:
                lines.append(f"- {ing}")
    return "\n".join(lines)


# ── Shopping list ─────────────────────────────────────────────────────────────
def build_shopping_list(week: list[dict], pantry: list[str]) -> str:
    all_ings: set[str] = set()
    for r in week:
        try:
            full = Path(r["path"]).read_text(encoding="utf-8", errors="ignore")
            in_ing = False
            for line in full.splitlines():
                if re.match(r"^##\s+Ingredients", line, re.IGNORECASE):
                    in_ing = True
                    continue
                if in_ing:
                    if line.startswith("##") or line.startswith("# "):
                        break
                    stripped = line.strip()
                    if stripped.startswith(("-", "•", "*")):
                        ing = normalize_ingredient(stripped)
                        if ing:
                            all_ings.add(ing)
        except Exception:
            all_ings.update(r["ingredients"])

    need = {ing for ing in all_ings
            if not any(p in ing or ing in p for p in pantry)}

    cats: dict[str, list[str]] = {
        "Produce": [], "Protein": [], "Pantry": [], "Other": []
    }
    for ing in sorted(need):
        cats[categorize(ing)].append(ing)

    lines = ["\nSHOPPING LIST", "─" * 45]
    for cat, items in cats.items():
        if items:
            lines.append(f"\n{cat}:")
            for item in items:
                lines.append(f"  ☐ {item}")
    if pantry:
        lines.append(f"\n(Pantry items already excluded: {len(all_ings) - len(need)} items)")
    return "\n".join(lines)


# ── LLM ───────────────────────────────────────────────────────────────────────
def build_context(results: list[dict], pantry: list[str]) -> str:
    pantry_set = set(pantry)
    lines = []
    for r in results:
        ings      = r["ingredients"][:8]
        has_pantry = any(any(p in ing or ing in p for p in pantry_set) for ing in ings)
        tag       = " (pantry match)" if has_pantry else ""
        book      = r["book"].replace("_", " ")
        lines.append(f"• {r['title']} [{book}]{tag} — {', '.join(ings)}")
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are Aboyeur, a culinary assistant with a 50,000-recipe personal cookbook library. "
    "Respond concisely — no bullet-point walls, no markdown tables, no lengthy preambles. "
    "CRITICAL: You may ONLY reference, discuss, or recommend recipes that appear in the "
    "recipe context provided to you. Never invent, fabricate, or suggest recipes not "
    "explicitly listed in the context. If the context lacks good options, say so honestly. "
    "Rules: reference recipes by exact title and book source; show the first 3 ingredients "
    "when recommending a dish; be direct and enthusiastic; never ask the user to pick from "
    "a list you just generated — make the call yourself; never produce tables or excessive "
    "headers; highlight when a recipe uses pantry items the user already has."
)


def stream_response(messages: list[dict]) -> str:
    stream = client.chat.completions.create(
        model=LLM_MODEL, messages=messages, stream=True,
        temperature=0.7, extra_body=EXTRA_BODY,
    )
    full = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            print(delta.content, end="", flush=True)
            full.append(delta.content)
    print()
    return "".join(full)


# ── Commands ──────────────────────────────────────────────────────────────────
def cmd_show(arg: str, index: list[dict]) -> str:
    arg_l = arg.lower()
    best, best_score = None, 0
    for r in index:
        title_l = r["title"].lower()
        if arg_l == title_l:
            best = r
            break
        hits = sum(1 for w in arg_l.split() if w in title_l)
        if hits > best_score or (arg_l in title_l and hits >= best_score):
            best_score = max(hits, 1)
            best = r
    if not best:
        return f"No recipe found matching '{arg}'"
    try:
        content = Path(best["path"]).read_text(encoding="utf-8", errors="ignore")
        book    = best["book"].replace("_", " ")
        return f"\n── {best['title']} ({book}) ──\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


def cmd_books(index: list[dict]) -> str:
    counts = Counter(r["book"] for r in index)
    lines  = [f"\n{'BOOK':<55} RECIPES", "─" * 65]
    for book, count in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"{book.replace('_',' ')[:54]:<55} {count:>5}")
    lines.append(f"\n{len(counts)} books, {len(index):,} recipes total")
    return "\n".join(lines)


def cmd_help() -> str:
    return """
Commands:
  show <recipe name>     — print full recipe from file (verbatim)
  books                  — list all books with recipe count
  week                   — plan 7 dinners with max ingredient overlap
  pantry: <items>        — save pantry items (comma-separated)
  shopping               — shopping list for current week plan
  clear                  — clear terminal
  help                   — this list
  Ctrl+C                 — exit

Chat: "northern Spanish dinners", "I have chicken and ginger",
      "something with miso", "Japanese next week", "French menu"
"""


# ── Intent detection ──────────────────────────────────────────────────────────
_WEEK_PHRASES = [
    "next week", "this week", "upcoming week", "week of", "week plan",
    "weekly menu", "weekly dinner", "week of dinner", "plan my week",
    "plan the week", "plan a week", "7 dinners", "seven dinners",
    "7 meals", "seven meals", "7 days", "seven days", "plan my meals",
    "plan meals for", "menu for the week", "menu for next", "weekly plan",
    "week menu",
]


def detect_intent(msg: str, index: list[dict]) -> str:
    ml = msg.lower().strip()

    if ml in ("week", "weekly", "week plan"):           return "week"
    if ml == "shopping":                                 return "shopping"
    if ml == "books":                                    return "books"
    if ml == "help":                                     return "help"
    if ml == "clear":                                    return "clear"
    if ml.startswith("show "):                           return "show"
    if ml.startswith("pantry:"):                         return "pantry"
    if any(p in ml for p in _WEEK_PHRASES):              return "week"
    if "shopping" in ml and any(w in ml for w in ("list","buy","need")): return "shopping"

    if len(detect_ingredients_in_query(msg)) >= 2:       return "ingredient"
    if detect_book_in_query(msg, index):                 return "book"
    return "search"


# ── Main REPL ─────────────────────────────────────────────────────────────────
def main():
    session_id = str(uuid.uuid4())[:8]
    setup_session_log(session_id)
    _slog("startup")

    print()
    print("Aboyeur — Culinary AI")

    index   = load_index()
    pantry  = load_pantry()
    profile = load_profile()

    book_count = len(Counter(r["book"] for r in index))
    print(f"{len(index):,} recipes from {book_count} cookbooks")
    print("Type 'help' for commands, Ctrl+C to exit")
    print("─" * 45)
    print()

    history: list[dict] = []
    current_week: list[dict] = []

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBon appétit.")
            _slog("exit")
            if _session_log:
                _session_log.close()
            break

        if not user_input:
            continue

        intent = detect_intent(user_input, index)
        _slog(f"[{intent}] {user_input[:120]}")

        # ── Special commands ──────────────────────────────────────────────────
        if intent == "clear":
            os.system("clear")
            continue

        if intent == "help":
            print(cmd_help())
            continue

        if intent == "books":
            out = cmd_books(index)
            print(out)
            log_interaction("system", out, intent="books", session_id=session_id)
            continue

        if intent == "show":
            arg = user_input[5:].strip()
            out = cmd_show(arg, index)
            print(out)
            log_interaction("system", f"show: {arg}", intent="show", session_id=session_id)
            continue

        if intent == "pantry":
            items_raw = user_input[7:].strip()
            new_items = [i.strip().lower() for i in items_raw.split(",") if i.strip()]
            pantry    = list(set(pantry + new_items))
            save_pantry(pantry)
            msg = f"Pantry updated: {len(pantry)} items ({', '.join(new_items)} added)"
            print(msg)
            log_interaction("system", msg, intent="pantry", session_id=session_id)
            continue

        if intent == "shopping":
            if not current_week:
                print("No week plan yet. Type 'week' first.")
                continue
            out = build_shopping_list(current_week, pantry)
            print(out)
            log_interaction("system", out, intent="shopping", session_id=session_id)
            continue

        # ── Search ────────────────────────────────────────────────────────────
        log_interaction("user", user_input, intent=intent, session_id=session_id)

        if intent == "week":
            results, found_c, missing_c = multi_cuisine_week_search(
                user_input, index, pantry, n=30
            )
            if missing_c:
                available_cuisines = get_available_cuisines(index)
                for missing in missing_c:
                    print(f"I don't have any {missing.title()} recipes in the library.")
                    print(f"Asking the LLM for the closest match...", flush=True)
                    sub = suggest_substitute_cuisine(missing, available_cuisines)
                    if sub:
                        print(f"Closest available: {sub.title()}. Adding those.")
                        sub_results = cuisine_search(
                            sub.lower(), index, pantry, keywords(user_input), n=15
                        )
                        results = dedup_results(results + sub_results)
                        found_c.append(f"{sub} (sub for {missing})")
                    _slog(f"cuisine fallback: {missing} → {sub}")
            if len(results) < 7:
                results = diverse_random_pool(index, size=50)

            # Adaptive per-book cap:
            #   ≥3 distinct books in pool → cap at 3 (prevent clustering)
            #   1-2 books (narrow cuisine pool) → cap at 6 (don't kill the pool)
            _unique_books = len(set(r["book"] for r in results))
            _per_book_cap = 3 if _unique_books >= 3 else 6
            _bk: dict = {}
            _capped: list = []
            for r in results:
                bc = _bk.get(r["book"], 0)
                if bc < _per_book_cap:
                    _capped.append(r)
                    _bk[r["book"]] = bc + 1

            # Supplement thin pool with title-keyword search (cuisine-aware, not random)
            if len(_capped) < 14:
                _seen = {r["path"] for r in _capped}
                _cuisine_kws = detect_cuisine_hints(user_input)
                _title_hits: list = []
                for kw in _cuisine_kws:
                    for r in index:
                        if kw in r["title"].lower() and r["path"] not in _seen:
                            _title_hits.append(r)
                            _seen.add(r["path"])
                random.shuffle(_title_hits)
                _capped.extend(_title_hits[:40])
                # Last resort: random fill only if still thin
                if len(_capped) < 14:
                    for r in diverse_random_pool(index, size=40):
                        if r["path"] not in {x["path"] for x in _capped}:
                            _capped.append(r)
            results = _capped

        elif intent == "ingredient":
            ings    = detect_ingredients_in_query(user_input)
            results = ingredient_search(ings, index, pantry, n=15)
            if not results:
                results = search_recipes(user_input, index, pantry, n=15)
        elif intent == "book":
            book    = detect_book_in_query(user_input, index) or ""
            results = book_search(book, index, n=20)
        else:
            results = search_recipes(user_input, index, pantry, n=15)

        if not results:
            print("Aboyeur: No matching recipes found. Try different keywords.\n")
            continue

        # ── Build LLM messages ────────────────────────────────────────────────
        if intent == "week":
            # Detect dietary modes + must-include and persist to profile
            dietary     = detect_dietary_modes(user_input, profile)
            must_inc    = detect_must_include(user_input)
            must_all    = detect_must_in_all(user_input)
            if dietary != set(profile.get("dietary", [])):
                profile["dietary"] = list(dietary)
                # Update meals_per_day from OMAD if detected
                if "omad" in dietary:
                    profile["meals_per_day"] = 1
                save_profile(profile)
            # First OMAD request → ask for caloric data if missing
            if "omad" in dietary and not profile.get("onboarded"):
                profile = maybe_collect_profile_data(profile)
            # Apply dietary filter to pool BEFORE pick_week
            filtered = apply_dietary_filter(results, dietary)
            current_week = pick_week(
                filtered, pantry, days=7,
                must_include=must_inc or None,
                dietary=dietary or None,
                must_in_all=must_all or None,
            )
            print(format_week_summary(current_week))
            context  = build_context(current_week, pantry)
            dietary_note = ""
            if "omad" in dietary:
                dietary_note += (" OMAD day — flag any meal that's calorie-light "
                                 "or protein-deficient for a single daily meal.")
            if "keto" in dietary:
                dietary_note += " Keto — confirm each dish is low-carb."
            if "vegetarian" in dietary:
                dietary_note += " Vegetarian — confirm no meat."
            if must_inc:
                dishes = ", ".join(f"{k}×{v}" for k, v in must_inc.items())
                dietary_note += f" User requested: {dishes} in the plan."
            user_msg = (
                f'The 7-day meal plan above has been proposed. '
                f'User asked: "{user_input}". '
                f'In 3-4 sentences, comment on why this lineup works — '
                f'highlight shared ingredients, cuisine balance, and nutritional variety.{dietary_note} '
                f'Do not list the recipes again. No tables.'
            )
        else:
            context  = build_context(results, pantry)
            user_msg = user_input

        # Inject profile context into system prompt
        prof_line  = profile_injection(profile)
        system_msg = SYSTEM_PROMPT
        if prof_line:
            system_msg += f"\n\nUser profile: {prof_line}"
        if pantry:
            system_msg += f"\nPantry: {', '.join(pantry)}"

        messages = [{"role": "system", "content": system_msg}]
        messages.extend(history[-6:])
        messages.append({"role": "user",
                         "content": f"Recipes from library:\n{context}\n\nRequest: {user_msg}"})

        print("\nAboyeur: ", end="", flush=True)
        response = stream_response(messages)

        # ── Confirmation gate before dumping full recipe content ──────────────
        if intent == "week":
            log_interaction("assistant", response, intent=intent,
                            results_count=len(results), session_id=session_id)
            _slog(f"[response] {len(response)} chars, {len(results)} results")
            history.append({"role": "user",      "content": user_input})
            history.append({"role": "assistant", "content": response})

            def _is_approval(s: str) -> bool:
                sl = s.lower().strip()
                if not sl:
                    return False
                _YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "good",
                        "great", "perfect", "fine", "approved", "love it", "si", "oui"}
                if sl in _YES or sl.startswith("yes"):
                    return True
                return any(w in sl for w in ("good menu", "great menu", "looks great",
                                              "i like", "i love", "that works",
                                              "let's do", "sounds good", "love this",
                                              "this is good", "this works"))

            def _refine_week(refined_query: str) -> list:
                """Re-search + adaptive book-cap + title-keyword supplement."""
                ref_results, _rf, _rm = multi_cuisine_week_search(
                    refined_query, index, pantry, n=30
                )
                if not ref_results:
                    ref_results = results
                _ub2 = len(set(r["book"] for r in ref_results))
                _pbcap2 = 3 if _ub2 >= 3 else 6
                _bc2: dict = {}
                _cp2: list = []
                for r in ref_results:
                    bc = _bc2.get(r["book"], 0)
                    if bc < _pbcap2:
                        _cp2.append(r)
                        _bc2[r["book"]] = bc + 1
                if len(_cp2) < 14:
                    _s2 = {r["path"] for r in _cp2}
                    for kw in detect_cuisine_hints(refined_query):
                        for r in index:
                            if kw in r["title"].lower() and r["path"] not in _s2:
                                _cp2.append(r)
                                _s2.add(r["path"])
                    if len(_cp2) < 14:
                        for r in diverse_random_pool(index, size=40):
                            if r["path"] not in {x["path"] for x in _cp2}:
                                _cp2.append(r)
                return pick_week(_cp2, pantry, days=7)

            # ── Round 1 approval ─────────────────────────────────────────────
            print("\n\nHappy with this menu? (yes to see full recipes / describe what to change)")
            try:
                confirm = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBon appétit.")
                if _session_log:
                    _session_log.close()
                break

            if _is_approval(confirm):
                print(format_week_recipes(current_week))
                _slog("menu approved round 1")
                print()
                continue

            # ── Round 2: one refinement attempt ──────────────────────────────
            _slog(f"menu rejected: {confirm[:80]}")
            # Learn dislikes from rejection text
            update_profile_from_rejection(confirm, profile)
            refined_query = f"{user_input}. {confirm}"
            log_interaction("user", refined_query, intent="week", session_id=session_id)

            current_week = _refine_week(refined_query)
            print(format_week_summary(current_week))

            omad2 = ("omad" in refined_query.lower() or
                     "one meal a day" in refined_query.lower())
            omad_note2 = (" OMAD constraint — flag calorie-light or protein-missing days."
                          if omad2 else "")
            ctx2 = build_context(current_week, pantry)
            msg2 = [{"role": "system", "content": system_msg}]
            msg2.extend(history[-6:])
            msg2.append({"role": "user", "content": (
                f"Recipes from library:\n{ctx2}\n\n"
                f"Revised plan based on: '{confirm}'. Original: '{user_input}'.{omad_note2} "
                f"Comment in 3-4 sentences on why this revised lineup works. No tables."
            )})
            print("\nAboyeur: ", end="", flush=True)
            response2 = stream_response(msg2)

            log_interaction("assistant", response2, intent="week",
                            results_count=len(current_week), session_id=session_id)
            history.append({"role": "user",      "content": refined_query})
            history.append({"role": "assistant", "content": response2})

            print("\n\nHappy with this revised menu? (yes for full recipes / type a new request)")
            try:
                confirm2 = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nBon appétit.")
                if _session_log:
                    _session_log.close()
                break

            if _is_approval(confirm2):
                print(format_week_recipes(current_week))
                _slog("menu approved round 2")
            else:
                # Two strikes — restart cleanly. User types fresh next iteration.
                _slog(f"menu rejected round 2: {confirm2[:80]}")
                print("\nAboyeur: Got it — let's start fresh. What are you craving?")
                if confirm2:
                    history.append({"role": "user", "content": confirm2})
            print()
            continue

        log_interaction("assistant", response, intent=intent,
                        results_count=len(results), session_id=session_id)
        _slog(f"[response] {len(response)} chars, {len(results)} results")

        history.append({"role": "user",      "content": user_input})
        history.append({"role": "assistant", "content": response})

        if results and intent != "week":
            titles = [r["title"] for r in results[:3]]
            print(f"\n[Found: {' · '.join(titles)}{'...' if len(results) > 3 else ''}]")
        print()


if __name__ == "__main__":
    main()
