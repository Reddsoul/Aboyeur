import { useEffect, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import './Onboarding.css';
import * as api from '../api';
import { useAuth } from '../auth/AuthContext';
import { useClients } from '../clients/ClientContext';
import Logo from '../components/Logo';

const CM_PER_IN = 2.54;
const KG_PER_LB = 0.45359237;

const ICONS: Record<string, string> = {
  person: 'M20 21a8 8 0 0 0-16 0M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  scale: 'M12 3v18M4 7h16M6 7l-3 6a3 3 0 0 0 6 0l-3-6Zm12 0l-3 6a3 3 0 0 0 6 0l-3-6Z',
  target: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm0-4a6 6 0 1 0 0-12 6 6 0 0 0 0 12Zm0-4a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z',
  alert: 'M10.3 3.9 1.8 18a1 1 0 0 0 .9 1.5h18.6a1 1 0 0 0 .9-1.5L13.7 3.9a1 1 0 0 0-1.7 0ZM12 9v4m0 4h.01',
  heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8Z',
  globe: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Zm0 0c-2.5-2.5-4-6-4-10s1.5-7.5 4-10m0 20c2.5-2.5 4-6 4-10s-1.5-7.5-4-10M2.5 9h19M2.5 15h19',
  calendar: 'M8 2v4m8-4v4M3 9h18M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z',
  check: 'M9 12.5 11 15l4-6m7 3a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
};

const ACTIVITY_DEFS = [
  { key: 'sedentary', label: 'Sedentary', sub: 'Little to no exercise' },
  { key: 'light', label: 'Light', sub: 'Exercise 1–3 days a week' },
  { key: 'moderate', label: 'Moderate', sub: 'Exercise 3–5 days a week' },
  { key: 'active', label: 'Active', sub: 'Hard exercise 6–7 days a week' },
  { key: 'very_active', label: 'Very active', sub: 'Very hard exercise & physical job' },
];
const GOAL_DEFS = [
  { key: 'maintain', label: 'Maintain' },
  { key: 'lose', label: 'Lose' },
  { key: 'gain', label: 'Gain' },
];
const ALLERGEN_DEFS = ['Peanut', 'Tree nut', 'Shellfish', 'Dairy', 'Egg', 'Gluten', 'Soy', 'Fish'];
const CUISINE_DEFS = ['Italian', 'Mexican', 'Indian', 'Chinese', 'Thai', 'Japanese', 'Mediterranean', 'French', 'American', 'Korean'];
const DIET_DEFS = ['Vegetarian', 'Vegan', 'Pescatarian', 'Keto', 'Paleo', 'Gluten-free', 'Dairy-free', 'Low-carb'];
// The tour steps sit between the save and the done screen — first login walks
// straight into them, and /onboarding?tour=1 jumps to them for a replay.
const TOUR_STEPS = ['tour_menu', 'tour_kit', 'tour_prompt'];
const STEP_NAMES = ['welcome', 'household', 'about', 'goal', 'allergies', 'taste', 'cuisine', 'meals', 'review', ...TOUR_STEPS, 'done'];
const DONE_STEP = STEP_NAMES.indexOf('done');
const FIRST_TOUR_STEP = STEP_NAMES.indexOf(TOUR_STEPS[0]);

/** What each tour card teaches, with a prompt the user can actually paste. */
const TOUR_CARDS: Record<string, { icon: string; title: string; body: string; prompts: string[] }> = {
  tour_menu: {
    icon: ICONS.calendar,
    title: 'The menu builder',
    body:
      'Pick a client, ask for a plan, and Aboyeur pulls real recipes out of your own cookbook library — never invented ones — fits them to that client’s calorie and macro targets, drops anything with an allergen in it, and rolls the whole thing into one shopping list grouped by aisle. The plan is filed under that client and nobody else.',
    prompts: ['Plan 5 dinners for this week', 'Swap Thursday for something faster', 'Give me the shopping list for that menu'],
  },
  tour_kit: {
    icon: ICONS.scale,
    title: 'Mix & match kits',
    body:
      'A Macro Kit is the other half: instead of fixed recipes it gives you columns of foods — proteins, carbs, vegetables, fats — with per-meal gram amounts that hit your macros in any combination. Cook once, assemble differently every day.',
    prompts: ['Build me a macro kit for the week', 'Make the kit higher protein', 'Swap chicken out of the kit'],
  },
  tour_prompt: {
    icon: ICONS.check,
    title: 'How to ask',
    body:
      'Plain sentences work best. Say how many days and any constraint that matters — time, budget, equipment, leftovers. Whoever is selected in the header is who it plans for, and it already knows their household, allergies and targets, so you never repeat those. Push back on anything it gives you and it will redo it.',
    prompts: ['Nothing that takes over 30 minutes', 'Make it cheaper — pantry staples where you can', 'Only recipes from Ottolenghi'],
  },
};
const SUBSTEPS = ['household', 'about', 'goal', 'allergies', 'taste', 'cuisine', 'meals', 'review'];
const STEP_META: Record<string, { icon: string; title: string; subtitle: string }> = {
  household: { icon: ICONS.person, title: 'Who are we planning for?', subtitle: "Your first client. Add anyone else in their household too — you can fill in each person's details later." },
  about: { icon: ICONS.scale, title: 'Tell us about yourself', subtitle: 'Height, weight, and activity drive your calorie and macro targets, always computed, never entered by hand.' },
  goal: { icon: ICONS.target, title: "What's your goal?", subtitle: "We'll work out the daily calorie deficit or surplus for you, no manual math." },
  allergies: { icon: ICONS.alert, title: 'Any allergies?', subtitle: 'Hard-excluded from every recipe and menu. Category words like “shellfish” cover shrimp, crab, mussels, and more.' },
  taste: { icon: ICONS.heart, title: 'Likes & dislikes', subtitle: 'Dislikes are soft-avoided; likes get you more of what you already enjoy.' },
  cuisine: { icon: ICONS.globe, title: 'Cuisine & diet', subtitle: 'Set a cuisine affinity to focus every menu on what you love. Diet modes filter recipes automatically.' },
  meals: { icon: ICONS.calendar, title: 'Meals per day', subtitle: 'Your household default, override it per person later, e.g. one meal a day (OMAD).' },
  review: { icon: ICONS.check, title: 'Review your profile', subtitle: 'Save to finish, or go back to change anything.' },
};

interface State {
  step: number;
  name: string;
  memberDraft: string;
  members: string[];
  units: 'imperial' | 'metric';
  sex: string;
  age: string;
  heightFt: string;
  heightIn: string;
  heightCm: string;
  weightLb: string;
  weightKg: string;
  activity: string;
  goal: string;
  goalWeightLb: string;
  goalWeightKg: string;
  allergyDraft: string;
  allergies: string[];
  dislikeDraft: string;
  dislikes: string[];
  likeDraft: string;
  likes: string[];
  cuisines: string[];
  diets: string[];
  mealsPerDay: number;
}

const INITIAL: State = {
  step: 0,
  name: 'Me',
  memberDraft: '',
  members: [],
  units: 'imperial',
  sex: '',
  age: '',
  heightFt: '',
  heightIn: '',
  heightCm: '',
  weightLb: '',
  weightKg: '',
  activity: 'moderate',
  goal: 'maintain',
  goalWeightLb: '',
  goalWeightKg: '',
  allergyDraft: '',
  allergies: [],
  dislikeDraft: '',
  dislikes: [],
  likeDraft: '',
  likes: [],
  cuisines: [],
  diets: [],
  mealsPerDay: 3,
};

function RemovableChip({ label, tone, onRemove }: { label: string; tone: 'danger' | 'accent' | 'neutral'; onRemove: () => void }) {
  return (
    <span className={`ob-rchip ob-rchip-${tone}`}>
      {label}
      <button type="button" className="ob-rchip-x" onClick={onRemove} aria-label="Remove">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
      </button>
    </span>
  );
}

export default function Onboarding() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { llmConfigured, refreshClientStatus } = useAuth();
  const { activeClient, setActiveClient, refreshClients } = useClients();
  const tourOnly = params.get('tour') === '1';
  const [s, setS] = useState<State>(tourOnly ? { ...INITIAL, step: FIRST_TOUR_STEP } : INITIAL);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const patch = (p: Partial<State>) => setS((prev) => ({ ...prev, ...p }));

  // Prefill from the active client's profile (editing flow), then land on
  // Welcome either way — a fast re-click through for someone already set up.
  useEffect(() => {
    if (!activeClient) return; // fresh account: no client to read from yet
    api
      .getProfile(activeClient.id)
      .then((profile) => {
        const primary = profile.household[0];
        const rest = profile.household.slice(1);
        patch({
          name: primary?.label ?? 'Me',
          members: rest.map((m) => m.label),
          sex: primary?.sex ?? '',
          age: primary?.age != null ? String(primary.age) : '',
          heightCm: primary?.height_cm != null ? String(primary.height_cm) : '',
          weightKg: primary?.weight_kg != null ? String(primary.weight_kg) : '',
          activity: primary?.activity ?? 'moderate',
          goal: primary?.goal ?? 'maintain',
          goalWeightKg: primary?.goal_weight_kg != null ? String(primary.goal_weight_kg) : '',
          allergies: profile.allergies ?? [],
          dislikes: profile.dislikes ?? [],
          likes: profile.likes ?? [],
          cuisines: profile.cuisine_affinity ?? [],
          diets: profile.diet_modes ?? [],
          mealsPerDay: profile.meals_per_day ?? 3,
          units: profile.units === 'imperial' ? 'imperial' : 'metric',
        });
      })
      .catch(() => {
        /* no profile on this client yet, nothing to prefill */
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeClient?.id]);

  const stepName = STEP_NAMES[s.step] ?? 'welcome';
  const subIndex = SUBSTEPS.indexOf(stepName);
  const showChrome = subIndex >= 0;
  const progressPct = showChrome ? Math.round(((subIndex + 1) / SUBSTEPS.length) * 100) : 0;
  const meta = STEP_META[stepName];
  const unitsIsImperial = s.units === 'imperial';
  const continueDisabled = s.step === 1 && !s.name.trim();
  const goalNeedsWeight = s.goal === 'lose' || s.goal === 'gain';
  const goalWeightUnit = unitsIsImperial ? 'lb' : 'kg';
  const goalWeightValue = unitsIsImperial ? s.goalWeightLb : s.goalWeightKg;
  const mealsPerDayLabel = s.mealsPerDay === 1 ? 'One meal a day (OMAD)' : `${s.mealsPerDay} meals a day`;

  function next() {
    if (s.step === 1 && !s.name.trim()) return;
    if (stepName === 'review') {
      void finishAndSave();
      return;
    }
    patch({ step: s.step + 1 });
  }
  function back() {
    patch({ step: Math.max(0, s.step - 1) });
  }

  function addMember() {
    const v = s.memberDraft.trim();
    if (v) patch({ members: [...s.members, v], memberDraft: '' });
  }
  function removeMember(i: number) {
    patch({ members: s.members.filter((_, j) => j !== i) });
  }
  function toggleAllergen(name: string) {
    const has = s.allergies.some((a) => a.toLowerCase() === name.toLowerCase());
    patch({ allergies: has ? s.allergies.filter((a) => a.toLowerCase() !== name.toLowerCase()) : [...s.allergies, name] });
  }
  function addAllergy() {
    const v = s.allergyDraft.trim();
    if (v && !s.allergies.some((a) => a.toLowerCase() === v.toLowerCase())) {
      patch({ allergies: [...s.allergies, v], allergyDraft: '' });
    }
  }
  function addDislike() {
    const v = s.dislikeDraft.trim();
    if (v) patch({ dislikes: [...s.dislikes, v], dislikeDraft: '' });
  }
  function addLike() {
    const v = s.likeDraft.trim();
    if (v) patch({ likes: [...s.likes, v], likeDraft: '' });
  }
  function toggleCuisine(name: string) {
    patch({ cuisines: s.cuisines.includes(name) ? s.cuisines.filter((c) => c !== name) : [...s.cuisines, name] });
  }
  function toggleDiet(name: string) {
    patch({ diets: s.diets.includes(name) ? s.diets.filter((d) => d !== name) : [...s.diets, name] });
  }

  function ftInToCm(ft: string, inch: string): number | null {
    const t = (Number(ft) || 0) * 12 + (Number(inch) || 0);
    return t ? Math.round(t * CM_PER_IN * 10) / 10 : null;
  }
  function lbToKg(lb: string): number | null {
    const n = Number(lb);
    return lb && n ? Math.round(n * KG_PER_LB * 100) / 100 : null;
  }

  async function finishAndSave() {
    setSaveError(null);
    setSaving(true);
    try {
      const heightCm = unitsIsImperial ? ftInToCm(s.heightFt, s.heightIn) : s.heightCm ? Number(s.heightCm) : null;
      const weightKg = unitsIsImperial ? lbToKg(s.weightLb) : s.weightKg ? Number(s.weightKg) : null;
      const goalWeightKg = unitsIsImperial ? lbToKg(s.goalWeightLb) : s.goalWeightKg ? Number(s.goalWeightKg) : null;

      const primary: api.HouseholdMember = {
        id: 'user',
        label: s.name.trim() || 'Me',
        sex: s.sex || null,
        age: s.age ? Number(s.age) : null,
        height_cm: heightCm,
        weight_kg: weightKg,
        activity: s.activity,
        goal: s.goal,
        goal_weight_kg: goalWeightKg,
      };
      const others: api.HouseholdMember[] = s.members.map((label, i) => ({
        id: `member_${i + 1}_${label.toLowerCase().replace(/[^a-z0-9]+/g, '_')}`,
        label,
      }));

      // First run has no client yet: the wizard's subject becomes client #1,
      // named after them. Re-running it writes to whoever is active instead
      // of quietly creating a duplicate person.
      let clientId = activeClient?.id ?? null;
      if (clientId == null) {
        const created = await api.createClient({ name: s.name.trim() || 'My household' });
        clientId = created.id;
        setActiveClient(created);
      }

      await api.saveProfile(clientId, {
        household: [primary, ...others],
        allergies: s.allergies,
        dislikes: s.dislikes,
        likes: s.likes,
        cuisine_affinity: s.cuisines,
        diet_modes: s.diets,
        meals_per_day: s.mealsPerDay,
        units: s.units,
      });
      await Promise.all([refreshClientStatus(), refreshClients()]);
      patch({ step: FIRST_TOUR_STEP }); // straight into the tour
    } catch {
      setSaveError("Couldn't save this client — check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="ob-stage">
      <div className="ob-orb" style={{ top: -180, left: -120, width: 560, height: 560, background: 'var(--accent)', opacity: 0.14 }} />
      <div className="ob-orb" style={{ bottom: -220, right: -140, width: 620, height: 620, background: 'var(--success)', opacity: 0.1 }} />
      <div className="ob-orb" style={{ top: '38%', right: '8%', width: 340, height: 340, background: '#ff9500', opacity: 0.08 }} />

      <div className="ob-card">
        {showChrome && (
          <>
            <div className="ob-topbar">
              <button type="button" className="ob-back-btn" onClick={back} aria-label="Back">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M15 18l-6-6 6-6" />
                </svg>
              </button>
              <span className="ob-step-counter">
                Step {subIndex + 1} of {SUBSTEPS.length}
              </span>
            </div>
            <div className="ob-progress-track">
              <div className="ob-progress-fill" style={{ width: `${progressPct}%` }} />
            </div>
            <div className="ob-step-header">
              <div className="ob-icon-badge">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--ink)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d={meta.icon} />
                </svg>
              </div>
              <div>
                <div className="ob-step-title">{meta.title}</div>
                <div className="ob-step-sub">{meta.subtitle}</div>
              </div>
            </div>
          </>
        )}

        <div className="ob-body">
          {stepName === 'welcome' && (
            <div className="ob-hero">
              <Logo size={88} className="ob-hero-mark" />
              <div>
                <h1 className="ob-hero-title">Welcome to Aboyeur</h1>
                <p className="ob-hero-sub">
                  Set up your first client. Every client keeps their own targets, allergies and plans — and nothing you build for one ever leaks into another.
                </p>
              </div>
              <ul className="ob-hero-list">
                <li>
                  <strong>Menu builder</strong> — weekly plans per client from real recipes in your library, with the shopping list.
                </li>
                <li>
                  <strong>Mix & match kits</strong> — foods and gram amounts you combine yourself, built to hit your macros.
                </li>
                <li>
                  <strong>Just ask</strong> — plain sentences. It already knows the selected client, their allergies and their targets.
                </li>
              </ul>
              <button type="button" className="ob-btn-hero" onClick={next}>
                Get Started
              </button>
              <p className="ob-hero-fine">Takes about a minute. Everything here is editable later in your profile.</p>
            </div>
          )}

          {stepName === 'household' && (
            <div className="ob-inner">
              <div>
                <label className="ob-field-label">Your name</label>
                <input className="text-input" value={s.name} onChange={(e) => patch({ name: e.target.value })} placeholder="Me" />
              </div>
              <div>
                <label className="ob-field-label">Add other household members</label>
                <div className="ob-input-row">
                  <input
                    className="text-input"
                    value={s.memberDraft}
                    onChange={(e) => patch({ memberDraft: e.target.value })}
                    onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addMember())}
                    placeholder="e.g. Sam, or Kid 1"
                  />
                  <button type="button" className="ob-icon-btn" onClick={addMember} aria-label="Add member">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M12 5v14M5 12h14" />
                    </svg>
                  </button>
                </div>
              </div>
              <div className="ob-chip-row">
                {s.members.map((m, i) => (
                  <RemovableChip key={i} label={m} tone="neutral" onRemove={() => removeMember(i)} />
                ))}
              </div>
            </div>
          )}

          {stepName === 'about' && (
            <div className="ob-inner">
              <div className="ob-seg-wrap">
                <button type="button" className={`ob-seg-btn ${unitsIsImperial ? 'ob-seg-btn-on' : ''}`} onClick={() => patch({ units: 'imperial' })}>
                  Imperial
                </button>
                <button type="button" className={`ob-seg-btn ${!unitsIsImperial ? 'ob-seg-btn-on' : ''}`} onClick={() => patch({ units: 'metric' })}>
                  Metric
                </button>
              </div>

              <div>
                <label className="ob-field-label">Sex</label>
                <div className="ob-seg-wrap">
                  {(['f', 'm', 'u'] as const).map((v) => (
                    <button key={v} type="button" className={`ob-seg-btn ${s.sex === v ? 'ob-seg-btn-on' : ''}`} onClick={() => patch({ sex: v })}>
                      {v === 'f' ? 'Female' : v === 'm' ? 'Male' : 'Rather not say'}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <label className="ob-field-label">Age</label>
                <input className="text-input" type="number" value={s.age} onChange={(e) => patch({ age: e.target.value })} placeholder="—" style={{ maxWidth: 140 }} />
              </div>

              {unitsIsImperial ? (
                <div className="ob-grid-2">
                  <div>
                    <label className="ob-field-label">Height</label>
                    <div className="ob-input-row">
                      <input className="text-input" type="number" value={s.heightFt} onChange={(e) => patch({ heightFt: e.target.value })} placeholder="ft" />
                      <input className="text-input" type="number" value={s.heightIn} onChange={(e) => patch({ heightIn: e.target.value })} placeholder="in" />
                    </div>
                  </div>
                  <div>
                    <label className="ob-field-label">Weight (lb)</label>
                    <input className="text-input" type="number" value={s.weightLb} onChange={(e) => patch({ weightLb: e.target.value })} placeholder="—" />
                  </div>
                </div>
              ) : (
                <div className="ob-grid-2">
                  <div>
                    <label className="ob-field-label">Height (cm)</label>
                    <input className="text-input" type="number" value={s.heightCm} onChange={(e) => patch({ heightCm: e.target.value })} placeholder="—" />
                  </div>
                  <div>
                    <label className="ob-field-label">Weight (kg)</label>
                    <input className="text-input" type="number" value={s.weightKg} onChange={(e) => patch({ weightKg: e.target.value })} placeholder="—" />
                  </div>
                </div>
              )}

              <div>
                <label className="ob-field-label">Activity level</label>
                <div className="ob-inner" style={{ gap: 8 }}>
                  {ACTIVITY_DEFS.map((a) => (
                    <button
                      key={a.key}
                      type="button"
                      className={`ob-radio-card ${s.activity === a.key ? 'ob-radio-card-on' : ''}`}
                      onClick={() => patch({ activity: a.key })}
                    >
                      <span>
                        <div className="ob-radio-card-title">{a.label}</div>
                        <div className="ob-radio-card-sub">{a.sub}</div>
                      </span>
                      {s.activity === a.key && (
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M20 6 9 17l-5-5" />
                        </svg>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {stepName === 'goal' && (
            <div className="ob-inner">
              <div className="ob-grid-3">
                {GOAL_DEFS.map((g) => (
                  <button key={g.key} type="button" className={`ob-goal-card ${s.goal === g.key ? 'ob-goal-card-on' : ''}`} onClick={() => patch({ goal: g.key })}>
                    {g.label}
                  </button>
                ))}
              </div>
              {goalNeedsWeight && (
                <div>
                  <label className="ob-field-label">Goal weight ({goalWeightUnit})</label>
                  <input
                    className="text-input"
                    type="number"
                    value={goalWeightValue}
                    onChange={(e) => patch(unitsIsImperial ? { goalWeightLb: e.target.value } : { goalWeightKg: e.target.value })}
                    placeholder="—"
                    style={{ maxWidth: 220 }}
                  />
                </div>
              )}
            </div>
          )}

          {stepName === 'allergies' && (
            <div className="ob-inner">
              <div className="ob-input-row">
                <input
                  className="text-input"
                  value={s.allergyDraft}
                  onChange={(e) => patch({ allergyDraft: e.target.value })}
                  onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addAllergy())}
                  placeholder="Type an allergy"
                />
                <button type="button" className="ob-icon-btn" onClick={addAllergy} aria-label="Add allergy">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <path d="M12 5v14M5 12h14" />
                  </svg>
                </button>
              </div>
              <div>
                <div className="ob-field-label">Common allergens</div>
                <div className="ob-chip-row">
                  {ALLERGEN_DEFS.map((a) => {
                    const on = s.allergies.some((x) => x.toLowerCase() === a.toLowerCase());
                    return (
                      <button key={a} type="button" className={`ob-chip ${on ? 'ob-chip-on-danger' : ''}`} onClick={() => toggleAllergen(a)}>
                        {a}
                      </button>
                    );
                  })}
                </div>
              </div>
              {s.allergies.length > 0 && (
                <div className="ob-chip-row" style={{ paddingTop: 12, borderTop: '1px solid var(--line)' }}>
                  {s.allergies.map((a, i) => (
                    <RemovableChip key={i} label={a} tone="danger" onRemove={() => patch({ allergies: s.allergies.filter((_, j) => j !== i) })} />
                  ))}
                </div>
              )}
            </div>
          )}

          {stepName === 'taste' && (
            <div className="ob-inner" style={{ gap: 24 }}>
              <div>
                <label className="ob-field-label">Dislikes</label>
                <div className="ob-input-row" style={{ marginBottom: 10 }}>
                  <input
                    className="text-input"
                    value={s.dislikeDraft}
                    onChange={(e) => patch({ dislikeDraft: e.target.value })}
                    onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addDislike())}
                    placeholder="e.g. cilantro"
                  />
                  <button type="button" className="ob-icon-btn" onClick={addDislike} aria-label="Add dislike">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M12 5v14M5 12h14" />
                    </svg>
                  </button>
                </div>
                <div className="ob-chip-row">
                  {s.dislikes.map((d, i) => (
                    <RemovableChip key={i} label={d} tone="neutral" onRemove={() => patch({ dislikes: s.dislikes.filter((_, j) => j !== i) })} />
                  ))}
                </div>
              </div>
              <div>
                <label className="ob-field-label">Likes</label>
                <div className="ob-input-row" style={{ marginBottom: 10 }}>
                  <input
                    className="text-input"
                    value={s.likeDraft}
                    onChange={(e) => patch({ likeDraft: e.target.value })}
                    onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addLike())}
                    placeholder="e.g. chicken thighs"
                  />
                  <button type="button" className="ob-icon-btn" onClick={addLike} aria-label="Add like">
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                      <path d="M12 5v14M5 12h14" />
                    </svg>
                  </button>
                </div>
                <div className="ob-chip-row">
                  {s.likes.map((l, i) => (
                    <RemovableChip key={i} label={l} tone="accent" onRemove={() => patch({ likes: s.likes.filter((_, j) => j !== i) })} />
                  ))}
                </div>
              </div>
            </div>
          )}

          {stepName === 'cuisine' && (
            <div className="ob-inner" style={{ gap: 24 }}>
              <div>
                <label className="ob-field-label">Cuisine affinity</label>
                <div className="ob-chip-row">
                  {CUISINE_DEFS.map((c) => (
                    <button key={c} type="button" className={`ob-chip ${s.cuisines.includes(c) ? 'ob-chip-on-accent' : ''}`} onClick={() => toggleCuisine(c)}>
                      {c}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="ob-field-label">Diet modes</label>
                <div className="ob-chip-row">
                  {DIET_DEFS.map((d) => (
                    <button key={d} type="button" className={`ob-chip ${s.diets.includes(d) ? 'ob-chip-on-success' : ''}`} onClick={() => toggleDiet(d)}>
                      {d}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {stepName === 'meals' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 24, alignItems: 'center', textAlign: 'center', paddingTop: 20 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
                <button type="button" className="ob-stepper-btn" onClick={() => patch({ mealsPerDay: Math.max(1, s.mealsPerDay - 1) })}>
                  −
                </button>
                <div style={{ fontSize: 44, fontWeight: 700, minWidth: 60 }}>{s.mealsPerDay}</div>
                <button type="button" className="ob-stepper-btn" onClick={() => patch({ mealsPerDay: Math.min(6, s.mealsPerDay + 1) })}>
                  +
                </button>
              </div>
              <div style={{ fontSize: 15, color: 'var(--muted)', fontWeight: 500 }}>{mealsPerDayLabel}</div>
            </div>
          )}

          {stepName === 'review' && (
            <div className="ob-inner">
              <div className="ob-review-box">
                {[
                  { label: 'Name', value: s.name || '—' },
                  { label: 'Sex', value: s.sex === 'f' ? 'Female' : s.sex === 'm' ? 'Male' : s.sex === 'u' ? 'Rather not say' : '—' },
                  { label: 'Age', value: s.age ? `${s.age} yrs` : '—' },
                  { label: 'Height', value: unitsIsImperial ? (s.heightFt || s.heightIn ? `${s.heightFt || 0}'${s.heightIn || 0}"` : '—') : s.heightCm ? `${s.heightCm} cm` : '—' },
                  { label: 'Weight', value: unitsIsImperial ? (s.weightLb ? `${s.weightLb} lb` : '—') : s.weightKg ? `${s.weightKg} kg` : '—' },
                  { label: 'Activity', value: ACTIVITY_DEFS.find((a) => a.key === s.activity)?.label ?? '—' },
                  { label: 'Goal', value: s.goal === 'maintain' ? 'Maintain' : `${s.goal === 'lose' ? 'Lose' : 'Gain'} weight${goalWeightValue ? ` to ${goalWeightValue}${goalWeightUnit}` : ''}` },
                  { label: 'Household', value: s.members.length ? s.members.join(', ') : 'Just me' },
                  { label: 'Allergies', value: s.allergies.length ? s.allergies.join(', ') : 'None' },
                  { label: 'Dislikes', value: s.dislikes.length ? s.dislikes.join(', ') : 'None' },
                  { label: 'Likes', value: s.likes.length ? s.likes.join(', ') : 'None' },
                  { label: 'Cuisine affinity', value: s.cuisines.length ? s.cuisines.join(', ') : 'Any' },
                  { label: 'Diet modes', value: s.diets.length ? s.diets.join(', ') : 'None' },
                  { label: 'Meals per day', value: mealsPerDayLabel },
                  { label: 'Units', value: unitsIsImperial ? 'Imperial' : 'Metric' },
                ].map((row) => (
                  <div className="ob-review-row" key={row.label}>
                    <span className="ob-review-label">{row.label}</span>
                    <span className="ob-review-value">{row.value}</span>
                  </div>
                ))}
              </div>
              {saveError && <div style={{ color: 'var(--danger)', fontSize: 13.5 }}>{saveError}</div>}
            </div>
          )}

          {TOUR_STEPS.includes(stepName) && (
            <div className="ob-hero ob-tour">
              <div className="ob-icon-badge ob-tour-badge">
                <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="var(--ink)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d={TOUR_CARDS[stepName].icon} />
                </svg>
              </div>
              <div>
                <h1 className="ob-hero-title" style={{ fontSize: 26 }}>
                  {TOUR_CARDS[stepName].title}
                </h1>
                <p className="ob-hero-sub">{TOUR_CARDS[stepName].body}</p>
              </div>
              <div className="ob-tour-prompts">
                <span className="ob-tour-prompts-label">Try saying</span>
                {TOUR_CARDS[stepName].prompts.map((q) => (
                  <span className="ob-tour-prompt" key={q}>
                    “{q}”
                  </span>
                ))}
              </div>
              <div className="ob-tour-actions">
                <button type="button" className="ob-btn-hero" onClick={() => patch({ step: s.step + 1 })}>
                  {stepName === TOUR_STEPS[TOUR_STEPS.length - 1] ? 'Finish' : 'Next'}
                </button>
                <button type="button" className="btn-ghost" onClick={() => patch({ step: DONE_STEP })}>
                  Skip the tour
                </button>
              </div>
              <div className="ob-tour-dots">
                {TOUR_STEPS.map((t) => (
                  <span key={t} className={`ob-tour-dot ${t === stepName ? 'ob-tour-dot-on' : ''}`} />
                ))}
              </div>
            </div>
          )}

          {stepName === 'done' && (
            <div className="ob-hero">
              <div className="ob-done-mark">
                <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M20 6 9 17l-5-5" />
                </svg>
              </div>
              <div>
                <h1 className="ob-hero-title" style={{ fontSize: 28 }}>
                  {tourOnly ? "That's the tour" : "You're all set"}
                </h1>
                <p className="ob-hero-sub">
                  {tourOnly
                    ? 'Everything on a client stays editable — people, allergies, likes, targets.'
                    : llmConfigured
                      ? 'Your first client is set up. Add more from the Clients tab whenever you like.'
                      : 'Your first client is set up. One last step: connect the AI model Aboyeur should run on.'}
                </p>
              </div>
              <button
                type="button"
                className="ob-btn-hero"
                onClick={() => navigate(tourOnly ? '/app/clients' : llmConfigured ? '/app/chat' : '/setup')}
              >
                {tourOnly ? 'Back to clients' : llmConfigured ? 'Open Aboyeur' : 'Connect a model'}
              </button>
            </div>
          )}
        </div>

        {showChrome && (
          <div className="ob-footer-bar">
            <button type="button" className="btn-primary" onClick={next} disabled={continueDisabled || saving} style={{ height: 48, padding: '0 30px' }}>
              {saving ? 'Saving…' : stepName === 'review' ? 'Finish Setup' : 'Continue'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
