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
_MEMBER_FLOAT_FIELDS = {"height_cm", "weight_kg", "daily_kcal", "goal_weight_kg"}
_MEMBER_STR_FIELDS = {"id", "label", "sex", "activity", "goal"}
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


def save_profile(conn: sqlite3.Connection, client_id: int, desired: dict) -> dict:
    """Diff the form's full desired state against the stored profile and apply
    it through update_profile's patch semantics. Returns the saved profile
    (with recomputed targets) for the form to re-render."""
    current = get_profile(conn, client_id)

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
    if desired.get("units") in ("metric", "imperial"):
        patch["set_units"] = desired["units"]
    update_profile(conn, client_id, patch)

    members = [m for m in (_clean_member(r) for r in desired.get("household") or []) if m]
    # Wholesale replace: remove everyone, re-add the form's rows. add_member's
    # merge semantics would resurrect fields the user deliberately blanked.
    for m in get_profile(conn, client_id).get("household", []):
        update_profile(conn, client_id, {"remove_member": m["id"]})
    if members:
        update_profile(conn, client_id, {"add_member": members})

    return get_profile(conn, client_id)


def _fmt_height(cm: float, imperial: bool) -> str:
    if not imperial:
        return f"{cm}cm"
    total_in = cm / 2.54
    ft = int(total_in // 12)
    return f"{ft}'{round(total_in - ft * 12)}\""


def _fmt_weight(kg: float, imperial: bool) -> str:
    return f"{round(kg / 0.45359237)}lb" if imperial else f"{kg}kg"


def profile_markdown(profile: dict, edit_url: str) -> str:
    """Chat-facing snapshot for the /profile command, in the profile's
    preferred units (storage is always metric)."""
    imperial = profile.get("units") == "imperial"
    lines = ["## Household profile", ""]
    household = profile.get("household", [])
    targets = profile.get("targets", {})
    if household:
        for m in household:
            t = targets.get(m["id"], {})
            bits = [f"**{m.get('label', m['id'])}**"]
            if m.get("sex") is not None:
                bits.append(str(m["sex"]))
            if m.get("age") is not None:
                bits.append(f"{m['age']}y")
            if m.get("height_cm") is not None:
                bits.append(_fmt_height(m["height_cm"], imperial))
            if m.get("weight_kg") is not None:
                bits.append(_fmt_weight(m["weight_kg"], imperial))
            if m.get("activity"):
                bits.append(m["activity"])
            if m.get("goal") and m["goal"] != "maintain":
                goal_bit = f"goal: {m['goal']}"
                if m.get("goal_weight_kg") is not None:
                    goal_bit += f" to {_fmt_weight(m['goal_weight_kg'], imperial)}"
                bits.append(goal_bit)
            if t:
                kcal_bit = f"→ {t.get('daily_kcal', '?')} kcal/day ({t.get('source', '?')})"
                if t.get("goal") and t["goal"] != "maintain":
                    kcal_bit += f", maintenance {t.get('maintenance_kcal', '?')}"
                bits.append(kcal_bit)
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
    lines.append(f"- **Units:** {profile.get('units', 'metric')}")
    lines.append("")
    lines.append("Hosting someone? Start a message with `/guest` plus their details "
                 "(e.g. `/guest 5ft woman, 120 lbs, allergic to shellfish`) — planning uses "
                 "their info instead of this profile and nothing they say is saved. "
                 "`/guest off` ends it.")
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
  main { max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.6rem; margin: 0 0 .5rem; }
  .topbar { display: flex; align-items: center; justify-content: space-between;
            gap: 1rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
  .units { display: flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .units button { border: 0; border-radius: 0; padding: .4rem .9rem; background: var(--card);
                  color: var(--muted); cursor: pointer; font: inherit; }
  .units button.active { background: var(--accent); color: #fff; }
  section { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
            padding: 1rem 1.25rem; margin-bottom: 1rem; }
  h2 { font-size: 1.05rem; margin: 0 0 .75rem; }
  label { display: block; font-size: .8rem; color: var(--muted); margin-bottom: .15rem; }
  input, select { width: 100%; padding: .45rem .55rem; border: 1px solid var(--line);
                  border-radius: 6px; background: var(--bg); color: var(--ink); font: inherit; }
  .member { display: grid; grid-template-columns: repeat(auto-fit, minmax(88px, 1fr));
            gap: .6rem; align-items: end; padding: .75rem 0; border-bottom: 1px dashed var(--line); }
  .member:last-of-type { border-bottom: 0; }
  .ftin { display: flex; gap: .3rem; }
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
  <div class="topbar">
    <h1>Aboyeur &mdash; Household Profile</h1>
    <div class="units" role="group" aria-label="Units">
      <button type="button" id="u-metric" onclick="setUnits('metric')">Metric</button>
      <button type="button" id="u-imperial" onclick="setUnits('imperial')">Imperial</button>
    </div>
  </div>

  <section>
    <h2>Household members</h2>
    <div id="members"></div>
    <div class="toolbar"><button type="button" onclick="addMember()">+ Add member</button></div>
    <p class="hint">Height/weight/age/activity drive BMR-based calorie and macro targets —
    calories are always computed, never entered by hand. Pick a goal (lose/gain) and a goal
    weight and the calorie deficit/surplus is worked out for you. Leave a field blank to
    unset it. Per-person meals/day overrides the household default (e.g. 1 = OMAD). Values
    are stored metric regardless of the display units.</p>
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
    <button type="button" class="primary" onclick="save(true)">Save profile</button>
    <span id="status">Changes auto-save.</span>
  </div>
</main>

<script>
const ACTIVITIES = ["", "sedentary", "light", "moderate", "active", "very_active"];
const GOALS = ["", "maintain", "lose", "gain"];
const CM_PER_IN = 2.54, KG_PER_LB = 0.45359237;
let targets = {};
let units = "metric";
let loaded = false;
let saveTimer = null;

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => k === "class" ? n.className = v : n[k] = v);
  kids.forEach(k => n.append(k));
  return n;
}

function field(labelText, input) {
  return el("div", {}, el("label", {}, labelText), input);
}

function cmToFtIn(cm) {
  if (cm === "" || cm == null) return ["", ""];
  const totalIn = Number(cm) / CM_PER_IN;
  let ft = Math.floor(totalIn / 12), inch = Math.round((totalIn - ft * 12) * 10) / 10;
  if (inch >= 12) { ft += 1; inch = 0; }
  return [ft, inch];
}
function ftInToCm(ft, inch) {
  if (ft === "" && inch === "") return "";
  const t = (Number(ft) || 0) * 12 + (Number(inch) || 0);
  return t ? Math.round(t * CM_PER_IN * 10) / 10 : "";
}
function kgToLb(kg) { return kg === "" || kg == null ? "" : Math.round(Number(kg) / KG_PER_LB * 10) / 10; }
function lbToKg(lb) { return lb === "" || lb == null ? "" : Math.round(Number(lb) * KG_PER_LB * 100) / 100; }

// memberRow renders from a METRIC member object; collectMembers converts the
// displayed values back to metric. Storage never sees imperial numbers.
function memberRow(m = {}) {
  const row = el("div", { class: "member" });
  row.dataset.id = m.id || "";
  const mk = (name, type, val, ph = "") =>
    Object.assign(el("input", { type, placeholder: ph }), { value: val ?? "" }, { name });
  const sex = el("select", { name: "sex" });
  ["", "m", "f", "u"].forEach(v => sex.append(el("option", { value: v }, v || "—")));
  sex.value = m.sex || "";
  const activity = el("select", { name: "activity" });
  ACTIVITIES.forEach(v => activity.append(el("option", { value: v }, v || "—")));
  activity.value = m.activity || "";
  const goal = el("select", { name: "goal" });
  GOALS.forEach(v => goal.append(el("option", { value: v }, v || "—")));
  goal.value = m.goal || "";
  // Goal weight appears only when losing/gaining — it drives the computed
  // calorie deficit/surplus; there is no manual calorie entry.
  const goalWtCell = units === "imperial"
    ? field("Goal wt lb", mk("goal_weight_lb", "number", kgToLb(m.goal_weight_kg ?? "")))
    : field("Goal wt kg", mk("goal_weight_kg", "number", m.goal_weight_kg));
  const syncGoalWt = () => {
    goalWtCell.style.display = (goal.value === "lose" || goal.value === "gain") ? "" : "none";
  };
  goal.addEventListener("change", syncGoalWt);
  syncGoalWt();

  let heightCell;
  if (units === "imperial") {
    const [ft, inch] = cmToFtIn(m.height_cm ?? "");
    heightCell = field("Height", el("div", { class: "ftin" },
      mk("height_ft", "number", ft, "ft"), mk("height_in", "number", inch, "in")));
  } else {
    heightCell = field("Height cm", mk("height_cm", "number", m.height_cm));
  }
  const weightCell = units === "imperial"
    ? field("Weight lb", mk("weight_lb", "number", kgToLb(m.weight_kg ?? "")))
    : field("Weight kg", mk("weight_kg", "number", m.weight_kg));

  row.append(
    field("Name", mk("label", "text", m.label)),
    field("Sex", sex),
    field("Age", mk("age", "number", m.age)),
    heightCell,
    weightCell,
    field("Activity", activity),
    field("Goal", goal),
    goalWtCell,
    field("Meals/day", mk("meals_per_day", "number", m.meals_per_day)),
    el("div", { class: "row-actions" },
       el("button", { type: "button", class: "ghost-danger",
                      onclick: () => { row.remove(); queueSave(); } }, "Remove")),
  );
  const t = targets[m.id];
  if (t) {
    const adj = t.goal_adjust_kcal || 0;
    const goalNote = t.goal && t.goal !== "maintain"
      ? ` (${t.goal}: maintenance ${t.maintenance_kcal} ${adj >= 0 ? "+" : "−"} ${Math.abs(adj)}/day)` : "";
    row.append(el("div", { class: "target" },
      `Target: ${t.daily_kcal} kcal/day (${t.source})${goalNote} — per meal ${t.per_meal.kcal} kcal, ` +
      `${t.per_meal.protein_g}g protein / ${t.per_meal.carb_g}g carb / ${t.per_meal.fat_g}g fat`));
  }
  return row;
}

function addMember(m) { document.getElementById("members").append(memberRow(m)); }

function collectMembers() {
  return [...document.querySelectorAll(".member")].map(row => {
    const v = {};
    row.querySelectorAll("input, select").forEach(i => { if (i.name) v[i.name] = i.value; });
    const out = { id: row.dataset.id, label: v.label, sex: v.sex, age: v.age,
                  activity: v.activity, goal: v.goal, meals_per_day: v.meals_per_day };
    if (units === "imperial") {
      out.height_cm = ftInToCm(v.height_ft, v.height_in);
      out.weight_kg = lbToKg(v.weight_lb);
      out.goal_weight_kg = lbToKg(v.goal_weight_lb);
    } else {
      out.height_cm = v.height_cm;
      out.weight_kg = v.weight_kg;
      out.goal_weight_kg = v.goal_weight_kg;
    }
    return out;
  });
}

function setUnits(u, skipSave) {
  if (u === units) return;
  const members = collectMembers();  // read current display in old units
  units = u;
  document.getElementById("u-metric").classList.toggle("active", u === "metric");
  document.getElementById("u-imperial").classList.toggle("active", u === "imperial");
  const box = document.getElementById("members");
  box.replaceChildren();
  members.forEach(addMember);
  if (!skipSave) queueSave();
}

function splitList(id) {
  return document.getElementById(id).value.split(",").map(s => s.trim()).filter(Boolean);
}

function render(p) {
  targets = p.targets || {};
  units = p.units === "imperial" ? "imperial" : "metric";
  document.getElementById("u-metric").classList.toggle("active", units === "metric");
  document.getElementById("u-imperial").classList.toggle("active", units === "imperial");
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
  loaded = true;
}

function queueSave() {
  if (!loaded) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => save(false), 900);
}

async function save(rerender) {
  const status = document.getElementById("status");
  const body = {
    household: collectMembers(),
    allergies: splitList("allergies"),
    dislikes: splitList("dislikes"),
    likes: splitList("likes"),
    cuisine_affinity: splitList("cuisines"),
    diet_modes: splitList("diets"),
    meals_per_day: Number(document.getElementById("mpd").value) || 3,
    units,
  };
  try {
    const r = await fetch("/profile/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    const saved = await r.json();
    // Auto-saves skip the re-render so typing focus is never stolen; the
    // explicit Save button refreshes computed targets.
    if (rerender) render(saved); else targets = saved.targets || {};
    status.textContent = "Saved " + new Date().toLocaleTimeString();
    status.className = "ok";
  } catch (e) {
    status.textContent = "Save failed: " + e.message;
    status.className = "err";
  }
}

document.querySelector("main").addEventListener("input", queueSave);
load();
</script>
</body>
</html>
"""
