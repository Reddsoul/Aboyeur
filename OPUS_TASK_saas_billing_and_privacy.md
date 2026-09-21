# Task: Aboyeur private beta → hosted SaaS (billing, hosted LLM, privacy, legal)

Aboyeur: FastAPI backend (`backend/main.py`, business logic in `aboyeur/`),
React/Vite SPA (`frontend/`), SQLite (`aboyeur/db.py`, WAL, one shared
connection, **single uvicorn worker — keep it that way**), LLM via
OpenAI-compatible endpoints (`aboyeur/llm.py`). Users are practitioners
(`users.practice`) who manage `clients` (their own profile is a client row
with `is_self=1`). Today: invite-only via `aboyeur create-user`, per-user
BYOK LLM (`aboyeur/llm_settings.py`, Fernet-encrypted keys), admin console
at `/admin` (SPA) + `/api/admin/*` (backend), per-turn token usage already
recorded in `events` (`tokens_in`, `tokens_out`, `model`, `latency_ms`).

Goal of this task: everything needed so the *current beta* can be flipped
to a paid, self-serve, hosted product without a rewrite. Work in the order
below — each phase is independently shippable to the beta and must leave
`pytest` green with new tests. Commit per phase (or finer). Do not touch
`/Volumes/docker`.

Read first (background only):
`~/.claude/projects/-Volumes-General-Coding-Python-Aboyeur/memory/aboyeur-admin-console.md`,
`aboyeur-turn-runner.md`, `aboyeur-beta-runbook.md`.

Secrets: Stripe **test** keys are already in `.env` (`STRIPE_SECRET_KEY`,
`STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` placeholder). `.env` is
gitignored. Never commit keys, never log them, never echo them in the UI.

---

## Decisions already made (do not re-litigate; flag if they break)

| Topic | Decision | Why |
|---|---|---|
| Hosting | One Hetzner CPX21 (US), Docker compose + Caddy + Litestream→B2 | Same stack as homelab; cheapest; upgrade in place |
| DB | Stay on SQLite, single worker | 179 MB, WAL, one process. Postgres only if one box stops being enough |
| Subscriptions | Stripe Checkout + Customer Portal + webhooks. No custom subscription UI | Stripe hosts card entry, portal, dunning, receipts |
| Per-country pricing | Our `plan_prices` table is source of truth; each row maps to one Stripe Price (USD). Admin edits → new Stripe Price created, old archived | Stripe Prices are immutable; country lookup is our concern, not Stripe's |
| Loyalty pricing | Tenure ladder → Stripe coupon swapped on the subscription at tier boundaries. Lapse resets tenure | Stripe has no native tenure pricing; coupons are the cheapest faithful model |
| Comped users | `users.billing_status='comp'` — app-side bypass, no Stripe object | Zero Stripe complexity for a handful of people |
| Hosted-LLM billing | **Prepaid credit wallet**, topped up via Stripe Checkout one-time payments, debited per turn | No negative balances, no dunning on overages, no 2-week Stripe-meter invoice lag, no chargeback exposure. (Postpaid Stripe Billing Meters = option B, not built now) |
| Providers | All hosted providers are OpenAI-compatible endpoints (OpenAI native, Anthropic's OpenAI-compat endpoint `https://api.anthropic.com/v1/`, self-hosted LiteLLM/vLLM/Ollama) | Existing `make_client(base_url, key, model)` works unchanged; no second SDK |
| HIPAA | Aboyeur is **not** a covered entity and offers **no BAA**. ToS forbids use as/for a covered entity's PHI. Product positioning: coaches, trainers, nutrition consultants, self-tracking — not clinical care | Only way to "get nowhere near HIPAA". Hetzner signs no BAA; being a Business Associate would force AWS/Azure + audit program. Revisit only as a deliberate future product line |
| Sensitivity flagging | Classifier runs on the user's own configured LLM (BYOK or hosted), async, after each user message. Admin sees a ★ marker, never the content by default | Requested. It is a UX/ops safeguard, not a compliance control — say so in docs |

---

## Phase 0 — Foundation (in beta now)

### 0.1 Schema migrations (`aboyeur/migrate.py`, `aboyeur/db.py`)

Add, idempotently (follow the existing "check column exists, then ALTER"
pattern in `migrate.py`):

```sql
-- users
ALTER TABLE users ADD COLUMN billing_status TEXT NOT NULL DEFAULT 'free';
  -- free | active | past_due | canceled | comp
ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free';   -- free | pro
ALTER TABLE users ADD COLUMN country TEXT;                        -- ISO-3166 alpha-2, user-editable
ALTER TABLE users ADD COLUMN stripe_customer_id TEXT UNIQUE;
ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT;
ALTER TABLE users ADD COLUMN paid_months INTEGER NOT NULL DEFAULT 0;   -- consecutive paid months (loyalty tenure)
ALTER TABLE users ADD COLUMN loyalty_tier TEXT;                   -- current ladder step id, NULL = base price
ALTER TABLE users ADD COLUMN llm_mode TEXT NOT NULL DEFAULT 'byok';   -- byok | hosted
ALTER TABLE users ADD COLUMN llm_provider_id INTEGER REFERENCES llm_providers(id);  -- NULL = platform default
ALTER TABLE users ADD COLUMN credit_cents INTEGER NOT NULL DEFAULT 0;  -- hosted-LLM wallet
ALTER TABLE users ADD COLUMN email_verified_at TEXT;
ALTER TABLE users ADD COLUMN tos_version TEXT;
ALTER TABLE users ADD COLUMN tos_accepted_at TEXT;
ALTER TABLE users ADD COLUMN deleted_at TEXT;                     -- soft delete, purge job hard-deletes after 30d

-- chat_messages
ALTER TABLE chat_messages ADD COLUMN sensitivity TEXT;   -- NULL (unclassified) | none | sensitive | unknown
ALTER TABLE chat_messages ADD COLUMN sensitivity_json TEXT;  -- classifier categories, no content

CREATE TABLE IF NOT EXISTS plan_prices (
    id               INTEGER PRIMARY KEY,
    plan             TEXT NOT NULL,             -- pro
    interval         TEXT NOT NULL,             -- month | year
    country          TEXT NOT NULL,             -- ISO-2 or '*' for default
    amount_cents     INTEGER NOT NULL,          -- USD
    stripe_price_id  TEXT,                      -- created lazily on first save
    active           INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL,
    UNIQUE(plan, interval, country, active)     -- one active row per combo
);

CREATE TABLE IF NOT EXISTS loyalty_ladder (
    id               INTEGER PRIMARY KEY,
    tier             TEXT NOT NULL UNIQUE,      -- m3 | m6 | m12 | ...
    min_paid_months  INTEGER NOT NULL,
    percent_off      INTEGER NOT NULL,          -- 0-100
    stripe_coupon_id TEXT,                      -- created lazily (duration=forever)
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS llm_providers (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,   -- "OpenAI gpt-4.1-mini", "Claude Haiku 4.5", "DGX LiteLLM fast"
    base_url            TEXT NOT NULL,
    model               TEXT NOT NULL,
    api_key_encrypted   TEXT NOT NULL,          -- Fernet via aboyeur/crypto.py
    price_in_per_mtok   INTEGER NOT NULL,       -- our cost, cents per 1M input tokens
    price_out_per_mtok  INTEGER NOT NULL,
    markup_pct          INTEGER NOT NULL DEFAULT 30,  -- what the user pays = cost * (1 + markup/100)
    is_default          INTEGER NOT NULL DEFAULT 0,   -- exactly one row may be 1
    enabled             INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id       INTEGER REFERENCES events(id),
    provider_id    INTEGER REFERENCES llm_providers(id),
    model          TEXT NOT NULL,
    tokens_in      INTEGER NOT NULL,
    tokens_out     INTEGER NOT NULL,
    cost_cents     INTEGER NOT NULL,            -- our cost at time of use (integer micro-rounding: round half up)
    charge_cents   INTEGER NOT NULL,            -- what we debited from credit_cents
    kind           TEXT NOT NULL                -- chat_turn | classify | topup | adjust | refund
);
CREATE INDEX IF NOT EXISTS idx_usage_user_ts ON usage_ledger(user_id, ts);

CREATE TABLE IF NOT EXISTS stripe_events (
    id          TEXT PRIMARY KEY,               -- Stripe event id, for idempotency
    type        TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    admin_id  INTEGER NOT NULL,
    action    TEXT NOT NULL,                    -- reveal_transcript | set_comp | set_provider | edit_price | ...
    target    TEXT,                             -- user:12 / session:44 / price:3
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS email_tokens (
    token      TEXT PRIMARY KEY,                -- urlsafe 32 bytes
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL,                   -- verify | reset
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
```

Seed `loyalty_ladder` with `m3/10%`, `m6/15%`, `m12/25%` and
`plan_prices` with `pro/month/*/1000` and `pro/year/*/8000`. Both editable
in admin; seeds are placeholders.

### 0.2 Config + secrets (`aboyeur/config.py`, `.env.example`)

- Remove the hardcoded Tailscale default at `config.py:41`. `LLM_BASE_URL`
  with no value = no platform fallback (BYOK required) — never a private IP.
- New env: `STRIPE_*` (present), `RESEND_API_KEY`, `EMAIL_FROM`,
  `APP_BASE_URL` (for links in emails / Stripe return URLs),
  `DATA_KEY` (Fernet, for PII at rest — separate from `LLM_SETTINGS_KEY`
  so rotating one doesn't touch the other), `SIGNUP_MODE`
  (`invite` | `open`, default `invite`), `SENTRY_DSN` (optional).
- Startup refuses to boot in `COOKIE_SECURE=true` mode with an empty
  `JWT_SECRET`, `DATA_KEY`, or `LLM_SETTINGS_KEY`.

### 0.3 Rate limits + quotas (`backend/main.py`)

- `slowapi` (add to `pyproject.toml` + `backend/requirements.txt`). Key by
  user id when authenticated, else client IP (respect `CF-Connecting-IP`
  when behind Cloudflare — read it only if `TRUST_CF_HEADERS=true`).
  - `/auth/login`: 10/min/IP. `/auth/signup`, `/auth/reset`: 5/hour/IP.
  - `/chat`, `/chat/stream`: 30/min/user.
  - `/api/llm-settings/test`: 10/hour/user.
- Plan quotas, enforced in the chat entrypoint before a turn starts:
  `free` = 20 turns/30 days (rolling, counted from `events` where
  `kind='chat_turn' AND ok=1`), `pro`/`comp` = unlimited turns. BYOK
  users on `free` are unlimited (they pay their own tokens). Over quota
  → HTTP 402 `{"error":"quota","upgrade_url":"/account/billing"}`.
  Hosted-LLM users additionally need `credit_cents > 0` (see Phase 2).

### 0.4 Compose + deploy skeleton

Rewrite `docker-compose.yml` for the real topology:
`caddy` (ports 80/443, `./Caddyfile`, volumes for certs), `backend`
(no host port; single `uvicorn` worker; `DB_PATH=/data/aboyeur.db` on a
named volume, **never** a bind-mount over SMB), `litestream` (sidecar
replicating `/data/aboyeur.db` to B2 — `litestream.yml` from env),
`frontend` as a multi-stage build whose `dist/` Caddy serves. Delete the
dead `watcher`/`ingest` service. Drop `chromadb` unless
`grep -rn "rag\." backend/ aboyeur/` shows a live caller in the chat or
recipe-search path — `backend/main.py:64` already says chat doesn't use it;
if only `/health` reads `rag.count()`, remove it and the dependency.

`Caddyfile`:

```
{$APP_DOMAIN} {
    encode zstd gzip
    handle /api/*  { reverse_proxy backend:8000 }
    handle /auth/* { reverse_proxy backend:8000 }
    handle /chat*  { reverse_proxy backend:8000 }
    handle /books* { reverse_proxy backend:8000 }
    handle /pantry* { reverse_proxy backend:8000 }
    handle /menu*  { reverse_proxy backend:8000 }
    handle /household* { reverse_proxy backend:8000 }
    handle /macros* { reverse_proxy backend:8000 }
    handle /preferences* { reverse_proxy backend:8000 }
    handle /ingest* { reverse_proxy backend:8000 }
    handle { root * /srv; try_files {path} /index.html; file_server }
    header {
        Strict-Transport-Security "max-age=31536000"
        X-Content-Type-Options nosniff
        Referrer-Policy strict-origin-when-cross-origin
        -Server
    }
}
```

(Better: move every backend route under `/api/` in one sweep and keep the
Caddyfile to two `handle` blocks. Do that if it's under ~2 hours of
mechanical renaming incl. `frontend/src/api.ts` and `vite.config.ts`.)

Acceptance: `docker compose up --build` on a clean Ubuntu VM (or a local
Multipass/OrbStack VM) serves the SPA + API on HTTPS with a self-signed
cert when `APP_DOMAIN=localhost`. Document in `DEPLOYMENT.md`.

### 0.5 Observability

- `/health` returns `{ok, db, litestream_lag_s?, version}`; no secrets.
- Sentry SDK (FastAPI + React) behind `SENTRY_DSN`; `send_default_pii=False`;
  scrub `content`, `email`, `api_key`, `Authorization` via `before_send`.
- Structured JSON logs. **No chat content in logs at any level.** Grep the
  codebase for `logger.*content` and fix.

---

## Phase 1 — Subscriptions (Stripe)

New module `aboyeur/billing.py` (pure functions over `sqlite3.Connection`
+ a thin Stripe client wrapper `aboyeur/stripe_client.py` so tests can
stub it). Routes in `backend/main.py` under `/api/billing/*`. Add `stripe`
to dependencies.

### 1.1 Country resolution

`resolve_country(request, user) -> str`: `users.country` if set, else
`CF-IPCountry` header (only when `TRUST_CF_HEADERS=true`), else `'*'`.
User can change country on the Account page; changing it after subscribing
does **not** reprice an active subscription (takes effect at next
checkout). Store the country used on `stripe_customer` metadata.

### 1.2 Price lookup

`get_price(conn, plan, interval, country) -> plan_prices row`: exact
country match with `active=1`, else `'*'`. Ensure `stripe_price_id`
exists: if NULL, create a Stripe Product (one per plan, id cached in a
`settings` KV table — add one: `settings(key TEXT PRIMARY KEY, value TEXT)`)
and a Stripe Price (`currency='usd'`, `unit_amount`, `recurring.interval`,
`lookup_key=f"{plan}_{interval}_{country}_{amount_cents}"`,
`metadata={plan, interval, country}`), persist the id.

### 1.3 Checkout + Portal

- `POST /api/billing/checkout {interval}` → creates/gets Stripe Customer
  (`email`, `metadata.user_id`), creates Checkout Session
  (`mode=subscription`, `line_items=[price]`, `automatic_tax` **off** until
  Stripe Tax is enabled by the owner, `success_url`/`cancel_url` under
  `APP_BASE_URL`, `client_reference_id=user_id`,
  `subscription_data.metadata.user_id`). Returns `{url}`. Frontend
  redirects. `comp` users get 409.
- `POST /api/billing/portal` → Customer Portal session, returns `{url}`.
- `GET /api/billing/me` → `{plan, billing_status, interval, renews_at,
  paid_months, loyalty_tier, percent_off, price_cents, country, llm_mode,
  credit_cents}`. Account page renders this; no Stripe JS on the client
  (publishable key unused for now — keep it in env for Elements later).

### 1.4 Webhook `POST /api/billing/webhook`

Verify signature with `STRIPE_WEBHOOK_SECRET`. Insert into `stripe_events`
first (PK = event id) — duplicate → 200 and return. Then handle:

| Event | Action |
|---|---|
| `checkout.session.completed` | link `stripe_customer_id`, `stripe_subscription_id` (if mode=subscription) or credit top-up (if mode=payment, see Phase 2) |
| `customer.subscription.created/updated` | set `plan=pro`, `billing_status` from `status` (`active`/`trialing`→active, `past_due`→past_due, `canceled`/`unpaid`/`incomplete_expired`→canceled + `plan=free`) |
| `customer.subscription.deleted` | `plan=free`, `billing_status=canceled`, **`paid_months=0`, `loyalty_tier=NULL`** |
| `invoice.paid` (subscription invoices, `billing_reason` in `subscription_cycle`/`subscription_create`) | `paid_months += 1`; run loyalty check (1.5) |
| `invoice.payment_failed` | `billing_status=past_due`. Grace: Stripe's own dunning (configure in dashboard: retry 3x over 2 weeks, then cancel). Cancellation event above does the reset |

All handlers idempotent; mark `processed=1` or store `error`. Return 200
even on handler error after recording it (Stripe retries otherwise), and
surface unprocessed events in admin.

### 1.5 Loyalty ladder

`apply_loyalty(conn, user_id)`: pick the highest `loyalty_ladder` row with
`min_paid_months <= paid_months`. If it differs from `users.loyalty_tier`:
ensure the tier's Stripe coupon exists (`percent_off`, `duration=forever`,
`name=f"Loyalty {tier}"`), then `stripe.Subscription.modify(sub_id,
discounts=[{"coupon": coupon_id}])` (replaces any existing discount), set
`loyalty_tier`. On lapse (`subscription.deleted`) the reset above means a
re-subscribe starts at base price — that's the requested behaviour.

Edge cases to handle + test: `paid_months` must not increment on
`invoice.paid` for `billing_reason=manual` or one-time top-ups; a
`past_due` → `active` recovery inside the dunning window keeps tenure; a
subscription that is canceled-at-period-end still gets its final
`invoice.paid`? (no — verify with `stripe trigger`); admin editing a
ladder percent must re-create the coupon (coupons are immutable — archive
old id, create new, re-apply to affected subscriptions lazily on their
next `invoice.paid`, not in bulk).

### 1.6 Comped individuals

Admin: `POST /api/admin/users/{id}/billing {billing_status: 'comp'|'free'}`.
Setting `comp` cancels any live Stripe subscription (`cancel_at_period_end=False`,
`prorate=True`) and sets `plan=pro`. Clearing `comp` → `plan=free`.
Writes `admin_audit`.

### 1.7 Admin pricing UI (`frontend/src/routes/admin/`)

- **Prices** tab: table of `plan_prices` grouped by plan/interval, rows
  per country (`*` default first). Edit amount → `PUT /api/admin/prices/{id}`
  which archives the old row (`active=0`), inserts a new one, creates the
  Stripe Price. Add country row. Country field = ISO-2 select. Existing
  subscribers are **not** repriced (Stripe subscription keeps its Price) —
  show that caveat in the UI.
- **Loyalty** tab: ladder editor (tier, min months, % off).
- **Users** tab additions: plan, status, tenure, tier, comp toggle,
  country, `llm_mode`, provider override, credit balance, "Add credit"
  (admin adjustment, ledger `kind=adjust`, audited).
- **Billing health** tile on overview: MRR (sum of active subs' price ×
  (1-discount)), active/past_due/canceled counts, unprocessed webhook
  events count.

### 1.8 Tests

`tests/test_billing.py` with a stubbed `stripe_client` (record calls,
return fakes). Cover: price lookup fallback, lazy Stripe Price creation,
each webhook branch, idempotent replay, tenure increment/reset, ladder
transitions (2→3 months promotes; cancel resets; resubscribe at base),
comp toggle, quota gate 402. Use `stripe listen --forward-to
localhost:8000/api/billing/webhook` + `stripe trigger` for a manual run;
document in `DEPLOYMENT.md`.

---

## Phase 2 — Hosted LLM with usage billing

### 2.1 Provider registry (`aboyeur/llm_providers.py`)

CRUD over `llm_providers`; keys via `aboyeur/crypto.py`. Admin routes
`/api/admin/llm-providers` (list/create/update/disable/set-default/test).
`test` performs a 1-token completion and reports latency. Never return the
key; return `key_last4`.

### 2.2 Resolution order (replace the block at `backend/main.py:~644`)

```
resolve_llm(conn, user) -> (client, model, provider_id | None, billable: bool)
  if user.llm_mode == 'byok' and llm_settings row exists → BYOK, billable=False
  elif user.llm_mode == 'hosted':
      provider = user.llm_provider_id or default provider (enabled=1)
      if user.plan not in (pro, comp) → 402 "hosted LLM requires Pro"
      if user.credit_cents <= 0 → 402 "no credit" (comp users: unlimited, ledger charge_cents=0)
      → hosted client, billable=True
  else → 409 "configure an LLM in Settings"
```

Settings page: two radio cards — "Use my own API key" (existing form) /
"Use Aboyeur's models (Pro, pay per use)" with the current balance,
"Add credit" button, and the effective per-1K-token price for the
provider they'll get (`cost × (1+markup)`; the provider *name* is shown,
the underlying endpoint is not).

### 2.3 Metering + debit

At the end of every turn (where `record_event` is called with
`tokens_in/out`), if `billable`: compute
`cost_cents = ceil(tokens_in * price_in / 1e6 + tokens_out * price_out / 1e6)`,
`charge_cents = ceil(cost_cents * (1 + markup/100))`, insert
`usage_ledger`, `UPDATE users SET credit_cents = credit_cents - charge_cents`.
Same transaction as the event insert. Balance may go slightly negative on
the last turn — allowed; next turn is blocked by the `<= 0` check. If the
endpoint returns no usage (`StreamCollector.usage is None`), estimate with
`len(text)/4` per side and mark `kind='chat_turn_est'`; surface the count
of estimated rows in admin so a broken provider gets noticed.

The sensitivity classifier (Phase 3) is also metered (`kind=classify`) for
hosted users; for BYOK it runs on their key at no charge from us.

### 2.4 Top-ups

`POST /api/billing/topup {amount_cents}` (allowed: 500, 1000, 2500, 5000)
→ Stripe Checkout `mode=payment`, `metadata={user_id, kind: topup}`. On
`checkout.session.completed` with that metadata: `credit_cents +=`,
ledger `kind=topup`. Refund via Stripe dashboard → `charge.refunded`
webhook → ledger `kind=refund`, debit balance (may go negative; block
until topped up). Low-balance email at < 100¢ (Phase 4 email plumbing),
at most once per 7 days.

### 2.5 Admin

Provider table + per-user override (Users tab). Overview tiles: hosted
tokens/day, our cost/day, charged/day, gross margin %, top 10 users by
charge (id + email only). Ledger view per user.

### 2.6 Tests

Resolution matrix (byok/hosted × free/pro/comp × credit>0/≤0), debit math
with rounding, negative-balance block, estimated-usage path, top-up
webhook, refund webhook, admin override wins over default.

---

## Phase 3 — Privacy, PII, sensitivity flagging

### 3.1 PII at rest

`aboyeur/crypto.py`: add `encrypt_field/decrypt_field` keyed by
`DATA_KEY`. Encrypt: `clients.name/email/phone/notes`,
`profile.data_json`. Migration encrypts existing rows (idempotent: mark
with a `enc:` prefix). All reads go through one accessor module
(`aboyeur/clients.py` already exists — extend) so no raw column reads
leak. FTS is not needed on these fields — verify nothing indexes them.

### 3.2 Admin console redaction (`aboyeur/admin.py`)

- `list_users` / `get_user`: email + name of the *account holder* only.
  Never client names, never profile health data.
- `get_transcript`: every message with `sensitivity IN ('sensitive','unknown')`
  is returned as `{"redacted": true, "sensitivity": ...}` unless the
  request carries `?reveal=1`, which requires `is_admin`, writes
  `admin_audit(action='reveal_transcript', target='session:N')`, and is
  visible to the affected user on their Account page ("Support viewed
  session X on <date>"). Unflagged messages: still mask emails/phones by
  regex before returning.
- `/api/admin/logs/tail`: strip anything matching email/phone/API-key
  patterns.

### 3.3 Sensitivity classifier (`aboyeur/sensitivity.py`)

After each **user** message is persisted (in the turn runner, off the
critical path — a background task on the Turn thread after the stream
ends, so it never delays first-token), call the user's resolved LLM with:

```
System: You are a data-classification filter. Reply with JSON only.
User: Classify this message. sensitive=true if it contains a medical
diagnosis, medication, mental-health condition, pregnancy, disability,
lab results, or identifies a third person by full name together with
any health detail. Categories from: [diagnosis, medication, mental_health,
pregnancy, disability, lab, third_party_health, none].
{"sensitive": bool, "categories": [...]}
Message: <<<content>>>
```

`max_tokens=60`, `temperature=0`. Parse strictly; on any failure or if no
LLM is resolvable, set `sensitivity='unknown'` (treated as sensitive for
redaction). Store categories only, never a summary. Metered per 2.3.
Sessions with ≥1 flagged message show ★ in admin session lists and on the
user's row ("N sensitive sessions"). Document plainly in `admin/README`
(or the admin Help page) that this is a best-effort UX safeguard, not a
compliance control.

### 3.4 Data rights

- `GET /api/account/export` → zip of the user's clients, profiles,
  sessions, menus, ledger as JSON (decrypted, their own data).
- `DELETE /api/account` → soft-delete now (`deleted_at`, sessions
  invalidated, Stripe subscription canceled, customer *retained* for tax
  records), hard purge by a daily job after 30 days (`aboyeur purge`
  CLI subcommand; also run from a compose `cron`-style service or a
  startup check). Feedback rows keep only the user id.
- Retention defaults: `events` 90 days (exists), `stripe_events` 1 year,
  `admin_audit` 2 years, chat sessions until user deletes.

### 3.5 HIPAA posture (documentation + guardrails, no code beyond ToS)

`docs/privacy-posture.md` stating: not a covered entity, no BAA, users
must not process PHI on behalf of a covered entity, no marketing to
clinical settings, data location (Hetzner US), subprocessors (Stripe,
Resend, Backblaze, Cloudflare, chosen LLM providers), encryption at rest
for health fields, admin access audit. Add a one-line "Not for clinical
use / not HIPAA-compliant" note on the signup page and Settings → Data.

---

## Phase 4 — Self-serve accounts + legal

### 4.1 Signup / verify / reset (`aboyeur/auth.py`, `backend/main.py`, SPA)

- `POST /auth/signup {email, password, country?, invite_code?, tos_version}`.
  `SIGNUP_MODE=invite` requires a valid code from a new `invite_codes`
  table (admin-generated, single/multi-use, expiry). `open` ignores it.
  Password ≥ 10 chars, checked against the HIBP k-anonymity range API
  (best-effort, skip on network error).
- Email verification token (24 h) → `email_verified_at`. Unverified
  accounts can log in but cannot start chat turns or checkout.
- Password reset token (1 h). Both via Resend (`aboyeur/mail.py`, one
  `send(to, template, ctx)` with plain-text + HTML templates in
  `aboyeur/templates/`). In dev with no `RESEND_API_KEY`, log the link.
- ToS acceptance required at signup; if `LEGAL_VERSION` (constant in
  `aboyeur/legal.py`) is newer than `users.tos_version`, the SPA shows a
  blocking "updated terms" modal → `POST /api/account/accept-tos`.
- SPA: `Signup.tsx`, `Verify.tsx`, `Reset.tsx`, and a logged-out landing
  page at `/` (`Landing.tsx`: what it does, one screenshot, "Not for
  clinical use", CTA). Login moves to `/login`.

### 4.2 Legal drafts (`legal/*.md`, served at `/legal/{terms,privacy,disclaimer,refunds,acceptable-use}`)

Write them as **DRAFT — requires attorney review before launch** and
render via `react-markdown`. Owner/entity name = placeholder
`[ENTITY NAME]` until the LLC exists. Required content:

- **Terms of Service**: parties; eligibility (18+); account
  responsibilities; subscription terms (auto-renew, cancel anytime via
  portal, effective end of period, no partial refunds except per refund
  policy); loyalty discount terms (continuous paid tenure, resets on
  cancellation or non-payment, discretionary, may change); prepaid
  credits (non-refundable except where required by law, no expiry for 12
  months, non-transferable); BYOK (their keys, their provider's terms,
  their cost); AI output disclaimer (may be inaccurate; verify before
  relying); **no medical advice / not a substitute for a licensed
  professional / not for clinical use / no PHI / no BAA**; user content
  license (limited, to operate the service); third-party content (Project
  Gutenberg texts, public domain; USDA data); acceptable use by reference;
  termination; warranty disclaimer; limitation of liability (cap at 12
  months' fees); indemnity; governing law `[STATE]`; arbitration
  placeholder; changes to terms with notice; contact.
- **Privacy Policy**: data collected (account, billing via Stripe — we
  never see card numbers, chat content, client profiles you enter, usage
  metrics, IP/country); purposes; legal bases (GDPR-style even if
  US-only: contract, legitimate interest, consent); subprocessors list
  with links; retention table (from 3.4); security (encryption at rest
  for health fields, TLS, access audit); your rights (export, delete,
  correct — with the endpoints); children (no under-18); international
  transfer (US hosting); cookies (session only, no ads/analytics
  trackers — keep it true: no GA); CCPA "do not sell" statement (we
  don't); breach notification commitment (72 h); contact.
- **Nutrition & Health Disclaimer**: standalone one-pager, linked from
  every menu/macro view. Informational only; consult a physician before
  dietary change; allergen data may be incomplete; calorie/macro numbers
  are estimates (USDA + model inference); not for medical nutrition
  therapy, eating-disorder treatment, pregnancy, pediatric, or
  condition-specific diets unless supervised by a professional.
- **Refund Policy**: 14-day no-questions refund on first subscription
  payment; renewals non-refundable; credits non-refundable once any part
  is used; how to request.
- **Acceptable Use**: no PHI/covered-entity use; no scraping; no reselling
  hosted LLM access; no abusive/illegal content; rate limits; we may
  suspend.

Also `legal/CHANGELOG.md` with version + date; `LEGAL_VERSION` bumps when
terms or privacy change materially.

### 4.3 Tests

Signup happy path + invite gate + duplicate email; verification gating of
chat; reset token single-use/expiry; ToS re-acceptance gate; export
contains only that user's rows; delete cascades + Stripe cancel call +
purge job.

---

## Phase 5 — Cutover checklist (owner does; write it into `DEPLOYMENT.md`)

1. Hetzner CPX21 (ASH1), Ubuntu 24.04, firewall 22 (owner IP) / 80 / 443,
   daily backups on.
2. Cloudflare: domain, `A` record proxied, SSL Full (strict), optional
   WAF free rules. Set `TRUST_CF_HEADERS=true`.
3. B2 bucket + app key for Litestream; test restore **before** cutover.
4. `.env` on server: all secrets fresh (rotate JWT/Fernet keys — beta
   sessions and stored BYOK keys will need re-entry; announce to beta
   users).
5. Stripe: switch to live keys; create webhook endpoint for
   `https://<domain>/api/billing/webhook`; enable Customer Portal;
   configure dunning; decide Stripe Tax on/off.
6. Resend: verify domain (SPF/DKIM).
7. Copy `aboyeur.db` (stop beta backend first, then `sqlite3 .backup`),
   run `aboyeur migrate`, start stack, smoke-test login as an existing
   beta user, checkout in test mode once, then flip live.
8. `SIGNUP_MODE=invite` for two weeks with beta users on the VPS, then
   `open`.

---

## Out of scope (explicitly)

LLC formation; Stripe Tax / merchant-of-record decision; multi-worker /
Postgres; postpaid metered billing; SEO/SSR for recipe pages; HIPAA
compliance program; native mobile.

## Definition of done

- Every phase's tests green; `pytest` total green.
- Beta instance runs Phases 0–4 with `SIGNUP_MODE=invite`, Stripe in
  test mode, at least one real test checkout, top-up, ladder promotion
  (simulate via `stripe trigger invoice.paid` ×3), cancel → reset,
  comp toggle, provider switch mid-conversation, sensitive-message ★ in
  admin with redaction + reveal audit, export + delete.
- `DEPLOYMENT.md` lets the owner bring up a fresh VPS without reading
  code.
