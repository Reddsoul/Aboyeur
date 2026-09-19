import { useCallback, useEffect, useMemo, useState } from 'react';
import './Profile.css';
import { DisclaimerLink } from '../components/Legal';
import * as api from '../api';
import { ApiError } from '../api';

const CM_PER_IN = 2.54;
const KG_PER_LB = 0.45359237;

const ACTIVITIES = [
  { key: 'sedentary', label: 'Sedentary' },
  { key: 'light', label: 'Light' },
  { key: 'moderate', label: 'Moderate' },
  { key: 'active', label: 'Active' },
  { key: 'very_active', label: 'Very active' },
];
const GOALS = [
  { key: 'maintain', label: 'Maintain' },
  { key: 'lose', label: 'Lose' },
  { key: 'gain', label: 'Gain' },
];
const SEXES = [
  { key: '', label: 'Unset' },
  { key: 'm', label: 'Male' },
  { key: 'f', label: 'Female' },
];
const ALLERGEN_PRESETS = ['Peanut', 'Tree nut', 'Shellfish', 'Dairy', 'Egg', 'Gluten', 'Soy', 'Fish'];
const CUISINE_PRESETS = ['Italian', 'Mexican', 'Indian', 'Chinese', 'Thai', 'Japanese', 'Mediterranean', 'French', 'American', 'Korean'];
const DIET_PRESETS = ['Vegetarian', 'Vegan', 'Pescatarian', 'Keto', 'Paleo', 'Gluten-free', 'Dairy-free', 'Low-carb'];

/** One member as the form holds it: every numeric is a string while editing so
 * a half-typed value never becomes NaN, and blank means "unset" on save. */
interface MemberForm {
  id: string;
  label: string;
  sex: string;
  age: string;
  heightCm: string;
  weightKg: string;
  activity: string;
  goal: string;
  goalWeightKg: string;
  mealsPerDay: string;
  open: boolean;
}

const num = (v: number | null | undefined) => (v == null ? '' : String(v));

function toForm(m: api.HouseholdMember): MemberForm {
  return {
    id: m.id,
    label: m.label ?? m.id,
    sex: m.sex ?? '',
    age: num(m.age),
    heightCm: num(m.height_cm),
    weightKg: num(m.weight_kg),
    activity: m.activity ?? 'moderate',
    goal: m.goal ?? 'maintain',
    goalWeightKg: num(m.goal_weight_kg),
    mealsPerDay: num(m.meals_per_day),
    open: false,
  };
}

function toApi(m: MemberForm): api.HouseholdMember {
  const n = (v: string) => (v.trim() === '' ? null : Number(v));
  return {
    id: m.id,
    label: m.label.trim() || m.id,
    sex: m.sex || null,
    age: n(m.age),
    height_cm: n(m.heightCm),
    weight_kg: n(m.weightKg),
    activity: m.activity || null,
    goal: m.goal || null,
    goal_weight_kg: n(m.goalWeightKg),
    meals_per_day: n(m.mealsPerDay),
  };
}

function RemovableChip({ label, tone, onRemove }: { label: string; tone: 'danger' | 'accent'; onRemove: () => void }) {
  return (
    <span className={`pf-rchip pf-rchip-${tone}`}>
      {label}
      <button type="button" onClick={onRemove} aria-label={`Remove ${label}`}>
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
          <path d="M18 6 6 18M6 6l12 12" />
        </svg>
      </button>
    </span>
  );
}

/** Height/weight inputs, in whichever units the profile prefers. Storage is
 * always metric — conversion happens here and nowhere else. */
function Anthro({ m, imperial, patch }: { m: MemberForm; imperial: boolean; patch: (p: Partial<MemberForm>) => void }) {
  const cm = m.heightCm ? Number(m.heightCm) : null;
  const totalIn = cm != null ? cm / CM_PER_IN : null;
  const ft = totalIn != null ? String(Math.floor(totalIn / 12)) : '';
  const inch = totalIn != null ? String(Math.round(totalIn - Math.floor(totalIn / 12) * 12)) : '';
  const lb = m.weightKg ? String(Math.round(Number(m.weightKg) / KG_PER_LB)) : '';
  const goalLb = m.goalWeightKg ? String(Math.round(Number(m.goalWeightKg) / KG_PER_LB)) : '';

  const setFtIn = (f: string, i: string) => {
    const total = (Number(f) || 0) * 12 + (Number(i) || 0);
    patch({ heightCm: total ? String(Math.round(total * CM_PER_IN * 10) / 10) : '' });
  };
  const setLb = (v: string, field: 'weightKg' | 'goalWeightKg') => {
    const n = Number(v);
    patch({ [field]: v && n ? String(Math.round(n * KG_PER_LB * 100) / 100) : '' } as Partial<MemberForm>);
  };

  return (
    <>
      <div className="pf-field">
        <label className="pf-label">Height</label>
        {imperial ? (
          <div className="pf-split">
            <input className="text-input" inputMode="numeric" placeholder="ft" value={ft} onChange={(e) => setFtIn(e.target.value, inch)} />
            <input className="text-input" inputMode="numeric" placeholder="in" value={inch} onChange={(e) => setFtIn(ft, e.target.value)} />
          </div>
        ) : (
          <input className="text-input" inputMode="decimal" placeholder="cm" value={m.heightCm} onChange={(e) => patch({ heightCm: e.target.value })} />
        )}
      </div>
      <div className="pf-field">
        <label className="pf-label">Weight</label>
        {imperial ? (
          <input className="text-input" inputMode="numeric" placeholder="lb" value={lb} onChange={(e) => setLb(e.target.value, 'weightKg')} />
        ) : (
          <input className="text-input" inputMode="decimal" placeholder="kg" value={m.weightKg} onChange={(e) => patch({ weightKg: e.target.value })} />
        )}
      </div>
      <div className="pf-field">
        <label className="pf-label">Goal weight</label>
        {imperial ? (
          <input className="text-input" inputMode="numeric" placeholder="lb" value={goalLb} onChange={(e) => setLb(e.target.value, 'goalWeightKg')} />
        ) : (
          <input className="text-input" inputMode="decimal" placeholder="kg" value={m.goalWeightKg} onChange={(e) => patch({ goalWeightKg: e.target.value })} />
        )}
      </div>
    </>
  );
}

interface Props {
  /** Whose profile this is. Every read and write on this screen is scoped to
   * this one client — there is no "current profile" independent of them. */
  clientId: number;
  clientName?: string;
  /** Told after a successful save, so the roster's summary numbers refresh. */
  onSaved?: () => void;
}

export default function Profile({ clientId, clientName, onSaved }: Props) {
  const [members, setMembers] = useState<MemberForm[]>([]);
  const [allergies, setAllergies] = useState<string[]>([]);
  const [dislikes, setDislikes] = useState<string[]>([]);
  const [likes, setLikes] = useState<string[]>([]);
  const [cuisines, setCuisines] = useState<string[]>([]);
  const [diets, setDiets] = useState<string[]>([]);
  const [mealsPerDay, setMealsPerDay] = useState(3);
  const [units, setUnits] = useState<'metric' | 'imperial'>('imperial');
  const [targets, setTargets] = useState<Record<string, api.MemberTarget>>({});

  const [drafts, setDrafts] = useState({ member: '', allergy: '', dislike: '', like: '' });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [status, setStatus] = useState<{ kind: 'ok' | 'bad'; text: string } | null>(null);

  const imperial = units === 'imperial';

  const apply = useCallback((profile: api.Profile) => {
    setMembers((profile.household ?? []).map(toForm));
    setAllergies(profile.allergies ?? []);
    setDislikes(profile.dislikes ?? []);
    setLikes(profile.likes ?? []);
    setCuisines(profile.cuisine_affinity ?? []);
    setDiets(profile.diet_modes ?? []);
    setMealsPerDay(profile.meals_per_day ?? 3);
    setUnits(profile.units === 'metric' ? 'metric' : 'imperial');
    setTargets(profile.targets ?? {});
  }, []);

  useEffect(() => {
    setLoading(true);
    api
      .getProfile(clientId)
      .then(apply)
      .catch(() => setStatus({ kind: 'bad', text: "Couldn't load this client's profile." }))
      .finally(() => setLoading(false));
  }, [apply, clientId]);

  // Every edit goes through here so the save bar knows there's something to save.
  function touch<T>(setter: (v: T) => void) {
    return (value: T) => {
      setter(value);
      setDirty(true);
      setStatus(null);
    };
  }

  const patchMember = (i: number, p: Partial<MemberForm>) => {
    setMembers((prev) => prev.map((m, j) => (j === i ? { ...m, ...p } : m)));
    if (!('open' in p)) {
      setDirty(true);
      setStatus(null);
    }
  };

  function addMember() {
    const label = drafts.member.trim();
    if (!label) return;
    const base = label.toLowerCase().replace(/[^a-z0-9]+/g, '_') || 'member';
    const id = members.some((m) => m.id === base) ? `${base}_${members.length + 1}` : base;
    setMembers((prev) => [...prev, { ...toForm({ id, label }), open: true }]);
    setDrafts((d) => ({ ...d, member: '' }));
    setDirty(true);
    setStatus(null);
  }

  function removeMember(i: number) {
    setMembers((prev) => prev.filter((_, j) => j !== i));
    setDirty(true);
    setStatus(null);
  }

  function addTo(list: string[], setList: (v: string[]) => void, key: 'allergy' | 'dislike' | 'like') {
    const v = drafts[key].trim();
    if (!v || list.some((x) => x.toLowerCase() === v.toLowerCase())) return;
    touch(setList)([...list, v]);
    setDrafts((d) => ({ ...d, [key]: '' }));
  }

  function toggle(list: string[], setList: (v: string[]) => void, value: string) {
    const has = list.some((x) => x.toLowerCase() === value.toLowerCase());
    touch(setList)(has ? list.filter((x) => x.toLowerCase() !== value.toLowerCase()) : [...list, value]);
  }

  async function save() {
    setSaving(true);
    setStatus(null);
    try {
      const saved = await api.saveProfile(clientId, {
        household: members.map(toApi),
        allergies,
        dislikes,
        likes,
        cuisine_affinity: cuisines,
        diet_modes: diets,
        meals_per_day: mealsPerDay,
        units,
      });
      // Re-render from what the server stored: ids get slugged and targets are
      // recomputed there, so echoing the local state back would drift.
      apply(saved);
      onSaved?.();
      setDirty(false);
      setStatus({ kind: 'ok', text: 'Saved. Their menus and targets use it from the next message on.' });
    } catch (err) {
      const detail = err instanceof ApiError ? ` (server said ${err.status})` : '';
      setStatus({ kind: 'bad', text: `Couldn't save your profile${detail}.` });
    } finally {
      setSaving(false);
    }
  }

  const mealsLabel = useMemo(
    () => (mealsPerDay === 1 ? 'One meal a day (OMAD)' : `${mealsPerDay} meals a day`),
    [mealsPerDay],
  );

  if (loading) return <p className="pf-sub">Loading profile…</p>;

  return (
    <>
        {/* --- household ------------------------------------------------ */}
        <section className="pf-section">
          <div className="pf-section-head">
            <div>
              <h2 className="pf-section-title">People</h2>
              <p className="pf-section-sub">
                {clientName ? `${clientName}'s household. ` : ''}Calorie and macro targets are computed per person from sex, age, height, weight and activity — never typed in.{' '}
                <DisclaimerLink />
              </p>
            </div>
          </div>

          {members.length === 0 && <p className="pf-sub">No one here yet. Add the first person below.</p>}

          {members.map((m, i) => {
            const t = targets[m.id];
            return (
              <div className="pf-member" key={m.id}>
                <div className="pf-member-bar">
                  <div className="pf-member-avatar">{(m.label || '?').slice(0, 1).toUpperCase()}</div>
                  <div className="pf-member-name">
                    <strong>{m.label || 'Unnamed'}</strong>
                    <span>
                      {t ? `${t.daily_kcal} kcal/day · ${t.goal}` : 'New — save to compute targets'}
                      {m.mealsPerDay ? ` · ${m.mealsPerDay} meal${m.mealsPerDay === '1' ? '' : 's'}/day` : ''}
                    </span>
                  </div>
                  <button
                    type="button"
                    className="pf-icon-btn"
                    onClick={() => patchMember(i, { open: !m.open })}
                    aria-label={m.open ? 'Collapse' : 'Edit'}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d={m.open ? 'm6 15 6-6 6 6' : 'm6 9 6 6 6-6'} />
                    </svg>
                  </button>
                  <button type="button" className="pf-icon-btn pf-icon-btn-danger" onClick={() => removeMember(i)} aria-label={`Remove ${m.label}`}>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6" />
                    </svg>
                  </button>
                </div>

                {m.open && (
                  <div className="pf-member-body">
                    <div className="pf-grid">
                      <div className="pf-field">
                        <label className="pf-label">Name</label>
                        <input className="text-input" value={m.label} onChange={(e) => patchMember(i, { label: e.target.value })} />
                      </div>
                      <div className="pf-field">
                        <label className="pf-label">Sex</label>
                        <select className="text-input" value={m.sex} onChange={(e) => patchMember(i, { sex: e.target.value })}>
                          {SEXES.map((o) => (
                            <option key={o.key} value={o.key}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="pf-field">
                        <label className="pf-label">Age</label>
                        <input className="text-input" inputMode="numeric" value={m.age} onChange={(e) => patchMember(i, { age: e.target.value })} />
                      </div>
                      <Anthro m={m} imperial={imperial} patch={(p) => patchMember(i, p)} />
                      <div className="pf-field">
                        <label className="pf-label">Activity</label>
                        <select className="text-input" value={m.activity} onChange={(e) => patchMember(i, { activity: e.target.value })}>
                          {ACTIVITIES.map((o) => (
                            <option key={o.key} value={o.key}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="pf-field">
                        <label className="pf-label">Goal</label>
                        <select className="text-input" value={m.goal} onChange={(e) => patchMember(i, { goal: e.target.value })}>
                          {GOALS.map((o) => (
                            <option key={o.key} value={o.key}>
                              {o.label}
                            </option>
                          ))}
                        </select>
                      </div>
                      <div className="pf-field">
                        <label className="pf-label">Meals per day</label>
                        <input
                          className="text-input"
                          inputMode="numeric"
                          placeholder={`Household default (${mealsPerDay})`}
                          value={m.mealsPerDay}
                          onChange={(e) => patchMember(i, { mealsPerDay: e.target.value })}
                        />
                      </div>
                    </div>

                    {t && (
                      <div className="pf-target">
                        <span className={`pf-badge ${t.source === 'bmr' ? 'pf-badge-ok' : ''}`}>
                          {t.source === 'bmr' ? 'computed' : 'estimate'}
                        </span>
                        <span>
                          <b>{t.daily_kcal}</b> kcal/day
                        </span>
                        <span className="pf-target-macros">
                          maintenance {t.maintenance_kcal} · goal {t.goal_adjust_kcal >= 0 ? '+' : ''}
                          {t.goal_adjust_kcal}
                        </span>
                        <span className="pf-target-macros">
                          per meal {Math.round(t.per_meal.kcal)} kcal · {Math.round(t.per_meal.protein_g)}p /{' '}
                          {Math.round(t.per_meal.carb_g)}c / {Math.round(t.per_meal.fat_g)}f
                        </span>
                        {t.source === 'fallback' && <span className="pf-target-macros">Fill in sex, age, height and weight for a real BMR number.</span>}
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}

          <div className="pf-add-row">
            <input
              className="text-input"
              placeholder="Add someone — name"
              value={drafts.member}
              onChange={(e) => setDrafts((d) => ({ ...d, member: e.target.value }))}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addMember())}
            />
            <button type="button" className="pf-add-btn" onClick={addMember}>
              Add person
            </button>
          </div>
        </section>

        {/* --- restrictions --------------------------------------------- */}
        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Allergies</h2>
            <p className="pf-section-sub">Hard-excluded from every recipe and menu. A category word like “shellfish” covers shrimp, crab and mussels.</p>
          </div>
          <div className="pf-chips">
            {ALLERGEN_PRESETS.map((a) => (
              <button
                key={a}
                type="button"
                className={`pf-chip ${allergies.some((x) => x.toLowerCase() === a.toLowerCase()) ? 'pf-chip-on' : ''}`}
                onClick={() => toggle(allergies, setAllergies, a)}
              >
                {a}
              </button>
            ))}
          </div>
          {allergies.length > 0 && (
            <div className="pf-chips">
              {allergies.map((a) => (
                <RemovableChip key={a} label={a} tone="danger" onRemove={() => toggle(allergies, setAllergies, a)} />
              ))}
            </div>
          )}
          <div className="pf-add-row">
            <input
              className="text-input"
              placeholder="Add another allergy"
              value={drafts.allergy}
              onChange={(e) => setDrafts((d) => ({ ...d, allergy: e.target.value }))}
              onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTo(allergies, setAllergies, 'allergy'))}
            />
            <button type="button" className="pf-add-btn" onClick={() => addTo(allergies, setAllergies, 'allergy')}>
              Add
            </button>
          </div>
        </section>

        {/* --- taste ----------------------------------------------------- */}
        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Likes & dislikes</h2>
            <p className="pf-section-sub">Dislikes are soft-avoided, not banned. Likes pull more of what you enjoy into menus.</p>
          </div>
          <div className="pf-grid">
            <div className="pf-field">
              <label className="pf-label">Dislikes</label>
              <div className="pf-chips">
                {dislikes.map((d) => (
                  <RemovableChip key={d} label={d} tone="danger" onRemove={() => toggle(dislikes, setDislikes, d)} />
                ))}
              </div>
              <div className="pf-add-row">
                <input
                  className="text-input"
                  placeholder="e.g. okra"
                  value={drafts.dislike}
                  onChange={(e) => setDrafts((d) => ({ ...d, dislike: e.target.value }))}
                  onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTo(dislikes, setDislikes, 'dislike'))}
                />
                <button type="button" className="pf-add-btn" onClick={() => addTo(dislikes, setDislikes, 'dislike')}>
                  Add
                </button>
              </div>
            </div>
            <div className="pf-field">
              <label className="pf-label">Likes</label>
              <div className="pf-chips">
                {likes.map((l) => (
                  <RemovableChip key={l} label={l} tone="accent" onRemove={() => toggle(likes, setLikes, l)} />
                ))}
              </div>
              <div className="pf-add-row">
                <input
                  className="text-input"
                  placeholder="e.g. salmon"
                  value={drafts.like}
                  onChange={(e) => setDrafts((d) => ({ ...d, like: e.target.value }))}
                  onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), addTo(likes, setLikes, 'like'))}
                />
                <button type="button" className="pf-add-btn" onClick={() => addTo(likes, setLikes, 'like')}>
                  Add
                </button>
              </div>
            </div>
          </div>
        </section>

        {/* --- cuisine & diet -------------------------------------------- */}
        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Cuisine & diet</h2>
            <p className="pf-section-sub">Affinity focuses menus on what you love; diet modes filter the recipe pool outright.</p>
          </div>
          <div>
            <label className="pf-label">Cuisines</label>
            <div className="pf-chips" style={{ marginTop: 8 }}>
              {CUISINE_PRESETS.map((c) => (
                <button key={c} type="button" className={`pf-chip ${cuisines.some((x) => x.toLowerCase() === c.toLowerCase()) ? 'pf-chip-on' : ''}`} onClick={() => toggle(cuisines, setCuisines, c)}>
                  {c}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="pf-label">Diet modes</label>
            <div className="pf-chips" style={{ marginTop: 8 }}>
              {DIET_PRESETS.map((d) => (
                <button key={d} type="button" className={`pf-chip ${diets.some((x) => x.toLowerCase() === d.toLowerCase()) ? 'pf-chip-on' : ''}`} onClick={() => toggle(diets, setDiets, d)}>
                  {d}
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* --- household defaults ---------------------------------------- */}
        <section className="pf-section">
          <div>
            <h2 className="pf-section-title">Household defaults</h2>
            <p className="pf-section-sub">Applied to anyone without their own override. {mealsLabel}.</p>
          </div>
          <div className="pf-grid">
            <div className="pf-field">
              <label className="pf-label">Meals per day</label>
              <div className="pf-seg">
                {[1, 2, 3, 4, 5].map((n) => (
                  <button key={n} type="button" aria-pressed={mealsPerDay === n} onClick={() => touch(setMealsPerDay)(n)}>
                    {n}
                  </button>
                ))}
              </div>
            </div>
            <div className="pf-field">
              <label className="pf-label">Units</label>
              <div className="pf-seg">
                <button type="button" aria-pressed={imperial} onClick={() => touch(setUnits)('imperial')}>
                  Imperial
                </button>
                <button type="button" aria-pressed={!imperial} onClick={() => touch(setUnits)('metric')}>
                  Metric
                </button>
              </div>
            </div>
          </div>
        </section>

        <div className="pf-savebar">
          <button type="button" className="btn-primary" disabled={!dirty || saving} onClick={() => void save()}>
            {saving ? 'Saving…' : 'Save changes'}
          </button>
          {status ? (
            <span className={`pf-status ${status.kind === 'ok' ? 'pf-status-ok' : 'pf-status-bad'}`}>{status.text}</span>
          ) : (
            <span className="pf-status">{dirty ? 'Unsaved changes.' : 'Everything saved.'}</span>
          )}
        </div>
    </>
  );
}
