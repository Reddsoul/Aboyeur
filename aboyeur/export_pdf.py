"""Menu → PDF: the plan a practitioner hands a client, with every recipe.

Layout, in order: header · shopping list · plan table (every meal with its
macros, daily totals vs target) · recipes by day, one per page.

Two halves on purpose. `load_menu_document` talks to the DB and the recipe
corpus and produces a plain dict; `render_menu_pdf` turns that dict into
bytes and touches nothing but fpdf2. Tests render synthetic documents
without a corpus mount, and a missing recipe file degrades to a note on that
recipe's page rather than failing the whole export (same contract as
tools/menu.get_menu_recipes).

fpdf2 rather than WeasyPrint: pure Python, so nothing to brew/apt on the Mac
or in the image, and the recipe markdown never needs more than headings,
lists and paragraphs. Fonts are bundled (aboyeur/data/fonts, DejaVu) because
the core PDF fonts are Latin-1 only and cookbook titles are not.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
from pathlib import Path

from fpdf import FPDF
from fpdf.enums import TableBordersLayout, XPos, YPos
from fpdf.fonts import FontFace, TextStyle
from fpdf.html import HTML2FPDF
from markdown_it import MarkdownIt

from aboyeur.config import Config
from aboyeur.crypto import decrypt_field
from aboyeur.engines.nutrition import fold_plural
from aboyeur.tools.recipe import get_recipe
from aboyeur.tools.shopping import get_shopping_list

FONT_DIR = Path(__file__).parent / "data" / "fonts"
FONT_FAMILY = "DejaVu"

# Condensed from legal/disclaimer.md — the three points that matter on a
# printed plan. The full text stays on /legal/disclaimer; this is not a
# substitute for it, just the part a client should see on the page.
DISCLAIMER = (
    "Informational only. This plan is general information for meal planning. "
    "It is not medical advice, medical nutrition therapy, or a substitute for the "
    "judgement of a licensed physician or registered dietitian who knows your situation. "
    "Consult a professional before making significant dietary changes.",
    "Numbers are estimates. Calorie and macronutrient figures are calculated from "
    "public datasets (USDA FoodData Central) combined with automated interpretation "
    "of recipe text. Portion sizes, cooking losses and substitutions all change the "
    "real values. Treat every number as approximate.",
    "Allergen information may be incomplete. Recipes were filtered using the "
    "allergies on file and the ingredient lists available, but older cookbooks often "
    "list ingredients incompletely and cross-contamination is never modelled. If an "
    "allergy is life-threatening, verify every ingredient yourself.",
)

_INK = (28, 28, 30)
_MUTED = (110, 110, 115)
_RULE = (210, 210, 214)
_HEAD_FILL = (240, 240, 243)
_ZEBRA = (248, 248, 250)


class ExportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Markdown clean-up
# ---------------------------------------------------------------------------

_FRONTMATTER = re.compile(r"\A\s*---\r?\n.*?\r?\n---[ \t]*\r?\n", re.DOTALL)
_WIKILINK = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?(?!/?(?:b|i|em|strong|u|br|p|ul|ol|li|h[1-6]|hr|a|table|thead|tbody|tr|th|td)\b)[a-zA-Z][^>]*>")


def strip_frontmatter(md: str) -> str:
    """Drop the YAML block Obsidian-style recipe files open with."""
    return _FRONTMATTER.sub("", md, count=1)


def unwrap_wikilinks(md: str) -> str:
    """[[_Ingredients/onions|onion]] -> onion; [[Some/Page]] -> Page."""
    def repl(m: re.Match) -> str:
        target, label = m.group(1), m.group(2)
        if label:
            return label
        return target.rsplit("/", 1)[-1]
    return _WIKILINK.sub(repl, md)


def clean_recipe_markdown(md: str) -> str:
    md = strip_frontmatter(md)
    md = unwrap_wikilinks(md)
    md = _IMAGE.sub("", md)  # write_html would try to open the file
    return md.strip()


# ---------------------------------------------------------------------------
# Ingredient quantity scaling (feature-spec-2026-09-26.md #4)
#
# recipes.ingredients_json is NOT quantity-bearing — it's the frontmatter's
# bare food-noun list ("mustard oil", "beef"), used for search/overlap/USDA
# lookup, not the "2 tablespoons mustard oil" line a cook actually reads.
# The real per-recipe quantities only exist in the recipe's own markdown,
# under its "## Ingredients" heading — so scaling has to operate on THAT
# text, not on ingredients_json as originally suggested.
#
# Only lines with a digit-led leading quantity ("2 tablespoons", "1/2 cup",
# "6 eggs") are scaled; older-book lines that spell the quantity as a word
# ("six of almonds", "the yolks of four eggs") are left exactly as written
# rather than risk mis-scaling prose — a conservative, honest limitation of
# this pass, not a silent wrong answer.
# ---------------------------------------------------------------------------

_ING_HEADING_RE = re.compile(r"(?im)^#{2,3}[ \t]*ingredients[ \t]*$\n")
_NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]")
_BULLET_RE = re.compile(r"(?m)^([-*][ \t]+|\d+[.)][ \t]+)")

_UNICODE_FRACTIONS = {
    "⅛": 0.125, "¼": 0.25, "⅓": 1 / 3, "⅜": 0.375, "½": 0.5,
    "⅝": 0.625, "⅔": 2 / 3, "¾": 0.75, "⅞": 0.875,
}
_DISPLAY_FRACTIONS = sorted(_UNICODE_FRACTIONS.items(), key=lambda kv: kv[1])

_LEADING_QTY_RE = re.compile(
    r"^(?:"
    r"(?P<num>\d+(?:\.\d+)?)(?!/)(?:[ \t]+(?P<mfrac>\d+/\d+))?"  # "2", "1.5", "1 1/2"
    r"|(?P<frac>\d+/\d+)"                                        # "1/2" with no leading whole
    r")?"
    r"(?P<ufrac>[¼½¾⅐-⅞])?"
    r"(?P<rest>.*)$",
    re.DOTALL,
)

# Measure words: a fraction of one of these ("1/2 cup") is fine as printed.
# Anything else right after the number is a discrete count of the food or
# its container itself ("6 eggs", "2 cans tomatoes", "2 red onions") — those
# round UP instead, since 2.3 eggs or 1.4 cans isn't cookable.
_CONTINUOUS_UNITS = {
    "cup", "tablespoon", "tbsp", "teaspoon", "tsp", "ounce", "oz", "pound",
    "lb", "gram", "g", "kg", "ml", "milliliter", "millilitre", "liter",
    "litre", "pint", "quart", "gallon", "pinch", "dash", "handful", "stick",
    "bunch",
}


def _leading_qty_value(
    num: str | None, mfrac: str | None, frac: str | None, ufrac: str | None
) -> float | None:
    if num is None and frac is None and ufrac is None:
        return None
    value = float(num) if num else 0.0
    for f in (mfrac, frac):
        if f:
            n, d = f.split("/")
            value += float(n) / float(d) if float(d) else 0.0
    if ufrac:
        value += _UNICODE_FRACTIONS.get(ufrac, 0.0)
    return value


def _format_qty(value: float) -> str:
    """A scaled continuous quantity as a kitchen-sane string: a clean
    unicode fraction when it's close to one, else a short decimal."""
    whole = int(value)
    frac = value - whole
    if frac < 0.04:
        return str(whole)
    for glyph, gval in _DISPLAY_FRACTIONS:
        if abs(frac - gval) < 0.04:
            return f"{whole} {glyph}" if whole else glyph
    return f"{value:.2g}".rstrip("0").rstrip(".") or "0"


def _round_continuous(value: float) -> float:
    """Snap to a measurable kitchen fraction — eighths below 8 (cups/tbsp/
    tsp resolution), halves above it (nobody measures 12⅛ cups)."""
    step = 8.0 if value <= 8 else 2.0
    return round(value * step) / step


def scale_ingredient_line(line: str, multiplier: float) -> str:
    """Scale one "## Ingredients" bullet's leading quantity by `multiplier`,
    leaving the unit and food name untouched. A continuous measure (cup,
    tablespoon, gram, ...) rounds to a clean kitchen fraction; a bare count
    of the food itself (eggs, cloves, cans — no measure word between the
    number and the noun) rounds UP with a note, since "2.3 eggs" isn't
    something anyone can cook. A line with no leading number ("salt to
    taste") is returned unchanged — there's nothing to scale."""
    m = _LEADING_QTY_RE.match(line)
    if not m:
        return line
    value = _leading_qty_value(m.group("num"), m.group("mfrac"), m.group("frac"), m.group("ufrac"))
    if value is None or value <= 0:
        return line
    rest = m.group("rest")
    unit_match = re.match(r"[ \t]*([A-Za-z]+)", rest)
    unit_word = fold_plural(unit_match.group(1)) if unit_match else ""
    scaled = value * multiplier

    if unit_word in _CONTINUOUS_UNITS:
        return _format_qty(_round_continuous(scaled)) + rest

    rounded = math.ceil(scaled - 1e-9)
    note = "" if abs(rounded - scaled) < 0.05 else " (rounded up)"
    return f"{rounded}{rest}{note}"


def scale_recipe_ingredients_markdown(markdown: str, multiplier: float) -> str:
    """Rewrite the quantities in a recipe's "## Ingredients" section to
    match `multiplier`, leaving every other section (instructions, notes)
    byte-for-byte alone. A multiplier of ~1.0, or a recipe with no
    Ingredients heading to find, returns the markdown unchanged."""
    if abs(multiplier - 1.0) < 0.005:
        return markdown
    heading = _ING_HEADING_RE.search(markdown)
    if not heading:
        return markdown
    start = heading.end()
    rest = markdown[start:]
    next_heading = _NEXT_HEADING_RE.search(rest)
    end = start + (next_heading.start() if next_heading else len(rest))
    section = markdown[start:end]

    scaled_lines = []
    for line in section.split("\n"):
        bullet = _BULLET_RE.match(line)
        if not bullet:
            scaled_lines.append(line)
            continue
        scaled_lines.append(bullet.group(0) + scale_ingredient_line(line[bullet.end():], multiplier))
    return markdown[:start] + "\n".join(scaled_lines) + markdown[end:]


_md = MarkdownIt("commonmark").enable("table").enable("strikethrough")


def markdown_to_html(md: str) -> str:
    html = _md.render(md)
    # Anything fpdf2's write_html doesn't know gets logged and skipped, but
    # raw HTML islands in old recipe files (<div>, <span style=...>) are
    # noise either way — drop the tags, keep the text.
    return _HTML_TAG.sub("", html)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "client"


def _fmt_g(v) -> str:
    if v is None:
        return "–"
    try:
        return f"{float(v):.0f}"
    except (TypeError, ValueError):
        return "–"


def _slot_sort_key(d: dict):
    return (d["day"], d.get("meal", 1), d.get("role", "main") != "main")


def load_menu_document(conn: sqlite3.Connection, config: Config, client_id: int,
                       menu_id: int) -> dict:
    """Everything the renderer needs, as plain data. Raises ExportError(404)
    when the menu isn't this client's — the route maps that straight through."""
    row = conn.execute(
        "SELECT m.id, m.week_of, m.days_json, m.status, m.created_at, "
        "c.name AS client_name, c.user_id "
        "FROM menus m JOIN clients c ON c.id = m.client_id "
        "WHERE m.id = ? AND m.client_id = ?",
        (menu_id, client_id),
    ).fetchone()
    if row is None:
        raise ExportError(f"no menu with id {menu_id}", 404)
    plan = json.loads(row["days_json"])
    user = conn.execute(
        "SELECT name, practice FROM users WHERE id = ?", (row["user_id"],)
    ).fetchone()

    # Cache-only: an export is one HTTP request, and a cold ingredient line
    # would otherwise wait on the LLM (or its 60 s timeout). Misses fall back
    # to the raw name with default grams — good enough for a grocery list.
    shopping = get_shopping_list(conn, config, client_id, menu_id=menu_id, allow_llm=False)
    if "error" in shopping:
        shopping = {"sections": {}, "total_items": 0, "to_buy": 0}

    recipes: dict[int, dict] = {}
    for slot in plan.get("days", []):
        rid = slot["recipe_id"]
        if rid in recipes:
            continue
        r = get_recipe(conn, rid)
        if "error" in r:
            recipes[rid] = {"error": r["error"]}
        else:
            recipes[rid] = {"markdown": clean_recipe_markdown(r["markdown"]),
                            "servings": r.get("servings")}

    client_name = decrypt_field(row["client_name"]) or "Client"
    return {
        "menu_id": row["id"],
        "week_of": row["week_of"],
        "status": row["status"],
        "client_name": client_name,
        "practitioner": (user["name"] if user else None) or "",
        "practice": (user["practice"] if user else None) or "",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "plan": plan,
        "shopping": shopping,
        "recipes": recipes,
        "filename": f"meal-plan-{_slug(client_name)}-{row['week_of']}.pdf",
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class _RecipeHTML(HTML2FPDF):
    """write_html builds a list item's bullet/number from the PDF's *current*
    font, which after a heading is still the heading's (fpdf2 2.8.8 applies
    its own pending font lazily, only when text is written). Sync the pending
    body font first so "1." isn't rendered at h2 size."""

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.pdf.set_font(self.font_family, self.font_emphasis.style, self.font_size_pt)
        super().handle_starttag(tag, attrs)


class _MenuPDF(FPDF):
    HTML2FPDF_CLASS = _RecipeHTML

    def __init__(self, footer_label: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.footer_label = footer_label
        self.add_font(FONT_FAMILY, "", FONT_DIR / "DejaVuSans.ttf")
        self.add_font(FONT_FAMILY, "B", FONT_DIR / "DejaVuSans-Bold.ttf")
        self.add_font(FONT_FAMILY, "I", FONT_DIR / "DejaVuSans-Oblique.ttf")
        # No bold-italic file; map BI onto bold so <b><i> in a recipe can't raise.
        self.add_font(FONT_FAMILY, "BI", FONT_DIR / "DejaVuSans-Bold.ttf")
        self.set_margins(18, 18, 18)
        self.set_auto_page_break(auto=True, margin=18)
        self.alias_nb_pages()

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(FONT_FAMILY, "", 8)
        self.set_text_color(*_MUTED)
        self.cell(0, 6, self.footer_label, align="L")
        self.cell(0, 6, f"{self.page_no()}/{{nb}}", align="R")
        self.set_text_color(*_INK)

    # -- small helpers --------------------------------------------------
    def h1(self, text: str) -> None:
        self.set_font(FONT_FAMILY, "B", 18)
        self.set_text_color(*_INK)
        self.multi_cell(0, 9, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(2)

    def h2(self, text: str) -> None:
        self.set_font(FONT_FAMILY, "B", 13)
        self.set_text_color(*_INK)
        self.multi_cell(0, 7, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def h3(self, text: str) -> None:
        self.set_font(FONT_FAMILY, "B", 10.5)
        self.set_text_color(*_INK)
        self.multi_cell(0, 6, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def muted(self, text: str, size: float = 9) -> None:
        self.set_font(FONT_FAMILY, "", size)
        self.set_text_color(*_MUTED)
        self.multi_cell(0, 5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(*_INK)

    def body(self, text: str, size: float = 10) -> None:
        self.set_font(FONT_FAMILY, "", size)
        self.set_text_color(*_INK)
        self.multi_cell(0, 5.5, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def rule(self) -> None:
        self.set_draw_color(*_RULE)
        y = self.get_y() + 1
        self.line(self.l_margin, y, self.w - self.r_margin, y)
        self.ln(3)


def _slot_heading(slot: dict, multi_meal: bool) -> str:
    head = f"Day {slot['day']}"
    if multi_meal:
        head += f" · Meal {slot.get('meal', 1)}"
    if slot.get("role") == "top-up":
        head += " (second dish)"
    return head


def _macro_line(slot: dict) -> str:
    parts = [f"{_fmt_g(slot.get('kcal'))} kcal",
             f"P {_fmt_g(slot.get('protein_g'))} g",
             f"C {_fmt_g(slot.get('carb_g'))} g",
             f"F {_fmt_g(slot.get('fat_g'))} g"]
    if slot.get("fiber_g") is not None:
        parts.append(f"fiber {_fmt_g(slot.get('fiber_g'))} g")
    return " · ".join(parts)


def _portion_note(slot: dict) -> str | None:
    """Ingredients below are already re-scaled by scale_recipe_ingredients_
    markdown — this states that plainly instead of quoting a bare
    multiplier the reader would otherwise have to apply themselves
    (feature-spec-2026-09-26.md #4)."""
    pm = slot.get("portion_multiplier") or 1.0
    if abs(pm - 1.0) < 0.005:
        return None
    note = f"Ingredients below are scaled to {pm:g}x — eat {pm:g} servings of this dish."
    if slot.get("recipe_servings"):
        note += f" Recipe as written makes {slot['recipe_servings']} servings."
    return note


def _render_header(pdf: _MenuPDF, doc: dict) -> None:
    plan = doc["plan"]
    days = plan.get("days", [])
    day_count = len({d["day"] for d in days})
    meals_per_day = max(1, int(plan.get("meals_per_day") or 1))
    target = plan.get("daily_target_kcal")

    pdf.set_font(FONT_FAMILY, "", 9)
    pdf.set_text_color(*_MUTED)
    who = doc["practitioner"] or ""
    if doc["practice"]:
        who = f"{who} · {doc['practice']}" if who else doc["practice"]
    pdf.cell(0, 5, who or "Aboyeur", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    pdf.h1(f"Meal plan for {doc['client_name']}")
    summary = f"Week of {doc['week_of']} · {day_count} day{'s' if day_count != 1 else ''}"
    summary += f" · {meals_per_day} meal{'s' if meals_per_day != 1 else ''}/day"
    if target:
        summary += f" · target {_fmt_g(target)} kcal/day"
    pdf.body(summary, 10.5)
    pdf.muted(f"Status: {doc['status']} · generated {doc['generated_at']} · menu #{doc['menu_id']}")
    pdf.rule()


def _render_shopping(pdf: _MenuPDF, doc: dict) -> None:
    shopping = doc["shopping"]
    sections: dict[str, list] = shopping.get("sections") or {}
    pdf.h2("Shopping list")
    if not sections:
        pdf.muted("No ingredients could be resolved for this menu.")
        pdf.ln(2)
        return
    pdf.muted(f"{shopping.get('to_buy', 0)} to buy · {shopping.get('total_items', 0)} ingredients "
              "in total. Items marked ✓ are already in the pantry; ~g is the approximate "
              "total across the week.")
    pdf.ln(1)
    for section, items in sections.items():
        pdf.h3(section.replace("_", " ").title())
        pdf.set_font(FONT_FAMILY, "", 9.5)
        for it in items:
            mark = "✓ " if it.get("have") else "☐ "
            line = f"{mark}{it['name']}"
            if it.get("approx_grams"):
                line += f" — ~{it['approx_grams']} g"
            if it.get("used_in_recipes", 0) > 1:
                line += f" ({it['used_in_recipes']} recipes)"
            if it.get("flag"):
                line += f" — {it['flag']}"
            pdf.set_text_color(*(_MUTED if it.get("have") else _INK))
            pdf.multi_cell(0, 5, line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_text_color(*_INK)
        pdf.ln(1.5)
    pdf.ln(1)


def _render_plan(pdf: _MenuPDF, doc: dict) -> None:
    plan = doc["plan"]
    slots = sorted(plan.get("days", []), key=_slot_sort_key)
    meals_per_day = max(1, int(plan.get("meals_per_day") or 1))
    multi_meal = meals_per_day > 1
    totals = {t["day"]: t for t in plan.get("daily_totals", [])}
    pdf.add_page()
    pdf.h2("Plan")
    pdf.muted("Macros are per meal, for the portion planned. Daily rows compare the day "
              "against the target.")
    pdf.ln(1)

    headings = FontFace(family=FONT_FAMILY, emphasis="B", size_pt=8.5, fill_color=_HEAD_FILL)
    pdf.set_font(FONT_FAMILY, "", 8.5)
    with pdf.table(
        col_widths=(11, 11, 62, 15, 14, 14, 14, 14, 15),
        text_align=("C", "C", "L", "C", "R", "R", "R", "R", "R"),
        headings_style=headings,
        borders_layout=TableBordersLayout.HORIZONTAL_LINES,
        line_height=5,
        padding=1.2,
        cell_fill_color=_ZEBRA,
        cell_fill_mode="ROWS",
    ) as table:
        head = table.row()
        for h in ("Day", "Meal", "Dish", "Portion", "kcal", "P g", "C g", "F g", "Fiber g"):
            head.cell(h)
        current_day = None
        for i, s in enumerate(slots):
            # Daily total row goes after the last slot of the day.
            if current_day is not None and s["day"] != current_day:
                _total_row(table, totals.get(current_day), current_day)
            current_day = s["day"]
            r = table.row()
            r.cell(str(s["day"]))
            r.cell(str(s.get("meal", 1)) if multi_meal else "–")
            dish = s.get("title", "")
            if s.get("role") == "top-up":
                dish += " (second dish)"
            if s.get("book"):
                dish += f"\n{s['book'].replace('_', ' ')}"
            r.cell(dish)
            pm = s.get("portion_multiplier") or 1.0
            r.cell(f"×{pm:g}")
            r.cell(_fmt_g(s.get("kcal")))
            r.cell(_fmt_g(s.get("protein_g")))
            r.cell(_fmt_g(s.get("carb_g")))
            r.cell(_fmt_g(s.get("fat_g")))
            r.cell(_fmt_g(s.get("fiber_g")))
        if current_day is not None:
            _total_row(table, totals.get(current_day), current_day)
    pdf.ln(2)

    # Targets, once, under the table.
    tm = plan.get("daily_target_macros") or {}
    if plan.get("daily_target_kcal") or tm:
        line = f"Daily target: {_fmt_g(plan.get('daily_target_kcal'))} kcal"
        if tm:
            line += (f" · P {_fmt_g(tm.get('protein_g'))} g · C {_fmt_g(tm.get('carb_g'))} g"
                     f" · F {_fmt_g(tm.get('fat_g'))} g")
            if tm.get("fiber_g") is not None:
                line += f" · fiber {_fmt_g(tm.get('fiber_g'))} g"
        pdf.muted(line)


def _total_row(table, total: dict | None, day: int) -> None:
    bold = FontFace(family=FONT_FAMILY, emphasis="B", size_pt=8.5)
    r = table.row(style=bold)
    r.cell(str(day))
    r.cell("")
    if total is None:
        r.cell("Day total")
        for _ in range(6):
            r.cell("")
        return
    label = "Day total"
    status = total.get("status")
    if status and status != "ok":
        label += f" — {status}"
    if total.get("target_kcal"):
        label += f" (target {_fmt_g(total['target_kcal'])})"
    notes = total.get("gap_notes") or []
    if notes:
        label += "\n" + "; ".join(notes)
    r.cell(label)
    r.cell("")
    r.cell(_fmt_g(total.get("kcal")))
    r.cell(_fmt_g(total.get("protein_g")))
    r.cell(_fmt_g(total.get("carb_g")))
    r.cell(_fmt_g(total.get("fat_g")))
    r.cell(_fmt_g(total.get("fiber_g")))


def _heading(size: float) -> TextStyle:
    return TextStyle(font_family=FONT_FAMILY, font_style="B", font_size_pt=size,
                     color=_INK, t_margin=3, b_margin=1)


_RECIPE_TAG_STYLES = {
    "h1": _heading(13), "h2": _heading(12), "h3": _heading(11),
    "h4": _heading(10.5), "h5": _heading(10), "h6": _heading(10),
    "a": TextStyle(font_family=FONT_FAMILY, color=_INK),
    "li": TextStyle(l_margin=5, t_margin=1.2),
}

_LEADING_H1 = re.compile(r"\A\s*#\s+[^\n]*\n")


def _drop_title_heading(md: str, title: str) -> str:
    """Recipe files open with `# Title`; the page already printed it."""
    return _LEADING_H1.sub("", md, count=1)


def _render_recipes(pdf: _MenuPDF, doc: dict) -> None:
    plan = doc["plan"]
    slots = sorted(plan.get("days", []), key=_slot_sort_key)
    multi_meal = max(1, int(plan.get("meals_per_day") or 1)) > 1
    recipes = doc["recipes"]
    seen: dict[int, str] = {}  # recipe_id -> heading of first full print

    for s in slots:
        rid = s["recipe_id"]
        heading = _slot_heading(s, multi_meal)
        pdf.add_page()
        pdf.muted(heading, 9)
        pdf.h1(s.get("title", ""))
        if s.get("book"):
            pdf.muted(f"From {s['book'].replace('_', ' ')}")
        pdf.body(_macro_line(s), 9.5)
        note = _portion_note(s)
        if note:
            pdf.muted(note)
        pdf.rule()

        if rid in seen:
            pdf.body(f"Same recipe as {seen[rid]} — see that page for the method.")
            continue
        seen[rid] = heading

        r = recipes.get(rid) or {}
        if "markdown" not in r:
            pdf.muted(f"(recipe text unavailable: {r.get('error', 'not found')})")
            continue
        pdf.set_font(FONT_FAMILY, "", 10)
        pdf.set_text_color(*_INK)
        scaled_markdown = scale_recipe_ingredients_markdown(
            r["markdown"], s.get("portion_multiplier") or 1.0
        )
        pdf.write_html(
            markdown_to_html(_drop_title_heading(scaled_markdown, s.get("title", ""))),
            font_family=FONT_FAMILY,
            tag_styles=_RECIPE_TAG_STYLES,
            li_prefix_color=_INK,
            warn_on_tags_not_matching=False,
        )


def _render_disclaimer(pdf: _MenuPDF) -> None:
    pdf.add_page()
    pdf.h2("Please read")
    for para in DISCLAIMER:
        pdf.body(para, 9.5)
        pdf.ln(1.5)


def render_menu_pdf(doc: dict) -> bytes:
    pdf = _MenuPDF(footer_label=f"{doc['client_name']} · week of {doc['week_of']} · Aboyeur")
    pdf.set_title(f"Meal plan · {doc['client_name']} · {doc['week_of']}")
    if doc.get("practitioner"):
        pdf.set_author(doc["practitioner"])
    pdf.add_page()
    _render_header(pdf, doc)
    _render_shopping(pdf, doc)
    _render_plan(pdf, doc)
    _render_recipes(pdf, doc)
    _render_disclaimer(pdf)
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Stored snapshots
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export_row(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "menu_id": r["menu_id"], "filename": r["filename"],
            "size_bytes": r["size_bytes"], "created_at": r["created_at"]}


def create_export(conn: sqlite3.Connection, config: Config, client_id: int, menu_id: int) -> dict:
    """Render and store one snapshot. Returns its metadata (no bytes)."""
    doc = load_menu_document(conn, config, client_id, menu_id)
    pdf = render_menu_pdf(doc)
    cur = conn.execute(
        "INSERT INTO menu_exports (menu_id, client_id, filename, size_bytes, pdf, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (menu_id, client_id, doc["filename"], len(pdf), pdf, _now()),
    )
    conn.commit()
    return {"id": cur.lastrowid, "menu_id": menu_id, "filename": doc["filename"],
            "size_bytes": len(pdf), "created_at": _now()}


def list_exports(conn: sqlite3.Connection, client_id: int) -> dict[int, list[dict]]:
    """Newest first, grouped by menu id — the Plans tab hangs them under each card."""
    out: dict[int, list[dict]] = {}
    for r in conn.execute(
        "SELECT id, menu_id, filename, size_bytes, created_at FROM menu_exports "
        "WHERE client_id = ? ORDER BY id DESC", (client_id,)
    ):
        out.setdefault(r["menu_id"], []).append(export_row(r))
    return out


def get_export(conn: sqlite3.Connection, client_id: int, export_id: int) -> tuple[dict, bytes]:
    r = conn.execute(
        "SELECT id, menu_id, filename, size_bytes, pdf, created_at FROM menu_exports "
        "WHERE id = ? AND client_id = ?", (export_id, client_id)
    ).fetchone()
    if r is None:
        raise ExportError(f"no export with id {export_id}", 404)
    return export_row(r), bytes(r["pdf"])


def delete_export(conn: sqlite3.Connection, client_id: int, export_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM menu_exports WHERE id = ? AND client_id = ?", (export_id, client_id)
    )
    conn.commit()
    if cur.rowcount == 0:
        raise ExportError(f"no export with id {export_id}", 404)
