"""Aboyeur FastAPI app — all HTTP endpoints + /chat SSE agent + MCP SSE mount.

Run: uvicorn backend.main:app --host 0.0.0.0 --port 8000
Imports use the `backend.` package (PYTHONPATH=/app in Docker).
"""
import hashlib
import json
import logging
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


import threading

from aboyeur import auth as _aboyeur_auth
from aboyeur.agent import Agent as _AboyeurAgent
from aboyeur.config import Config as _AboyeurConfig
from aboyeur.db import connect as _aboyeur_connect, init_db as _aboyeur_init_db
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
_aboyeur_init_db(_aboyeur_conn)
_aboyeur_migrate(_aboyeur_conn)  # idempotent; no-op once the DB is already multi-tenant

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
_aboyeur_session_agents: dict[int, _AboyeurAgent] = {}
_aboyeur_sessions_lock = threading.Lock()  # guards dict get-or-create, not turn execution


def _get_session_agent(user_id: int) -> _AboyeurAgent:
    with _aboyeur_sessions_lock:
        agent = _aboyeur_session_agents.get(user_id)
        if agent is None:
            agent = _AboyeurAgent(_profile_conn(), _aboyeur_config, user_id)
            _aboyeur_session_agents[user_id] = agent
        return agent


def _reset_session_agent(user_id: int, guest: bool = False) -> _AboyeurAgent:
    with _aboyeur_sessions_lock:
        agent = _AboyeurAgent(
            _profile_conn(), _aboyeur_config, user_id,
            guest_profile=_make_guest_profile() if guest else None,
        )
        _aboyeur_session_agents[user_id] = agent
        return agent


# ===========================================================================
# Auth — invite-only accounts (see `aboyeur create-user`), JWT in an httpOnly
# cookie. No public signup route.
# ===========================================================================

_SESSION_COOKIE = "aboyeur_session"


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
        _SESSION_COOKIE, token, httponly=True, samesite="lax", secure=True,
        max_age=_aboyeur_auth.JWT_TTL_SECONDS,
    )
    return {"user_id": user_id}


@app.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(_SESSION_COOKIE)
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


def _agent_stream(agent: _AboyeurAgent, message: str):
    try:
        for tok in _agent_tokens(agent, message):
            yield _sse(tok)
    except Exception:
        # Anything raised mid-generator here previously killed the chunked
        # HTTP response outright — the client sees a bare transfer-encoding
        # error with zero context. Log the real cause server-side and end
        # the stream cleanly instead, so the client at least gets a
        # well-formed (if apologetic) response rather than a truncated one.
        logger.exception("agent stream failed mid-response")
        yield _sse("\n\n[Aboyeur hit an internal error and couldn't finish this response — check server logs.]")
    yield _sse("[DONE]")


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
def profile_data(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return _aboyeur_get_profile(conn, user_id)
    finally:
        conn.close()


@app.post("/profile/save")
def profile_save(desired: dict, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return profile_ui.save_profile(conn, user_id, desired)
    finally:
        conn.close()


def _profile_command_reply(message: str, request: Request, user_id: int) -> Optional[str]:
    """Deterministic /profile handler — the command never touches the LLM."""
    if not message.strip().lower().startswith("/profile"):
        return None
    host = request.headers.get("host", "localhost:8000")
    edit_url = f"http://{host}/profile/ui"
    conn = _profile_conn()
    try:
        profile = _aboyeur_get_profile(conn, user_id)
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
    reply = _profile_command_reply(body.message, request, user_id)
    if reply is not None:
        return StreamingResponse(iter([_sse(reply), _sse("[DONE]")]),
                                 media_type="text/event-stream")

    guest = _guest_command(body.message)
    if guest == "OFF":
        _reset_session_agent(user_id)
        return StreamingResponse(
            iter([_sse("Guest session ended — back to the household profile."), _sse("[DONE]")]),
            media_type="text/event-stream")
    if guest is not None:
        agent = _reset_session_agent(user_id, guest=True)
        return StreamingResponse(_agent_stream(agent, guest or _GUEST_DEFAULT_PROMPT),
                                 media_type="text/event-stream")

    agent = _get_session_agent(user_id)
    return StreamingResponse(_agent_stream(agent, body.message),
                             media_type="text/event-stream")


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

    reply = _profile_command_reply(message, request, _SEED_USER_ID)
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
    agent = _AboyeurAgent(
        _profile_conn(), _aboyeur_config, _SEED_USER_ID, history=history,
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
    user_id: int = Depends(get_current_user),
):
    conn = _profile_conn()
    try:
        profile = _aboyeur_get_profile(conn, user_id)
        results, total = browse_recipes(
            conn, query=query, cuisine=cuisine, book=book,
            allergies=profile.get("allergies", []), offset=offset, limit=limit,
        )
        favorites = list_favorite_ids(conn, user_id)
        for r in results:
            r["favorite"] = r["id"] in favorites
        return {"results": results, "total": total, "offset": offset, "limit": limit}
    finally:
        conn.close()


@app.get("/api/recipes/{recipe_id}")
def api_recipe_detail(recipe_id: int, user_id: int = Depends(get_current_user)):
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
        recipe["favorite"] = recipe_id in list_favorite_ids(conn, user_id)
        return recipe
    finally:
        conn.close()


@app.post("/api/recipes/{recipe_id}/favorite")
def api_favorite_add(recipe_id: int, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        add_favorite(conn, user_id, recipe_id)
        return {"favorite": True}
    finally:
        conn.close()


@app.delete("/api/recipes/{recipe_id}/favorite")
def api_favorite_remove(recipe_id: int, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        remove_favorite(conn, user_id, recipe_id)
        return {"favorite": False}
    finally:
        conn.close()


class MenuPlanBody(BaseModel):
    candidate_ids: list[int]
    days: int
    meals_per_day: int = 1
    constraints: Optional[dict] = None


@app.post("/api/menu/plan")
def api_menu_plan(body: MenuPlanBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return _plan_menu(
            conn, _aboyeur_config, user_id, body.candidate_ids, body.days,
            meals_per_day=body.meals_per_day, constraints=body.constraints,
        )
    finally:
        conn.close()


@app.get("/api/menu/{menu_id}/shopping-list")
def api_menu_shopping_list(menu_id: int, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        result = _get_shopping_list(conn, _aboyeur_config, user_id, menu_id)
        if "error" in result:
            raise HTTPException(404, result["error"])
        return result
    finally:
        conn.close()


# ===========================================================================
# MCP mount (Phase 6) — best-effort so the app still boots if fastmcp differs.
# ===========================================================================
try:
    from backend.cookbook_mcp import get_asgi_app
    app.mount("/mcp", get_asgi_app())
    logger.info("MCP mounted at /mcp (SSE: /mcp/sse)")
except Exception as e:  # pragma: no cover
    logger.warning("MCP mount skipped: %s", e)
