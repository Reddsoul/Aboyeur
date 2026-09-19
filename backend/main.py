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
import traceback as _traceback
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from backend import llm, macro_engine, menu_engine, preference_engine, rag
from backend.models import (
    Book, Recipe, PantryItem, Menu, IngestJob, init_db, get_db, SessionLocal,
)
from backend.utils import categorize_ingredient, slugify

from aboyeur import logging_setup as _logging_setup
from aboyeur.config import check_deploy_secrets as _check_deploy_secrets, env_flag as _env_flag

# Refuse to boot behind HTTPS with empty signing/encryption keys — before
# anything below reads them and quietly falls back to a derived default.
_check_deploy_secrets()

_logging_setup.configure(json_logs=_env_flag("JSON_LOGS", True))
logger = logging.getLogger("aboyeur")

APP_VERSION = os.environ.get("APP_VERSION") or "dev"

# Error reporting. send_default_pii=False keeps request bodies, cookies and
# user emails out; before_send strips the few fields that could still carry
# a transcript line or a key if some log statement slipped.
_SENTRY_DSN = os.environ.get("SENTRY_DSN") or None
if _SENTRY_DSN:
    import sentry_sdk

    _SCRUB_KEYS = {"content", "email", "api_key", "authorization", "cookie", "password",
                   "message", "body", "notes", "phone", "name"}

    def _scrub(obj):
        if isinstance(obj, dict):
            return {k: ("[scrubbed]" if str(k).lower() in _SCRUB_KEYS else _scrub(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [_scrub(v) for v in obj]
        return obj

    def _before_send(event, hint):
        for key in ("request", "extra", "contexts", "breadcrumbs", "user"):
            if key in event:
                event[key] = _scrub(event[key])
        return event

    sentry_sdk.init(dsn=_SENTRY_DSN, send_default_pii=False, before_send=_before_send,
                    traces_sample_rate=0.0, release=APP_VERSION)

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

# Only behind Cloudflare: the edge sets CF-Connecting-IP / CF-IPCountry and
# strips any a client sent. Anywhere else those headers are attacker-chosen.
_TRUST_CF = _env_flag("TRUST_CF_HEADERS", False)


def _client_ip(request: Request) -> str:
    if _TRUST_CF:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
    return request.client.host if request.client else "unknown"


def _rate_key(request: Request) -> str:
    """User id when the session cookie verifies, else the client IP. Read
    directly rather than via the dependency so an invalid cookie doesn't
    401 a login attempt before the limiter sees it."""
    token = request.cookies.get("aboyeur_session")
    if token:
        try:
            from aboyeur import auth as _auth
            return f"u:{_auth.verify_token(token)}"
        except Exception:
            pass
    return f"ip:{_client_ip(request)}"


def _ip_key(request: Request) -> str:
    return f"ip:{_client_ip(request)}"


# RATE_LIMIT=off disables enforcement (the test suite; a load test). The
# decorators stay in place so the limits are documented at the route.
_limiter = Limiter(key_func=_rate_key, enabled=_env_flag("RATE_LIMIT", True),
                   headers_enabled=True)
app.state.limiter = _limiter


@app.exception_handler(RateLimitExceeded)
def _rate_limited(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        {"detail": f"rate limit exceeded: {exc.detail}", "error": "rate_limited"}, status_code=429,
    )


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
    logger.info("Aboyeur up", extra={"event": "startup"})
    # Deleted accounts past their grace period: purge on boot as well as via
    # `aboyeur purge` so a box with no cron still honours the 30-day promise.
    try:
        conn = _profile_conn()
        try:
            purged = _aboyeur_account.purge_deleted(conn)
        finally:
            conn.close()
        if purged:
            logger.info("purged %d deleted account(s)", len(purged), extra={"event": "purge"})
    except Exception:
        logger.exception("startup purge failed")


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

from aboyeur import account as _aboyeur_account
from aboyeur import admin as _aboyeur_admin
from aboyeur import auth as _aboyeur_auth
from aboyeur import billing as _aboyeur_billing
from aboyeur import chat_history as _aboyeur_chat_history
from aboyeur import clients as _aboyeur_clients
from aboyeur import legal as _aboyeur_legal
from aboyeur import llm_providers as _aboyeur_providers
from aboyeur import llm_settings as _aboyeur_llm_settings
from aboyeur import mail as _aboyeur_mail
from aboyeur import sensitivity as _aboyeur_sensitivity
from aboyeur.agent import Agent as _AboyeurAgent
from aboyeur.config import Config as _AboyeurConfig
from aboyeur.stripe_client import StripeClient as _StripeClient, StripeError as _StripeError, \
    StripeNotConfigured as _StripeNotConfigured
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
from backend.turns import Turn as _Turn, TurnInFlight as _TurnInFlight, TurnRegistry as _TurnRegistry

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

# One Stripe client for the process. Tests swap it for a recorder via
# `backend.main._stripe = FakeStripe()`; billing.py only ever sees the
# argument it's handed, so nothing here needs re-import.
_stripe = _StripeClient(_aboyeur_config.stripe_secret_key, _aboyeur_config.stripe_webhook_secret)

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

# One in-flight turn per conversation, living on its own thread so a dropped
# browser connection can't abort it (see backend/turns.py). Keyed by
# chat_session_id — the same conversation identity the agent cache uses, so
# "one Agent per conversation" and "one turn at a time per Agent" line up.
_turns = _TurnRegistry()


# Each account brings its own LLM endpoint + key (the Settings screen, stored
# encrypted by aboyeur/llm_settings.py) or opts into a hosted provider
# (aboyeur/llm_providers.py). Env LLM_* is only a fallback, and only for the
# two paths that have no logged-in user of their own: the CLI and the
# OpenWebUI shim's seed household — and only when LLM_BASE_URL is set.


def _resolve_llm(user_id: int) -> _aboyeur_providers.ResolvedLlm:
    """Which LLM this user's turn runs on — BYOK (their key, unmetered) or
    hosted (our provider, metered against their credit). Raises
    HTTPException with the billing/setup reason; see llm_providers.resolve_llm.

    The seed account keeps the env-level LLM_* fallback the CLI uses, but
    only when one is configured — an empty LLM_BASE_URL is "no fallback",
    never a private default."""
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.get_user(conn, user_id)
        try:
            return _aboyeur_providers.resolve_llm(conn, _aboyeur_config, user)
        except _aboyeur_providers.ProviderError as e:
            if (e.status == 409 and user_id == _SEED_USER_ID
                    and _aboyeur_config.has_platform_llm):
                return _aboyeur_providers.ResolvedLlm(
                    config=_aboyeur_config, provider=None, billable=False)
            raise HTTPException(e.status, {"message": str(e), **e.extra})
    except _aboyeur_billing.BillingError as e:
        raise HTTPException(e.status, {"message": str(e), **e.extra})
    finally:
        conn.close()


def _effective_config(user_id: int) -> Optional[_AboyeurConfig]:
    """Back-compat shim for callers that only want a Config or None."""
    try:
        return _resolve_llm(user_id).config
    except HTTPException:
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
        name = _aboyeur_clients.client_name(conn, client_id)
    finally:
        conn.close()
    return name or "this client"


def _get_session_agent(user_id: int, client_id: int,
                       chat_session_id: int) -> Optional[_AboyeurAgent]:
    with _aboyeur_sessions_lock:
        key = (user_id, client_id, chat_session_id)
        agent = _aboyeur_session_agents.get(key)
        if agent is None:
            resolved = _resolve_llm(user_id)
            agent = _AboyeurAgent(
                _profile_conn(), resolved.config, client_id,
                history=_stored_history(chat_session_id),
                client_name=_client_name(client_id),
            )
            # Which provider (if any) to meter this agent's turns against.
            # Set once at build time; a provider switch drops the agent
            # (_drop_session_agent), so the next turn re-resolves.
            agent.resolved = resolved
            _aboyeur_session_agents[key] = agent
        return agent


def _reset_session_agent(user_id: int, client_id: int, chat_session_id: int,
                         guest: bool = False) -> Optional[_AboyeurAgent]:
    resolved = _resolve_llm(user_id)
    with _aboyeur_sessions_lock:
        agent = _AboyeurAgent(
            _profile_conn(), resolved.config, client_id,
            guest_profile=_make_guest_profile() if guest else None,
            client_name=_client_name(client_id),
        )
        agent.resolved = resolved
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
        user_id = _aboyeur_auth.verify_token(token)
    except _aboyeur_auth.AuthError as e:
        raise HTTPException(401, str(e))
    # A soft-deleted account's cookie may still be valid for 30 days; the
    # account isn't. One indexed read per request.
    conn = _profile_conn()
    try:
        if _aboyeur_account.is_deleted(conn, user_id):
            raise HTTPException(401, "account deleted")
    finally:
        conn.close()
    return user_id


def _billing_user(user_id: int) -> dict:
    conn = _profile_conn()
    try:
        return _aboyeur_billing.get_user(conn, user_id)
    finally:
        conn.close()


def require_verified(user_id: int = Depends(get_current_user)) -> int:
    """Unverified accounts can log in and look around, but not start turns
    or pay. 403 with error=unverified so the SPA can offer a resend."""
    user = _billing_user(user_id)
    if not user["email_verified_at"]:
        raise HTTPException(403, {"message": "verify your email address first",
                                  "error": "unverified"})
    return user_id


class LoginBody(BaseModel):
    email: str
    password: str


@app.post("/auth/login")
@_limiter.limit("10/minute", key_func=_ip_key)
def auth_login(body: LoginBody, request: Request, response: Response):
    conn = _profile_conn()
    try:
        user_id = _aboyeur_auth.verify_login(conn, body.email, body.password)
        if _aboyeur_account.is_deleted(conn, user_id):
            raise HTTPException(401, "invalid email or password")
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


def require_admin(user_id: int = Depends(get_current_user)) -> int:
    """The only gate on /api/admin. 403, not 404 — the caller is authenticated,
    they just aren't the operator, and pretending the console doesn't exist
    buys nothing when the frontend bundle ships to everyone anyway."""
    conn = _profile_conn()
    try:
        if not _aboyeur_admin.is_admin(conn, user_id):
            raise HTTPException(403, "admin only")
    finally:
        conn.close()
    return user_id


@app.get("/auth/me")
def auth_me(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        admin = _aboyeur_admin.is_admin(conn, user_id)
        user = _aboyeur_billing.get_user(conn, user_id)
        tos = _aboyeur_account.tos_status(conn, user_id)
    finally:
        conn.close()
    # The frontend hides the admin nav on this flag. It is a convenience, not
    # the gate — every /admin route re-checks server-side.
    return {
        "user_id": user_id,
        "is_admin": admin,
        "email_verified": bool(user["email_verified_at"]),
        "plan": user["plan"],
        "billing_status": user["billing_status"],
        "llm_mode": user["llm_mode"],
        "tos": tos,
    }


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


def _run_agent_turn(turn: _Turn, agent: _AboyeurAgent, message: str,
                    record_for: Optional[int] = None,
                    user_id: Optional[int] = None, client_id: Optional[int] = None) -> None:
    """The worker body of one agent turn — runs on the Turn's thread, to
    completion, whether or not anyone is still reading (backend/turns.py).

    record_for: the conversation this turn belongs to. A real (even if
    half-streamed) answer is saved so it's still there on the next visit — but
    a turn that produced nothing but the internal-error notice is NOT persisted,
    so a transient endpoint failure doesn't litter the transcript with error
    messages the user re-reads every time they reopen the chat.

    user_id/client_id: who this turn was for, recorded on the `events` row so
    the admin console can answer "who hit this error" — the thing the bare
    logger.exception below could never say.

    This is the ONLY place a turn's transcript row and chat_turn event are
    written, and it runs exactly once per turn — so however many readers
    (the original response, a reconnect) observe the same turn, nothing is
    persisted twice. Because the thread is not an HTTP-bound generator, a
    client disconnect can no longer throw GeneratorExit into the agent loop:
    a genuine mid-turn failure now reaches `except Exception` here and
    Agent._send_turn's own history rollback, as intended."""
    parts: list[str] = []
    errored = False
    error_type: Optional[str] = None
    error_detail: Optional[str] = None
    started = time.monotonic()
    try:
        for tok in _agent_tokens(agent, message):
            parts.append(tok)
            turn.append(tok)
    except Exception as e:
        # Log the real cause server-side and end the turn cleanly with a
        # well-formed (if apologetic) tail, rather than a truncated stream.
        logger.exception("agent turn failed mid-response")
        errored = True
        error_type = type(e).__name__
        error_detail = _traceback.format_exc()
        turn.append("\n\n[Aboyeur hit an internal error and couldn't finish this response — check server logs.]")
    latency_ms = int((time.monotonic() - started) * 1000)
    answer = "".join(parts)
    try:
        if record_for is not None and answer.strip():
            _record_message(record_for, "assistant", answer)
        elif record_for is not None and errored:
            # Nothing worth keeping — drop the orphaned user turn too so a retry
            # starts clean instead of stacking two user messages in the history.
            _delete_last_user_message(record_for)
        # After the stream, never inside it: one short write on a connection of
        # its own, so the event log can't slow a turn down or hold a lock while
        # the LLM is still talking.
        _record_turn_event(
            user_id=user_id, client_id=client_id, session_id=record_for,
            agent=agent, latency_ms=latency_ms, ok=not errored,
            error_type=error_type, error_detail=error_detail,
            answer_chars=len(answer), prompt_chars=len(message),
        )
    finally:
        # Readers unblock only once persistence is done, so "the stream ended"
        # always means "the transcript has it".
        turn.finish(error=errored)
    # Off the critical path: readers are already unblocked. Still on this
    # thread so a slow classifier can't hold up anything but itself.
    if record_for is not None and user_id is not None:
        _classify_session_messages(agent, user_id, record_for)


def _start_agent_turn(turn: _Turn, agent: _AboyeurAgent, message: str,
                      record_for: Optional[int] = None,
                      user_id: Optional[int] = None, client_id: Optional[int] = None) -> _Turn:
    turn.start(lambda t: _run_agent_turn(t, agent, message, record_for=record_for,
                                         user_id=user_id, client_id=client_id))
    return turn


def _turn_sse(turn: _Turn, start: int = 0) -> Iterator[str]:
    """SSE frames for one turn's buffered + live chunks, then [DONE]. Pure
    reader: closing this generator (client gone) doesn't touch the turn."""
    for tok in turn.stream(start):
        yield _sse(tok)
    yield _sse("[DONE]")


def _agent_stream(agent: _AboyeurAgent, message: str, record_for: Optional[int] = None,
                  user_id: Optional[int] = None, client_id: Optional[int] = None,
                  turn: Optional[_Turn] = None) -> Iterator[str]:
    """Start a turn and stream it as SSE. `turn` is the registry-reserved
    Turn from /chat (the in-flight guard + reconnect handle); without one an
    unregistered Turn is used (direct callers with no conversation)."""
    if turn is None:
        turn = _Turn()
    _start_agent_turn(turn, agent, message, record_for=record_for,
                      user_id=user_id, client_id=client_id)
    yield from _turn_sse(turn)


def _record_turn_event(*, user_id: Optional[int], client_id: Optional[int],
                       session_id: Optional[int], agent: _AboyeurAgent,
                       latency_ms: int, ok: bool,
                       error_type: Optional[str] = None,
                       error_detail: Optional[str] = None,
                       answer_chars: int = 0, prompt_chars: int = 0) -> None:
    """The turn's `events` row and — for a hosted (billable) agent — its
    usage_ledger debit, in one transaction so a crash between the two can't
    leave a turn recorded but unbilled or billed but unrecorded."""
    tokens_in, tokens_out = getattr(agent, "last_usage", None) or (None, None)
    resolved = getattr(agent, "resolved", None)
    billable = bool(resolved and resolved.billable and user_id is not None)
    conn = _profile_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO events (ts, kind, user_id, client_id, session_id, model, "
            "latency_ms, tool_calls, tokens_in, tokens_out, ok, error_type, "
            "error_detail, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "chat_turn",
                user_id, client_id, session_id, getattr(agent.config, "llm_model", None),
                latency_ms, getattr(agent, "last_tool_calls", None),
                tokens_in, tokens_out, 1 if ok else 0, error_type,
                (error_detail or "")[:4000] or None,
                json.dumps({"provider_id": resolved.provider_id}) if billable else None,
            ),
        )
        event_id = cur.lastrowid
        if billable:
            estimated = tokens_in is None and tokens_out is None
            if estimated:
                # No usage chunk from the endpoint: charge on a chars/4
                # estimate and mark the row so admin can see the provider
                # isn't reporting.
                tokens_in, tokens_out = prompt_chars // 4, answer_chars // 4
            user = _aboyeur_billing.get_user(conn, user_id)
            _aboyeur_billing.ledger_entry(
                conn, user_id,
                "chat_turn_est" if estimated else "chat_turn",
                _turn_charge_for(user, resolved.provider, tokens_in or 0, tokens_out or 0),
                cost_cents=_aboyeur_billing.turn_charge(
                    resolved.provider["price_in_per_mtok"], resolved.provider["price_out_per_mtok"],
                    resolved.provider["markup_pct"], tokens_in or 0, tokens_out or 0)[0],
                tokens_in=tokens_in or 0, tokens_out=tokens_out or 0,
                model=resolved.provider["model"], provider_id=resolved.provider_id,
                event_id=event_id, commit=False,
            )
        conn.execute("COMMIT")
    except Exception:  # monitoring must never break the thing it monitors
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.exception("couldn't record chat_turn event")
    finally:
        conn.close()
    if billable:
        _maybe_low_balance_email(user_id)


def _turn_charge_for(user: dict, provider: dict, tokens_in: int, tokens_out: int) -> int:
    _, charge = _aboyeur_billing.turn_charge(
        provider["price_in_per_mtok"], provider["price_out_per_mtok"], provider["markup_pct"],
        tokens_in, tokens_out,
    )
    return 0 if user["billing_status"] == "comp" else charge


def _maybe_low_balance_email(user_id: int) -> None:
    conn = _profile_conn()
    try:
        if not _aboyeur_billing.needs_low_balance_email(conn, user_id):
            return
        user = _aboyeur_billing.get_user(conn, user_id)
    finally:
        conn.close()
    _aboyeur_mail.send(user["email"], "low_balance", {
        "balance": f"${max(user['credit_cents'], 0) / 100:.2f}",
        "link": f"{_aboyeur_config.app_base_url}/app/settings",
    })


def _classify_session_messages(agent: _AboyeurAgent, user_id: int, chat_session_id: int) -> None:
    """Sensitivity-label every unlabeled user message in this conversation
    using the agent's own LLM client (BYOK: their key, free; hosted: metered
    as kind=classify). Any failure -> 'unknown', which the console treats
    as sensitive. See aboyeur/sensitivity.py."""
    conn = _profile_conn()
    try:
        pending = _aboyeur_sensitivity.unclassified_user_messages(conn, chat_session_id)
        if not pending:
            return
        resolved = getattr(agent, "resolved", None)
        for msg in pending:
            label, cats, usage = _aboyeur_sensitivity.classify_text(
                agent.client, agent.config.llm_model, msg["content"],
            )
            _aboyeur_sensitivity.store(conn, msg["id"], label, cats)
            if resolved and resolved.billable and usage:
                user = _aboyeur_billing.get_user(conn, user_id)
                _aboyeur_billing.meter_turn(
                    conn, user, resolved.provider, usage[0], usage[1],
                    estimated=False, event_id=None, kind="classify",
                )
    except Exception:
        logger.exception("sensitivity classification failed for session %s", chat_session_id)
        try:
            for msg in pending:  # type: ignore[possibly-undefined]
                _aboyeur_sensitivity.mark_unknown(conn, msg["id"])
        except Exception:
            pass
    finally:
        conn.close()


def _delete_last_user_message(chat_session_id: int) -> None:
    conn = _profile_conn()
    try:
        _aboyeur_chat_history.delete_last_message_if_role(conn, chat_session_id, "user")
    except Exception:  # transcript cleanup must never break the response
        logger.exception("couldn't roll back user message for conversation %s", chat_session_id)
    finally:
        conn.close()


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
@_limiter.limit("30/minute")
def chat(body: ChatBody, request: Request, user_id: int = Depends(require_verified)):
    conn = _profile_conn()
    try:
        client_id = _require_client(conn, user_id, body.client_id)
        chat_session_id = _require_chat_session(conn, client_id, body.session_id)
        # Plan quota before anything is reserved or recorded: a 402 here
        # leaves no orphan user message and no in-flight turn.
        user = _aboyeur_billing.get_user(conn, user_id)
        byok = user["llm_mode"] == "byok" and \
            _aboyeur_llm_settings.get_llm_settings_public(conn, user_id)["configured"]
        try:
            _aboyeur_billing.check_turn_quota(conn, user, byok=byok)
        except _aboyeur_billing.BillingError as e:
            raise HTTPException(e.status, {"message": str(e), **e.extra})
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

    # One turn at a time per conversation. A second message while one is
    # still generating is REJECTED (409), not queued: the UI disables send
    # while streaming and, after a refresh, while catching up — so this only
    # fires for a second tab or an impatient retry, where "wait for the
    # answer in progress" is the honest response. Reserve before recording
    # the user message so a rejected duplicate leaves no orphan row.
    try:
        turn = _turns.begin(chat_session_id)
    except _TurnInFlight:
        raise HTTPException(
            409, "Aboyeur is still answering the previous message in this conversation — "
                 "wait for it to finish (reload to catch up on it) before sending another.")

    try:
        if guest is not None:
            agent = _reset_session_agent(user_id, client_id, chat_session_id, guest=True)
            # Guest turns are deliberately not recorded — nothing a guest says is
            # persisted anywhere, transcript included.
            _start_agent_turn(turn, agent, guest or _GUEST_DEFAULT_PROMPT)
            return StreamingResponse(_turn_sse(turn), media_type="text/event-stream")

        # Raises 402/409/503 (billing, setup, no provider) — the except below
        # releases the reservation so the user can retry once it's fixed.
        agent = _get_session_agent(user_id, client_id, chat_session_id)
        _record_message(chat_session_id, "user", body.message)
        _start_agent_turn(turn, agent, body.message, record_for=chat_session_id,
                          user_id=user_id, client_id=client_id)
    except Exception:
        _turns.release(chat_session_id, turn)
        raise
    return StreamingResponse(_turn_sse(turn), media_type="text/event-stream")


@app.get("/chat/status")
def chat_status(client_id: Optional[int] = None, session_id: Optional[int] = None,
                user_id: int = Depends(get_current_user)):
    """Is a turn in flight (or just finished) for this conversation? The
    chat screen asks on load so a refresh mid-answer shows "still
    generating" and catches up instead of presenting stale state."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        chat_session_id = _require_chat_session(conn, resolved, session_id)
    finally:
        conn.close()
    turn = _turns.get(chat_session_id)
    return {"session_id": chat_session_id, "turn": turn.snapshot() if turn is not None else None}


@app.get("/chat/stream")
def chat_stream(client_id: Optional[int] = None, session_id: Optional[int] = None,
                start: int = Query(0, ge=0), user_id: int = Depends(get_current_user)):
    """Re-attach to a conversation's current turn: replays the chunks
    buffered so far from index `start`, then streams live until it ends —
    same SSE framing as POST /chat, so the client reads both the same way.
    404 when there's no turn to attach to (nothing running or recently done)."""
    conn = _profile_conn()
    try:
        resolved = _require_client(conn, user_id, client_id)
        chat_session_id = _require_chat_session(conn, resolved, session_id)
    finally:
        conn.close()
    turn = _turns.get(chat_session_id)
    if turn is None:
        raise HTTPException(404, "no turn in progress for this conversation")
    return StreamingResponse(_turn_sse(turn, start), media_type="text/event-stream")


def _require_chat_session(conn: sqlite3.Connection, client_id: int,
                          session_id: Optional[int]) -> int:
    """A conversation id is only valid for the client that owns it — and that
    client has already been checked against the account."""
    try:
        return _aboyeur_chat_history.resolve_session(conn, client_id, session_id)
    except _aboyeur_chat_history.UnknownSession:
        raise HTTPException(404, "no such conversation")


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
    if not _aboyeur_config.has_platform_llm:
        # No env fallback configured (the hosted deploy). The shim has no
        # login and therefore no BYOK/hosted resolution of its own.
        raise HTTPException(503, "no platform LLM configured (LLM_BASE_URL is empty)")
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
        "SELECT id, email, name, practice, created_at, country, plan, billing_status, "
        "llm_mode, credit_cents, email_verified_at, tos_version, deleted_at "
        "FROM users WHERE id = ?", (user_id,)
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
        "self_client": ({"id": self_client["id"],
                         "name": _aboyeur_clients.client_name(conn, self_client["id"])}
                        if self_client is not None else None),
        "country": row["country"],
        "plan": row["plan"],
        "billing_status": row["billing_status"],
        "llm_mode": row["llm_mode"],
        "credit_cents": row["credit_cents"],
        "email_verified": bool(row["email_verified_at"]),
        "tos": _aboyeur_account.tos_status(conn, user_id),
        "support_access": [
            dict(r) for r in conn.execute(
                "SELECT ts, session_id FROM support_access WHERE user_id = ? "
                "ORDER BY ts DESC LIMIT 20", (user_id,)
            )
        ],
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
        out = _aboyeur_llm_settings.get_llm_settings_public(conn, user_id)
        user = _aboyeur_billing.get_user(conn, user_id)
        out["byok_configured"] = out["configured"]
        offer = _aboyeur_providers.hosted_offer(conn, user)
        # `configured` = "a turn will find a model", which is what the SPA's
        # /setup gate and the Chat composer key off.
        if user["llm_mode"] == "hosted":
            out["configured"] = bool(offer) and _aboyeur_billing.is_paid(user)
        out["llm_mode"] = user["llm_mode"]
        out["plan"] = user["plan"]
        out["billing_status"] = user["billing_status"]
        out["is_paid"] = _aboyeur_billing.is_paid(user)
        out["credit_cents"] = user["credit_cents"]
        out["hosted_offer"] = offer
        out["billing_enabled"] = _stripe.configured
        out["topup_amounts"] = list(_aboyeur_billing.TOPUP_AMOUNTS_CENTS)
        return out
    finally:
        conn.close()


@app.put("/api/llm-settings")
def api_llm_settings_put(body: LlmSettingsBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        public = _aboyeur_llm_settings.save_llm_settings(
            conn, user_id, body.base_url, body.model, body.api_key,
        )
        # Entering a key is choosing BYOK.
        conn.execute("UPDATE users SET llm_mode = 'byok' WHERE id = ?", (user_id,))
        conn.commit()
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
@_limiter.limit("10/hour")
def api_llm_settings_test(body: LlmTestBody, request: Request,
                          user_id: int = Depends(get_current_user)):
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
# Feedback — a thumb on one assistant message. User-facing half; the triage
# half lives under /admin below.
# ===========================================================================

class FeedbackBody(BaseModel):
    session_id: int
    message_id: int
    rating: int          # +1 | -1
    body: Optional[str] = None


@app.post("/api/feedback")
def api_feedback_post(body: FeedbackBody, user_id: int = Depends(get_current_user)):
    """Rate an assistant turn. Re-thumbing the same message updates the row
    rather than adding one."""
    conn = _profile_conn()
    try:
        # The message must be in a conversation on this account's own book.
        # Without this an id from someone else's transcript would attach
        # feedback to a turn the caller can't even read.
        if not _aboyeur_admin.owns_message(conn, user_id, body.session_id, body.message_id):
            raise HTTPException(404, "no such message")
        return _aboyeur_admin.upsert_feedback(
            conn, user_id, body.session_id, body.message_id, body.rating, body.body,
        )
    except _aboyeur_admin.AdminError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@app.delete("/api/feedback/message/{message_id}")
def api_feedback_delete(message_id: int, user_id: int = Depends(get_current_user)):
    """Un-thumb. Scoped to the caller's own row, so this can't clear anyone
    else's rating even with a guessed id."""
    conn = _profile_conn()
    try:
        return {"deleted": _aboyeur_admin.delete_feedback(conn, user_id, message_id)}
    finally:
        conn.close()


@app.get("/api/sessions/{session_id}/feedback")
def api_session_feedback(session_id: int, user_id: int = Depends(get_current_user)):
    """Which turns in this conversation the caller has already rated — what
    the chat screen needs to render its thumbs lit on load."""
    conn = _profile_conn()
    try:
        ratings = _aboyeur_admin.my_feedback_for_session(conn, user_id, session_id)
        return {"ratings": {str(k): v for k, v in ratings.items()}}
    finally:
        conn.close()


# ===========================================================================
# Admin console — operator-only. Every route here is gated by require_admin
# and every one of them is a read, except the feedback triage PATCH.
#
# Mounted under /api/admin, not /admin: /admin is the *frontend* route for the
# console's own screens. One path can't be both a SPA route and an API prefix
# — the dev proxy and the production reverse proxy each have to send it to
# exactly one place, and deep-linking to /admin has to reach the React app.
# ===========================================================================

@app.get("/api/admin/overview")
def admin_overview(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        out = _aboyeur_admin.overview(conn)
        out["billing"] = _aboyeur_billing.billing_health(conn)
        out["hosted_1d"] = _aboyeur_admin.hosted_usage(conn, 1)
        out["hosted_7d"] = _aboyeur_admin.hosted_usage(conn, 7)
        out["estimated_rows_7d"] = _aboyeur_providers.estimated_rows_count(conn, 7)
        return out
    finally:
        conn.close()


@app.get("/api/admin/users")
def admin_users(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"users": _aboyeur_admin.list_users(conn)}
    finally:
        conn.close()


@app.get("/api/admin/users/{target_id}")
def admin_user_detail(target_id: int, _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_admin.get_user(conn, target_id)
    except _aboyeur_admin.AdminError as e:
        raise HTTPException(404, str(e))
    finally:
        conn.close()


@app.get("/api/admin/sessions/{session_id}")
def admin_transcript(session_id: int, reveal: bool = False,
                     admin_id: int = Depends(require_admin)):
    """Read a conversation. Messages the classifier flagged come back
    redacted unless ?reveal=1 — which is audited and shown to the user on
    their Account page. See docs/privacy-posture.md."""
    conn = _profile_conn()
    try:
        return _aboyeur_admin.get_transcript(conn, session_id, reveal=reveal, admin_id=admin_id)
    except _aboyeur_admin.AdminError as e:
        raise HTTPException(404, str(e))
    finally:
        conn.close()


@app.get("/api/admin/events")
def admin_events(kind: Optional[str] = None, errors_only: bool = False,
                 target_user_id: Optional[int] = None, days: Optional[int] = None,
                 limit: int = Query(200, ge=1, le=1000),
                 _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {
            "events": _aboyeur_admin.list_events(
                conn, kind=kind, errors_only=errors_only,
                user_id=target_user_id, days=days, limit=limit,
            )
        }
    finally:
        conn.close()


@app.get("/api/admin/logs/tail")
def admin_log_tail(lines: int = Query(200, ge=1, le=2000),
                   _: int = Depends(require_admin)):
    """Raw uvicorn.log tail — the escape hatch for whatever `events` didn't
    think to record. Reads the last chunk of the file rather than the whole
    thing, so a log that has grown to hundreds of MB still answers fast."""
    log_path = PROJECT_ROOT / "logs" / "uvicorn.log"
    if not log_path.exists():
        return {"path": str(log_path), "lines": [], "detail": "no log file yet"}
    # ~400 bytes per line is generous for access lines and leaves tracebacks
    # room; worst case the tail is short, which the caller can live with.
    size = log_path.stat().st_size
    window = min(size, lines * 400 + 8192)
    with open(log_path, "rb") as handle:
        handle.seek(size - window)
        chunk = handle.read().decode("utf-8", "replace")
    tail = chunk.splitlines()
    # A mid-file seek lands inside a line; drop that first partial one.
    if window < size and tail:
        tail = tail[1:]
    # Emails / phones / anything key-shaped are masked before leaving.
    return {"path": str(log_path), "lines": _aboyeur_admin.sanitize_log_lines(tail[-lines:])}


@app.get("/api/admin/feedback")
def admin_feedback_list(status: Optional[str] = None,
                        limit: int = Query(200, ge=1, le=1000),
                        _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"feedback": _aboyeur_admin.list_feedback(conn, status, limit)}
    finally:
        conn.close()


class FeedbackPatch(BaseModel):
    status: Optional[str] = None
    admin_note: Optional[str] = None


@app.patch("/api/admin/feedback/{feedback_id}")
def admin_feedback_update(feedback_id: int, patch: FeedbackPatch,
                          _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_admin.update_feedback(conn, feedback_id, patch.status, patch.admin_note)
    except _aboyeur_admin.AdminError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


# ===========================================================================
# Health — no secrets, no counts that leak tenancy. Litestream's replication
# lag is read from its metrics endpoint when LITESTREAM_METRICS_URL is set
# (compose does), else omitted.
# ===========================================================================

@app.get("/health")
def health():
    ok, db_ok = True, True
    try:
        conn = _profile_conn()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except Exception:
        ok = db_ok = False
    out = {"ok": ok, "db": db_ok, "version": APP_VERSION}
    metrics_url = os.environ.get("LITESTREAM_METRICS_URL")
    if metrics_url:
        try:
            import httpx as _httpx
            text = _httpx.get(metrics_url, timeout=2).text
            lag = _litestream_lag(text)
            if lag is not None:
                out["litestream_lag_s"] = lag
        except Exception:
            out["litestream_lag_s"] = None
    return JSONResponse(out, status_code=200 if ok else 503)


def _litestream_lag(metrics_text: str) -> Optional[float]:
    """Seconds since Litestream's last successful sync, from its Prometheus
    metrics (litestream_replica_sync_time_seconds or ..._last_sync)."""
    import re as _re
    latest = None
    for line in metrics_text.splitlines():
        if line.startswith("litestream_replica_sync_time_seconds") or \
                line.startswith("litestream_replica_last_sync"):
            m = _re.search(r"\s([0-9.eE+-]+)$", line)
            if m:
                latest = max(latest or 0.0, float(m.group(1)))
    if latest is None:
        return None
    return round(max(0.0, time.time() - latest), 1)


# ===========================================================================
# Self-serve auth: signup / verify / reset. Login/logout are above.
# ===========================================================================

class SignupBody(BaseModel):
    email: str
    password: str
    country: Optional[str] = None
    invite_code: Optional[str] = None
    tos_version: Optional[str] = None


def _account_http(e: _aboyeur_account.AccountError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), **e.extra})


def _send_verification(user_id: int, email: str) -> None:
    conn = _profile_conn()
    try:
        token = _aboyeur_account.issue_token(conn, user_id, "verify")
    finally:
        conn.close()
    _aboyeur_mail.send(email, "verify", {"link": f"{_aboyeur_config.app_base_url}/verify?token={token}"})


@app.post("/auth/signup")
@_limiter.limit("5/hour", key_func=_ip_key)
def auth_signup(body: SignupBody, request: Request, response: Response):
    conn = _profile_conn()
    try:
        user_id = _aboyeur_account.signup(
            conn, email=body.email, password=body.password,
            signup_mode=_aboyeur_config.signup_mode, invite_code=body.invite_code,
            country=body.country, tos_version=body.tos_version,
        )
        email = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"]
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()
    _send_verification(user_id, email)
    _aboyeur_admin_event("signup", user_id)
    token = _aboyeur_auth.issue_token(user_id)
    response.set_cookie(
        _SESSION_COOKIE, token, httponly=True, samesite="lax", secure=_COOKIE_SECURE,
        max_age=_aboyeur_auth.JWT_TTL_SECONDS,
    )
    return {"user_id": user_id, "email_verified": False}


def _aboyeur_admin_event(kind: str, user_id: Optional[int]) -> None:
    conn = _profile_conn()
    try:
        _aboyeur_admin.record_event(conn, kind, user_id=user_id)
    except Exception:
        logger.exception("couldn't record %s event", kind)
    finally:
        conn.close()


@app.get("/auth/signup-mode")
def auth_signup_mode():
    return {"mode": _aboyeur_config.signup_mode, "legal_version": _aboyeur_legal.LEGAL_VERSION}


class TokenBody(BaseModel):
    token: str


@app.post("/auth/verify")
def auth_verify(body: TokenBody):
    conn = _profile_conn()
    try:
        user_id = _aboyeur_account.verify_email(conn, body.token)
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()
    return {"ok": True, "user_id": user_id}


@app.post("/auth/resend-verification")
@_limiter.limit("5/hour")
def auth_resend_verification(request: Request, user_id: int = Depends(get_current_user)):
    user = _billing_user(user_id)
    if user["email_verified_at"]:
        return {"ok": True, "already_verified": True}
    _send_verification(user_id, user["email"])
    return {"ok": True}


class ResetRequestBody(BaseModel):
    email: str


@app.post("/auth/reset")
@_limiter.limit("5/hour", key_func=_ip_key)
def auth_reset_request(body: ResetRequestBody, request: Request):
    """Always 200 — the response must not reveal whether the email exists."""
    conn = _profile_conn()
    try:
        found = _aboyeur_account.request_reset(conn, body.email)
    finally:
        conn.close()
    if found:
        _, token = found
        _aboyeur_mail.send(body.email.strip().lower(), "reset", {
            "email": body.email.strip().lower(),
            "link": f"{_aboyeur_config.app_base_url}/reset?token={token}",
        })
    return {"ok": True}


class ResetConfirmBody(BaseModel):
    token: str
    new_password: str


@app.post("/auth/reset/confirm")
@_limiter.limit("10/hour", key_func=_ip_key)
def auth_reset_confirm(body: ResetConfirmBody, request: Request):
    conn = _profile_conn()
    try:
        _aboyeur_account.reset_password(conn, body.token, body.new_password)
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()
    return {"ok": True}


# ===========================================================================
# Legal documents — markdown served to the SPA's /legal/<slug> route.
# ===========================================================================

_LEGAL_DIR = PROJECT_ROOT / "legal"


@app.get("/api/legal/{slug}")
def api_legal(slug: str):
    if slug not in _aboyeur_legal.LEGAL_DOCS:
        raise HTTPException(404, "no such document")
    path = _LEGAL_DIR / f"{slug}.md"
    if not path.exists():
        raise HTTPException(404, "document not published")
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/markdown")


@app.get("/api/legal")
def api_legal_index():
    return {"version": _aboyeur_legal.LEGAL_VERSION, "documents": list(_aboyeur_legal.LEGAL_DOCS)}


# ===========================================================================
# Account: ToS, country, export, delete
# ===========================================================================

class AcceptTosBody(BaseModel):
    version: str


@app.post("/api/account/accept-tos")
def api_accept_tos(body: AcceptTosBody, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        return _aboyeur_account.accept_tos(conn, user_id, body.version)
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()


class CountryBody(BaseModel):
    country: Optional[str] = None


@app.put("/api/account/country")
def api_account_country(body: CountryBody, user_id: int = Depends(get_current_user)):
    """Changing country never reprices a live subscription — it applies at
    the next checkout. Stripe's customer metadata is updated then."""
    country = (body.country or "").strip().upper() or None
    if country and not (len(country) == 2 and country.isalpha()):
        raise HTTPException(400, "country must be a two-letter ISO code")
    conn = _profile_conn()
    try:
        conn.execute("UPDATE users SET country = ? WHERE id = ?", (country, user_id))
        conn.commit()
    finally:
        conn.close()
    return {"country": country}


@app.get("/api/account/export")
def api_account_export(user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        data = _aboyeur_account.export_zip(conn, user_id)
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()
    return Response(
        data, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="aboyeur-export-{user_id}.zip"'},
    )


class DeleteAccountBody(BaseModel):
    password: Optional[str] = None
    confirm: str


@app.delete("/api/account")
def api_account_delete(body: DeleteAccountBody, response: Response,
                       user_id: int = Depends(get_current_user)):
    """Soft delete now; hard purge after 30 days (`aboyeur purge`, also on
    boot). The Stripe subscription is canceled at once; the customer is
    kept for tax records."""
    if body.confirm.strip().upper() != "DELETE":
        raise HTTPException(400, "type DELETE to confirm")
    conn = _profile_conn()
    try:
        row = conn.execute("SELECT email, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if row["password_hash"] and not _aboyeur_auth.verify_password(
                body.password or "", row["password_hash"]):
            raise HTTPException(403, "password is wrong")
        result = _aboyeur_account.soft_delete(conn, user_id)
        sub_id = result.pop("stripe_subscription_id", None)
        if sub_id:
            try:
                _stripe.cancel_subscription(sub_id, prorate=False)
            except _StripeError as e:
                logger.warning("delete: couldn't cancel subscription %s: %s", sub_id, e)
            conn.execute(
                "UPDATE users SET stripe_subscription_id = NULL, billing_status = 'canceled', "
                "plan = 'free' WHERE id = ?", (user_id,),
            )
            conn.commit()
        email = row["email"]
    except _aboyeur_account.AccountError as e:
        raise _account_http(e)
    finally:
        conn.close()
    _drop_session_agent(user_id)
    _aboyeur_mail.send(email, "deleted", {"email": email, "purge_date": result["purge_after"]})
    response.delete_cookie(_SESSION_COOKIE, httponly=True, samesite="lax", secure=_COOKIE_SECURE)
    return result


# ===========================================================================
# Billing — Stripe Checkout / Portal / webhook. No Stripe JS on the client.
# ===========================================================================

def _billing_http(e: _aboyeur_billing.BillingError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), **e.extra})


def _stripe_ready() -> None:
    if not _stripe.configured:
        raise HTTPException(503, {"message": "billing isn't enabled on this deploy",
                                  "error": "billing_disabled"})


def _country_for(request: Request, user: dict) -> str:
    return _aboyeur_billing.resolve_country(user, dict(request.headers), _TRUST_CF)


class CheckoutBody(BaseModel):
    interval: str = "month"


@app.post("/api/billing/checkout")
def api_billing_checkout(body: CheckoutBody, request: Request,
                         user_id: int = Depends(get_current_user)):
    _stripe_ready()
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.get_user(conn, user_id)
        url = _aboyeur_billing.create_subscription_checkout(
            conn, _stripe, user_id, body.interval, _country_for(request, user),
            _aboyeur_config.app_base_url,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    except _StripeError as e:
        raise HTTPException(502, {"message": f"Stripe: {e}", "error": "stripe"})
    finally:
        conn.close()
    return {"url": url}


class TopupBody(BaseModel):
    amount_cents: int


@app.post("/api/billing/topup")
def api_billing_topup(body: TopupBody, request: Request, user_id: int = Depends(get_current_user)):
    _stripe_ready()
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.get_user(conn, user_id)
        url = _aboyeur_billing.create_topup_checkout(
            conn, _stripe, user_id, body.amount_cents, _country_for(request, user),
            _aboyeur_config.app_base_url,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    except _StripeError as e:
        raise HTTPException(502, {"message": f"Stripe: {e}", "error": "stripe"})
    finally:
        conn.close()
    return {"url": url}


@app.post("/api/billing/portal")
def api_billing_portal(user_id: int = Depends(get_current_user)):
    _stripe_ready()
    conn = _profile_conn()
    try:
        url = _aboyeur_billing.create_portal(conn, _stripe, user_id, _aboyeur_config.app_base_url)
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    except _StripeError as e:
        raise HTTPException(502, {"message": f"Stripe: {e}", "error": "stripe"})
    finally:
        conn.close()
    return {"url": url}


@app.get("/api/billing/me")
def api_billing_me(request: Request, user_id: int = Depends(get_current_user)):
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.get_user(conn, user_id)
        out = _aboyeur_billing.billing_me(conn, user_id, _country_for(request, user))
        out["billing_enabled"] = _stripe.configured
        out["hosted_offer"] = _aboyeur_providers.hosted_offer(conn, user)
        return out
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()


@app.post("/api/billing/webhook")
async def api_billing_webhook(request: Request):
    """Stripe -> us. Signature verified, event id recorded first (duplicate
    -> 200 and done), then handled. A handler error is stored on the event
    row and still answered 200: Stripe would otherwise retry for days, and
    the admin console lists unprocessed events for a human to look at."""
    payload = await request.body()
    try:
        event = _stripe.construct_event(payload, request.headers.get("stripe-signature"))
    except _StripeNotConfigured:
        raise HTTPException(503, "billing disabled")
    except _StripeError as e:
        raise HTTPException(400, str(e))
    conn = _profile_conn()
    try:
        if not _aboyeur_billing.record_event_once(conn, event):
            return {"ok": True, "duplicate": True}
        try:
            result = _aboyeur_billing.handle_event(conn, _stripe, event)
        except Exception:
            logger.exception("stripe event %s (%s) failed", event.get("id"), event.get("type"))
            return {"ok": False, "recorded": True}
        # A plan/provider change makes cached agents stale.
        obj = (event.get("data") or {}).get("object") or {}
        uid = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
        if uid:
            try:
                _drop_session_agent(int(uid))
            except (TypeError, ValueError):
                pass
    finally:
        conn.close()
    return {"ok": True, **result}


# ===========================================================================
# LLM mode (byok | hosted) — the Settings page's two radio cards.
# ===========================================================================

class LlmModeBody(BaseModel):
    mode: str


@app.put("/api/llm-settings/mode")
def api_llm_mode(body: LlmModeBody, user_id: int = Depends(get_current_user)):
    if body.mode not in ("byok", "hosted"):
        raise HTTPException(400, "mode must be byok or hosted")
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.get_user(conn, user_id)
        if body.mode == "hosted" and not _aboyeur_billing.is_paid(user):
            raise HTTPException(402, {"message": "Aboyeur's hosted models need a Pro plan",
                                      "error": "hosted_requires_pro", "upgrade_url": "/app/account"})
        conn.execute("UPDATE users SET llm_mode = ? WHERE id = ?", (body.mode, user_id))
        conn.commit()
    finally:
        conn.close()
    _drop_session_agent(user_id)
    return {"llm_mode": body.mode}


# ===========================================================================
# Admin: billing, prices, ladder, providers, invites, audit
# ===========================================================================

class AdminBillingBody(BaseModel):
    billing_status: str  # comp | free


@app.post("/api/admin/users/{target_id}/billing")
def admin_user_billing(target_id: int, body: AdminBillingBody,
                       admin_id: int = Depends(require_admin)):
    if body.billing_status not in ("comp", "free"):
        raise HTTPException(400, "billing_status must be comp or free")
    conn = _profile_conn()
    try:
        user = _aboyeur_billing.set_comp(conn, _stripe, admin_id, target_id,
                                         comp=body.billing_status == "comp")
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()
    _drop_session_agent(target_id)
    return user


class AdminCreditBody(BaseModel):
    cents: int
    note: Optional[str] = None


@app.post("/api/admin/users/{target_id}/credit")
def admin_user_credit(target_id: int, body: AdminCreditBody,
                      admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_billing.admin_add_credit(conn, admin_id, target_id, body.cents, body.note)
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()


class AdminLlmBody(BaseModel):
    llm_mode: Optional[str] = None
    llm_provider_id: Optional[int] = None
    clear_provider: bool = False


@app.post("/api/admin/users/{target_id}/llm")
def admin_user_llm(target_id: int, body: AdminLlmBody, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        provider = None if body.clear_provider else (
            body.llm_provider_id if body.llm_provider_id is not None else "unset")
        user = _aboyeur_billing.set_user_llm(
            conn, admin_id, target_id, llm_mode=body.llm_mode, llm_provider_id=provider,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()
    # Takes effect on the user's next turn, mid-conversation included.
    _drop_session_agent(target_id)
    return user


@app.get("/api/admin/users/{target_id}/ledger")
def admin_user_ledger(target_id: int, limit: int = Query(200, ge=1, le=1000),
                      _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"ledger": _aboyeur_billing.user_ledger(conn, target_id, limit)}
    finally:
        conn.close()


@app.get("/api/admin/prices")
def admin_prices(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"prices": _aboyeur_billing.list_prices(conn)}
    finally:
        conn.close()


class PriceBody(BaseModel):
    plan: str = "pro"
    interval: str
    country: str = "*"
    amount_cents: int


@app.post("/api/admin/prices")
def admin_price_create(body: PriceBody, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_billing.upsert_price(
            conn, _stripe, admin_id, plan=body.plan, interval=body.interval,
            country=body.country, amount_cents=body.amount_cents,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()


class PricePatch(BaseModel):
    amount_cents: int


@app.put("/api/admin/prices/{price_id}")
def admin_price_update(price_id: int, body: PricePatch, admin_id: int = Depends(require_admin)):
    """Archives the row (and its Stripe Price) and inserts a replacement.
    Existing subscribers are not repriced."""
    conn = _profile_conn()
    try:
        old = conn.execute("SELECT * FROM plan_prices WHERE id = ? AND active = 1", (price_id,)).fetchone()
        if old is None:
            raise HTTPException(404, "no such active price")
        return _aboyeur_billing.upsert_price(
            conn, _stripe, admin_id, plan=old["plan"], interval=old["interval"],
            country=old["country"], amount_cents=body.amount_cents,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()


@app.delete("/api/admin/prices/{price_id}")
def admin_price_delete(price_id: int, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        _aboyeur_billing.delete_price(conn, _stripe, admin_id, price_id)
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()
    return {"deleted": price_id}


@app.get("/api/admin/loyalty")
def admin_loyalty(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"ladder": _aboyeur_billing.list_ladder(conn)}
    finally:
        conn.close()


class TierBody(BaseModel):
    tier: str
    min_paid_months: int
    percent_off: int
    active: bool = True


@app.put("/api/admin/loyalty/{tier}")
def admin_loyalty_upsert(tier: str, body: TierBody, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_billing.upsert_tier(
            conn, _stripe, admin_id, tier=tier, min_paid_months=body.min_paid_months,
            percent_off=body.percent_off, active=body.active,
        )
    except _aboyeur_billing.BillingError as e:
        raise _billing_http(e)
    finally:
        conn.close()


@app.get("/api/admin/stripe-events")
def admin_stripe_events(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"events": _aboyeur_billing.unprocessed_events(conn)}
    finally:
        conn.close()


@app.get("/api/admin/audit")
def admin_audit_log(limit: int = Query(200, ge=1, le=1000), _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"audit": _aboyeur_admin.audit_log(conn, limit)}
    finally:
        conn.close()


# -- providers ---------------------------------------------------------------

def _provider_http(e: _aboyeur_providers.ProviderError) -> HTTPException:
    return HTTPException(e.status, {"message": str(e), **e.extra})


@app.get("/api/admin/llm-providers")
def admin_providers(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"providers": _aboyeur_providers.list_providers(conn)}
    finally:
        conn.close()


class ProviderBody(BaseModel):
    name: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    price_in_per_mtok: int
    price_out_per_mtok: int
    markup_pct: int = 30
    enabled: bool = True


@app.post("/api/admin/llm-providers")
def admin_provider_create(body: ProviderBody, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        p = _aboyeur_providers.create_provider(
            conn, name=body.name, base_url=body.base_url, model=body.model,
            api_key=body.api_key or "", price_in_per_mtok=body.price_in_per_mtok,
            price_out_per_mtok=body.price_out_per_mtok, markup_pct=body.markup_pct,
            enabled=body.enabled,
        )
        _aboyeur_billing.audit(conn, admin_id, "create_provider", f"provider:{p['id']}",
                               {"name": p["name"], "model": p["model"]})
        return p
    except _aboyeur_providers.ProviderError as e:
        raise _provider_http(e)
    finally:
        conn.close()


class ProviderPatch(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    price_in_per_mtok: Optional[int] = None
    price_out_per_mtok: Optional[int] = None
    markup_pct: Optional[int] = None
    enabled: Optional[bool] = None


@app.patch("/api/admin/llm-providers/{provider_id}")
def admin_provider_update(provider_id: int, body: ProviderPatch,
                          admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        p = _aboyeur_providers.update_provider(conn, provider_id, body.model_dump(exclude_unset=True))
        _aboyeur_billing.audit(conn, admin_id, "edit_provider", f"provider:{provider_id}",
                               {k: v for k, v in body.model_dump(exclude_unset=True).items()
                                if k != "api_key"})
    except _aboyeur_providers.ProviderError as e:
        raise _provider_http(e)
    finally:
        conn.close()
    _drop_all_hosted_agents()
    return p


@app.post("/api/admin/llm-providers/{provider_id}/default")
def admin_provider_default(provider_id: int, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        p = _aboyeur_providers.set_default(conn, provider_id)
        _aboyeur_billing.audit(conn, admin_id, "set_default_provider", f"provider:{provider_id}")
    except _aboyeur_providers.ProviderError as e:
        raise _provider_http(e)
    finally:
        conn.close()
    _drop_all_hosted_agents()
    return p


@app.post("/api/admin/llm-providers/{provider_id}/test")
def admin_provider_test(provider_id: int, _: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return _aboyeur_providers.test_provider(conn, provider_id)
    except _aboyeur_providers.ProviderError as e:
        raise _provider_http(e)
    finally:
        conn.close()


def _drop_all_hosted_agents() -> None:
    """A provider edit changes what hosted users should be running on; drop
    their cached agents so the next turn re-resolves. BYOK agents stay."""
    with _aboyeur_sessions_lock:
        for key in [k for k, a in _aboyeur_session_agents.items()
                    if getattr(getattr(a, "resolved", None), "billable", False)]:
            _aboyeur_session_agents.pop(key, None)


# -- invites -----------------------------------------------------------------

class InviteBody(BaseModel):
    max_uses: Optional[int] = 1
    expires_days: Optional[int] = 30
    note: Optional[str] = None


@app.get("/api/admin/invites")
def admin_invites(_: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        return {"invites": _aboyeur_account.list_invites(conn),
                "signup_mode": _aboyeur_config.signup_mode}
    finally:
        conn.close()


@app.post("/api/admin/invites")
def admin_invite_create(body: InviteBody, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        invite = _aboyeur_account.create_invite(
            conn, admin_id, max_uses=body.max_uses, expires_days=body.expires_days, note=body.note,
        )
        _aboyeur_billing.audit(conn, admin_id, "create_invite", f"invite:{invite['code']}")
        return invite
    finally:
        conn.close()


@app.delete("/api/admin/invites/{code}")
def admin_invite_revoke(code: str, admin_id: int = Depends(require_admin)):
    conn = _profile_conn()
    try:
        _aboyeur_account.revoke_invite(conn, code)
        _aboyeur_billing.audit(conn, admin_id, "revoke_invite", f"invite:{code}")
    finally:
        conn.close()
    return {"revoked": code}


# ===========================================================================
# MCP mount (Phase 6) — best-effort so the app still boots if fastmcp differs.
# ===========================================================================
try:
    from backend.cookbook_mcp import get_asgi_app
    app.mount("/mcp", get_asgi_app())
    logger.info("MCP mounted at /mcp (SSE: /mcp/sse)")
except Exception as e:  # pragma: no cover
    logger.warning("MCP mount skipped: %s", e)
