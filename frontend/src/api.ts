// Thin fetch wrapper over backend/main.py's routes. Every call sends the
// httpOnly session cookie (credentials: 'include') — Vite's dev proxy keeps
// browser and API on one origin so that just works, no CORS config needed.
//
// VITE_API_BASE lets a build point at another origin (default: same-origin,
// which is how DEPLOYMENT.md serves dist/ — one Caddy, uvicorn behind it).
// Note a cross-origin deploy needs more than this: a SameSite=Lax cookie
// isn't sent on cross-site XHR, so that setup also needs bearer auth +
// CORS on the backend (see FRONTEND_PLAN.md).

export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      // no JSON body
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export function login(email: string, password: string) {
  return request<{ user_id: number }>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  });
}

export function logout() {
  return request<{ ok: true }>('/auth/logout', { method: 'POST' });
}

export function me() {
  return request<{ user_id: number }>('/auth/me');
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

export interface HouseholdMember {
  id: string;
  label: string;
  sex?: string | null;
  age?: number | null;
  height_cm?: number | null;
  weight_kg?: number | null;
  activity?: string | null;
  goal?: string | null;
  goal_weight_kg?: number | null;
  /** Per-person override of the household default (1 = OMAD). */
  meals_per_day?: number | null;
  /** Hand-set daily calories; overrides the computed BMR/TDEE number. */
  daily_kcal?: number | null;
}

export interface Macros {
  kcal: number;
  protein_g: number;
  carb_g: number;
  fat_g: number;
}

/** Server-computed, never user-entered — see engines/nutrition.household_targets. */
export interface MemberTarget {
  label: string;
  goal: string;
  goal_weight_kg: number | null;
  goal_adjust_kcal: number;
  maintenance_kcal: number;
  daily_kcal: number;
  /** 'bmr' once sex/age/height/weight are all set, else 'fallback'. */
  source: 'bmr' | 'fallback';
  per_meal: Macros;
  daily_macros: Macros;
  strategy_daily_macros?: Macros & { strategy: string; basis: string };
}

export interface Profile {
  household: HouseholdMember[];
  allergies: string[];
  dislikes: string[];
  likes: string[];
  cuisine_affinity: string[];
  diet_modes: string[];
  meals_per_day: number;
  units: 'metric' | 'imperial';
  targets: Record<string, MemberTarget>;
}

export function getProfile(clientId: number) {
  return request<Profile>(`/api/clients/${clientId}/profile`);
}

export function saveProfile(clientId: number, desired: {
  household: HouseholdMember[];
  allergies: string[];
  dislikes: string[];
  likes: string[];
  cuisine_affinity: string[];
  diet_modes: string[];
  meals_per_day: number;
  units: string;
}) {
  return request<Profile>(`/api/clients/${clientId}/profile`, {
    method: 'POST',
    body: JSON.stringify(desired),
  });
}

// ---------------------------------------------------------------------------
// Clients — the people this practitioner plans for. Every per-person call
// above and below hangs off one of these ids; the server 404s any id that
// isn't on this account's book.
// ---------------------------------------------------------------------------

export interface Client {
  id: number;
  name: string;
  email: string | null;
  phone: string | null;
  notes: string | null;
  status: 'active' | 'archived';
  created_at: string;
  updated_at: string;
  /** Roster-card extras, computed server-side. */
  household_size: number;
  goal: string | null;
  daily_kcal: number | null;
  allergies: string[];
  menu_count: number;
  kit_count: number;
  last_plan_at: string | null;
  has_profile: boolean;
}

export function listClients(includeArchived = false) {
  return request<{ clients: Client[] }>(
    `/api/clients${includeArchived ? '?include_archived=true' : ''}`,
  ).then((r) => r.clients);
}

export function createClient(body: { name: string; email?: string; phone?: string; notes?: string }) {
  return request<Client>('/api/clients', { method: 'POST', body: JSON.stringify(body) });
}

export function getClient(id: number) {
  return request<Client>(`/api/clients/${id}`);
}

export function updateClient(id: number, patch: Partial<Pick<Client, 'name' | 'email' | 'phone' | 'notes' | 'status'>>) {
  return request<Client>(`/api/clients/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
}

/** Archive by default — their plans and profile survive. `purge` is forever. */
export function removeClient(id: number, purge = false) {
  return request<Client | { deleted: number }>(`/api/clients/${id}${purge ? '?purge=true' : ''}`, {
    method: 'DELETE',
  });
}

export interface MenuSummary {
  id: number;
  week_of: string;
  status: string;
  created_at: string;
  days: number;
  meals: number;
}

export interface KitSummary {
  id: number;
  created_at: string;
  days: number | null;
  meals_per_day: number | null;
  strategy: string | null;
  component_count: number;
}

export function getClientPlans(id: number) {
  return request<{ menus: MenuSummary[]; kits: KitSummary[] }>(`/api/clients/${id}/plans`);
}

// ---------------------------------------------------------------------------
// Recipe catalog
// ---------------------------------------------------------------------------

export interface RecipeSummary {
  id: number;
  title: string;
  book: string;
  cuisine: string | null;
  servings: number | null;
  ingredients: string[];
  tags: string[];
  favorite: boolean;
}

export interface RecipeBrowseResult {
  results: RecipeSummary[];
  total: number;
  offset: number;
  limit: number;
}

export function browseRecipes(params: {
  query?: string;
  cuisine?: string;
  book?: string;
  offset?: number;
  limit?: number;
}) {
  const qs = new URLSearchParams();
  if (params.query) qs.set('query', params.query);
  if (params.cuisine) qs.set('cuisine', params.cuisine);
  if (params.book) qs.set('book', params.book);
  qs.set('offset', String(params.offset ?? 0));
  qs.set('limit', String(params.limit ?? 24));
  return request<RecipeBrowseResult>(`/api/recipes?${qs.toString()}`);
}

export interface RecipeFacets {
  cuisines: { value: string; count: number }[];
  books: { value: string; count: number }[];
}

export function getFacets() {
  return request<RecipeFacets>('/api/recipes/facets');
}

export interface RecipeDetail extends RecipeSummary {
  markdown: string;
  path: string;
}

export function getRecipe(id: number) {
  return request<RecipeDetail>(`/api/recipes/${id}`);
}

export function setFavorite(id: number, favorite: boolean) {
  return request<{ favorite: boolean }>(`/api/recipes/${id}/favorite`, {
    method: favorite ? 'POST' : 'DELETE',
  });
}

// ---------------------------------------------------------------------------
// Menu / shopping list
// ---------------------------------------------------------------------------

export interface MenuDay {
  day: number;
  meal: number;
  role: string;
  recipe_id: number;
  title: string;
  book: string;
  kcal: number;
  protein_g: number;
  carb_g: number;
  fat_g: number;
  portion_multiplier: number;
  pick_reason: string;
}

export interface MenuPlan {
  menu_id: number;
  days: MenuDay[];
  meals_per_day: number;
  overlap_score: number;
  daily_target_kcal: number;
  daily_totals: {
    day: number;
    kcal: number;
    target_kcal: number;
    status: 'ok' | 'under' | 'over' | 'unknown';
  }[];
}

export function planMenu(body: {
  client_id: number;
  candidate_ids: number[];
  days: number;
  meals_per_day?: number;
  constraints?: Record<string, number>;
}) {
  return request<MenuPlan>('/api/menu/plan', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export interface ShoppingItem {
  name: string;
  approx_grams: number;
  used_in_recipes: number;
  have: boolean;
  flag: string | null;
}

export interface ShoppingList {
  sections: Record<string, ShoppingItem[]>;
  total_items: number;
  to_buy: number;
}

export function getShoppingList(menuId: number, clientId: number) {
  return request<ShoppingList>(`/api/menu/${menuId}/shopping-list?client_id=${clientId}`);
}

// ---------------------------------------------------------------------------
// LLM settings (per-user endpoint + key; the key is write-only — the server
// never sends it back, so `configured` is all the UI gets to know about it)
// ---------------------------------------------------------------------------

export interface LlmSettings {
  base_url: string;
  model: string;
  configured: boolean;
}

export function getLlmSettings() {
  return request<LlmSettings>('/api/llm-settings');
}

export function saveLlmSettings(body: { base_url: string; model: string; api_key?: string }) {
  return request<LlmSettings>('/api/llm-settings', {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export interface LlmTestResult {
  ok: boolean;
  detail: string;
}

/** Probes an endpoint without saving it. Omit fields to test what's stored. */
export function testLlmConnection(body: { base_url?: string; model?: string; api_key?: string }) {
  return request<LlmTestResult>('/api/llm-settings/test', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Chat (SSE)
// ---------------------------------------------------------------------------

/** Streams assistant text chunks from POST /chat. Yields each `data: ...`
 * payload as it arrives; the backend sends a final "[DONE]" sentinel. */
export async function* streamChat(message: string, clientId: number): AsyncGenerator<string> {
  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    // The server keeps one agent per (account, client), so this id decides
    // whose conversation and whose plans this turn belongs to.
    body: JSON.stringify({ message, client_id: clientId }),
  });
  if (!res.ok || !res.body) {
    throw new ApiError(res.status, res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // Events are blank-line-terminated; a multi-line payload (routine in
    // streamed markdown) carries one "data: " prefix per line (see
    // backend/main.py's _sse) — rejoin them with \n to recover the original
    // chunk instead of only reading the first line.
    const events = buffer.split('\n\n');
    buffer = events.pop() ?? '';
    for (const event of events) {
      const payload = event
        .split('\n')
        .filter((l) => l.startsWith('data: '))
        .map((l) => l.slice('data: '.length))
        .join('\n');
      if (payload === '[DONE]') return;
      yield payload;
    }
  }
}
