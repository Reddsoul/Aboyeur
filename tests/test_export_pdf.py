"""Menu → PDF export: markdown clean-up, rendering, stored snapshots, the
routes, and the chat transcript's menu_id stamp."""

from __future__ import annotations

import json
import os
import re
import types

import pytest

from aboyeur import chat_history, clients, export_pdf


_PAGE_RE = re.compile(rb"/Type\s*/Page(?!s)")


def _page_count(pdf: bytes) -> int:
    return len(_PAGE_RE.findall(pdf))


# ---------------------------------------------------------------------------
# Markdown clean-up
# ---------------------------------------------------------------------------

RAW_RECIPE = """---
title: Baden Stewed Lentils
ingredient_links:
  - "[[_Ingredients/lentils|lentils]]"
tags:
  - recipe
---

# Baden Stewed Lentils

![photo](lentils.jpg)

## Ingredients

- 1 quart of [[_Ingredients/lentils|lentils]]
- see [[Sauces/Brown Sauce]]

## Instructions

1. Boil until tender.
"""


def test_strip_frontmatter_only_removes_the_leading_block():
    out = export_pdf.strip_frontmatter(RAW_RECIPE)
    assert out.lstrip().startswith("# Baden Stewed Lentils")
    assert "title:" not in out
    # A horizontal rule later in the body is not frontmatter.
    body = "# T\n\ntext\n\n---\n\nmore"
    assert export_pdf.strip_frontmatter(body) == body


def test_unwrap_wikilinks_prefers_label_then_last_segment():
    assert export_pdf.unwrap_wikilinks("[[_Ingredients/lentils|lentils]]") == "lentils"
    assert export_pdf.unwrap_wikilinks("see [[Sauces/Brown Sauce]]") == "see Brown Sauce"


def test_clean_recipe_markdown_drops_images_too():
    out = export_pdf.clean_recipe_markdown(RAW_RECIPE)
    assert "![photo]" not in out and "lentils.jpg" not in out
    assert "- 1 quart of lentils" in out
    assert "- see Brown Sauce" in out


def test_markdown_to_html_strips_unknown_raw_tags():
    html = export_pdf.markdown_to_html("<div class='x'>hi <b>there</b></div>\n\n## H\n\n- a\n")
    assert "<div" not in html and "<b>there</b>" in html and "<h2>H</h2>" in html


# ---------------------------------------------------------------------------
# Rendering a synthetic document
# ---------------------------------------------------------------------------

def _slot(day, meal, rid, title, role="main", **kw):
    base = {
        "day": day, "meal": meal, "role": role, "recipe_id": rid, "title": title,
        "book": "Test_Book", "kcal": 500.0, "protein_g": 30.0, "carb_g": 50.0,
        "fat_g": 15.0, "fiber_g": 4.0, "portion_multiplier": 1.0, "recipe_servings": 4,
    }
    base.update(kw)
    return base


def _doc(**overrides) -> dict:
    plan = {
        "meals_per_day": 2,
        "daily_target_kcal": 2000,
        "daily_target_macros": {"protein_g": 150, "carb_g": 200, "fat_g": 70, "fiber_g": 30},
        "days": [
            _slot(1, 1, 10, "Lentils"),
            _slot(1, 1, 11, "Rice", role="top-up", portion_multiplier=1.5),
            _slot(1, 2, 12, "Stew"),
            _slot(2, 1, 10, "Lentils"),          # repeat -> "see Day 1"
            _slot(2, 2, 13, "Ghost dish"),       # no recipe text
        ],
        "daily_totals": [
            {"day": 1, "kcal": 1900, "protein_g": 140, "carb_g": 190, "fat_g": 60,
             "fiber_g": 10, "target_kcal": 2000, "status": "ok",
             "gap_notes": ["fiber 10 g vs 30 g target (low)"]},
            {"day": 2, "kcal": 1500, "protein_g": 100, "carb_g": 150, "fat_g": 50,
             "fiber_g": 5, "target_kcal": 2000, "status": "under", "gap_notes": []},
        ],
    }
    doc = {
        "menu_id": 7, "week_of": "2026-09-21", "status": "draft",
        "client_name": "Zoë Ünal", "practitioner": "Pat", "practice": "Pat Nutrition",
        "generated_at": "2026-09-21 12:00 UTC",
        "plan": plan,
        "shopping": {
            "sections": {
                "produce": [{"name": "onion", "approx_grams": 300, "used_in_recipes": 2,
                             "have": False, "flag": None}],
                "pantry": [{"name": "lentils", "approx_grams": 500, "used_in_recipes": 1,
                            "have": True, "flag": "sub needed"}],
            },
            "total_items": 2, "to_buy": 1,
        },
        "recipes": {
            10: {"markdown": export_pdf.clean_recipe_markdown(RAW_RECIPE), "servings": 4},
            11: {"markdown": "# Rice\n\n## Ingredients\n\n- rice 200 g\n\n## Instructions\n\n1. Cook.", "servings": 2},
            12: {"markdown": "# Stew\n\nJust stew. 味噌 — non-Latin text must render.", "servings": 4},
            13: {"error": "could not read recipe file (mount issue?)"},
        },
        "filename": "meal-plan-zoe-unal-2026-09-21.pdf",
    }
    doc.update(overrides)
    return doc


def test_render_produces_a_pdf_with_one_page_per_slot_plus_front_matter():
    pdf = export_pdf.render_menu_pdf(_doc())
    assert pdf.startswith(b"%PDF")
    # header+shopping (1) + plan (1) + 5 slots + disclaimer (1)
    assert _page_count(pdf) == 8


def test_render_tolerates_missing_fields_in_old_plans():
    doc = _doc()
    for s in doc["plan"]["days"]:
        s.pop("fiber_g")
        s.pop("recipe_servings")
        s.pop("book")
    doc["plan"].pop("daily_target_macros")
    doc["plan"]["daily_totals"] = []
    pdf = export_pdf.render_menu_pdf(doc)
    assert pdf.startswith(b"%PDF") and _page_count(pdf) == 8


def test_render_empty_shopping_and_no_recipes():
    doc = _doc(shopping={"sections": {}, "total_items": 0, "to_buy": 0}, recipes={})
    pdf = export_pdf.render_menu_pdf(doc)
    assert pdf.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Stored snapshots against a real DB (indexed fixture corpus)
# ---------------------------------------------------------------------------

def _seed_menu(conn, client_id: int, recipe_ids: list[int]) -> int:
    days = [_slot(i + 1, 1, rid, f"Dish {rid}") for i, rid in enumerate(recipe_ids)]
    plan = {"meals_per_day": 1, "daily_target_kcal": 1800, "days": days, "daily_totals": []}
    cur = conn.execute(
        "INSERT INTO menus (client_id, week_of, days_json, status, created_at) "
        "VALUES (?, '2026-09-21', ?, 'draft', '2026-09-21T00:00:00Z')",
        (client_id, json.dumps(plan)),
    )
    conn.commit()
    return cur.lastrowid


def _some_recipe_ids(conn, n: int) -> list[int]:
    return [r["id"] for r in conn.execute("SELECT id FROM recipes ORDER BY id LIMIT ?", (n,))]


def test_create_list_get_delete_export(db_conn, config, client_id):
    rids = _some_recipe_ids(db_conn, 3)
    menu_id = _seed_menu(db_conn, client_id, rids)

    meta = export_pdf.create_export(db_conn, config, client_id, menu_id)
    assert meta["menu_id"] == menu_id and meta["size_bytes"] > 1000
    assert meta["filename"] == "meal-plan-test-client-2026-09-21.pdf"

    listed = export_pdf.list_exports(db_conn, client_id)
    assert [e["id"] for e in listed[menu_id]] == [meta["id"]]

    got, data = export_pdf.get_export(db_conn, client_id, meta["id"])
    assert got["id"] == meta["id"] and data.startswith(b"%PDF")
    assert len(data) == meta["size_bytes"]
    # Real recipe text made it in: one page per slot, none of them "unavailable".
    assert _page_count(data) == 2 + len(rids) + 1

    export_pdf.delete_export(db_conn, client_id, meta["id"])
    assert export_pdf.list_exports(db_conn, client_id) == {}
    with pytest.raises(export_pdf.ExportError) as e:
        export_pdf.get_export(db_conn, client_id, meta["id"])
    assert e.value.status == 404


def test_export_is_scoped_to_the_menus_client(db_conn, config, user_id, client_id):
    other = clients.create_client(db_conn, user_id, "Other")["id"]
    menu_id = _seed_menu(db_conn, client_id, _some_recipe_ids(db_conn, 1))
    with pytest.raises(export_pdf.ExportError) as e:
        export_pdf.create_export(db_conn, config, other, menu_id)
    assert e.value.status == 404
    meta = export_pdf.create_export(db_conn, config, client_id, menu_id)
    with pytest.raises(export_pdf.ExportError):
        export_pdf.get_export(db_conn, other, meta["id"])
    with pytest.raises(export_pdf.ExportError):
        export_pdf.delete_export(db_conn, other, meta["id"])


def test_missing_recipe_file_still_exports(db_conn, config, client_id):
    rid = _some_recipe_ids(db_conn, 1)[0]
    db_conn.execute("UPDATE recipes SET path = '/nonexistent/gone.md' WHERE id = ?", (rid,))
    db_conn.commit()
    menu_id = _seed_menu(db_conn, client_id, [rid])
    meta = export_pdf.create_export(db_conn, config, client_id, menu_id)
    assert meta["size_bytes"] > 0


def test_exports_cascade_with_their_client(db_conn, config, user_id, client_id):
    menu_id = _seed_menu(db_conn, client_id, _some_recipe_ids(db_conn, 1))
    export_pdf.create_export(db_conn, config, client_id, menu_id)
    assert db_conn.execute("SELECT COUNT(*) FROM menu_exports").fetchone()[0] == 1
    clients.delete_client(db_conn, user_id, client_id)
    assert db_conn.execute("SELECT COUNT(*) FROM menu_exports").fetchone()[0] == 0


def test_chat_message_menu_id_round_trips(db_conn, client_id):
    sid = chat_history.create_session(db_conn, client_id)["id"]
    chat_history.append_message(db_conn, sid, "user", "plan my week")
    chat_history.append_message(db_conn, sid, "assistant", "Here is the plan", menu_id=42)
    msgs = chat_history.list_messages(db_conn, sid)
    assert [m["menu_id"] for m in msgs] == [None, 42]


# ---------------------------------------------------------------------------
# Through the routes
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def backend_main(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("backend-export") / "aboyeur.db"
    previous = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(db_path)
    try:
        import backend.main as main
    finally:
        if previous is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = previous
    return main


@pytest.fixture()
def api(backend_main):
    from fastapi.testclient import TestClient
    return TestClient(backend_main.app)


def _as(backend_main, api, email: str) -> int:
    from aboyeur import auth
    conn = backend_main._profile_conn()
    try:
        user_id = auth.get_or_create_user(conn, email)
    finally:
        conn.close()
    api.cookies.set(backend_main._SESSION_COOKIE, auth.issue_token(user_id))
    return user_id


def _http_menu(backend_main, tmp_path, user_id: int, name: str) -> tuple[int, int]:
    """A client, a recipe row backed by a real file, and a one-slot menu."""
    recipe_file = tmp_path / f"{name}.md"
    recipe_file.write_text(RAW_RECIPE, encoding="utf-8")
    conn = backend_main._profile_conn()
    try:
        cid = clients.create_client(conn, user_id, name)["id"]
        cur = conn.execute(
            "INSERT INTO recipes (path, title, book, cuisine, servings, ingredients_json, "
            "tags_json, mtime, indexed_at) VALUES (?, 'Lentils', 'Fixtures', 'test', 4, "
            "'[\"lentils\", \"onion\"]', '[]', 0, '2026-01-01T00:00:00Z')",
            (str(recipe_file),),
        )
        rid = cur.lastrowid
        menu_id = _seed_menu(conn, cid, [rid])
    finally:
        conn.close()
    return cid, menu_id


def test_export_routes(backend_main, api, tmp_path):
    uid = _as(backend_main, api, "exporter@example.com")
    cid, menu_id = _http_menu(backend_main, tmp_path, uid, "Ada")

    res = api.post(f"/api/menu/{menu_id}/export?client_id={cid}")
    assert res.status_code == 200, res.text
    meta = res.json()
    assert meta["menu_id"] == menu_id and meta["filename"].endswith(".pdf")

    res = api.get(f"/api/exports/{meta['id']}?client_id={cid}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/pdf")
    assert meta["filename"] in res.headers["content-disposition"]
    assert res.content.startswith(b"%PDF") and len(res.content) == meta["size_bytes"]

    plans = api.get(f"/api/clients/{cid}/plans").json()
    menu = next(m for m in plans["menus"] if m["id"] == menu_id)
    assert [e["id"] for e in menu["exports"]] == [meta["id"]]

    assert api.delete(f"/api/exports/{meta['id']}?client_id={cid}").status_code == 204
    assert api.get(f"/api/exports/{meta['id']}?client_id={cid}").status_code == 404
    assert api.get(f"/api/clients/{cid}/plans").json()["menus"][0]["exports"] == []


def test_export_routes_never_cross_accounts(backend_main, api, tmp_path):
    uid = _as(backend_main, api, "owner@example.com")
    cid, menu_id = _http_menu(backend_main, tmp_path, uid, "Bo")
    export_id = api.post(f"/api/menu/{menu_id}/export?client_id={cid}").json()["id"]

    _as(backend_main, api, "intruder@example.com")
    assert api.post(f"/api/menu/{menu_id}/export?client_id={cid}").status_code == 404
    assert api.get(f"/api/exports/{export_id}?client_id={cid}").status_code == 404
    assert api.delete(f"/api/exports/{export_id}?client_id={cid}").status_code == 404
    # Same account, a different client of theirs: still not that client's menu.
    _as(backend_main, api, "owner@example.com")
    other, _ = _http_menu(backend_main, tmp_path, uid, "Cy")
    assert api.post(f"/api/menu/{menu_id}/export?client_id={other}").status_code == 404
    assert api.get(f"/api/exports/{export_id}?client_id={other}").status_code == 404


def test_account_export_zip_includes_pdfs(backend_main, api, tmp_path):
    import io
    import zipfile
    uid = _as(backend_main, api, "ziptester@example.com")
    cid, menu_id = _http_menu(backend_main, tmp_path, uid, "Dee")
    meta = api.post(f"/api/menu/{menu_id}/export?client_id={cid}").json()
    res = api.get("/api/account/export")
    assert res.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(res.content)).namelist()
    assert f"clients/{cid}/exports/{meta['id']}-{meta['filename']}" in names


class _MenuAgent:
    """One plan_menu tool round then an answer — the shape _run_agent_turn
    reads menu_id out of."""

    def __init__(self, menu_id: int | None, tool: str = "plan_menu"):
        self.menu_id = menu_id
        self.tool = tool
        self.config = types.SimpleNamespace(llm_model="fake-model")
        self.last_tool_calls = 1
        self.last_usage = (10, 20)
        self.client = None  # sensitivity classifier reads it; it fails soft

    def send(self, _message):
        yield ("tool_call", {"name": self.tool, "args": {}})
        result = {"menu_id": self.menu_id, "days": []} if self.menu_id else {"error": "no menu"}
        yield ("tool_result", {"name": self.tool, "result": result})
        yield ("text", "Here is ")
        yield ("text", "your week.")


@pytest.mark.parametrize("agent,expected", [
    (_MenuAgent(65), 65),
    (_MenuAgent(66, tool="swap_menu_item"), 66),
    (_MenuAgent(67, tool="get_menu_recipes"), 67),
    (_MenuAgent(None), None),                      # tool errored: no stamp
    (_MenuAgent(68, tool="search_recipes"), None),  # not a menu tool
])
def test_turn_stamps_menu_id_on_the_assistant_message(backend_main, api, agent, expected):
    uid = _as(backend_main, api, "stamper@example.com")
    conn = backend_main._profile_conn()
    try:
        cid = clients.create_client(conn, uid, "Stamp")["id"]
        session_id = chat_history.create_session(conn, cid)["id"]
    finally:
        conn.close()
    backend_main._record_message(session_id, "user", "plan my week")
    turn = backend_main._turns.begin(session_id)
    backend_main._start_agent_turn(turn, agent, "plan my week", record_for=session_id,
                                   user_id=uid, client_id=cid)
    assert turn.wait(timeout=10)
    assert turn.text() == "Here is your week."

    res = api.get(f"/api/clients/{cid}/messages?session_id={session_id}")
    assert res.status_code == 200, res.text
    msgs = res.json()["messages"]
    assert [(m["role"], m["menu_id"]) for m in msgs] == [("user", None), ("assistant", expected)]
