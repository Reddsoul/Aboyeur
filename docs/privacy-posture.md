# Privacy posture

What Aboyeur is and isn't, from a data-protection point of view. This is the
operator-facing companion to `legal/privacy.md`; the numbers and promises in
both must match.

## Positioning: not a healthcare tool

Aboyeur is a meal-planning and nutrition-estimation assistant for coaches,
personal trainers, nutrition consultants and people tracking their own diet.
It is **not** offered to, marketed to, or designed for clinical settings.

- **Not a HIPAA covered entity.** We do not bill health plans and are not a
  provider, plan or clearinghouse.
- **Not a business associate.** We sign **no BAA**, ever. The Terms of Service
  and Acceptable Use Policy forbid using the Service to create, receive,
  maintain or transmit PHI on behalf of a covered entity or business
  associate, and forbid use in clinical settings.
- **No marketing to clinical settings.** No dietitian-in-hospital, no
  "medical nutrition therapy", no EHR integrations. If that product line is
  ever wanted it is a separate, deliberate decision that would require AWS/
  Azure hosting with a BAA, an audit program, and a rewrite of this document.

Why: Hetzner signs no BAA. Being a business associate would force a hosting
migration, a compliance program, and legal exposure that a one-person SaaS
cannot carry. The only way to "get nowhere near HIPAA" is to not be in
healthcare, and to say so in the contract.

## Data location

Single Hetzner CPX21 in Ashburn, Virginia (US). SQLite on the box; Litestream
streams the WAL to a Backblaze B2 bucket (US). Cloudflare terminates TLS at
its edge worldwide and forwards to the origin.

## Subprocessors

| Who | What they see |
|---|---|
| Hetzner | Everything at rest on the VPS (disk encrypted by Hetzner; PII fields additionally encrypted by us) |
| Backblaze B2 | Litestream replica of the SQLite DB (PII fields encrypted; the DATA_KEY is **not** in the bucket) |
| Cloudflare | Request metadata, TLS; no bodies retained |
| Stripe | Email, name, country, card (we never see the card), invoices |
| Resend | Email address + the transactional message body (verification/reset links, low-balance notice) |
| Hosted LLM providers (admin-configured, e.g. OpenAI, Anthropic) | The chat messages and system prompt of **hosted-mode** users, per turn; and the message text sent to the sensitivity classifier |
| The user's own provider (BYOK) | Same as above, but it's their contract, not ours |
| Sentry (optional) | Exceptions + stack traces; PII scrubbed in `before_send` on both ends |

## Encryption at rest

`clients.name`, `clients.email`, `clients.phone`, `clients.notes` and
`profile.data_json` (household anthropometrics, goals, allergies) are
Fernet-encrypted under `DATA_KEY` with an `enc:` prefix. Reads go through
`aboyeur/clients.py` and `aboyeur/tools/profile.py` only. `DATA_KEY` is
separate from `LLM_SETTINGS_KEY` (BYOK API keys) and `JWT_SECRET` so each
can be rotated on its own; `aboyeur reencrypt-pii` handles a DATA_KEY
rotation. Chat message content is **not** encrypted at rest (it is searched
and rendered constantly; it is protected by disk encryption and access
control instead) — this is a known gap to revisit.

## Operator access and audit

- The admin console shows the **account holder's** email and name only. It
  never shows client names, contact details or profile/health data — client
  rows appear as ids.
- Transcripts: every user message is classified after the turn by the user's
  own LLM (`aboyeur/sensitivity.py`). Messages labelled `sensitive`, or
  `unknown` (classifier failed), and the assistant reply that follows each,
  are redacted from the operator by default. Unflagged lines have emails,
  phone numbers and key-shaped strings masked.
- `?reveal=1` returns the flagged text. It writes `admin_audit`
  (`reveal_transcript`, 2-year retention) **and** `support_access`, which the
  affected user sees on their Account page ("Support viewed session N on
  <date>") and in their data export.
- The log tail endpoint masks the same patterns. No chat content is ever
  logged (`aboyeur/logging_setup.py` masks as a second net).
- Comp toggles, credit adjustments, provider/price/ladder edits and invite
  minting are all audited.

**The classifier is a UX/ops safeguard, not a compliance control.** It is
best-effort, depends on whichever model the user configured, and fails
closed (unknown → redacted). It does not make the Service suitable for PHI;
the ToS prohibition does that.

## Retention

| Data | Kept |
|---|---|
| Accounts, clients, profiles, conversations, menus | Until the user deletes them |
| Soft-deleted accounts | 30 days, then hard-purged (`aboyeur purge`, cron + on boot) |
| `events` (turn telemetry) | 90 days (`aboyeur purge-events`) |
| `usage_ledger` | Life of the account |
| `stripe_events` | 1 year |
| `admin_audit` | 2 years |
| `support_access` | Life of the account |
| Caddy access logs | 14 days (logrotate on the host) |
| Stripe customer object | Retained by Stripe after deletion (tax records); subscription cancelled at once |

## User rights (self-serve)

- `GET /api/account/export` — zip of everything that is theirs, decrypted.
- `DELETE /api/account` — soft delete now, Stripe subscription cancelled now,
  purge after 30 days. Feedback rows keep only the numeric user id.
- Country, name, email, password, ToS re-acceptance: in-app.

## Breach handling

If a breach affecting personal data is confirmed: notify affected users by
email within 72 hours, rotate `JWT_SECRET` / `DATA_KEY` / `LLM_SETTINGS_KEY`
(users re-enter BYOK keys; `aboyeur reencrypt-pii` migrates PII), rotate
Stripe/Resend/B2 keys, and record the incident in `admin_audit`.
