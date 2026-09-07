# Practitioner mode — clients, per-client data, isolated plans

**Status: built (2026-09-07).** Phases 1–5 below are done; the "out of scope"
list at the end is still out of scope.

Turning Aboyeur from "one account = one household" into a tool a nutritionist
or personal trainer runs their book of business on. Companion to
`HANDOFF.md` (product state) and `FRONTEND_PLAN.md` (the BYO-LLM round).

## The three requirements

| Ask | What it means concretely |
|---|---|
| Plans **for people** | A practitioner account holds many clients. Each is a first-class record with a name and their own history. |
| **Their data** is instrumental | Every client carries their own anthropometrics, goals, allergies, likes/dislikes, targets, pantry — the whole profile, per client, not per account. |
| One plan **must not feed into another** | A menu or kit belongs to exactly one client. No tool call, no query, and no chat turn may read across clients. |

## The core decision: the subject key

Today every per-person table is keyed by `user_id`, so "the account" and "the
person being planned for" are the same thing. That identity is exactly what
has to break.

**`clients` becomes the subject key.** Per-person tables (`profile`, `menus`,
`kits`, `pantry`) re-key from `user_id` to `client_id`; `clients` itself
carries the `user_id` that owns it. Account-level tables (`llm_settings`,
`favorites`) stay on `user_id` — a practitioner's model config and recipe
stars are theirs, not any client's.

Rejected: adding a nullable `client_id` alongside `user_id` on every table.
It keeps two meanings alive forever ("NULL = my own household"), doubles
every query path, and makes cross-client leakage a matter of remembering a
second predicate. One key, never null, is auditable.

**Isolation comes from one choke point.** `resolve_client(conn, user_id,
client_id)` raises unless that client belongs to that practitioner. Every
route calls it before anything else; tools below it take a `client_id` they
can trust. Cross-client reads aren't defended against downstream, they're
unrepresentable — a tool filtering `WHERE client_id = ?` cannot see another
client's rows even if asked to.

Chat isolation is the same idea at the session layer: the agent cache keys on
`(user_id, client_id)`, so switching clients gets a different Agent with its
own history and its own system prompt. Nothing from the previous client's
conversation survives the switch.

## Migration

Existing accounts have a household and possibly menus/kits. `migrate.py`
gains a step that, per user with data, creates one client (named after the
primary household member, else "My household") and rewrites their
`profile` / `menus` / `kits` / `pantry` rows onto it. Idempotent like the
rest of that module: it checks each table's shape before touching it.

The OpenWebUI shim and the CLI have no client picker, so both resolve the
account's **default client** — its oldest active one, auto-created on first
use if the account has none.

---

## Work breakdown

### Phase 1 — data layer

1. `aboyeur/db.py` — `clients` table; `profile` / `menus` / `kits` / `pantry`
   re-keyed to `client_id`.
2. `aboyeur/clients.py` (new) — `create_client`, `list_clients`,
   `get_client`, `update_client`, `archive_client`, `delete_client`,
   `resolve_client` (the ownership gate), `default_client_id`.
3. `aboyeur/migrate.py` — the fold-household-into-a-client step above.

### Phase 2 — tools + agent

4. `aboyeur/tools/*` — `user_id` → `client_id` on `profile`, `pantry`,
   `menu`, `kit`, `shopping`. Signature-only for most; the queries change
   column name and nothing else.
5. `aboyeur/agent.py` — `Agent(conn, config, client_id, ...)`, dispatch
   passes `client_id`. System prompt says who this session is planning for.
6. `aboyeur/cli.py` — resolves the default client.

### Phase 3 — API

7. `backend/main.py`:
   - `GET/POST /api/clients`, `GET/PATCH/DELETE /api/clients/{id}`
   - `GET/POST /api/clients/{id}/profile` (the old `/profile/*` routes stay,
     pointed at the default client, for the shim and the profile page)
   - `GET /api/clients/{id}/plans` — that client's menus and kits
   - `POST /chat` takes `client_id`; agents cached per `(user, client)`.

### Phase 4 — UI

8. `/app/clients` — the roster. Search, add, archive, per-card summary
   (goal, daily kcal, last plan).
9. `/app/clients/:id` — client workspace: **Profile** (the editor, scoped to
   them), **Plans** (their menus and kits), **Notes**.
10. Header: an active-client switcher. Chat shows *who* it is planning for,
    and refuses to chat with no client selected rather than quietly using
    somebody's data.
11. Library and Settings stay account-level.

### Phase 5 — docs + verification

12. `HANDOFF.md`, `frontend/README.md`, `DEPLOYMENT.md`, this file's status.
13. Tests: ownership gate, per-client isolation of profile/menus/kits, the
    migration, agent cache keying. Browser pass over roster → client →
    profile edit → plans → switch client → chat banner.

### What the migration hit on the way through

Two bugs worth remembering, both found by running it against the real DB:

- **Concurrent migrations.** uvicorn's `--reload` calls `migrate()` on every
  restart, so an edit landing while another process migrated interleaved
  their `ALTER TABLE ... RENAME` calls and left `profile` in the old shape
  with `profile_old` holding the new one. Fixed with `BEGIN IMMEDIATE` around
  the whole client step, plus dropping a stale `*_old` before re-running.
- **A guard that aged badly.** The older multi-tenant steps skipped a table
  when it already had a `user_id` column. A client-keyed table has no
  `user_id` either, so they read "not yet migrated" and rebuilt an
  already-migrated table back into the user_id shape, dropping its rows. The
  guard now tests for the absence of *both* scoping columns
  (`_is_pre_multitenant`).

## Explicitly out of scope

- Client-facing logins (clients are records here, not accounts).
- Progress tracking over time (weigh-ins, adherence, charts).
- PDF/print export of a plan, and emailing it to a client.
- Billing, scheduling, intake forms, e-signature.
- Team accounts (two practitioners sharing one client book).
