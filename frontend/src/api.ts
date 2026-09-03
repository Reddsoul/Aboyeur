// Thin fetch wrapper over backend/main.py's routes. Every call sends the
// httpOnly session cookie (credentials: 'include') — Vite's dev proxy keeps
// browser and API on one origin so that just works, no CORS config needed.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
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
  meals_per_day?: number | null;
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
  targets: Record<string, unknown>;
}

export function getProfile() {
  return request<Profile>('/profile/data');
}

export function saveProfile(desired: {
  household: HouseholdMember[];
  allergies: string[];
  dislikes: string[];
  likes: string[];
  cuisine_affinity: string[];
  diet_modes: string[];
  meals_per_day: number;
  units: string;
}) {
  return request<Profile>('/profile/save', {
    method: 'POST',
    body: JSON.stringify(desired),
  });
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

export function getShoppingList(menuId: number) {
  return request<ShoppingList>(`/api/menu/${menuId}/shopping-list`);
}

// ---------------------------------------------------------------------------
// Chat (SSE)
// ---------------------------------------------------------------------------

/** Streams assistant text chunks from POST /chat. Yields each `data: ...`
 * payload as it arrives; the backend sends a final "[DONE]" sentinel. */
export async function* streamChat(message: string): AsyncGenerator<string> {
  const res = await fetch('/chat', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
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
