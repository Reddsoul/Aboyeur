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

/** A non-2xx answer. `code` is the machine-readable reason the billing /
 * account routes attach (quota, unverified, hosted_requires_pro, no_credit,
 * llm_unconfigured, invite_required, ...); `extra` carries any URL that goes
 * with it (upgrade_url, topup_url, settings_url). */
export class ApiError extends Error {
  status: number;
  code: string | null;
  extra: Record<string, unknown>;
  constructor(status: number, message: string, code: string | null = null, extra: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.extra = extra;
  }
}

/** FastAPI puts either a string or our {message, error, ...} object in
 * `detail`. Normalise to (message, code, extra). */
export async function errorFromResponse(res: Response): Promise<ApiError> {
  let message = res.statusText;
  let code: string | null = null;
  let extra: Record<string, unknown> = {};
  try {
    const body = await res.json();
    const detail = body.detail ?? body;
    if (typeof detail === 'string') {
      message = detail || message;
    } else if (detail && typeof detail === 'object') {
      const { message: m, error, ...rest } = detail as Record<string, unknown>;
      message = (typeof m === 'string' && m) || message;
      code = typeof error === 'string' ? error : null;
      extra = rest;
    }
    if (!code && typeof body.error === 'string') code = body.error;
  } catch {
    // no JSON body
  }
  return new ApiError(res.status, message, code, extra);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + path, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!res.ok) throw await errorFromResponse(res);
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

/** Revokes every session for the account (this one included). */
export function logoutAll() {
  return request<{ ok: true }>('/auth/logout-all', { method: 'POST' });
}

export interface TosStatus {
  current: string;
  accepted: string | null;
  needs_acceptance: boolean;
}

export interface Me {
  user_id: number;
  is_admin: boolean;
  email_verified: boolean;
  plan: 'free' | 'pro';
  billing_status: BillingStatus;
  llm_mode: LlmMode;
  tos: TosStatus;
}

export function me() {
  return request<Me>('/auth/me');
}

// ---------------------------------------------------------------------------
// Self-serve auth: signup / verify / reset
// ---------------------------------------------------------------------------

export function signupMode() {
  return request<{ mode: 'invite' | 'open'; legal_version: string }>('/auth/signup-mode');
}

export function signup(body: {
  email: string;
  password: string;
  country?: string | null;
  invite_code?: string | null;
  tos_version: string;
}) {
  return request<{ user_id: number; email_verified: boolean }>('/auth/signup', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export function verifyEmail(token: string) {
  return request<{ ok: true; user_id: number }>('/auth/verify', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
}

export function resendVerification() {
  return request<{ ok: true; already_verified?: boolean }>('/auth/resend-verification', { method: 'POST' });
}

export function requestReset(email: string) {
  return request<{ ok: true }>('/auth/reset', { method: 'POST', body: JSON.stringify({ email }) });
}

export function confirmReset(token: string, newPassword: string) {
  return request<{ ok: true }>('/auth/reset/confirm', {
    method: 'POST',
    body: JSON.stringify({ token, new_password: newPassword }),
  });
}

export function acceptTos(version: string) {
  return request<TosStatus>('/api/account/accept-tos', {
    method: 'POST',
    body: JSON.stringify({ version }),
  });
}

export function legalIndex() {
  return request<{ version: string; documents: string[] }>('/api/legal');
}

export async function legalDoc(slug: string): Promise<string> {
  const res = await fetch(`${API_BASE}/api/legal/${slug}`, { credentials: 'include' });
  if (!res.ok) throw await errorFromResponse(res);
  return res.text();
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
  fiber_g?: number;
}

/** Server-computed, never user-entered — see engines/nutrition.household_targets.
 * Exception: source 'override' means a practitioner manually corrected the
 * daily kcal/macros (set_member_target_override) — daily_kcal/daily_macros/
 * per_meal below reflect THAT, not the BMR/TDEE calc. */
export interface MemberTarget {
  label: string;
  goal: string;
  goal_weight_kg: number | null;
  goal_adjust_kcal: number;
  maintenance_kcal: number;
  daily_kcal: number;
  /** 'bmr' once sex/age/height/weight are all set, 'fallback' otherwise,
   * 'override' when manually corrected. */
  source: 'bmr' | 'fallback' | 'override';
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
  /** 0-100 (default 50): how much menus may repeat dishes / share
   * ingredients. 0 = same dishes every day, 100 = never repeat. Absent on
   * profiles saved before the field existed. */
  variety?: number;
  /** Kitchen equipment on hand — recipes classified as needing something
   * missing are excluded from this client's menus. Defaults to a
   * common-kitchen set (stovetop, oven, pots/pans, mixing bowls). */
  tools_available?: string[];
  /** Free-text diet-logic notes, parsed into meal-type hints / a meal-prep
   * flag the next time a menu is planned. */
  notes?: string;
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
  variety?: number;
  tools_available?: string[];
  notes?: string;
}) {
  return request<Profile>(`/api/clients/${clientId}/profile`, {
    method: 'POST',
    body: JSON.stringify(desired),
  });
}

/** Manually correct one member's computed daily calorie/macro target — the
 * BMR/TDEE number was wrong for them. Wins over the computed target
 * everywhere it's read until cleared with clearMemberTargetOverride. */
export function setMemberTargetOverride(
  clientId: number,
  memberId: string,
  macros: { kcal: number; protein_g: number; carb_g: number; fat_g: number; fiber_g?: number },
) {
  return request<Profile>(`/api/clients/${clientId}/members/${encodeURIComponent(memberId)}/target_override`, {
    method: 'POST',
    body: JSON.stringify(macros),
  });
}

export function clearMemberTargetOverride(clientId: number, memberId: string) {
  return request<Profile>(`/api/clients/${clientId}/members/${encodeURIComponent(memberId)}/target_override`, {
    method: 'DELETE',
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
  /** The practitioner's own record — they plan for themselves too. */
  is_self: boolean;
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

/** One stored PDF snapshot of a menu (POST /api/menu/{id}/export). */
export interface MenuExport {
  id: number;
  menu_id: number;
  filename: string;
  size_bytes: number;
  created_at: string;
}

export interface MenuSummary {
  id: number;
  week_of: string;
  status: string;
  created_at: string;
  days: number;
  meals: number;
  /** Newest first. */
  exports: MenuExport[];
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
  /** Whose catalog view this is: their allergens filter it, their stars light up. */
  clientId: number;
  query?: string;
  cuisine?: string;
  book?: string;
  offset?: number;
  limit?: number;
  /** Only what this client has starred — filtered server-side. */
  favoritesOnly?: boolean;
}) {
  const qs = new URLSearchParams();
  qs.set('client_id', String(params.clientId));
  if (params.favoritesOnly) qs.set('favorites_only', 'true');
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

export interface RecipeMacros {
  kcal: number;
  protein_g: number;
  carb_g: number;
  fat_g: number;
  fiber_g: number;
  /** 'override' means a practitioner corrected these by hand — see
   * set_recipe_macro_override / clearMacroOverride below. */
  confidence: 'low' | 'medium' | 'override' | string;
  servings_est: number | null;
}

export interface RecipeDetail extends RecipeSummary {
  markdown: string;
  path: string;
  macros: RecipeMacros | null;
}

export function getRecipe(id: number, clientId: number) {
  return request<RecipeDetail>(`/api/recipes/${id}?client_id=${clientId}`);
}

/** A star is per client — "this suits this person", not a global bookmark. */
export function setFavorite(id: number, favorite: boolean, clientId: number) {
  return request<{ favorite: boolean }>(`/api/recipes/${id}/favorite?client_id=${clientId}`, {
    method: favorite ? 'POST' : 'DELETE',
  });
}

/** Manual macro correction — global to the recipe, not scoped to one
 * client (feature-spec-2026-09-26.md #1). */
export function setMacroOverride(
  id: number,
  macros: { kcal: number; protein_g: number; carb_g: number; fat_g: number; fiber_g?: number },
) {
  return request<{ recipe_id: number; is_override: boolean; macros: RecipeMacros }>(
    `/api/recipes/${id}/macro_override`,
    { method: 'POST', body: JSON.stringify(macros) },
  );
}

export function clearMacroOverride(id: number) {
  return request<{ recipe_id: number; is_override: boolean }>(`/api/recipes/${id}/macro_override`, {
    method: 'DELETE',
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
// Menu → PDF. Each export is a stored snapshot; the Plans tab lists them.
// ---------------------------------------------------------------------------

export function exportMenu(menuId: number, clientId: number) {
  return request<MenuExport>(`/api/menu/${menuId}/export?client_id=${clientId}`, { method: 'POST' });
}

/** Plain URL — the session cookie rides along, so an <a download> works. */
export function exportDownloadUrl(exportId: number, clientId: number) {
  return `${API_BASE}/api/exports/${exportId}?client_id=${clientId}`;
}

export function deleteExport(exportId: number, clientId: number) {
  return request<void>(`/api/exports/${exportId}?client_id=${clientId}`, { method: 'DELETE' });
}

/** Build a snapshot, then hand it to the browser as a download. */
export async function exportMenuAndDownload(menuId: number, clientId: number) {
  const exp = await exportMenu(menuId, clientId);
  triggerDownload(exportDownloadUrl(exp.id, clientId), exp.filename);
  return exp;
}

export function triggerDownload(url: string, filename: string) {
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------------------
// LLM settings (per-user endpoint + key; the key is write-only — the server
// never sends it back, so `configured` is all the UI gets to know about it)
// ---------------------------------------------------------------------------

export type LlmMode = 'byok' | 'hosted';
export type BillingStatus = 'free' | 'active' | 'past_due' | 'canceled' | 'comp';

/** What the Settings page shows about the hosted option: the provider's
 * display name and the per-1K-token price the user pays. The endpoint
 * behind it is never sent. */
export interface HostedOffer {
  name: string;
  model: string;
  price_in_per_ktok_cents: number;
  price_out_per_ktok_cents: number;
}

export interface LlmSettings {
  base_url: string;
  model: string;
  /** "A turn will resolve to some model": a saved BYOK key, or hosted mode
   * with a provider available and a paid plan. Drives the /setup gate. */
  configured: boolean;
  /** A BYOK key is stored (the form's placeholder), regardless of mode. */
  byok_configured: boolean;
  llm_mode: LlmMode;
  plan: 'free' | 'pro';
  billing_status: BillingStatus;
  is_paid: boolean;
  credit_cents: number;
  hosted_offer: HostedOffer | null;
  billing_enabled: boolean;
  topup_amounts: number[];
}

export function setLlmMode(mode: LlmMode) {
  return request<{ llm_mode: LlmMode }>('/api/llm-settings/mode', {
    method: 'PUT',
    body: JSON.stringify({ mode }),
  });
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
// Chat history — one transcript per client, kept server-side
// ---------------------------------------------------------------------------

export interface ChatMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
  /** Set on an assistant turn that built, swapped or delivered a menu —
   * the bubble offers "Export PDF" for it. */
  menu_id: number | null;
}

/** One of a client's conversations. They keep as many as they like; starting
 * a new one never touches the others. */
export interface ChatSession {
  id: number;
  client_id: number;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_at: string | null;
  preview: string;
}

export function listSessions(clientId: number) {
  return request<{ sessions: ChatSession[] }>(`/api/clients/${clientId}/sessions`).then(
    (r) => r.sessions,
  );
}

export function createSession(clientId: number, title?: string) {
  return request<ChatSession>(`/api/clients/${clientId}/sessions`, {
    method: 'POST',
    body: JSON.stringify({ title: title ?? null }),
  });
}

export function renameSession(clientId: number, sessionId: number, title: string) {
  return request<ChatSession>(`/api/clients/${clientId}/sessions/${sessionId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  });
}

export function deleteSession(clientId: number, sessionId: number) {
  return request<{ deleted: number }>(`/api/clients/${clientId}/sessions/${sessionId}`, {
    method: 'DELETE',
  });
}

/** Omit sessionId to open the conversation this client last used. */
export function getClientMessages(clientId: number, sessionId?: number) {
  const qs = sessionId ? `?session_id=${sessionId}` : '';
  return request<{ session_id: number; messages: ChatMessage[] }>(
    `/api/clients/${clientId}/messages${qs}`,
  );
}

export function clearClientMessages(clientId: number, sessionId: number) {
  return request<{ cleared: number; session_id: number }>(
    `/api/clients/${clientId}/messages?session_id=${sessionId}`,
    { method: 'DELETE' },
  );
}

// ---------------------------------------------------------------------------
// Account — the practitioner who is logged in, not anyone they plan for
// ---------------------------------------------------------------------------

export interface SupportAccess {
  ts: string;
  session_id: number;
}

export interface Account {
  id: number;
  email: string;
  name: string | null;
  practice: string | null;
  created_at: string;
  client_count: number;
  active_client_count: number;
  /** Their own client record — themselves, as someone to plan for. */
  self_client: { id: number; name: string } | null;
  country: string | null;
  plan: 'free' | 'pro';
  billing_status: BillingStatus;
  llm_mode: LlmMode;
  credit_cents: number;
  email_verified: boolean;
  tos: TosStatus;
  /** Times an operator revealed one of your flagged conversations. */
  support_access: SupportAccess[];
}

export function getAccount() {
  return request<Account>('/api/account');
}

export function setCountry(country: string | null) {
  return request<{ country: string | null }>('/api/account/country', {
    method: 'PUT',
    body: JSON.stringify({ country }),
  });
}

export const exportUrl = `${API_BASE}/api/account/export`;

export function deleteAccount(password: string) {
  return request<{ deleted_at: string; purge_after: string }>('/api/account', {
    method: 'DELETE',
    body: JSON.stringify({ confirm: 'DELETE', password }),
  });
}

// ---------------------------------------------------------------------------
// Billing — Stripe Checkout / Customer Portal are Stripe-hosted pages; the
// client only ever gets a URL to redirect to.
// ---------------------------------------------------------------------------

export interface BillingMe {
  plan: 'free' | 'pro';
  billing_status: BillingStatus;
  interval: 'month' | 'year' | null;
  renews_at: string | null;
  paid_months: number;
  loyalty_tier: string | null;
  percent_off: number;
  price_cents: number | null;
  prices: { month: number; year: number };
  country: string;
  country_source: 'account' | 'detected';
  llm_mode: LlmMode;
  credit_cents: number;
  email_verified: boolean;
  has_customer: boolean;
  ladder: { tier: string; min_paid_months: number; percent_off: number }[];
  topup_amounts: number[];
  free_turns_limit: number;
  turns_used_30d: number;
  support_access: SupportAccess[];
  billing_enabled: boolean;
  hosted_offer: HostedOffer | null;
}

export function billingMe() {
  return request<BillingMe>('/api/billing/me');
}

export function billingCheckout(interval: 'month' | 'year') {
  return request<{ url: string }>('/api/billing/checkout', {
    method: 'POST',
    body: JSON.stringify({ interval }),
  });
}

export function billingTopup(amountCents: number) {
  return request<{ url: string }>('/api/billing/topup', {
    method: 'POST',
    body: JSON.stringify({ amount_cents: amountCents }),
  });
}

export function billingPortal() {
  return request<{ url: string }>('/api/billing/portal', { method: 'POST' });
}

export function updateAccount(patch: { name?: string; practice?: string; email?: string }) {
  return request<Account>('/api/account', { method: 'PATCH', body: JSON.stringify(patch) });
}

export function changePassword(body: { current_password: string; new_password: string }) {
  return request<{ ok: true }>('/api/account/password', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Chat (SSE)
// ---------------------------------------------------------------------------

/** Streams assistant text chunks from POST /chat. Yields each `data: ...`
 * payload as it arrives; the backend sends a final "[DONE]" sentinel. */
export async function* streamChat(
  message: string,
  clientId: number,
  sessionId: number,
): AsyncGenerator<string> {
  const res = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    // The server keeps one agent per (account, client, conversation), so these
    // ids decide whose plans this turn touches and which thread it continues.
    body: JSON.stringify({ message, client_id: clientId, session_id: sessionId }),
  });
  yield* readSse(res);
}

/** A turn the server is running (or just ran) for one conversation. The
 * turn lives on the server independently of any HTTP connection, so a
 * refresh mid-answer can find it here and re-attach (see resumeChat). */
export interface TurnStatus {
  turn_id: string;
  in_flight: boolean;
  done: boolean;
  error: boolean;
  /** How many chunks are buffered — pass as `start` to resumeChat to get
   * only what comes after `text`. */
  chunk_count: number;
  /** Everything streamed so far. */
  text: string;
  started_at: number;
  finished_at: number | null;
}

export function chatStatus(clientId: number, sessionId: number) {
  return request<{ session_id: number; turn: TurnStatus | null }>(
    `/chat/status?client_id=${clientId}&session_id=${sessionId}`,
  );
}

/** Re-attach to the conversation's current turn: replays buffered chunks
 * from index `start`, then streams live until "[DONE]". Same framing as
 * streamChat. 404 when there is nothing to attach to. */
export async function* resumeChat(clientId: number, sessionId: number, start = 0): AsyncGenerator<string> {
  const res = await fetch(`${API_BASE}/chat/stream?client_id=${clientId}&session_id=${sessionId}&start=${start}`, {
    credentials: 'include',
  });
  yield* readSse(res);
}

async function* readSse(res: Response): AsyncGenerator<string> {
  if (!res.ok || !res.body) {
    throw await errorFromResponse(res);
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

// ---------------------------------------------------------------------------
// Feedback — a thumb on one assistant turn. Anchored to (session, message)
// so the admin board can always deep-link back to the turn that earned it.
// ---------------------------------------------------------------------------

export interface Feedback {
  id: number;
  created_at: string;
  updated_at: string;
  user_id: number;
  client_id: number | null;
  session_id: number;
  message_id: number;
  /** -1 or +1. */
  rating: number;
  body: string | null;
  status: 'new' | 'triaged' | 'in_progress' | 'resolved' | 'wontfix';
  admin_note: string | null;
  resolved_at: string | null;
  /** Joined in by the admin list only. The excerpt is PII-masked, or
   * "[redacted]" when the turn was flagged sensitive. */
  email?: string;
  message_excerpt?: string;
}

export function sendFeedback(body: {
  session_id: number;
  message_id: number;
  rating: number;
  body?: string;
}) {
  return request<Feedback>('/api/feedback', { method: 'POST', body: JSON.stringify(body) });
}

export function clearFeedback(messageId: number) {
  return request<{ deleted: number }>(`/api/feedback/message/${messageId}`, { method: 'DELETE' });
}

/** `{ [messageId]: rating }` for the turns this user already rated here. */
export function getSessionFeedback(sessionId: number) {
  return request<{ ratings: Record<string, number> }>(`/api/sessions/${sessionId}/feedback`).then(
    (r) => r.ratings,
  );
}

// ---------------------------------------------------------------------------
// Admin console — operator-only. Every call here 403s for a normal account;
// the frontend guard is a convenience, the server check is the gate.
//
// Under /api/admin, while the console's own screens live at /admin: one path
// can't be both an API prefix and a SPA route (see backend/main.py).
// ---------------------------------------------------------------------------

export interface AdminDay {
  day: string;
  messages: number;
  turns: number;
  errors: number;
}

export interface AdminOverview {
  users_total: number;
  users_active_7d: number;
  users_active_30d: number;
  clients_total: number;
  sessions_total: number;
  messages_24h: number;
  messages_7d: number;
  messages_30d: number;
  turns_7d: number;
  errors_7d: number;
  /** null until at least one turn has been recorded — not the same as 0. */
  error_rate_7d: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  menus_7d: number;
  feedback_open: number;
  thumbs_up_30d: number;
  thumbs_down_30d: number;
  daily: AdminDay[];
  billing: BillingHealth;
  hosted_1d: HostedUsage;
  hosted_7d: HostedUsage;
  /** usage_ledger rows charged on a chars/4 estimate in the last 7 days —
   * non-zero means a provider stopped reporting token usage. */
  estimated_rows_7d: number;
}

export interface BillingHealth {
  mrr_cents: number;
  active: number;
  past_due: number;
  canceled: number;
  comp: number;
  free: number;
  unprocessed_webhooks: number;
  credit_outstanding_cents: number;
}

export interface HostedUsage {
  days: number;
  tokens_in: number;
  tokens_out: number;
  cost_cents: number;
  charged_cents: number;
  gross_margin_pct: number | null;
  turns: number;
  estimated_rows: number;
  top_users: { user_id: number; email: string | null; charge_cents: number }[];
}

export function adminOverview() {
  return request<AdminOverview>('/api/admin/overview');
}

export interface AdminUser {
  id: number;
  email: string;
  name: string | null;
  practice: string | null;
  is_admin: boolean;
  created_at: string;
  client_count: number;
  session_count: number;
  message_count: number;
  last_active: string | null;
  errors_7d: number;
  plan: 'free' | 'pro';
  billing_status: BillingStatus;
  country: string | null;
  paid_months: number;
  loyalty_tier: string | null;
  llm_mode: LlmMode;
  llm_provider_id: number | null;
  credit_cents: number;
  email_verified: boolean;
  deleted_at: string | null;
  has_stripe_customer: boolean;
  /** Conversations with at least one message the classifier flagged. */
  sensitive_sessions: number;
}

/** Client names are never sent to the console — ids only. */
export interface AdminSessionRow {
  id: number;
  title: string;
  client_id: number;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_at: string | null;
  sensitive: boolean;
}

export interface AdminUserDetail extends AdminUser {
  clients: {
    id: number;
    status: string;
    is_self: boolean;
    created_at: string;
    session_count: number;
  }[];
  sessions: AdminSessionRow[];
}

export interface LedgerRow {
  id: number;
  ts: string;
  kind: string;
  model: string;
  tokens_in: number;
  tokens_out: number;
  cost_cents: number;
  charge_cents: number;
  provider_name: string | null;
  event_id: number | null;
}

export function adminUserLedger(id: number) {
  return request<{ ledger: LedgerRow[] }>(`/api/admin/users/${id}/ledger`).then((r) => r.ledger);
}

export function adminSetBilling(id: number, billingStatus: 'comp' | 'free') {
  return request<AdminUser>(`/api/admin/users/${id}/billing`, {
    method: 'POST',
    body: JSON.stringify({ billing_status: billingStatus }),
  });
}

export function adminAddCredit(id: number, cents: number, note?: string) {
  return request<AdminUser>(`/api/admin/users/${id}/credit`, {
    method: 'POST',
    body: JSON.stringify({ cents, note }),
  });
}

export function adminSetUserLlm(id: number, body: { llm_mode?: LlmMode; llm_provider_id?: number | null }) {
  const payload: Record<string, unknown> = {};
  if (body.llm_mode) payload.llm_mode = body.llm_mode;
  if (body.llm_provider_id === null) payload.clear_provider = true;
  else if (body.llm_provider_id != null) payload.llm_provider_id = body.llm_provider_id;
  return request<AdminUser>(`/api/admin/users/${id}/llm`, { method: 'POST', body: JSON.stringify(payload) });
}

// -- prices / loyalty / providers / invites / audit ---------------------------

export interface PlanPrice {
  id: number;
  plan: string;
  interval: 'month' | 'year';
  country: string;
  amount_cents: number;
  stripe_price_id: string | null;
  active: number;
  updated_at: string;
}

export function adminPrices() {
  return request<{ prices: PlanPrice[] }>('/api/admin/prices').then((r) => r.prices);
}

export function adminCreatePrice(body: { plan?: string; interval: 'month' | 'year'; country: string; amount_cents: number }) {
  return request<PlanPrice>('/api/admin/prices', { method: 'POST', body: JSON.stringify(body) });
}

export function adminUpdatePrice(id: number, amountCents: number) {
  return request<PlanPrice>(`/api/admin/prices/${id}`, {
    method: 'PUT',
    body: JSON.stringify({ amount_cents: amountCents }),
  });
}

export function adminDeletePrice(id: number) {
  return request<{ deleted: number }>(`/api/admin/prices/${id}`, { method: 'DELETE' });
}

export interface LoyaltyTier {
  id: number;
  tier: string;
  min_paid_months: number;
  percent_off: number;
  stripe_coupon_id: string | null;
  active: number;
}

export function adminLoyalty() {
  return request<{ ladder: LoyaltyTier[] }>('/api/admin/loyalty').then((r) => r.ladder);
}

export function adminUpsertTier(tier: string, body: { min_paid_months: number; percent_off: number; active: boolean }) {
  return request<LoyaltyTier>(`/api/admin/loyalty/${encodeURIComponent(tier)}`, {
    method: 'PUT',
    body: JSON.stringify({ tier, ...body }),
  });
}

export interface StripeEventRow {
  id: string;
  type: string;
  received_at: string;
  processed: number;
  error: string | null;
}

export function adminStripeEvents() {
  return request<{ events: StripeEventRow[] }>('/api/admin/stripe-events').then((r) => r.events);
}

export interface AuditRow {
  id: number;
  ts: string;
  admin_id: number;
  admin_email: string | null;
  action: string;
  target: string | null;
  meta_json: string | null;
}

export function adminAudit(limit = 200) {
  return request<{ audit: AuditRow[] }>(`/api/admin/audit?limit=${limit}`).then((r) => r.audit);
}

export interface LlmProvider {
  id: number;
  name: string;
  base_url: string;
  model: string;
  key_last4: string;
  price_in_per_mtok: number;
  price_out_per_mtok: number;
  markup_pct: number;
  user_price_in_per_mtok: number;
  user_price_out_per_mtok: number;
  is_default: boolean;
  enabled: boolean;
  updated_at: string;
}

export function adminProviders() {
  return request<{ providers: LlmProvider[] }>('/api/admin/llm-providers').then((r) => r.providers);
}

export interface ProviderBody {
  name: string;
  base_url: string;
  model: string;
  api_key?: string;
  price_in_per_mtok: number;
  price_out_per_mtok: number;
  markup_pct: number;
  enabled?: boolean;
}

export function adminCreateProvider(body: ProviderBody) {
  return request<LlmProvider>('/api/admin/llm-providers', { method: 'POST', body: JSON.stringify(body) });
}

export function adminUpdateProvider(id: number, patch: Partial<ProviderBody>) {
  return request<LlmProvider>(`/api/admin/llm-providers/${id}`, { method: 'PATCH', body: JSON.stringify(patch) });
}

export function adminDefaultProvider(id: number) {
  return request<LlmProvider>(`/api/admin/llm-providers/${id}/default`, { method: 'POST' });
}

export function adminTestProvider(id: number) {
  return request<{ ok: boolean; latency_ms: number; detail: string }>(`/api/admin/llm-providers/${id}/test`, {
    method: 'POST',
  });
}

export interface InviteCode {
  code: string;
  created_by: number | null;
  created_at: string;
  expires_at: string | null;
  max_uses: number | null;
  uses: number;
  note: string | null;
}

export function adminInvites() {
  return request<{ invites: InviteCode[]; signup_mode: 'invite' | 'open' }>('/api/admin/invites');
}

export function adminCreateInvite(body: { max_uses: number | null; expires_days: number | null; note?: string }) {
  return request<InviteCode>('/api/admin/invites', { method: 'POST', body: JSON.stringify(body) });
}

export function adminRevokeInvite(code: string) {
  return request<{ revoked: string }>(`/api/admin/invites/${encodeURIComponent(code)}`, { method: 'DELETE' });
}

export function adminUsers() {
  return request<{ users: AdminUser[] }>('/api/admin/users').then((r) => r.users);
}

export function adminUser(id: number) {
  return request<AdminUserDetail>(`/api/admin/users/${id}`);
}

export interface AdminTranscriptMessage {
  id: number;
  role: 'user' | 'assistant';
  /** null when redacted. Otherwise PII-masked unless the transcript was
   * fetched with reveal. */
  content: string | null;
  created_at: string;
  /** The thumb this turn got, if any — shown inline on the transcript. */
  feedback: { rating: number; body: string | null; status: string } | null;
  sensitivity: 'none' | 'sensitive' | 'unknown' | null;
  categories: string[];
  redacted: boolean;
}

export interface AdminTranscript {
  id: number;
  title: string;
  client_id: number;
  user_id: number;
  email: string;
  created_at: string;
  updated_at: string;
  messages: AdminTranscriptMessage[];
  redacted_count: number;
  revealed: boolean;
}

/** reveal=true returns flagged messages verbatim. The server audits it and
 * shows the affected user "Support viewed session N" on their Account page —
 * so only do it for a real support case. */
export function adminTranscript(sessionId: number, reveal = false) {
  return request<AdminTranscript>(`/api/admin/sessions/${sessionId}${reveal ? '?reveal=1' : ''}`);
}

export interface AdminEvent {
  id: number;
  ts: string;
  kind: string;
  user_id: number | null;
  email: string | null;
  client_id: number | null;
  session_id: number | null;
  model: string | null;
  latency_ms: number | null;
  tool_calls: number | null;
  /** Null whenever the endpoint didn't report usage — treat as unknown. */
  tokens_in: number | null;
  tokens_out: number | null;
  ok: boolean;
  error_type: string | null;
  error_detail: string | null;
}

export function adminEvents(params: {
  kind?: string;
  errorsOnly?: boolean;
  userId?: number;
  days?: number;
  limit?: number;
} = {}) {
  const qs = new URLSearchParams();
  if (params.kind) qs.set('kind', params.kind);
  if (params.errorsOnly) qs.set('errors_only', 'true');
  if (params.userId != null) qs.set('target_user_id', String(params.userId));
  if (params.days != null) qs.set('days', String(params.days));
  qs.set('limit', String(params.limit ?? 200));
  return request<{ events: AdminEvent[] }>(`/api/admin/events?${qs.toString()}`).then((r) => r.events);
}

export function adminLogTail(lines = 200) {
  return request<{ path: string; lines: string[]; detail?: string }>(
    `/api/admin/logs/tail?lines=${lines}`,
  );
}

/** `status` accepts a literal status, or 'open' for everything not closed. */
export function adminFeedback(status?: string) {
  return request<{ feedback: Feedback[] }>(
    `/api/admin/feedback${status ? `?status=${encodeURIComponent(status)}` : ''}`,
  ).then((r) => r.feedback);
}

export function adminUpdateFeedback(id: number, patch: { status?: string; admin_note?: string }) {
  return request<Feedback>(`/api/admin/feedback/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  });
}
