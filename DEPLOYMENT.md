# Deploying Aboyeur

One VPS, four containers. This document is meant to be enough to bring up a
fresh box without reading code. Sections: local smoke test → first VPS →
Stripe → email → cutover from the beta → day-2 operations.

Topology (`docker-compose.yml`):

```
Cloudflare (DNS, TLS to edge, WAF)
   │
   ▼
caddy :80/:443 ── serves the SPA from /srv (built into the image)
   │                proxies /api/* /auth/* /chat* /profile/* /health
   ▼
backend :8000  ── uvicorn, ONE worker, SQLite on the `data` volume
   │
   ├─ litestream ── streams /data/aboyeur.db to Backblaze B2, metrics :9090
   └─ cron      ── daily `aboyeur purge` + `aboyeur purge-events`
```

Keep it single-worker: one process owns the SQLite file (WAL). Never put
`/data` on a network filesystem.

---

## 0. Local smoke test (any machine with Docker)

```bash
cp .env.example .env
# fill JWT_SECRET, DATA_KEY, LLM_SETTINGS_KEY (generators are in the comments),
# leave APP_DOMAIN=localhost and the Stripe/Resend/B2 keys empty for now.
mkdir -p corpus                       # empty is fine for a smoke test
docker compose up --build
```

Then open <https://localhost> (self-signed cert — accept the warning).
`curl -k https://localhost/health` → `{"ok":true,"db":true,"version":"compose"}`.
Litestream will log replica errors with empty B2 vars; that's expected here.

Create the first admin inside the backend container:

```bash
docker compose exec backend aboyeur create-user you@example.com --admin
docker compose exec backend aboyeur invite --uses 5   # prints a signup link
```

---

## 1. First VPS

1. **Hetzner CPX21**, Ashburn (`ash`), Ubuntu 24.04. Firewall: 22 from your IP
   only, 80 and 443 from anywhere. Turn on Hetzner's daily backups.
2. On the box:
   ```bash
   apt update && apt install -y docker.io docker-compose-v2 git
   useradd -m -s /bin/bash aboyeur && usermod -aG docker aboyeur
   su - aboyeur
   git clone <repo> app && cd app
   cp .env.example .env && chmod 600 .env
   ```
3. **Cloudflare**: add the domain, `A` record → VPS IP, **proxied**. SSL/TLS
   mode "Full (strict)". Optional: enable the free WAF managed rules and
   "Bot fight mode". Caddy obtains a Let's Encrypt cert on first request via
   HTTP-01, which works through Cloudflare's proxy as long as the record is
   orange-clouded *after* the first cert is issued, or use
   [Cloudflare Origin CA](https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/)
   and point `Caddyfile`'s `tls` at the cert files. Set
   `TRUST_CF_HEADERS=true` in `.env` — rate limits and country pricing then
   use `CF-Connecting-IP` / `CF-IPCountry`.
4. **Backblaze B2**: create a private bucket and an application key scoped to
   it. Fill `LITESTREAM_*` in `.env` (`LITESTREAM_B2_ENDPOINT` looks like
   `https://s3.us-west-004.backblazeb2.com`).
5. **`.env`** — every secret fresh:
   ```
   APP_DOMAIN=app.yourdomain.com
   APP_BASE_URL=https://app.yourdomain.com
   COOKIE_SECURE=            # leave blank/true — the app refuses to boot with
                             # COOKIE_SECURE=true and an empty JWT_SECRET,
                             # DATA_KEY or LLM_SETTINGS_KEY
   JWT_SECRET=               python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   DATA_KEY=                 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   LLM_SETTINGS_KEY=         (same generator, different value)
   LLM_BASE_URL=             # EMPTY on the hosted deploy — no platform fallback
   SIGNUP_MODE=invite
   TRUST_CF_HEADERS=true
   ```
6. **Recipe corpus**: the DB only holds the index; `get_recipe` reads the
   markdown files. Copy them once (and again whenever you re-index):
   ```bash
   rsync -a --delete /Volumes/General/Cookbooks/Recipes/ aboyeur@vps:app/corpus/
   ```
   Compose mounts `./corpus` read-only at `/corpus`; stored paths from the Mac
   are re-rooted under `RECIPES_DIR=/corpus` automatically.
7. `docker compose up -d --build` and check `docker compose logs -f backend`
   for the JSON `startup` line and `curl https://app.yourdomain.com/health`.

---

## 2. Stripe

Test mode first, live keys only at cutover.

1. Dashboard → Developers → API keys → `STRIPE_SECRET_KEY`,
   `STRIPE_PUBLISHABLE_KEY` (unused today, kept for Elements later).
2. Developers → Webhooks → Add endpoint
   `https://app.yourdomain.com/api/billing/webhook` with events:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `invoice.paid`, `invoice.payment_failed`, `charge.refunded`.
   Copy the signing secret → `STRIPE_WEBHOOK_SECRET`.
3. Settings → Billing → **Customer portal**: enable; allow cancel (at period
   end), update payment method, view invoices. Disallow plan switching (our
   prices are per-country; users re-checkout instead).
4. Settings → Billing → **Subscriptions and emails** → Manage failed
   payments: retry 3× over 2 weeks, then cancel the subscription. The
   resulting `customer.subscription.deleted` is what resets loyalty tenure.
5. Stripe Tax: off by default (`automatic_tax` is disabled in code). Decide
   with the accountant; turning it on is a one-line change in
   `billing.create_subscription_checkout`.
6. Products/Prices/Coupons are created **lazily by the app** from
   `plan_prices` and `loyalty_ladder` (admin → Billing). Don't create them by
   hand in the dashboard.

Local end-to-end run against Stripe test mode:

```bash
stripe listen --forward-to localhost:8000/api/billing/webhook   # prints whsec_… → .env
uv run uvicorn backend.main:app --port 8000
# In the app: Account → Go Pro → test card 4242 4242 4242 4242
stripe trigger invoice.paid              # ×3 → user promoted to m3 (10% coupon)
stripe trigger customer.subscription.deleted   # → plan free, tenure 0
```

`stripe trigger` fixtures create their own customer/subscription with no
`metadata.user_id`; to exercise the real path, use the app's checkout and
then trigger events against **that** subscription id from the dashboard
(Subscriptions → … → "Send test webhook" isn't available for invoices, so
advance the test clock instead: Billing → Test clocks). Unprocessed events
show under admin → Billing → Webhooks.

---

## 3. Email (Resend)

1. Add and verify the sending domain (SPF + DKIM records in Cloudflare DNS).
2. API key → `RESEND_API_KEY`; `EMAIL_FROM=Aboyeur <no-reply@yourdomain.com>`.
3. Without a key the backend logs the verification/reset link instead of
   sending (dev only — a hosted deploy without email locks new users out of
   chat).

---

## 4. Cutover from the beta

The beta runs uvicorn + vite on the Mac Mini over Tailscale. Order matters:
the new code encrypts client PII on its first migration, so set `DATA_KEY`
**before** the first start anywhere, including a beta restart.

1. Fresh secrets in the VPS `.env` (step 1.5). Beta users' sessions and stored
   BYOK keys will not carry over (different `JWT_SECRET` /
   `LLM_SETTINGS_KEY`) — announce that they need to sign in again and
   re-enter their API key.
2. Stop the beta backend (`pkill -f "uvicorn backend.main"`), then take a
   consistent copy:
   ```bash
   sqlite3 ~/.local/share/aboyeur/aboyeur.db ".backup /tmp/aboyeur.db"
   scp /tmp/aboyeur.db aboyeur@vps:/tmp/
   ```
3. On the VPS, before the stack is up for real:
   ```bash
   docker compose run --rm -v /tmp/aboyeur.db:/tmp/aboyeur.db backend \
     sh -c 'cp /tmp/aboyeur.db /data/aboyeur.db && aboyeur migrate'
   ```
   `aboyeur migrate` adds the SaaS columns, seeds prices/ladder, marks
   pre-existing accounts as email-verified, and encrypts client PII.
4. `docker compose up -d --build`. Smoke-test as an existing beta user: log
   in, open a client, send one chat turn (BYOK key must be re-entered).
5. Stripe in **test** mode: one real checkout, one top-up, then flip
   `STRIPE_SECRET_KEY` / webhook secret to live keys and restart the backend.
6. `SIGNUP_MODE=invite` for the first two weeks (admin → Invites), then
   `open`.
7. `aboyeur set-admin you@example.com` if the admin flag didn't carry over.

Restoring from B2 (test this **before** cutover):

```bash
docker compose run --rm litestream restore -config /etc/litestream.yml -o /data/aboyeur.db /data/aboyeur.db
```

---

## 5. Day-2 operations

| Task | How |
|---|---|
| Deploy a change | `git pull && docker compose up -d --build` (backend restart drops in-flight chat turns — do it at a quiet hour) |
| Logs | `docker compose logs -f backend` (JSON lines; no chat content, PII masked) |
| Health | `GET /health` → `{ok, db, litestream_lag_s, version}`; alert if `litestream_lag_s` > 60 |
| Grant admin | `docker compose exec backend aboyeur set-admin <email>` |
| Invite codes | admin → Invites, or `aboyeur invite --uses 1 --expires-days 30` |
| Comp a user / add credit / override provider | admin → Users → account |
| Add a hosted model | admin → Models (name shown to users; endpoint hidden) |
| Change prices / loyalty ladder | admin → Billing |
| Rotate `DATA_KEY` | stop backend; `OLD_DATA_KEY=<old> DATA_KEY=<new> aboyeur reencrypt-pii`; start |
| Rotate `LLM_SETTINGS_KEY` | users re-enter BYOK keys (no migration by design) |
| Retention | the `cron` service runs `aboyeur purge` and `aboyeur purge-events` daily |
| Errors | `SENTRY_DSN` (backend) and `VITE_SENTRY_DSN` (build arg for the caddy image) |
| Rate limits | login 10/min/IP · signup + reset 5/h/IP · chat 30/min/user · LLM test 10/h/user. `RATE_LIMIT=off` disables (tests) |

### Legal

`legal/*.md` are **drafts pending attorney review**. Replace `[ENTITY NAME]`,
`[STATE]`, `[CONTACT EMAIL]`, `[POSTAL ADDRESS]`, the arbitration placeholder
and the subprocessor model list before launch, then bump `LEGAL_VERSION` in
`aboyeur/legal.py` and add a row to `legal/CHANGELOG.md`; every user is asked
to re-accept on next visit.
