"""Shared fixtures for tool tests: a real SQLite DB built from tests/fixtures."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from aboyeur.config import Config
from aboyeur.db import connect
from aboyeur.indexer import index_corpus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def _fixtures_local(tmp_path_factory) -> Path:
    """One NAS read per session — the repo lives on SMB, where per-file
    round-trips dominate; per-test copies then go local-to-local."""
    src = tmp_path_factory.mktemp("fixtures") / "recipes"
    shutil.copytree(FIXTURES, src)
    return src


@pytest.fixture()
def config(tmp_path: Path, _fixtures_local: Path) -> Config:
    # Copy fixtures per-test: estimate_nutrition now writes nutrition blocks
    # back into recipe .md frontmatter, and tests must never dirty the repo's
    # fixture files (a committed block would flip later cache-behavior tests).
    recipes_dir = tmp_path / "recipes"
    shutil.copytree(_fixtures_local, recipes_dir)
    return Config(
        recipes_dir=recipes_dir,
        reference_dir=tmp_path / "reference",  # unused by these tests, no dir needed
        db_path=tmp_path / "test.db",
        llm_base_url="http://unused",
        llm_model="unused",
        usda_api_key=None,  # no real network calls in tests
        embedding_model="unused",
    )


@pytest.fixture(autouse=True)
def _no_llm_ingredient_parse(monkeypatch):
    """No test should hit a real LLM for ingredient parsing — default to the
    heuristic fallback path. Tests exercising the LLM path re-patch this."""
    from aboyeur.engines import nutrition as nutrition_engine
    monkeypatch.setattr(nutrition_engine, "_llm_parse_lines", lambda cfg, lines: None)


@pytest.fixture()
def db_conn(config: Config) -> sqlite3.Connection:
    index_corpus(config, rebuild=True)
    conn = connect(config.db_path)
    yield conn
    conn.close()


@pytest.fixture()
def user_id(db_conn: sqlite3.Connection) -> int:
    """A real users.id row — profile/pantry/menus all FK to this, and
    connect() turns PRAGMA foreign_keys=ON, so tool calls need a real owner."""
    from aboyeur import auth
    return auth.get_or_create_user(db_conn, "tester@example.com")


# ---------------------------------------------------------------------------
# Macro Kit: stand-in USDA answers for the component library
# ---------------------------------------------------------------------------

# Real per-100 g raw values for the whole foods the kit engine looks up, so
# kit tests exercise the actual portion math instead of solving against
# zeros. Keyed by the component's usda_query. Components carrying a
# macros_override (leafy greens, sauces) never reach a lookup at all.
COMPONENT_MACROS: dict[str, tuple[float, float, float, float]] = {
    # query: (kcal, protein_g, carb_g, fat_g)
    "chicken breast raw": (120, 22.5, 0, 2.6),
    "ground chicken breast raw": (143, 23.0, 0, 5.0),
    "turkey breast raw": (111, 23.0, 0, 1.7),
    "ground beef 93% lean raw": (152, 21.0, 0, 7.0),
    "pork tenderloin raw": (120, 21.0, 0, 3.5),
    "shrimp raw": (85, 20.0, 0.9, 0.5),
    "tuna light canned in water drained": (116, 26.0, 0, 0.8),
    "tilapia raw": (96, 20.0, 0, 1.7),
    "cod atlantic raw": (82, 18.0, 0, 0.7),
    "salmon atlantic raw": (208, 20.0, 0, 13.0),
    "egg white raw": (52, 11.0, 0.7, 0.2),
    "egg whole raw": (143, 12.6, 0.7, 9.5),
    "tofu firm raw": (144, 17.0, 3.0, 9.0),
    "tempeh": (192, 20.0, 8.0, 11.0),
    "edamame frozen unprepared": (121, 12.0, 9.0, 5.0),
    "lentils raw": (352, 25.0, 63.0, 1.1),
    "yogurt greek plain nonfat": (59, 10.0, 3.6, 0.4),
    "cottage cheese lowfat 1% milkfat": (72, 12.0, 2.7, 1.0),
    "milk nonfat fluid": (34, 3.4, 5.0, 0.1),
    "mozzarella cheese part skim": (254, 24.0, 2.8, 16.0),
    "soymilk unsweetened": (33, 2.8, 1.8, 1.6),
    "rice white long grain raw": (365, 7.1, 80.0, 0.7),
    "rice brown long grain raw": (370, 7.9, 77.0, 2.9),
    "oats rolled dry": (379, 13.0, 67.0, 6.5),
    "potatoes raw flesh and skin": (77, 2.0, 17.0, 0.1),
    "sweet potato raw": (86, 1.6, 20.0, 0.1),
    "spaghetti dry enriched": (371, 13.0, 75.0, 1.5),
    "spaghetti whole wheat dry": (348, 15.0, 71.0, 2.5),
    "quinoa uncooked": (368, 14.0, 64.0, 6.0),
    "couscous dry": (376, 13.0, 77.0, 0.6),
    "bulgur dry": (342, 12.0, 76.0, 1.3),
    "rice noodles dry": (364, 6.0, 82.0, 0.6),
    "cornmeal degermed enriched yellow": (370, 7.0, 79.0, 1.7),
    "tortillas ready to bake flour": (306, 8.0, 51.0, 7.5),
    "tortillas ready to bake corn": (218, 5.7, 45.0, 2.9),
    "bread whole wheat": (254, 13.0, 43.0, 3.5),
    "bread french or vienna": (274, 9.0, 52.0, 3.0),
    "bagels plain enriched": (257, 10.0, 50.0, 1.7),
    "chickpeas garbanzo beans dry raw": (378, 20.0, 63.0, 6.0),
    "black beans mature seeds raw": (341, 21.0, 62.0, 1.4),
    "honey": (304, 0.3, 82.0, 0.0),
    "broccoli raw": (34, 2.8, 7.0, 0.4),
    "cauliflower raw": (25, 1.9, 5.0, 0.3),
    "cabbage raw": (25, 1.3, 6.0, 0.1),
    "squash summer zucchini raw": (17, 1.2, 3.1, 0.3),
    "mushrooms white raw": (22, 3.1, 3.3, 0.3),
    "peppers sweet red raw": (31, 1.0, 6.0, 0.3),
    "tomatoes red ripe raw": (18, 0.9, 3.9, 0.2),
    "green snap beans raw": (31, 1.8, 7.0, 0.2),
    "asparagus raw": (20, 2.2, 3.9, 0.1),
    "brussels sprouts raw": (43, 3.4, 9.0, 0.3),
    "carrots raw": (41, 0.9, 10.0, 0.2),
    "pumpkin raw": (26, 1.0, 6.5, 0.1),
    "squash winter butternut raw": (45, 1.0, 12.0, 0.1),
    "strawberries raw": (32, 0.7, 7.7, 0.3),
    "bananas raw": (89, 1.1, 23.0, 0.3),
    "watermelon raw": (30, 0.6, 7.6, 0.2),
    "melons cantaloupe raw": (34, 0.8, 8.2, 0.2),
    "blueberries raw": (57, 0.7, 14.0, 0.3),
    "apples raw with skin": (52, 0.3, 14.0, 0.2),
    "oranges raw": (47, 0.9, 12.0, 0.1),
    "dates medjool": (277, 1.8, 75.0, 0.2),
    "olive oil": (884, 0.0, 0.0, 100.0),
    "avocados raw": (160, 2.0, 9.0, 15.0),
    "almonds raw": (579, 21.0, 22.0, 50.0),
    "walnuts english raw": (654, 15.0, 14.0, 65.0),
    "peanut butter smooth": (588, 25.0, 20.0, 50.0),
    "sesame butter tahini": (595, 17.0, 21.0, 54.0),
    "chia seeds dried": (486, 17.0, 42.0, 31.0),
    "olives ripe canned": (115, 0.8, 6.0, 11.0),
}


@pytest.fixture()
def component_macros(monkeypatch):
    """Answer the kit engine's USDA lookups from COMPONENT_MACROS.

    Nothing in the test suite may hit USDA (config.usda_api_key is None, so
    every real lookup would come back status="skipped" with zero macros, and
    a portion solve against zeros proves nothing). Anything not in the table
    resolves as a genuine miss, which is also worth exercising.
    """
    from aboyeur.engines import kit as kit_engine

    def fake(conn, cfg, query):
        hit = COMPONENT_MACROS.get(query)
        if hit is None:
            return {"kcal": 0, "protein_g": 0, "carb_g": 0, "fat_g": 0, "status": "failed"}
        return {"kcal": hit[0], "protein_g": hit[1], "carb_g": hit[2], "fat_g": hit[3],
                "status": "success"}

    monkeypatch.setattr(kit_engine, "lookup_ingredient_macros", fake)
    return COMPONENT_MACROS
