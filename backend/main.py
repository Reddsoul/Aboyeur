"""Aboyeur FastAPI app — all HTTP endpoints + /chat SSE agent + MCP SSE mount.

Run: uvicorn backend.main:app --host 0.0.0.0 --port 8000
Imports use the `backend.` package (PYTHONPATH=/app in Docker).
"""
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
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
    try:
        logger.info("Aboyeur up. Recipes in Chroma: %s", rag.count())
    except Exception as e:
        # Chroma isn't running bare-metal (chat now runs on aboyeur.agent's
        # FTS5 DB instead) — don't let an unreachable vector store block boot.
        logger.warning("Chroma unreachable at boot (non-fatal): %s", e)


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
    # Which client this turn is for. Omitted (CLI-ish callers, older builds)
    # falls back to the account's default client rather than guessing.
    client_id: Optional[int] = None
    # Which of that client's conversations. Omitted continues their most
    # recent one, creating the first if they have none.
    session_id: Optional[int] = None


import threading

from dataclasses import replace as _dc_replace

from aboyeur import auth as _aboyeur_auth
from aboyeur import chat_history as _aboyeur_chat_history
from aboyeur import clients as _aboyeur_clients
from aboyeur import llm_settings as _aboyeur_llm_settings
from aboyeur.agent import Agent as _AboyeurAgent
from aboyeur.config import Config as _AboyeurConfig
from aboyeur.db import (
    connect as _aboyeur_connect,
    init_db as _aboyeur_init_db,
    schema_lock as _aboyeur_schema_lock,
)
from aboyeur.migrate import SEED_USER_EMAIL as _SEED_USER_EMAIL, migrate as _aboyeur_migrate
from aboyeur.tools.profile import (
    get_profile as _aboyeur_get_profile,
    make_guest_profile as _make_guest_profile,
)
from backend import profile_ui

# The real Aboyeur brain: 59k-recipe FTS5 DB, native tool calling, profile +
# nutrition tools. backend/'s own SQLAlchemy Book/Recipe schema below has no
# ingest pipeline wired up (never populated) — chat delegates here instead.
_aboyeur_config = _AboyeurConfig.load()
_aboyeur_conn = _aboyeur_connect(_aboyeur_config.db_path)
# Under one cross-process lock: --reload starts a fresh app process per edit,
# and two of these overlapping can rebuild a table the other has renamed away
# mid-migration (see aboyeur.db.schema_lock).
with _aboyeur_schema_lock(_aboyeur_config.db_path):
    _aboyeur_init_db(_aboyeur_conn)
    _aboyeur_migrate(_aboyeur_conn)  # idempotent; no-op once already migrated

# The unauthenticated OpenAI-compat shim (oa_chat_completions, for Odysseus/
# OpenWebUI) has no login of its own — it always plans for this one seed
# household, exactly the single-user behavior this app had before accounts
# existed.
_SEED_USER_ID = _aboyeur_auth.get_or_create_user(_aboyeur_conn, _SEED_USER_EMAIL)

# /chat holds one live Agent per authenticated user (not a client-supplied
# session_id — that was never authenticated, so anyone could read/steer
# another account's conversation by guessing its id). Each Agent gets its
# OWN sqlite3.Connection (not the shared _aboyeur_conn used only at startup)
# — check_same_thread=False only disables the same-thread assertion, it
# doesn't make concurrent execute() calls on one connection object safe.
# With separate connections, different users' turns need no lock between
# them at all: WAL mode is built for exactly this multi-connection pattern.
# Keyed by (user_id, client_id, chat_session_id). Two reasons, in order of
# importance: a practitioner switching clients must get a different Agent, or
# one client's plan bleeds into the next one's conversation (the single thing
# PRACTITIONER_PLAN.md promises can't happen); and starting a new conversation
# with the SAME client must also start a fresh Agent, or "new chat" would keep
# answering out of the old thread's context.
_aboyeur_session_agents: dict[tuple[int, int, int], _AboyeurAgent] = {}
_aboyeur_sessions_lock = threading.Lock()  # guards dict get-or-create, not turn execution


# Each account brings its own LLM endpoint + key (the Settings screen, stored
# encrypted by aboyeur/llm_settings.py). Env LLM_* is only a fallback, and
# only for the two paths that have no logged-in user of their own: the CLI
# and the OpenWebUI shim's seed household.
NEEDS_LLM_SETUP_MESSAGE = (
    "Set up your AI model in Settings before chatting — Aboyeur needs an LLM "
    "endpoint and key to answer."
)


def _effective_config(user_id: int) -> Optional[_AboyeurConfig]:
    """The Config this user's agent/tools should run with.

    None means "this user has no LLM configured" — the caller shows the setup
    nudge rather than building an agent that would fail on its first round trip.
    """
    conn = _profile_conn()
    try:
        stored = _aboyeur_llm_settings.get_llm_settings(conn, user_id)
    finally:
        conn.close()
    if stored is not None:
        return _dc_replace(
            _aboyeur_config,
            llm_base_url=stored["base_url"],
            llm_model=stored["model"],
            llm_api_key=stored["api_key"],
        )
    if user_id == _SEED_USER_ID:
        return _aboyeur_config
    return None


def _stored_history(chat_session_id: int) -> list[dict]:
    """Seed a fresh Agent with this conversation's recent turns, so picking it
    back up after a restart (or after working with someone else) continues
    where it left off instead of starting cold."""
    conn = _profile_conn()
    try:
        return _aboyeur_chat_history.agent_history(conn, chat_session_id)
    finally:
        conn.close()


def _client_name(client_id: int) -> str:
    conn = _profile_conn()
    try:
        row = conn.execute("SELECT name FROM clients WHERE id = ?", (client_id,)).fetchone()
    finally:
        conn.close()
    return row["name"] if row is not None else "this client"


def _get_session_agent(user_id: int, client_id: int,
                       chat_session_id: int) -> Optional[_AboyeurAgent]:
    with _aboyeur_sessions_lock:
        key = (user_id, client_id, chat_session_id)
        agent = _aboyeur_session_agents.get(key)
        if agent is None:
            config = _effective_config(user_id)
            if config is None:
                return None
            agent = _AboyeurAgent(
                _profile_conn(), config, client_id,
                history=_stored_history(chat_session_id),
                client_name=_client_name(client_id),
            )
            _aboyeur_session_agents[key] = agent
        return agent


def _reset_session_agent(user_id: int, client_id: int, chat_session_id: int,
                         guest: bool = False) -> Optional[_AboyeurAgent]:
    config = _effective_config(user_id)
    if config is None:
        _drop_session_agent(user_id, client_id)
        return None
    with _aboyeur_sessions_lock:
        agent = _AboyeurAgent(
            _profile_conn(), config, client_id,
            guest_profile=_make_guest_profile() if guest else None,
            client_name=_client_name(client_id),
        )
        _aboyeur_session_agents[(user_id, client_id, chat_session_id)] = agent
        return agent


def _drop_session_agent(user_id: int, client_id: Optional[int] = None,
                        chat_session_id: Optional[int] = None) -> None:
    """Forget cached agents so the next turn rebuilds them.

    Widening scope on purpose: no client_id drops every agent for the account
    (an LLM settings change applies to all of them); a client_id with no
    conversation drops all of that client's (a profile edit changes every one
    of their conversations' system prompt).
    """
    with _aboyeur_sessions_lock:
        if client_id is None:
            keys = [k for k in _aboyeur_session_agents if k[0] == user_id]
        elif chat_session_id is None:
            keys = [k for k in _aboyeur_session_agents if k[:2] == (user_id, client_id)]
        else:
            keys = [(user_id, client_id, chat_session_id)]
        for key in keys:
            _aboyeur_session_agents.pop(key, None)


def _require_client(conn: sqlite3.Connection, user_id: int, client_id: Optional[int]) -> int:
    """Every client-scoped route goes through here. A client id belonging to
    another account is a 404, never a read."""
    try:
        return _aboyeur_clients.resolve_client(conn, user_id, client_id)
    except _aboyeur_clients.UnknownClient:
        raise HTTPException(404, "no such client")


# ===========================================================================
# Auth — invite-only accounts (see `aboyeur create-user`), JWT in an httpOnly
# cookie. No public signup route.
# ===========================================================================

_SESSION_COOKIE = "aboyeur_session"

# Secure cookies are refused by Safari over plain http — including
# http://localhost, where Chrome makes an exception. Default on (the deploy is
# HTTPS behind Caddy); set COOKIE_SECURE=false in a local .env so `npm run dev`
# on http://localhost:5173 can hold a session.
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
if not _COOKIE_SECURE:
    logger.warning("COOKIE_SECURE=false — session cookies are sent over plain HTTP. Local dev only.")


def get_current_user(request: Request) -> int:
    token = request.cookies.get(_SESSION_COOKIE)
    if not token:
        raise HTTPException(401, "not logged in")
    try:
        return _aboyeur_auth.verify_token(token)
    except _aboyeur_auth.AuthError as e:
        raise HTTPException(401, str(e))


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
def auth_login(body: LoginBody, response: Response):
    conn = _profile_conn()
    try:
        user_id = _aboyeur_auth.verify_login(conn, body.email, body.password)
    except _aboyeur_auth.AuthError:
        raise HTTPException(401, "invalid email or password")
    finally:
        conn.close()
    token = _aboyeur_auth.issue_token(user_id)
    response.set_cookie(
        _SESSION_COOKIE, token, httponly=True, samesite="lax", secure=_COOKIE_SECURE,
        max_age=_aboyeur_auth.JWT_TTL_SECONDS,
    )
    return {"user_id": user_id}


@app.post("/auth/logout")
def auth_logout(response: Response):
    # Attributes must match the ones it was set with or the browser keeps it.
    response.delete_cookie(_SESSION_COOKIE, httponly=True, samesite="lax",
                           secure=_COOKIE_SECURE)
    return {"ok": True}


@app.get("/auth/me")
def auth_me(user_id: int = Depends(get_current_user)):
    return {"user_id": user_id}


def _sse(data: str) -> str:
    # Per the SSE spec, a multi-line payload needs its own "data: " prefix on
    # EVERY line — a bare embedded "\n" (routine in streamed markdown: list
    # items, paragraph breaks) would otherwise silently truncate the event at
    # a spec-compliant client, since only "data:"-prefixed lines are part of
    # the field. Empty input still yields one "data: " line, not zero — a
    # dropped event is worse than an empty one.
    lines = data.split("\n") or [""]
    return "".join(f"data: {line}\n" for line in lines) + "\n"


def _agent_tokens(agent: _AboyeurAgent, message: str) -> Iterator[str]:
    """Streams a reply from the given Agent. Yields raw content strings,
    unwrapped. Shared by the native /chat SSE endpoint and the
    /v1/chat/completions OpenAI-compat shim. No lock here — the LLM
    round-trip can run for multiple users at once; only the brief DB moments
    inside agent.send()'s tool dispatch serialize (aboyeur/agent.py's
    _DB_LOCK).
    """
    for kind, payload in agent.send(message):
        if kind == "text":
            yield payload


def _agent_stream(agent: _AboyeurAgent, message: str, record_for: Optional[int] = None):
    """record_for: the conversation this turn belongs to. The reply is saved
    once the stream finishes (including the error tail), so a half-streamed
    answer is still there on the next visit."""
    parts: list[str] = []
    try:
        for tok in _agent_tokens(agent, message):
            parts.append(tok)
            yield _sse(tok)
    except Exception:
        # Anything raised mid-generator here previously killed the chunked
        # HTTP response outright — the client sees a bare transfer-encoding
        # error with zero context. Log the real cause server-side and end
        # the stream cleanly instead, so the client at least gets a
        # well-formed (if apologetic) response rather than a truncated one.
        logger.exception("agent stream failed mid-response")
        tail = "\n\n[Aboyeur hit an internal error and couldn't finish this response — check server logs.]"
        parts.append(tail)
        yield _sse(tail)
    if record_for is not None:
        _record_message(record_for, "assistant", "".join(parts))
    yield _sse("[DONE]")


def _record_message(chat_session_id: int, role: str, content: str) -> None:
    if not content.strip():
        return
    conn = _profile_conn()
    try:
        _aboyeur_chat_history.append_message(conn, chat_session_id, role, content)
    except Exception:  # a transcript write must never break the reply
        logger.exception("couldn't record %s message for conversation %s", role, chat_session_id)
    finally:
        conn.close()


# ===========================================================================
# Profile editor — /profile chat command + browser form (backend/profile_ui.py).
# Writes the same `profile` table the agent's tools read, so the system
# prompt, allergen filter, and menu targets pick edits up on the next turn.
# ===========================================================================

@app.get("/profile/ui", response_class=HTMLResponse)
def profile_page():
    # Static page; its own JS calls /profile/data and /profile/save below,
    # both auth-gated — an unauthenticated visitor sees the page shell but
    # every fetch it makes 401s until they've logged in via /auth/login.
    return HTMLResponse(profile_ui.PROFILE_PAGE_HTML)


# Every route below opens its own short-lived connection rather than sharing
# one across threads — an agent turn can run for minutes (USDA lookups, LLM
# rounds) and the profile editor / catalog API must stay responsive
# meanwhile. WAL mode makes cross-connection reads/writes safe; busy_timeout
# covers the rare write collision.
def _profile_conn() -> sqlite3.Connection:
    conn = _aboyeur_connect(_aboyeur_config.db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@app.get("/profile/data")
def profile_data(client_id: Optional[int] = None, user_id: int = Depends(get_current_user)):
    """The account's default client unless one is named — keeps the standalone
    /profile/ui page and older frontends working after the client split."""
    conn = _profile_conn()
    try:
        return _aboyeur_get_profile(conn, _require_client(conn, user_id, client_id))
    finally:
        conn.close()


@app.post("/profile/save")
def profile_save(desired: dict, client_id: Optional[int] = None,
                 user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        saved = profile_ui.save_profile(conn, resolved, desired)
    finally:
        conn.close()
    # Targets and restrictions changed — the agent's system prompt is stale.
    _drop_session_agent(user_id, resolved)
    return saved


def _profile_command_reply(message: str, request: Request, client_id: int) -> Optional[str]:
    """Deterministic /profile handler — the command never touches the LLM."""
    if not message.strip().lower().startswith("/profile"):
        return None
    host = request.headers.get("host", "localhost:8000")
    edit_url = f"http://{host}/profile/ui"
    conn = _profile_conn()
    try:
        profile = _aboyeur_get_profile(conn, client_id)
    finally:
        conn.close()
    return profile_ui.profile_markdown(profile, edit_url)


# In-memory guest-profile carry-over for the stateless OpenAI shim (see the
# long comment in oa_chat_completions). Keyed by a hash of the conversation's
# user-turn contents; bounded LRU so abandoned conversations age out. Memory
# only, by design — guest data must never touch the DB.
_OA_GUEST_CACHE_MAX = 64
_oa_guest_profiles: "OrderedDict[str, dict]" = OrderedDict()
_oa_guest_lock = threading.Lock()


def _oa_history_key(user_contents: list[str]) -> str:
    h = hashlib.sha256()
    for c in user_contents:
        h.update(c.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _oa_guest_lookup(key: str) -> Optional[dict]:
    with _oa_guest_lock:
        return _oa_guest_profiles.get(key)


def _oa_guest_store(key: str, profile: dict) -> None:
    with _oa_guest_lock:
        _oa_guest_profiles[key] = profile
        _oa_guest_profiles.move_to_end(key)
        while len(_oa_guest_profiles) > _OA_GUEST_CACHE_MAX:
            _oa_guest_profiles.popitem(last=False)


_GUEST_OFF_WORDS = ("off", "end", "stop", "exit")
_GUEST_DEFAULT_PROMPT = (
    "A guest is joining for meals. Greet them briefly and ask for the basics you "
    "need to plan for them (height, weight, age, sex, and any allergies)."
)


def _guest_command(message: str) -> Optional[str]:
    """'/guest <intro>' -> the intro text to hand the guest agent ('' meaning
    none given); '/guest off|end|stop|exit' -> 'OFF'; anything else -> None."""
    stripped = message.strip()
    if not stripped.lower().startswith("/guest"):
        return None
    rest = stripped[len("/guest"):].strip()
    if rest.lower() in _GUEST_OFF_WORDS:
        return "OFF"
    return rest


@app.post("/chat")
def chat(body: ChatBody, request: Request, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        client_id = _require_client(conn, user_id, body.client_id)
        chat_session_id = _require_chat_session(conn, client_id, body.session_id)
    finally:
        conn.close()

    reply = _profile_command_reply(body.message, request, client_id)
    if reply is not None:
        _record_message(chat_session_id, "user", body.message)
        _record_message(chat_session_id, "assistant", reply)
        return StreamingResponse(iter([_sse(reply), _sse("[DONE]")]),
                                 media_type="text/event-stream")

    guest = _guest_command(body.message)
    if guest == "OFF":
        _drop_session_agent(user_id, client_id, chat_session_id)
        return StreamingResponse(
            iter([_sse("Guest session ended — back to this client's profile."), _sse("[DONE]")]),
            media_type="text/event-stream")
    if guest is not None:
        agent = _reset_session_agent(user_id, client_id, chat_session_id, guest=True)
        if agent is None:
            return _needs_llm_setup_stream()
        # Guest turns are deliberately not recorded — nothing a guest says is
        # persisted anywhere, transcript included.
        return StreamingResponse(_agent_stream(agent, guest or _GUEST_DEFAULT_PROMPT),
                                 media_type="text/event-stream")

    agent = _get_session_agent(user_id, client_id, chat_session_id)
    if agent is None:
        return _needs_llm_setup_stream()
    _record_message(chat_session_id, "user", body.message)
    return StreamingResponse(_agent_stream(agent, body.message, record_for=chat_session_id),
                             media_type="text/event-stream")


def _require_chat_session(conn: sqlite3.Connection, client_id: int,
                          session_id: Optional[int]) -> int:
    """A conversation id is only valid for the client that owns it — and that
    client has already been checked against the account."""
    try:
        return _aboyeur_chat_history.resolve_session(conn, client_id, session_id)
    except _aboyeur_chat_history.UnknownSession:
        raise HTTPException(404, "no such conversation")


def _needs_llm_setup_stream() -> StreamingResponse:
    # A 200 with one assistant message, not a 4xx: the Chat screen renders it
    # as a normal bubble, and the SSE contract (text chunks then [DONE]) stays
    # identical for every caller.
    return StreamingResponse(
        iter([_sse(NEEDS_LLM_SETUP_MESSAGE), _sse("[DONE]")]),
        media_type="text/event-stream",
    )


# ===========================================================================
# OpenAI-compatible shim (Phase 7) — lets OpenWebUI drive the Aboyeur agent
# as a regular chat model, instead of duplicating its tool-routing/orchestration.
# ===========================================================================

class OAMessage(BaseModel):
    role: str
    content: str


class OAChatRequest(BaseModel):
    model: str = "aboyeur"
    messages: list[OAMessage]
    stream: bool = False


@app.get("/v1/models")
def oa_models():
    return {"object": "list", "data": [{"id": "aboyeur", "object": "model", "owned_by": "aboyeur"}]}


def _oa_chunk(request_id: str, created: int, content: Optional[str] = None,
              finish_reason: Optional[str] = None) -> str:
    delta = {"content": content} if content is not None else {}
    payload = {
        "id": request_id, "object": "chat.completion.chunk", "created": created,
        "model": "aboyeur",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _oa_stream(agent: _AboyeurAgent, message: str, on_done=None):
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    try:
        for tok in _agent_tokens(agent, message):
            yield _oa_chunk(request_id, created, content=tok)
    except Exception:
        # See _agent_stream's matching comment: don't let an internal error
        # abort the chunked response — OpenWebUI reports that as an opaque
        # TransferEncodingError with no indication anything actually broke.
        logger.exception("agent stream failed mid-response")
        yield _oa_chunk(
            request_id, created,
            content="\n\n[Aboyeur hit an internal error and couldn't finish this response — check server logs.]",
        )
    finally:
        if on_done is not None:
            on_done()
    yield _oa_chunk(request_id, created, finish_reason="stop")
    yield "data: [DONE]\n\n"


def _oa_static_stream(content: str):
    request_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yield _oa_chunk(request_id, created, content=content)
    yield _oa_chunk(request_id, created, finish_reason="stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
def oa_chat_completions(body: OAChatRequest, request: Request):
    """OpenAI-compatible endpoint so OpenWebUI can register Aboyeur as a model.

    Stateless per request: any OpenAI-style client (OpenWebUI included)
    resends the full conversation on every call, so a fresh Agent (and its
    own sqlite3.Connection — see the Agent construction below) is rebuilt
    from body.messages each time instead of relying on server-side memory.
    That's what makes concurrent testers safe — two callers never share an
    Agent, a messages list, or a connection.
    """
    user_msgs = [m for m in body.messages if m.role == "user"]
    if not user_msgs:
        raise HTTPException(400, "no user message")
    message = user_msgs[-1].content

    conn = _profile_conn()
    try:
        shim_client_id = _aboyeur_clients.default_client_id(conn, _SEED_USER_ID)
    finally:
        conn.close()
    reply = _profile_command_reply(message, request, shim_client_id)
    if reply is not None:
        if body.stream:
            return StreamingResponse(_oa_static_stream(reply),
                                     media_type="text/event-stream")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "aboyeur",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Stateless guest detection: the /guest command lives in the resent
    # history, so scan user turns in order — the last /guest directive wins
    # ("/guest off" ends it).
    guest_active = False
    for m in body.messages:
        if m.role != "user":
            continue
        g = _guest_command(m.content)
        if g == "OFF":
            guest_active = False
        elif g is not None:
            guest_active = True

    current_guest = _guest_command(message)
    if current_guest == "OFF":
        message = "The guest session just ended. Confirm you're back to planning for the household."
    elif current_guest is not None:
        message = current_guest or _GUEST_DEFAULT_PROMPT

    history = [
        {"role": m.role, "content": m.content}
        for m in body.messages[:-1] if m.role in ("user", "assistant")
    ]

    # Guest continuity across stateless requests: update_profile writes from a
    # guest turn land only in the per-request in-memory dict, and the model
    # does NOT re-derive them from the resent history on later turns (it never
    # re-calls update_profile for old messages). Without carry-over, turn N+1's
    # plan_menu sees an empty guest profile and silently plans against the
    # generic 600 kcal/meal fallback — observed in the wild as a 1200 kcal/day
    # "target" presented as legit. So the guest dict is cached in memory keyed
    # by the hash of the conversation's user turns: this request looks up the
    # dict stored by the previous request (whose user turns are exactly our
    # prefix) and stores its own updated copy for the next one. Copy-on-read
    # keeps branched/regenerated conversations from mutating each other.
    # Nothing is ever written to the stored household profile.
    guest_profile = None
    on_done = None
    if guest_active:
        user_contents = [m.content for m in body.messages if m.role == "user"]
        prev = _oa_guest_lookup(_oa_history_key(user_contents[:-1]))
        guest_profile = json.loads(json.dumps(prev)) if prev is not None else _make_guest_profile()
        next_key = _oa_history_key(user_contents)
        on_done = lambda: _oa_guest_store(next_key, guest_profile)  # noqa: E731

    # Always the seed household — this shim has no login of its own (see the
    # _SEED_USER_ID comment above). Stateless per request (per this
    # function's own docstring), so a fresh connection per call is the
    # natural fit — no connection is shared across concurrent callers here.
    # The shim has no client picker either — it plans for the seed account's
    # default client, the same one the CLI uses.
    conn = _profile_conn()
    try:
        seed_client_id = _aboyeur_clients.default_client_id(conn, _SEED_USER_ID)
    finally:
        conn.close()
    agent = _AboyeurAgent(
        _profile_conn(), _aboyeur_config, seed_client_id, history=history,
        guest_profile=guest_profile,
    )

    if body.stream:
        return StreamingResponse(_oa_stream(agent, message, on_done=on_done),
                                 media_type="text/event-stream")

    content = "".join(_agent_tokens(agent, message))
    if on_done is not None:
        on_done()
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "aboyeur",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _monday_of_week() -> str:
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()


# ===========================================================================
# Recipe catalog + menu REST API — the new authenticated web frontend's
# browsing surface. Distinct from /chat: calls the same aboyeur/tools
# functions directly (no LLM round-trip) for list/detail/favorite/plan
# actions instead of a conversation turn. Each route opens its own
# short-lived connection via _profile_conn() (defined above), same pattern
# as the profile editor — no locking needed since every request gets its own
# sqlite3.Connection object and WAL mode allows safe concurrent access
# across connections to the same file.
# ===========================================================================

from aboyeur.tools.favorites import add_favorite, list_favorite_ids, remove_favorite
from aboyeur.tools.menu import plan_menu as _plan_menu
from aboyeur.tools.recipe import get_recipe as _get_recipe
from aboyeur.tools.search import browse_recipes, recipe_facets
from aboyeur.tools.shopping import get_shopping_list as _get_shopping_list


@app.get("/api/recipes/facets")
def api_recipe_facets(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return recipe_facets(conn)
    finally:
        conn.close()


@app.get("/api/recipes")
def api_recipes(
    query: str = "", cuisine: Optional[str] = None, book: Optional[str] = None,
    offset: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100),
    favorites_only: bool = False,
    client_id: Optional[int] = None,
    user_id: int = Depends(get_current_user),
):
    """The catalog, seen through one client: their allergens are filtered out
    and their stars are the ones lit.

    favorites_only restricts it to what they've starred — done in SQL, because
    a starred recipe is rarely inside the first page of a 75k-recipe catalog.
    """
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        profile = _aboyeur_get_profile(conn, resolved)
        favorites = list_favorite_ids(conn, resolved)
        results, total = browse_recipes(
            conn, query=query, cuisine=cuisine, book=book,
            allergies=profile.get("allergies", []), offset=offset, limit=limit,
            only_ids=favorites if favorites_only else None,
        )
        for r in results:
            r["favorite"] = r["id"] in favorites
        return {"results": results, "total": total, "offset": offset, "limit": limit}
    finally:
        conn.close()


@app.get("/api/recipes/{recipe_id}")
def api_recipe_detail(recipe_id: int, client_id: Optional[int] = None,
                      user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        recipe = _get_recipe(conn, recipe_id)
        if "error" in recipe:
            raise HTTPException(404, recipe["error"])
        # get_recipe only returns {id, title, path, servings, markdown} — the
        # verbatim-recipe tool contract. The catalog detail view also wants
        # the same summary fields the grid card already showed (book,
        # cuisine, ingredients, tags), so the panel doesn't have to re-fetch
        # or lose context when it opens.
        row = conn.execute(
            "SELECT book, cuisine, ingredients_json, tags_json FROM recipes WHERE id = ?",
            (recipe_id,),
        ).fetchone()
        if row is not None:
            recipe["book"] = row["book"]
            recipe["cuisine"] = row["cuisine"]
            recipe["ingredients"] = json.loads(row["ingredients_json"])
            recipe["tags"] = json.loads(row["tags_json"])
        recipe["favorite"] = recipe_id in list_favorite_ids(
            conn, _require_client(conn, user_id, client_id))
        return recipe
    finally:
        conn.close()


@app.post("/api/recipes/{recipe_id}/favorite")
def api_favorite_add(recipe_id: int, client_id: Optional[int] = None,
                     user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        add_favorite(conn, _require_client(conn, user_id, client_id), recipe_id)
        return {"favorite": True}
    finally:
        conn.close()


@app.delete("/api/recipes/{recipe_id}/favorite")
def api_favorite_remove(recipe_id: int, client_id: Optional[int] = None,
                        user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        remove_favorite(conn, _require_client(conn, user_id, client_id), recipe_id)
        return {"favorite": False}
    finally:
        conn.close()


class MenuPlanBody(BaseModel):
    client_id: Optional[int] = None
    candidate_ids: list[int]
    days: int
    meals_per_day: int = 1
    constraints: Optional[dict] = None


@app.post("/api/menu/plan")
def api_menu_plan(body: MenuPlanBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        client_id = _require_client(conn, user_id, body.client_id)
        return _plan_menu(
            conn, _aboyeur_config, client_id, body.candidate_ids, body.days,
            meals_per_day=body.meals_per_day, constraints=body.constraints,
        )
    finally:
        conn.close()


@app.get("/api/menu/{menu_id}/shopping-list")
def api_menu_shopping_list(menu_id: int, client_id: Optional[int] = None,
                           user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        result = _get_shopping_list(conn, _aboyeur_config, resolved, menu_id)
        if "error" in result:
            raise HTTPException(404, result["error"])
        return result
    finally:
        conn.close()


# ===========================================================================
# Account — the practitioner themselves. Distinct from any client record,
# including their own "Me" client: this is who is logged in, not who is
# being planned for.
# ===========================================================================

class AccountPatch(BaseModel):
    name: Optional[str] = None
    practice: Optional[str] = None
    email: Optional[str] = None


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


def _account(conn: sqlite3.Connection, user_id: int) -> dict:
    row = conn.execute(
        "SELECT id, email, name, practice, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "account not found")
    counts = conn.execute(
        "SELECT COUNT(*) total, SUM(status = 'active') active FROM clients WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    self_client = conn.execute(
        "SELECT id, name FROM clients WHERE user_id = ? AND is_self = 1 ORDER BY id LIMIT 1",
        (user_id,),
    ).fetchone()
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "practice": row["practice"],
        "created_at": row["created_at"],
        "client_count": counts["total"] or 0,
        "active_client_count": counts["active"] or 0,
        # The practitioner's own client record — "you, as someone to plan for".
        "self_client": ({"id": self_client["id"], "name": self_client["name"]}
                        if self_client is not None else None),
    }


@app.get("/api/account")
def api_account_get(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        # Every account has a client for its owner; created here on first read
        # so the account screen can always link to it.
        _aboyeur_clients.ensure_self_client(conn, user_id)
        return _account(conn, user_id)
    finally:
        conn.close()


@app.patch("/api/account")
def api_account_update(patch: AccountPatch, user_id: int = Depends(get_current_user)):
    fields = patch.model_dump(exclude_unset=True)
    conn = _profile_conn()
    try:
        sets, values = [], []
        for column in ("name", "practice"):
            if column in fields:
                value = (fields[column] or "").strip() or None
                sets.append(f"{column} = ?")
                values.append(value)
        if "email" in fields:
            email = (fields["email"] or "").strip().lower()
            if not email or "@" not in email:
                raise HTTPException(400, "that doesn't look like an email address")
            taken = conn.execute(
                "SELECT id FROM users WHERE email = ? AND id != ?", (email, user_id)
            ).fetchone()
            if taken is not None:
                raise HTTPException(409, "another account already uses that email")
            sets.append("email = ?")
            values.append(email)
        if sets:
            values.append(user_id)
            conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()
        return _account(conn, user_id)
    finally:
        conn.close()


@app.post("/api/account/password")
def api_account_password(body: PasswordBody, user_id: int = Depends(get_current_user)):
    if len(body.new_password) < 8:
        raise HTTPException(400, "use at least 8 characters")
    conn = _profile_conn()
    try:
        row = conn.execute(
            "SELECT email, password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "account not found")
        # A password-less account (created by the CLI's get_or_create_user)
        # can set one without proving the old one it never had.
        if row["password_hash"] and not _aboyeur_auth.verify_password(
            body.current_password, row["password_hash"]
        ):
            raise HTTPException(403, "current password is wrong")
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_aboyeur_auth.hash_password(body.new_password), user_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ===========================================================================
# Clients — the people this practitioner plans for, and the scope key for
# every per-person route above. _require_client is the only door: a client id
# from another account 404s here rather than reaching a query.
# ===========================================================================

class ClientBody(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None


class ClientPatch(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


def _client_summary(conn: sqlite3.Connection, client: dict) -> dict:
    """Roster-card extras: what the practitioner needs to tell two clients
    apart at a glance, without opening either."""
    profile = _aboyeur_get_profile(conn, client["id"])
    household = profile.get("household") or []
    targets = profile.get("targets") or {}
    primary = household[0] if household else None
    target = targets.get(primary["id"]) if primary else None
    plans = conn.execute(
        "SELECT (SELECT COUNT(*) FROM menus WHERE client_id = ?) AS menus, "
        "(SELECT COUNT(*) FROM kits WHERE client_id = ?) AS kits, "
        "(SELECT MAX(created_at) FROM menus WHERE client_id = ?) AS last_menu, "
        "(SELECT MAX(created_at) FROM kits WHERE client_id = ?) AS last_kit",
        (client["id"],) * 4,
    ).fetchone()
    last = max([d for d in (plans["last_menu"], plans["last_kit"]) if d], default=None)
    return {
        **client,
        "household_size": len(household),
        "goal": (target or {}).get("goal"),
        "daily_kcal": (target or {}).get("daily_kcal"),
        "allergies": profile.get("allergies") or [],
        "menu_count": plans["menus"],
        "kit_count": plans["kits"],
        "last_plan_at": last,
        "has_profile": bool(household),
    }


@app.get("/api/clients")
def api_clients_list(include_archived: bool = False, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return {
            "clients": [
                _client_summary(conn, c)
                for c in _aboyeur_clients.list_clients(conn, user_id, include_archived)
            ]
        }
    finally:
        conn.close()


@app.post("/api/clients")
def api_clients_create(body: ClientBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        client = _aboyeur_clients.create_client(
            conn, user_id, body.name, body.email, body.phone, body.notes,
        )
        return _client_summary(conn, client)
    except _aboyeur_clients.ClientError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.get("/api/clients/{client_id}")
def api_client_get(client_id: int, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        return _client_summary(conn, _aboyeur_clients.get_client(conn, user_id, resolved))
    finally:
        conn.close()


@app.patch("/api/clients/{client_id}")
def api_client_update(client_id: int, patch: ClientPatch,
                      user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        updated = _aboyeur_clients.update_client(
            conn, user_id, resolved, patch.model_dump(exclude_unset=True),
        )
        summary = _client_summary(conn, updated)
    except _aboyeur_clients.ClientError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    # The name is in the agent's system prompt, so a rename invalidates it.
    _drop_session_agent(user_id, resolved)
    return summary


@app.delete("/api/clients/{client_id}")
def api_client_delete(client_id: int, purge: bool = False,
                      user_id: int = Depends(get_current_user)):
    """Archive by default; `?purge=true` hard-deletes the client and cascades
    to their profile, pantry, menus and kits."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        if purge:
            _aboyeur_clients.delete_client(conn, user_id, resolved)
            result = {"deleted": resolved}
        else:
            result = _aboyeur_clients.archive_client(conn, user_id, resolved)
    finally:
        conn.close()
    _drop_session_agent(user_id, resolved)
    return result


@app.get("/api/clients/{client_id}/profile")
def api_client_profile(client_id: int, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return _aboyeur_get_profile(conn, _require_client(conn, user_id, client_id))
    finally:
        conn.close()


@app.post("/api/clients/{client_id}/profile")
def api_client_profile_save(client_id: int, desired: dict,
                            user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        saved = profile_ui.save_profile(conn, resolved, desired)
    finally:
        conn.close()
    _drop_session_agent(user_id, resolved)
    return saved


@app.get("/api/clients/{client_id}/messages")
def api_client_messages(client_id: int, session_id: Optional[int] = None,
                        user_id: int = Depends(get_current_user)):
    """One conversation's messages, oldest first. Defaults to this client's
    most recent conversation — what the chat screen opens on."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        chat_session_id = _require_chat_session(conn, resolved, session_id)
        return {
            "session_id": chat_session_id,
            "messages": _aboyeur_chat_history.list_messages(conn, chat_session_id),
        }
    finally:
        conn.close()


class SessionBody(BaseModel):
    title: Optional[str] = None


@app.get("/api/clients/{client_id}/sessions")
def api_client_sessions(client_id: int, user_id: int = Depends(get_current_user)):
    """This client's conversations, most recently active first."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        return {"sessions": _aboyeur_chat_history.list_sessions(conn, resolved)}
    finally:
        conn.close()


@app.post("/api/clients/{client_id}/sessions")
def api_client_session_create(client_id: int, body: SessionBody = SessionBody(),
                              user_id: int = Depends(get_current_user)):
    """Start a new conversation alongside the existing ones — the old ones
    stay exactly where they were."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        return _aboyeur_chat_history.create_session(conn, resolved, body.title)
    finally:
        conn.close()


@app.patch("/api/clients/{client_id}/sessions/{session_id}")
def api_client_session_rename(client_id: int, session_id: int, body: SessionBody,
                              user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        _require_chat_session(conn, resolved, session_id)
        return _aboyeur_chat_history.rename_session(conn, resolved, session_id, body.title or "")
    finally:
        conn.close()


@app.delete("/api/clients/{client_id}/sessions/{session_id}")
def api_client_session_delete(client_id: int, session_id: int,
                              user_id: int = Depends(get_current_user)):
    """Delete one conversation. The client's other conversations, profile and
    plans are untouched."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        _require_chat_session(conn, resolved, session_id)
        _aboyeur_chat_history.delete_session(conn, resolved, session_id)
    finally:
        conn.close()
    _drop_session_agent(user_id, resolved, session_id)
    return {"deleted": session_id}


@app.delete("/api/clients/{client_id}/messages")
def api_client_messages_clear(client_id: int, session_id: Optional[int] = None,
                              user_id: int = Depends(get_current_user)):
    """Empty one conversation without deleting it, and drop the live agent
    that was seeded from it."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        chat_session_id = _require_chat_session(conn, resolved, session_id)
        removed = _aboyeur_chat_history.clear_messages(conn, chat_session_id)
    finally:
        conn.close()
    _drop_session_agent(user_id, resolved, chat_session_id)
    return {"cleared": removed, "session_id": chat_session_id}


@app.get("/api/clients/{client_id}/plans")
def api_client_plans(client_id: int, user_id: int = Depends(get_current_user)):
    """This client's menus and kits. Scoped by client_id alone — there is no
    query here that could return another client's plan."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        menus = [
            {
                "id": r["id"],
                "week_of": r["week_of"],
                "status": r["status"],
                "created_at": r["created_at"],
                "days": len({d.get("day") for d in json.loads(r["days_json"])}),
                "meals": len(json.loads(r["days_json"])),
            }
            for r in conn.execute(
                "SELECT id, week_of, status, created_at, days_json FROM menus "
                "WHERE client_id = ? ORDER BY created_at DESC, id DESC",
                (resolved,),
            )
        ]
        kits = []
        for r in conn.execute(
            "SELECT id, data_json, created_at FROM kits WHERE client_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (resolved,),
        ):
            data = json.loads(r["data_json"])
            kits.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "days": data.get("days"),
                "meals_per_day": data.get("meals_per_day"),
                "strategy": data.get("strategy"),
                "component_count": len(data.get("components") or []),
            })
        return {"menus": menus, "kits": kits}
    finally:
        conn.close()


# ===========================================================================
# Per-user LLM settings — the web Settings screen. The key is stored Fernet-
# encrypted and never sent back to the browser; only {base_url, model,
# configured} ever leaves the server.
# ===========================================================================

class LlmSettingsBody(BaseModel):
    base_url: str
    model: str
    # Omitted/blank on an edit = keep the stored key (see save_llm_settings).
    api_key: Optional[str] = None


@app.get("/api/llm-settings")
def api_llm_settings_get(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return _aboyeur_llm_settings.get_llm_settings_public(conn, user_id)
    finally:
        conn.close()


@app.put("/api/llm-settings")
def api_llm_settings_put(body: LlmSettingsBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        public = _aboyeur_llm_settings.save_llm_settings(
            conn, user_id, body.base_url, body.model, body.api_key,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
    # Drop every cached agent for this account so the next turn — for any
    # client — is built against the new endpoint.
    _drop_session_agent(user_id)
    return public


class LlmTestBody(BaseModel):
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


@app.post("/api/llm-settings/test")
def api_llm_settings_test(body: LlmTestBody, user_id: int = Depends(get_current_user)):
    """Probe an endpoint before saving it. Never echoes the key back.

    An authenticated user can point this at any URL the server can reach —
    an SSRF surface accepted for an invite-only self-hosted app (see
    FRONTEND_PLAN.md's risks). Tighten with an IP allow-list if that changes.
    """
    base_url = (body.base_url or "").strip().rstrip("/")
    model = (body.model or "").strip()
    api_key = (body.api_key or "").strip()

    if not base_url or not model or not api_key:
        conn = _profile_conn()
        try:
            stored = _aboyeur_llm_settings.get_llm_settings(conn, user_id)
        finally:
            conn.close()
        if stored is not None:
            base_url = base_url or stored["base_url"]
            model = model or stored["model"]
            api_key = api_key or stored["api_key"]
    if not base_url or not model:
        raise HTTPException(400, "base_url and model are required")

    from openai import OpenAI as _OpenAI

    client = _OpenAI(base_url=base_url, api_key=api_key or "unused",
                     timeout=10, max_retries=0)
    try:
        models = [m.id for m in client.models.list().data]
    except Exception as e:
        # Not every OpenAI-compatible server implements /models — fall back to
        # the thing we actually care about: can this model answer at all.
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as e2:
            logger.info("LLM test failed for user %s: %s / %s", user_id, e, e2)
            return {"ok": False, "detail": _llm_error_detail(e2)}
        return {"ok": True, "detail": f"Connected — {model} answered."}

    if models and model not in models:
        return {
            "ok": False,
            "detail": f"Endpoint reachable, but it doesn't serve \"{model}\". "
                      f"Available: {', '.join(models[:8])}"
                      + (" …" if len(models) > 8 else ""),
        }
    return {"ok": True, "detail": f"Connected — {model} is available."}


def _llm_error_detail(exc: Exception) -> str:
    """A one-line, key-free reason for the Settings screen."""
    detail = str(exc).strip() or exc.__class__.__name__
    detail = detail.split("\n")[0]
    return detail[:300]


# ===========================================================================
# MCP mount (Phase 6) — best-effort so the app still boots if fastmcp differs.
# ===========================================================================
try:
    from backend.cookbook_mcp import get_asgi_app
    app.mount("/mcp", get_asgi_app())
    logger.info("MCP mounted at /mcp (SSE: /mcp/sse)")
except Exception as e:  # pragma: no cover
    logger.warning("MCP mount skipped: %s", e)
