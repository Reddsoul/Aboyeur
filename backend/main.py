"""Aboyeur FastAPI app — all HTTP endpoints + /chat SSE agent + MCP SSE mount.

Run: uvicorn backend.main:app --host 0.0.0.0 --port 8000
Imports use the `backend.` package (PYTHONPATH=/app in Docker).
"""
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend import llm, macro_engine, menu_engine, preference_engine, rag
from backend.models import (
    Book, Recipe, PantryItem, Menu, IngestJob, init_db, get_db, SessionLocal,
)
from backend.utils import categorize_ingredient, slugify

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aboyeur")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BOOKS_DIR = PROJECT_ROOT / "books"
CORPUS_DIR = BOOKS_DIR / "corpus"

BASE_SYSTEM = (
    "You are Aboyeur, a culinary planning assistant working from the user's personal "
    "cookbook library. You plan weekly dinners, optimize ingredient overlap, respect "
    "household nutrition targets, and produce pantry-aware shopping lists. "
    "Always label recipes: '[Curated Recipe]' for library recipes, '[Fusion Concept]' "
    "for generated ideas. NEVER invent calorie counts or portion numbers yourself — use "
    "only the numbers returned by tools."
)

app = FastAPI(title="Aboyeur", version="1.0.0")

# In-memory current draft menu (single-user, self-hosted). Approved menus persist to DB.
_DRAFT: dict = {}


@app.on_event("startup")
def _startup():
    init_db()
    # interaction_log.jsonl must exist (Phase 1.2) — create if missing so logging never errors.
    log = preference_engine.LOG_PATH
    if not log.exists():
        log.parent.mkdir(parents=True, exist_ok=True)
        log.touch()
    logger.info("Aboyeur up. Recipes in Chroma: %s", rag.count())


# ===========================================================================
# Stats / books
# ===========================================================================

@app.get("/stats")
def stats(db: Session = Depends(get_db)):
    return {
        "books": db.query(Book).count(),
        "recipes": db.query(Recipe).count(),
        "pantry_items": db.query(PantryItem).count(),
        "chroma_recipes": rag.count(),
    }


@app.get("/books")
def list_books(db: Session = Depends(get_db)):
    return [_book_dict(b) for b in db.query(Book).order_by(Book.title).all()]


@app.get("/books/{book_id}")
def get_book(book_id: str, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    d = _book_dict(b)
    d["recipe_count"] = db.query(Recipe).filter(Recipe.book_id == book_id).count()
    return d


class BookPatch(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    cuisine: Optional[list] = None
    tier: Optional[int] = None
    flavor_profiles: Optional[list] = None
    key_ingredients: Optional[list] = None
    complexity: Optional[str] = None


@app.patch("/books/{book_id}")
def patch_book(book_id: str, patch: BookPatch, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    for k, v in patch.model_dump(exclude_none=True).items():
        setattr(b, k, v)
    db.commit()
    return _book_dict(b)


@app.delete("/books/{book_id}")
def delete_book(book_id: str, db: Session = Depends(get_db)):
    b = db.get(Book, book_id)
    if not b:
        raise HTTPException(404, "book not found")
    rag.delete_book(book_id)
    db.query(Recipe).filter(Recipe.book_id == book_id).delete()
    db.delete(b)
    db.commit()
    folder = CORPUS_DIR / book_id
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return {"deleted": book_id}


def _book_dict(b: Book) -> dict:
    return {
        "id": b.id, "title": b.title, "author": b.author, "cuisine": b.cuisine or [],
        "tier": b.tier, "flavor_profiles": b.flavor_profiles or [],
        "key_ingredients": b.key_ingredients or [], "complexity": b.complexity,
        "ingested_at": b.ingested_at.isoformat() if b.ingested_at else None,
    }


# ===========================================================================
# Pantry
# ===========================================================================

class PantryAdd(BaseModel):
    text: str


@app.post("/pantry/add")
def pantry_add(body: PantryAdd, db: Session = Depends(get_db)):
    items = _parse_pantry_text(body.text)
    added = []
    for it in items:
        name = (it.get("name") or "").strip().lower()
        if not name:
            continue
        expires = _parse_date(it.get("expires_at"))
        row = PantryItem(
            name=name, quantity=it.get("quantity", "") or "",
            category=it.get("category") or categorize_ingredient(name),
            expires_at=expires,
        )
        db.add(row)
        added.append(name)
    db.commit()
    return {"added": added, "count": len(added)}


@app.get("/pantry")
def pantry_list(db: Session = Depends(get_db)):
    return [_pantry_dict(p) for p in db.query(PantryItem).order_by(PantryItem.category).all()]


@app.delete("/pantry/{item_id}")
def pantry_delete(item_id: int, db: Session = Depends(get_db)):
    p = db.get(PantryItem, item_id)
    if not p:
        raise HTTPException(404, "item not found")
    db.delete(p)
    db.commit()
    return {"deleted": item_id}


@app.post("/pantry/clear")
def pantry_clear(db: Session = Depends(get_db)):
    n = db.query(PantryItem).delete()
    db.commit()
    return {"cleared": n}


@app.get("/pantry/expiring")
def pantry_expiring(days: int = Query(3, ge=0), db: Session = Depends(get_db)):
    cutoff = datetime.utcnow() + timedelta(days=days)
    rows = (db.query(PantryItem)
            .filter(PantryItem.expires_at.isnot(None), PantryItem.expires_at <= cutoff)
            .order_by(PantryItem.expires_at).all())
    return [_pantry_dict(p) for p in rows]


def _pantry_dict(p: PantryItem) -> dict:
    return {"id": p.id, "name": p.name, "quantity": p.quantity, "category": p.category,
            "expires_at": p.expires_at.isoformat() if p.expires_at else None}


def _parse_pantry_text(text: str) -> list[dict]:
    """LLM parses freeform pantry text; comma-split fallback if LLM unavailable."""
    try:
        parsed = llm.chat_json([
            {"role": "system", "content":
                "Extract pantry items as JSON only: "
                '{"items":[{"name":"","quantity":"","category":"produce|protein|dairy|pantry|spice|frozen","expires_at":"YYYY-MM-DD or null"}]}.'},
            {"role": "user", "content": text},
        ])
        items = parsed.get("items")
        if isinstance(items, list) and items:
            return items
    except Exception as e:
        logger.warning("pantry LLM parse failed: %s", e)
    return [{"name": part.strip()} for part in text.split(",") if part.strip()]


def _parse_date(val) -> Optional[datetime]:
    if not val or val in ("null", "None"):
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(val, fmt)
        except (ValueError, TypeError):
            continue
    return None


# ===========================================================================
# Household (canonical store = user_profile.json, consumed by the engines)
# ===========================================================================

class HouseholdAdd(BaseModel):
    id: Optional[str] = None
    label: str
    age: Optional[int] = None
    sex: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    activity: str = "moderate"
    daily_kcal: int = 2000


@app.post("/household/add")
def household_add(body: HouseholdAdd):
    profile = preference_engine.load_profile()
    member = body.model_dump()
    if not member.get("id"):
        member["id"] = slugify(body.label) or f"member_{len(profile.get('household', [])) + 1}"
    household = [m for m in profile.get("household", []) if m["id"] != member["id"]]
    household.append(member)
    profile["household"] = household
    preference_engine.save_profile(profile)
    return {"added": member["id"], "household": household}


@app.get("/household")
def household_list():
    return preference_engine.load_profile().get("household", [])


@app.delete("/household/{member_id}")
def household_delete(member_id: str):
    profile = preference_engine.load_profile()
    before = len(profile.get("household", []))
    profile["household"] = [m for m in profile.get("household", []) if m["id"] != member_id]
    preference_engine.save_profile(profile)
    if len(profile["household"]) == before:
        raise HTTPException(404, "member not found")
    return {"deleted": member_id}


# ===========================================================================
# Menu generate / swap / approve
# ===========================================================================

class MenuGenerate(BaseModel):
    query: str = "weeknight dinners"
    days: Optional[int] = None
    constraints: str = ""


def _library_ingredients(db: Session, cap: int = 500) -> set:
    out = set()
    for (ings,) in db.query(Recipe.ingredients).limit(2000):
        for i in (ings or []):
            out.add(i.lower())
            if len(out) >= cap:
                return out
    return out


@app.post("/menu/generate")
def menu_generate(body: MenuGenerate, db: Session = Depends(get_db)):
    profile = preference_engine.load_profile()
    days = body.days or profile.get("plan_days", 7)
    query = f"{body.query}. {body.constraints}".strip()

    result = menu_engine.generate_menu(db, query, days, profile)

    # Phase 4: merge fusion concepts (best-effort — LLM may be down).
    result["fusion_concepts"] = []
    try:
        if result["curated_recipes"]:
            top = [{
                "ingredients": c["ingredients"], "technique": c["technique"],
                "cuisine": c["cuisine"],
            } for c in result["curated_recipes"][:5]]
            from backend import fusion_engine
            result["fusion_concepts"] = fusion_engine.generate_fusion_concepts(
                top, n=2, library_ingredients=_library_ingredients(db),
                fusion_tolerance=profile.get("preferences", {}).get("fusion_tolerance", "medium"),
            )
    except Exception as e:
        logger.warning("fusion generation failed: %s", e)

    _DRAFT.clear()
    _DRAFT.update({"query": query, "days": days, "result": result})
    return result


class MenuSwap(BaseModel):
    day: int
    reason: str = ""


@app.post("/menu/swap")
def menu_swap(body: MenuSwap, db: Session = Depends(get_db)):
    if not _DRAFT:
        raise HTTPException(400, "no draft menu — call /menu/generate first")
    result = _DRAFT["result"]
    curated = result["curated_recipes"]
    idx = body.day - 1
    if idx < 0 or idx >= len(curated):
        raise HTTPException(400, f"day {body.day} out of range")

    profile = preference_engine.load_profile()
    used_ids = {c["recipe_id"] for c in curated}
    pool = menu_engine.candidate_pool(db, _DRAFT["query"] + " " + body.reason, pool_size=80)
    pantry = menu_engine._pantry_set(db)
    targets = macro_engine.household_targets(profile)
    replacement = next((c for c in pool if c["id"] not in used_ids), None)
    if not replacement:
        raise HTTPException(409, "no alternative recipe available")

    portions = menu_engine.portion_recipe(replacement, targets)
    curated[idx] = {
        "label": "[Curated Recipe]", "day": body.day, "recipe_id": replacement["id"],
        "title": replacement["title"], "book_id": replacement["book_id"],
        "cuisine": replacement["cuisine"], "technique": replacement["technique"],
        "ingredients": replacement["ingredients"], "shared_ingredients": [],
        "base_macros": replacement["macros"], "portions": portions,
        "pick_reason": f"swapped: {body.reason}" or "swapped",
    }
    # Recompute overlap + shopping list over the new selection.
    selection = [{"ingredients": c["ingredients"], "technique": c["technique"]} for c in curated]
    result["overlap_score"] = menu_engine.compute_overlap_score(selection)
    dislikes = [d.lower() for d in profile.get("preferences", {}).get("dislikes", [])]
    result["shopping_list"] = menu_engine.build_shopping_list(selection, pantry, dislikes)
    return result


@app.post("/menu/approve")
def menu_approve(db: Session = Depends(get_db)):
    if not _DRAFT:
        raise HTTPException(400, "no draft menu to approve")
    result = _DRAFT["result"]
    monday = _monday_of_week()
    menu = Menu(
        week_of=monday, plan_json=result,
        overlap_score=result.get("overlap_score", 0),
        approved_at=datetime.utcnow(),
    )
    db.add(menu)
    db.commit()
    return {"approved_menu_id": menu.id, "week_of": monday,
            "overlap_score": menu.overlap_score}


def _current_menu(db: Session) -> Optional[dict]:
    """Latest approved menu, else the in-memory draft."""
    m = db.query(Menu).order_by(Menu.id.desc()).first()
    if m and m.plan_json:
        return m.plan_json
    return _DRAFT.get("result")


# ===========================================================================
# Macros / shopping list
# ===========================================================================

@app.get("/macros")
def macros(db: Session = Depends(get_db)):
    profile = preference_engine.load_profile()
    targets = macro_engine.household_targets(profile)
    menu = _current_menu(db)
    if not menu:
        # No menu yet — just show per-person targets (BMR-derived if data present).
        return {"targets": targets, "menu": None}

    per_person_totals: dict = {}
    days_out = []
    for c in menu.get("curated_recipes", []):
        day_entry = {"day": c["day"], "title": c["title"], "per_person": {}}
        for mid, pp in c.get("portions", {}).get("per_person", {}).items():
            day_entry["per_person"][mid] = {
                "calories": pp["calories"], "protein_g": pp["protein_g"],
                "carb_g": pp["carb_g"], "fat_g": pp["fat_g"], "flag": pp.get("flag"),
            }
            agg = per_person_totals.setdefault(mid, {"calories": 0, "protein_g": 0,
                                                     "carb_g": 0, "fat_g": 0})
            for k in agg:
                agg[k] = round(agg[k] + pp[k], 1)
        days_out.append(day_entry)

    return {"targets": targets, "days": days_out, "totals_per_person": per_person_totals}


@app.get("/shopping-list")
def shopping_list(db: Session = Depends(get_db)):
    menu = _current_menu(db)
    if not menu:
        raise HTTPException(400, "no menu — generate one first")
    # Recompute against *current* pantry (it may have changed since generation).
    profile = preference_engine.load_profile()
    pantry = menu_engine._pantry_set(db)
    dislikes = [d.lower() for d in profile.get("preferences", {}).get("dislikes", [])]
    selection = [{"ingredients": c["ingredients"], "technique": c.get("technique", "")}
                 for c in menu.get("curated_recipes", [])]
    return menu_engine.build_shopping_list(selection, pantry, dislikes)


class ShoppingExport(BaseModel):
    format: str = "markdown"  # markdown | text


@app.post("/shopping-list/export")
def shopping_export(body: ShoppingExport, db: Session = Depends(get_db)):
    sl = shopping_list(db)
    lines = []
    md = body.format != "text"
    title = "# Shopping List" if md else "SHOPPING LIST"
    lines.append(title)
    for section, items in sl["sections"].items():
        lines.append(f"\n## {section.title()}" if md else f"\n{section.upper()}")
        for it in items:
            mark = "x" if it["have"] else " "
            flag = f" {it['flag']}" if it["flag"] else ""
            if md:
                lines.append(f"- [{mark}] {it['name']}{flag}")
            else:
                lines.append(f"  [{mark}] {it['name']}{flag}")
    return {"format": body.format, "content": "\n".join(lines)}


# ===========================================================================
# Search
# ===========================================================================

@app.get("/search")
def search(q: str, n: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    hits = rag.search(q, n=n)
    out = []
    for h in hits:
        r = db.get(Recipe, h["id"])
        out.append({
            "id": h["id"], "score": h["score"],
            "title": r.title if r else h["metadata"].get("title", ""),
            "book_id": r.book_id if r else h["metadata"].get("book_id", ""),
            "technique": r.technique if r else "",
            "ingredients": (r.ingredients if r else []) or [],
            "snippet": (h["document"] or "")[:300],
        })
    return {"query": q, "results": out}


# ===========================================================================
# Preferences (Phase 1)
# ===========================================================================

@app.get("/preferences")
def get_preferences():
    return preference_engine.load_profile()


@app.post("/preferences/update")
def update_preferences(last_n: int = Query(10, ge=1, le=100)):
    diff = preference_engine.infer_preferences(last_n=last_n)
    profile = preference_engine.update_profile(diff)
    return {"diff": diff, "profile": profile}


# ===========================================================================
# Ingest status
# ===========================================================================

@app.get("/ingest/status")
def ingest_status(db: Session = Depends(get_db)):
    jobs = []
    for j in db.query(IngestJob).order_by(IngestJob.detected_at.desc()).all():
        jobs.append({
            "id": j.id, "filename": j.filename, "stage": j.stage,
            "failed_stage": j.failed_stage, "stage_error": (j.stage_error or "")[:500],
            "extractor": j.extractor, "chunks_found": j.chunks_found,
            "chunks_indexed": j.chunks_indexed, "chunks_skipped": j.chunks_skipped,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            "audit_log": f"books/corpus/{j.id}/00-ingest.log",
        })
    return {"jobs": jobs}


# ===========================================================================
# Recipe corrections (Phase 7)
# ===========================================================================

class RecipeCorrect(BaseModel):
    raw: str


@app.post("/recipes/correct")
def recipes_correct(body: RecipeCorrect):
    record = preference_engine.parse_correction(body.raw)
    indexed = False
    try:
        # Lazy import — ingest package is on the path in the watcher image; in the
        # backend image we degrade gracefully if it's unavailable.
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "ingest"))
        import chunk_and_index
        chunk_and_index.index_user_corrections()
        indexed = True
    except Exception as e:
        logger.warning("correction re-index skipped: %s", e)
    preference_engine.log_interaction({"shown": [], "chosen": [], "skipped": [],
                                       "feedback_raw": body.raw,
                                       "feedback_parsed": {"title": record["title"]}})
    return {"record": record, "reindexed": indexed}


# ===========================================================================
# Chat — SSE streaming agent with tool routing (Phase 5)
# ===========================================================================

class ChatBody(BaseModel):
    message: str
    session_id: str = "default"


TOOLS = [
    {"type": "function", "function": {
        "name": "plan_menu",
        "description": "Generate a meal plan optimized for ingredient overlap and household nutrition targets.",
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer"}, "constraints": {"type": "string"}},
        }}},
    {"type": "function", "function": {
        "name": "search_recipes",
        "description": "Search the cookbook library by semantic query.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "n": {"type": "integer"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "scale_recipe",
        "description": "Scale a recipe to per-person calorie targets.",
        "parameters": {"type": "object", "properties": {
            "recipe_id": {"type": "string"}}, "required": ["recipe_id"]}}},
    {"type": "function", "function": {
        "name": "update_preferences",
        "description": "Log feedback and update household food preferences.",
        "parameters": {"type": "object", "properties": {
            "feedback": {"type": "string"}}, "required": ["feedback"]}}},
    {"type": "function", "function": {
        "name": "get_shopping_list",
        "description": "Get the pantry-aware, section-grouped shopping list for the current menu.",
        "parameters": {"type": "object", "properties": {}}}},
]


def dispatch_tool(name: str, args: dict, db: Session) -> dict:
    """Python executes all math/state. The LLM only routes + formats."""
    profile = preference_engine.load_profile()
    if name == "plan_menu":
        days = args.get("days") or profile.get("plan_days", 7)
        body = MenuGenerate(query=args.get("constraints") or "weeknight dinners",
                            days=days, constraints=args.get("constraints", ""))
        return menu_generate(body, db)
    if name == "search_recipes":
        return search(args["query"], n=args.get("n", 10), db=db)
    if name == "scale_recipe":
        r = db.get(Recipe, args["recipe_id"])
        if not r:
            return {"error": "recipe not found"}
        targets = macro_engine.household_targets(profile)
        base = {"calories": r.calories, "protein_g": r.protein_g,
                "carb_g": r.carb_g, "fat_g": r.fat_g}
        return {"recipe_id": r.id, "title": r.title,
                "portions": {mid: macro_engine.scale_recipe(base, t["per_meal"]["kcal"])
                             for mid, t in targets.items()}}
    if name == "update_preferences":
        preference_engine.log_interaction({"shown": [], "chosen": [], "skipped": [],
                                           "feedback_raw": args["feedback"]})
        diff = preference_engine.infer_preferences(last_n=5)
        return {"updated": preference_engine.update_profile(diff)["preferences"]}
    if name == "get_shopping_list":
        return shopping_list(db)
    return {"error": f"unknown tool {name}"}


def _sse(data: str) -> str:
    return f"data: {data}\n\n"


def _json_route(message: str) -> tuple:
    """Portable tool router via JSON output (no native function-calling needed).

    Returns (tool_name|None, args_dict). Used when the LLM endpoint lacks
    vLLM's --enable-auto-tool-choice.
    """
    try:
        out = llm.chat_json([
            {"role": "system", "content":
                "You route a user's cooking request to ONE tool. Tools:\n"
                "- plan_menu(days:int, constraints:str): weekly meal plan / dinners\n"
                "- search_recipes(query:str, n:int): find recipes in the library\n"
                "- scale_recipe(recipe_id:str): scale a recipe to household targets\n"
                "- update_preferences(feedback:str): record likes/dislikes feedback\n"
                "- get_shopping_list(): shopping list for the current menu\n"
                'Output ONLY JSON: {"tool":"<name|none>","args":{...}}. '
                'Use "none" if the user is just chatting or asking general questions.'},
            {"role": "user", "content": message},
        ])
        tool = out.get("tool")
        if tool and tool != "none" and any(t["function"]["name"] == tool for t in TOOLS):
            return tool, out.get("args", {}) or {}
    except Exception as e:
        logger.warning("json router failed: %s", e)
    return None, {}


def _agent_stream(message: str, session_id: str):
    db = SessionLocal()
    injection = preference_engine.get_prompt_injection()
    system = f"{BASE_SYSTEM}\n\nUser context: {injection}"
    base_msgs = [{"role": "system", "content": system},
                 {"role": "user", "content": message}]

    tool_used, tool_args, tool_result = None, None, None
    try:
        call = llm.tool_call(base_msgs, TOOLS)
        tool_used, tool_args = call["name"], call["arguments"]
    except llm.ToolCallError:
        pass
    except Exception as e:
        # Native function-calling unsupported on this vLLM (no --enable-auto-tool-choice).
        # Fall back to a portable JSON router that works on any OpenAI-compatible endpoint.
        logger.info("native tool-calling unavailable (%s); using JSON router", str(e)[:80])
        tool_used, tool_args = _json_route(message)

    if tool_used:
        try:
            tool_result = dispatch_tool(tool_used, tool_args or {}, db)
        except Exception as e:
            logger.warning("tool %s failed: %s", tool_used, e)
            tool_result = {"error": str(e)}

    # Single system message — LiteLLM/Qwen rejects multiple/late system messages.
    sys_content = system
    if tool_result is not None:
        sys_content += (f"\n\nTool `{tool_used}` returned this data — base your answer "
                        f"ONLY on it, keep all numbers exact, and label recipes:\n"
                        f"{json.dumps(tool_result)[:6000]}")
    final_msgs = [{"role": "system", "content": sys_content},
                  {"role": "user", "content": message}]

    collected = []
    try:
        for tok in llm.stream_chat(final_msgs):
            collected.append(tok)
            yield _sse(tok)
    except Exception as e:
        yield _sse(f"[error: {e}]")
    yield _sse("[DONE]")

    try:
        preference_engine.log_interaction({
            "session_id": session_id, "message": message, "tool_used": tool_used,
            "shown": _ids_from_result(tool_result), "chosen": [], "skipped": [],
            "response_preview": "".join(collected)[:300],
        })
    finally:
        db.close()


def _ids_from_result(result) -> list:
    if not isinstance(result, dict):
        return []
    ids = []
    for c in result.get("curated_recipes", []):
        ids.append(c.get("recipe_id"))
    for r in result.get("results", []):
        ids.append(r.get("id"))
    return [i for i in ids if i]


@app.post("/chat")
def chat(body: ChatBody):
    return StreamingResponse(_agent_stream(body.message, body.session_id),
                             media_type="text/event-stream")


def _monday_of_week() -> str:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


# ===========================================================================
# MCP mount (Phase 6) — best-effort so the app still boots if fastmcp differs.
# ===========================================================================
try:
    from backend.cookbook_mcp import get_asgi_app
    app.mount("/mcp", get_asgi_app())
    logger.info("MCP mounted at /mcp (SSE: /mcp/sse)")
except Exception as e:  # pragma: no cover
    logger.warning("MCP mount skipped: %s", e)
