"""Profile editor: /profile chat command + browser form.

The form is full-state: it loads the entire profile, the user edits it, and
save replaces what changed. Allergies still go through update_profile's
add/remove patch semantics (never a blind overwrite); household members are
replaced wholesale since a blank field in the form means "cleared".

Everything writes to the same `profile` table the agent reads — the system
prompt, search_recipes' hard allergen filter, and plan_menu's calorie/macro
targets pick the edits up on the next turn with no extra wiring.
"""

from __future__ import annotations

import re
import sqlite3

from aboyeur.tools.profile import get_profile, update_profile

_MEMBER_INT_FIELDS = {"age", "meals_per_day"}
_MEMBER_FLOAT_FIELDS = {"height_cm", "weight_kg", "daily_kcal"}
_MEMBER_STR_FIELDS = {"id", "label", "sex", "activity"}
_MEMBER_FIELDS = _MEMBER_INT_FIELDS | _MEMBER_FLOAT_FIELDS | _MEMBER_STR_FIELDS

_ACTIVITIES = ("sedentary", "light", "moderate", "active", "very_active")


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s or "member"


def _clean_member(raw: dict) -> dict | None:
    """Coerce one form member to the stored shape; drop blank fields entirely
    (absent = unset, so BMR falls back correctly). Returns None for an empty row."""
    if not isinstance(raw, dict):
        return None
    member: dict = {}
    for key in _MEMBER_FIELDS & set(raw):
        val = raw[key]
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        try:
            if key in _MEMBER_INT_FIELDS:
                member[key] = int(float(val))
            elif key in _MEMBER_FLOAT_FIELDS:
                member[key] = float(val)
            else:
                member[key] = str(val).strip()
        except (TypeError, ValueError):
            continue  # a garbled number never blocks saving the rest
    if not member.get("id"):
        if not member.get("label"):
            return None
        member["id"] = _slug(member["label"])
    return member


def _norm_list(values) -> list[str]:
    return sorted({str(v).lower().strip() for v in (values or []) if str(v).strip()})


def save_profile(conn: sqlite3.Connection, desired: dict) -> dict:
    """Diff the form's full desired state against the stored profile and apply
    it through update_profile's patch semantics. Returns the saved profile
    (with recomputed targets) for the form to re-render."""
    current = get_profile(conn)

    patch: dict = {}
    for field in ("allergies", "dislikes", "likes"):
        new = set(_norm_list(desired.get(field)))
        old = set(current.get(field, []))
        if new - old:
            patch[f"add_{field}"] = sorted(new - old)
        if old - new:
            patch[f"remove_{field}"] = sorted(old - new)
    patch["set_diet_modes"] = _norm_list(desired.get("diet_modes"))
    patch["set_cuisine_affinity"] = [
        str(c).strip() for c in (desired.get("cuisine_affinity") or []) if str(c).strip()
    ]
    patch["set_meals_per_day"] = max(1, int(desired.get("meals_per_day") or 3))
    update_profile(conn, patch)

    members = [m for m in (_clean_member(r) for r in desired.get("household") or []) if m]
    # Wholesale replace: remove everyone, re-add the form's rows. add_member's
    # merge semantics would resurrect fields the user deliberately blanked.
    for m in get_profile(conn).get("household", []):
        update_profile(conn, {"remove_member": m["id"]})
    if members:
        update_profile(conn, {"add_member": members})

    return get_profile(conn)


def profile_markdown(profile: dict, edit_url: str) -> str:
    """Chat-facing snapshot for the /profile command."""
    lines = ["## Household profile", ""]
    household = profile.get("household", [])
    targets = profile.get("targets", {})
    if household:
        for m in household:
            t = targets.get(m["id"], {})
            bits = [f"**{m.get('label', m['id'])}**"]
            for key, unit in (("sex", ""), ("age", "y"), ("height_cm", "cm"), ("weight_kg", "kg")):
                if m.get(key) is not None:
                    bits.append(f"{m[key]}{unit}")
            if m.get("activity"):
                bits.append(m["activity"])
            if t:
                bits.append(f"→ {t.get('daily_kcal', '?')} kcal/day ({t.get('source', '?')})")
            if m.get("meals_per_day"):
                bits.append(f"{m['meals_per_day']} meals/day")
            lines.append("- " + ", ".join(bits))
    else:
        lines.append("- no household members yet")
    lines.append("")
    for label, key in (
        ("Allergies", "allergies"), ("Dislikes", "dislikes"), ("Likes", "likes"),
        ("Diet modes", "diet_modes"), ("Cuisine affinity", "cuisine_affinity"),
    ):
        vals = profile.get(key) or []
        lines.append(f"- **{label}:** {', '.join(vals) if vals else '—'}")
    lines.append(f"- **Household meals/day:** {profile.get('meals_per_day', 3)}")
    lines.append("")
    lines.append(f"Edit everything here: {edit_url}")
    lines.append("Changes apply to the next message — menus, calorie targets, and the hard allergen filter all read this profile.")
    return "\n".join(lines)


PROFILE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aboyeur Profile</title>
<style>
  :root {
    --bg: #f6f4ef; --card: #ffffff; --ink: #2b2620; --muted: #7a715f;
    --line: #e2ddd2; --accent: #a4552e; --danger: #b3372e; --ok: #3d6b35;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #211e19; --card: #2b2721; --ink: #ece6da; --muted: #a89c86;
      --line: #3d382f; --accent: #d98850; --danger: #e06a5f; --ok: #8fbb7f;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 2rem 1rem 4rem; background: var(--bg); color: var(--ink);
         font: 16px/1.5 -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 1.6rem; margin: 0 0 1.25rem; }
  section { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
            padding: 1rem 1.25rem; margin-bottom: 1rem; }
  h2 { font-size: 1.05rem; margin: 0 0 .75rem; }
  label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: .15rem; }
  input, select { width: 100%; padding: .45rem .55rem; border: 1px solid var(--line);
                  border-radius: 6px; background: var(--bg); color: var(--ink); font: inherit; }
  .member { display: grid; grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
            gap: .6rem; align-items: end; padding: .75rem 0; border-bottom: 1px dashed var(--line); }
  .member:last-of-type { border-bottom: 0; }
  .hint { font-size: .8rem; color: var(--muted); margin: .35rem 0 0; }
  .allergy-note { color: var(--danger); font-size: .85rem; margin: .35rem 0 0; }
  button { padding: .5rem .9rem; border: 1px solid var(--line); border-radius: 7px;
           background: var(--card); color: var(--ink); font: inherit; cursor: pointer; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.ghost-danger { color: var(--danger); }
  .row-actions { display: flex; align-items: end; }
  .toolbar { display: flex; gap: .75rem; align-items: center; margin-top: 1rem; }
  #status { font-size: .9rem; }
  #status.ok { color: var(--ok); } #status.err { color: var(--danger); }
  .target { font-size: .8rem; color: var(--muted); grid-column: 1 / -1; }
</style>
</head>
<body>
<main>
  <h1>Aboyeur &mdash; Household Profile</h1>

  <section>
    <h2>Household members</h2>
    <div id="members"></div>
    <div class="toolbar"><button type="button" onclick="addMember()">+ Add member</button></div>
    <p class="hint">Height/weight/age/activity drive BMR-based calorie targets. Leave a
    field blank to unset it; without full measurements, daily kcal (if set) or a generic
    default is used. Per-person meals/day overrides the household default (e.g. 1 = OMAD).</p>
  </section>

  <section>
    <h2>Allergies</h2>
    <input id="allergies" placeholder="peanut, shellfish, ...">
    <p class="allergy-note">Hard-excluded from every recipe search and menu — category
    words like &ldquo;shellfish&rdquo; expand to shrimp, crab, mussels, etc.</p>
  </section>

  <section>
    <h2>Preferences</h2>
    <label for="dislikes">Dislikes (soft-avoided)</label>
    <input id="dislikes" placeholder="cilantro, okra">
    <label for="likes" style="margin-top:.6rem">Likes</label>
    <input id="likes" placeholder="chicken thighs, noodles">
    <label for="cuisines" style="margin-top:.6rem">Cuisine affinity (menus filter to these when set)</label>
    <input id="cuisines" placeholder="Indian, Mexican">
    <label for="diets" style="margin-top:.6rem">Diet modes</label>
    <input id="diets" placeholder="vegetarian, keto">
    <label for="mpd" style="margin-top:.6rem">Household default meals per day</label>
    <input id="mpd" type="number" min="1" max="6" style="max-width:8rem">
  </section>

  <div class="toolbar">
    <button type="button" class="primary" onclick="save()">Save profile</button>
    <span id="status"></span>
  </div>
</main>

<script>
const ACTIVITIES = ["", "sedentary", "light", "moderate", "active", "very_active"];
let targets = {};

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => k === "class" ? n.className = v : n[k] = v);
  kids.forEach(k => n.append(k));
  return n;
}

function field(labelText, input) {
  return el("div", {}, el("label", {}, labelText), input);
}

function memberRow(m = {}) {
  const row = el("div", { class: "member" });
  row.dataset.id = m.id || "";
  const mk = (name, type, val, extra = {}) =>
    Object.assign(el("input", { type }), { value: val ?? "" }, extra, { name });
  const label = mk("label", "text", m.label);
  const sex = el("select", { name: "sex" });
  ["", "m", "f", "u"].forEach(v => sex.append(el("option", { value: v }, v || "—")));
  sex.value = m.sex || "";
  const activity = el("select", { name: "activity" });
  ACTIVITIES.forEach(v => activity.append(el("option", { value: v }, v || "—")));
  activity.value = m.activity || "";
  row.append(
    field("Name", label),
    field("Sex", sex),
    field("Age", mk("age", "number", m.age)),
    field("Height cm", mk("height_cm", "number", m.height_cm)),
    field("Weight kg", mk("weight_kg", "number", m.weight_kg)),
    field("Activity", activity),
    field("Daily kcal", mk("daily_kcal", "number", m.daily_kcal)),
    field("Meals/day", mk("meals_per_day", "number", m.meals_per_day)),
    el("div", { class: "row-actions" },
       el("button", { type: "button", class: "ghost-danger", onclick: () => row.remove() }, "Remove")),
  );
  const t = targets[m.id];
  if (t) row.append(el("div", { class: "target" },
    `Target: ${t.daily_kcal} kcal/day (${t.source}) — per meal ${t.per_meal.kcal} kcal, ` +
    `${t.per_meal.protein_g}g protein / ${t.per_meal.carb_g}g carb / ${t.per_meal.fat_g}g fat`));
  return row;
}

function addMember(m) { document.getElementById("members").append(memberRow(m)); }

function splitList(id) {
  return document.getElementById(id).value.split(",").map(s => s.trim()).filter(Boolean);
}

function render(p) {
  targets = p.targets || {};
  const box = document.getElementById("members");
  box.replaceChildren();
  (p.household || []).forEach(addMember);
  document.getElementById("allergies").value = (p.allergies || []).join(", ");
  document.getElementById("dislikes").value = (p.dislikes || []).join(", ");
  document.getElementById("likes").value = (p.likes || []).join(", ");
  document.getElementById("cuisines").value = (p.cuisine_affinity || []).join(", ");
  document.getElementById("diets").value = (p.diet_modes || []).join(", ");
  document.getElementById("mpd").value = p.meals_per_day ?? 3;
}

async function load() {
  const r = await fetch("/profile/data");
  render(await r.json());
}

async function save() {
  const status = document.getElementById("status");
  const household = [...document.querySelectorAll(".member")].map(row => {
    const out = { id: row.dataset.id };
    row.querySelectorAll("input, select").forEach(i => { if (i.name) out[i.name] = i.value; });
    return out;
  });
  const body = {
    household,
    allergies: splitList("allergies"),
    dislikes: splitList("dislikes"),
    likes: splitList("likes"),
    cuisine_affinity: splitList("cuisines"),
    diet_modes: splitList("diets"),
    meals_per_day: Number(document.getElementById("mpd").value) || 3,
  };
  try {
    const r = await fetch("/profile/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    render(await r.json());
    status.textContent = "Saved — targets recomputed.";
    status.className = "ok";
  } catch (e) {
    status.textContent = "Save failed: " + e.message;
    status.className = "err";
  }
}

load();
</script>
</body>
</html>
"""
