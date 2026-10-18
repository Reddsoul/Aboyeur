# Beta health baseline — 2026-09-24

Author: CTO. Task: ABO-3.
Scope: what is broken in the beta, and what it would actually take to bring
Aboyeur back if the Mac mini died tonight.

Everything below is a command I ran and its output. The live beta database was
never modified — verified at the end of the restore drill (`aboyeur.db` mtime
still `Sep 23 09:30`).

---

## Headline

**There is no offsite backup. Litestream has never run. B2 has nothing in it.**

`litestream.yml`, the `litestream` compose service, and the DEPLOYMENT.md
restore command all exist, are all correct-looking, and are all inert. The Mac
mini has no Litestream binary, no credentials, no scheduled job, and no Docker
daemon to run the sidecar in. The only copies of beta user data are four files
sitting in the same directory, on the same disk, as the live database.

If that disk dies tonight, Aboyeur's user data is gone. Not degraded — gone.
That is the one finding on this page that outranks everything else, because
**data loss is terminal** and downtime is not.

---

## Part 1 — Baseline

### The test suite passes

```
$ .venv/bin/python -m pytest -q
508 passed, 10 warnings in 62.20s (0:01:02)
```

Zero failures, 62 seconds. That is a genuinely good suite for a two-person
company, and it is the reason I am comfortable calling the defects below
operational rather than functional.

Distribution (`pytest --collect-only`):

| File | Tests | Covers |
|---|---:|---|
| `test_engines.py` | 104 | menu/macro/fusion/preference engines |
| `test_tools.py` | 85 | agent tool surface |
| `test_llm_settings.py` | 39 | BYOK settings, key handling |
| `test_security_hardening.py` | 38 | SSRF guard (`urlguard`), cookie flags, rate limits |
| `test_billing.py` | 33 | Stripe flows, webhooks |
| `test_hosted_llm.py` | 29 | hosted-model metering |
| `test_variety.py` | 25 | variety slider |
| `test_admin.py` | 25 | admin routes |
| `test_privacy.py` | 24 | PII crypto, purge, retention |
| `test_clients.py` | 21 | multi-client scoping |
| `test_export_pdf.py` | 20 | menu → PDF |
| `test_account.py` | 14 | signup / verify / reset / invite gating |
| `test_indexer.py` + `test_reference_indexer.py` | 21 | corpus indexing |
| `test_nutrition_batch.py` | 10 | batch nutrition estimator |
| `test_profile_ui.py` | 9 | profile UI |
| `test_agent.py` | 7 | agent loop |
| `test_llm.py` | 4 | LLM client |

### What the suite does not cover

These are the gaps that matter, in order:

1. **Backup and restore. Nothing.** `grep -ril backup tests/` returns nothing.
   The only hit for `litestream` in the whole suite is a string literal in
   `test_security_hardening.py:29` — `"http://litestream:9090/metrics"` used as
   an SSRF-rejection fixture. No test has ever exercised replication or restore.
2. **The production container.** The suite runs on the local venv —
   `Python 3.14.6`. `docker/backend.Dockerfile` builds on
   `python:3.12-slim-bookworm`. Two minor versions apart, and the Docker daemon
   is not even installed on this machine (`docker ps` →
   `dial unix /var/run/docker.sock: no such file or directory`). The compose
   stack in DEPLOYMENT.md has never been executed. Green tests on 3.14 are not
   evidence about the image we intend to ship.
3. **Migration rollback.** `aboyeur/migrate.py` is 532 lines of forward-only
   steps. No version table, no down-migrations, and no test that runs a
   migration against a realistic pre-migration database and asserts the result.
   `_migrate_chat_sessions` rebuilds `chat_messages`; `_encrypt_pii_rows`
   encrypts PII in place. Both are one-way.
4. **Concurrency under the single-writer constraint.** One grep hit in
   `test_clients.py`. Nothing asserts behaviour when a write collides with the
   hot-snapshot read, the purge job, or a second process.
5. **Startup and supervision.** `_startup()` in `backend/main.py:161` calls
   `purge_deleted()` on every boot. No test covers boot-time purge against a
   database with accounts near the grace boundary. Given there is no backup, a
   bug there deletes data permanently.

### Defects, worst first

**D1 — No offsite backup (beta-blocking, critical).**
Covered in full in Part 2. Blast radius: every beta tester, total loss.

**D2 — Schema migration runs at module import, with no snapshot and no way back
(beta-blocking).**
`backend/main.py:714`:

```python
with _aboyeur_schema_lock(_aboyeur_config.db_path):
    _aboyeur_init_db(_aboyeur_conn)
    _aboyeur_migrate(_aboyeur_conn)  # idempotent; no-op once already migrated
```

This is at import time, not in a startup hook — every process start rewrites the
schema. `migrate()` calls `_migrate_chat_sessions` (rebuilds `chat_messages`)
and `_encrypt_pii_rows` (in-place PII encryption). The comment is accurate
today: it *is* idempotent once migrated. The problem is the next migration.
Ship one, restart the app, and live tables are rewritten with no pre-migration
snapshot and no down path. **Rollback path**: there isn't one. `git revert`
gives you back the old code against a database the old code can no longer read.

**D3 — The app is not supervised; it does not survive a reboot (beta-blocking).**

```
$ ps -o pid,ppid,command -p 62981
  PID  PPID COMMAND
62981     1 uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

PPID 1 means orphaned, not managed. `ls ~/Library/LaunchAgents/` lists five
plists — SearXNG, Kiwix, Odysseus, Ollama, openwebui-update — and no Aboyeur.
`launchctl` has no Aboyeur job; `crontab -l` has two recipe-repair jobs and
nothing else. If the process crashes or the Mac mini reboots, Aboyeur stays down
until the founder notices and types the command again. Every tester is down for
as long as that takes.

**D4 — `/health` cannot see the thing it was built to watch (beta-blocking as
observability, and it currently lies by omission).**

```
$ curl -s http://127.0.0.1:8000/health
{"ok":true,"db":true,"version":"dev"}
```

DEPLOYMENT.md:204 says `GET /health` → `{ok, db, litestream_lag_s, version}`
and "alert if `litestream_lag_s` > 60". The field is absent, because
`backend/main.py:2658` only adds it when `LITESTREAM_METRICS_URL` is set, and it
isn't. So the documented backup alarm is not merely un-armed — a reader of
`/health` sees `ok: true` on a box with zero backups. **Observability before
launch**: right now a green health check is evidence of nothing.

Also note `version: "dev"` — `APP_VERSION` is unset, so we cannot tell from the
outside which commit the beta is running.

**D5 — Python 3.14 in the beta, 3.12 in the image (launch-blocking, migration
cost).** `.venv/bin/python -V` → `3.14.6`. Image → `python:3.12-slim-bookworm`.
Named now so the cost is visible: the first time we build that image we will be
debugging a version difference *and* a container difference *and* a cutover at
the same time. **Migration cost** of leaving this alone until Q1 2027 is a
cutover with two unknowns instead of zero.

**D6 — Secrets and data share one disk and one failure domain (launch-blocking).**
`.env` holds `JWT_SECRET`, `DATA_KEY`, and Stripe test keys in plaintext, and is
correctly gitignored (`git ls-files --error-unmatch .env` → not tracked; good).
But `DATA_KEY` is the only thing that makes the encrypted PII in the database
readable, and it lives on the same disk as the database and all four backups.
A restored database without `DATA_KEY` is unreadable PII. Whatever offsite plan
we adopt has to cover the key too, stored separately from the data.

**D7 — Legacy `_DRAFT` global is process-wide, not per-user (not blocking, but
do not re-enable legacy routes without fixing it).**
`backend/main.py:157` is a module-level `dict` holding one draft menu, written
by `/menu` and read by `/menu/swap` and `/menu/current`. Single-user design
carried into a multi-tenant app. Currently harmless:

```
$ grep -n ENABLE_LEGACY_ROUTES .env    # absent, defaults false
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/menu/current
404
```

Dormant, correctly gated. Flagging it so nobody flips `ENABLE_LEGACY_ROUTES=true`
for a debugging session and hands one tester another tester's draft.

**D8 — Deploy drops in-flight chat turns (accepted for beta).**
Documented at DEPLOYMENT.md:202. Single worker, restart kills open streams.
`a81b2a0` already made agent turns outlive the HTTP connection, so a tester who
reconnects recovers. Acceptable in beta; revisit before paying customers.

### Beta-blocking vs. launch-blocking

**Beta-blocking** — fix before the next tester touches it:

- D1 offsite backup. **Data loss is terminal.**
- D2 pre-migration snapshot + a written rollback. Never approve a change to
  the beta with no way back.
- D3 supervise the process so it survives a crash and a reboot.
- D4 make `/health` report backup freshness truthfully.

**Launch-blocking** — must be true before Q1 2027, not before the next tester:

- D5 build and run the image; settle on one Python version.
- D6 offsite `DATA_KEY` custody, separate from the data.
- Hetzner cutover runbook with named rollback and data-loss window (separate
  task; this document is its input).
- D7 before legacy routes are ever enabled in a multi-tenant deploy.

**Waits** — D8.

---

## Part 2 — The restore test

### Q: if the Mac mini died tonight, what would it take to bring Aboyeur back?

**A: you could not. Beta user data would be permanently lost.**

Not "slow to recover", not "recover with gaps". There is no copy of the data
anywhere except on the Mac mini's disk.

Evidence — every check for an offsite copy, and its result:

```
$ which litestream
(no output)
$ litestream version
zsh: command not found: litestream
$ ls /opt/homebrew/bin/litestream
No such file or directory
$ ls /opt/homebrew/Cellar | grep -i lite
sqlite                          # sqlite, not litestream
```

```
$ ps aux | grep -iE 'litestream|uvicorn|caddy' | grep -v grep
macmini-m4 62985 ... uvicorn backend.main:app --host 0.0.0.0 --port 8000
macmini-m4 62981 ... uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

One process. No Litestream. No Caddy either — the beta is bare uvicorn on
`0.0.0.0:8000` behind Tailscale. (The macOS application firewall is on —
`socketfilterfw --getglobalstate` → `State = 1` — which limits the exposure of
that bind, but **Tailscale is not security** and this posture must not travel to
Hetzner.)

```
$ grep -E '^LITESTREAM' .env
(no output — the variables litestream.yml interpolates do not exist)
$ grep -n -i litestream .env.example
83:LITESTREAM_B2_BUCKET=
85:LITESTREAM_ACCESS_KEY_ID=
86:LITESTREAM_SECRET_ACCESS_KEY=
87:LITESTREAM_B2_ENDPOINT=      # all blank in the template, never filled in .env
```

No B2 bucket name has ever been configured, so there is no bucket to point a
restore at. No credentials either.

```
$ docker ps -a
failed to connect to the docker API at unix:///var/run/docker.sock
$ ls ~/Library/LaunchAgents/
com.anacaona.openwebui-update.plist  com.anacaona.searxng.plist
com.homelab.kiwix-update.plist       com.odysseus.app.plist
homebrew.mxcl.ollama.plist           # no aboyeur, no litestream
$ ls /Library/LaunchDaemons/ | grep -iE 'lite|aboyeur'
(no output)
$ crontab -l
*/30 * * * * ... repair_chain.sh
*/30 * * * * ... repair_chain2.sh    # recipe repair only
```

No container, no launchd job, no cron entry. Nothing has ever started it.

```
$ which rclone restic b2 aws
rclone not found                     # (none of the four are installed)
$ tmutil destinationinfo
tmutil: No destinations configured.
```

No alternative backup tool, and no Time Machine.

```
$ grep -rn "VACUUM INTO\|wal_checkpoint\|sqlite3_backup" aboyeur/ backend/
(no output)
```

The application has no backup code path of its own.

The documented restore command —
`docker compose run --rm litestream restore -config /etc/litestream.yml -o /data/aboyeur.db /data/aboyeur.db`
(DEPLOYMENT.md:190–194) — cannot be run: no Docker daemon, no bucket, no
credentials. I did not run it, because there is nothing for it to contact.

### What actually exists

```
$ ls -la ~/.local/share/aboyeur/
178925568  Sep 23 09:30  aboyeur.db                       <- live
  6151192  Sep 23 09:54  aboyeur.db-wal
178925568  Sep 19 15:40  aboyeur.db.bak-20260919-154022   <- newest copy
164556800  Sep  8 11:22  aboyeur.db.bak-20260908-112228
164556800  Sep  7 11:55  aboyeur.db.bak-preclients
$ ls -la ~/.local/share/aboyeur/backups/
178925568  Sep 19 15:34  aboyeur-pre-saas-2026-09-19.db
```

Four hand-made `cp` copies, date-stamped by hand, all in the same directory tree
as the live file. They protect against "I broke the schema". They protect
against nothing else — not disk failure, not theft, not the Mac mini not turning
on tomorrow.

### The restore drill I did run

Two drills, both into `$PAPERCLIP_RUN_SCRATCH_DIR/restore-drill`.

**Drill A — restore the newest existing artifact and boot the app on it.**

```
$ cp ~/.local/share/aboyeur/aboyeur.db.bak-20260919-154022 $S/restored.db
$ sqlite3 restored.db "PRAGMA integrity_check;"
ok                                            # 1.4s on 178 MB
```

Then, with `DB_PATH` pointed at the scratch copy and a spare port:

```
$ DB_PATH=$S/boot-test.db .venv/bin/python -m uvicorn backend.main:app \
    --host 127.0.0.1 --port 8099
$ curl -s http://127.0.0.1:8099/health
{"ok":true,"db":true,"version":"dev"}         # green ~6s after launch
$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8099/api/legal/terms
200
$ sqlite3 $S/boot-test.db "SELECT count(*) FROM recipes;"
74191
```

So the *file format* restores cleanly and the app runs on a restored database
unattended, including running `migrate()` against it on boot. Restore mechanics
are fine. There is simply nothing offsite to feed them.

**Drill B — prove a hot backup is possible without stopping the app.**
`VACUUM INTO` on a read-only connection, against the live database, while the
beta was serving:

```
$ sqlite3 "file:$HOME/.local/share/aboyeur/aboyeur.db?mode=ro" \
    "VACUUM INTO '$S/hot-snapshot.db';"
1.28s total                                   # 174 MB written
$ sqlite3 $S/hot-snapshot.db "PRAGMA integrity_check;"
ok
$ sqlite3 $S/hot-snapshot.db "SELECT max(created_at) FROM chat_messages;"
2026-09-23T14:13:42Z                          # matches live; WAL captured
```

1.28 seconds, no downtime, WAL contents included, integrity verified. In WAL
mode readers do not block writers, so this respects the **single writer**
constraint — it is a read, and the write goes to a different file. This is the
command a backup job should run. It is not installed anywhere yet.

Live database after both drills:

```
$ ls -la ~/.local/share/aboyeur/aboyeur.db
178925568  Sep 23 09:30  aboyeur.db      # mtime unchanged. Live DB untouched.
```

### Q: how much user data would be lost?

Two answers, and the first is the one that counts.

**If the Mac mini is gone — disk failure, theft, fire, a stolen laptop bag:
100%. All beta user data.** Every copy is in one directory on one disk.

**If only the live file is corrupted but the disk survives**, the newest local
copy is `aboyeur.db.bak-20260919-154022`. Its data-loss window is worse than its
filename suggests:

| | Newest chat message | Newest menu |
|---|---|---|
| `aboyeur.db.bak-20260919-154022` | `2026-09-14T22:31:52Z` | `2026-09-14T22:26:41Z` |
| live | `2026-09-23T14:13:42Z` | `2026-09-23T14:12:28Z` |

**Data-loss window: 9 days 19 hours of conversation and menu history**
(`2026-09-14T22:31:52Z` → `2026-09-24T17:28Z`, measured), not the 5 days the
file's `Sep 19` timestamp implies.
The copy was taken on the 19th, but the database it copied had seen no user
activity since the 14th.

Concretely, restoring that file would drop 14 chat messages, 3 chat sessions, 4
feedback entries, 7 events and 159 nutrition rows:

| table | in the backup | live |
|---|---:|---:|
| `chat_messages` | 29 | **43** |
| `chat_sessions` | 9 | **12** |
| `feedback` | 5 | **9** |
| `events` | 14 | **21** |
| `nutrition` | 536 | **695** |
| `users` | **5** | 3 |
| `menus` | **64** | 12 |
| `clients` | **8** | 6 |
| `kits` | **3** | 2 |
| `recipes` | 74191 | 74191 |

Note the last four rows carefully: the backup has **more** users, menus, clients
and kits than production. It is not a stale subset — it is a divergent branch.
Restoring it would resurrect 2 accounts and 52 menus that have since been
deleted. For accounts we deleted on a 30-day retention promise
(`docs/privacy-posture.md`, `purge_deleted()` at boot), un-deleting them is a
privacy incident on top of the data loss. **Any restore runbook we write has to
say what to do about rows the backup has and production deliberately does not.**

### What a restore *should* cost, once a backup exists

Recovery time is not the problem; recovery *point* is. Drill A boots a restored
database in about six seconds, and a 174 MB file pulls from B2 in well under a
minute on a normal connection. With a working replica the picture becomes: RTO
in single-digit minutes, RPO equal to the Litestream sync interval — the
`litestream.yml` already says `sync-interval: 10s`, so roughly 10 seconds of
writes. We are currently trading **10 seconds of exposure for total loss**, and
paying nothing for the privilege.

---

## What I am asking for

Four child tasks, filed against the Beta project. D1 is the only one I would
stop the beta for.

1. **D1 — Get beta data offsite tonight.** Two options, and this is a spend
    decision for the founder, so I am proposing rather than committing:
    - **(a) Turn Litestream on for real, as designed.** `brew install
      litestream`, a B2 bucket, credentials as Paperclip secrets (never in
      `.env` in a comment or a commit), a launchd job with `KeepAlive`. Buys
      ~10 s RPO and matches the Hetzner target exactly, so there is no second
      thing to build at cutover. Backblaze B2 is $6/TB/month with a 10 GB free
      tier; a 175 MB database with 168 h retention sits inside the free tier,
      so realistically **$0–1/month**. This is the one I recommend.
    - **(b) A `VACUUM INTO` snapshot on a timer, pushed offsite.** The command
      is proven above. Cheaper to write, but RPO is the interval (hours, not
      seconds), it is a second mechanism we throw away at cutover, and it still
      needs somewhere offsite to go. **Migration cost**: named and real.

    Either way: the job must be verified by restoring from it, not by observing
    that it ran. **Restore, not backup.** And `DATA_KEY` has to go somewhere
    offsite and separate, or the restored file is unreadable PII (D6).

2. **D2 — Snapshot before every migration, and write the rollback.** Make the
    `VACUUM INTO` snapshot a precondition of `migrate()` when it is about to do
    destructive work, and write down the restore-and-downgrade steps. No change
    reaches beta testers without a way back.

3. **D3 — Supervise the process.** A launchd job with `KeepAlive` so a crash and
    a reboot both self-heal.

4. **D4 — Make `/health` tell the truth.** Set `LITESTREAM_METRICS_URL` (or
    whatever D1 lands on) so `/health` reports real backup freshness, set
    `APP_VERSION` so we can tell which commit is live, and fix DEPLOYMENT.md:204
    so it stops describing an alarm that does not exist.

D5 and D6 are launch-blocking and belong with the Hetzner cutover plan, which is
a separate task and takes this document as input.

---

## Addendum, same day — D9: the source code has no offsite copy either

Found while committing this document, which is worse than finding it later:

```
$ git remote -v
                      # no output
$ git config --local --list | grep -iE 'remote|url'
                      # no output
$ git rev-parse --absolute-git-dir
/Volumes/General/Coding/Python/Aboyeur/.git
```

**There is no git remote. Not a stale one — none configured at all.** The only
copy of the Aboyeur source is `.git` on the same volume as `aboyeur.db`, all four
`.bak` files, and `DATA_KEY` in `.env`.

This changes the answer to "what would it take to bring Aboyeur back," and makes
it worse than the backup finding alone. Even if D1 lands and B2 holds a
ten-second-old replica of the database, **there would be nothing to restore it
into.** No application, no `litestream.yml`, no DEPLOYMENT.md, no `migrate()` to
bring a schema forward — 508 passing tests and the whole SaaS layer, gone with
the disk. A database with no application is not a recovery.

Note also that `size-pack: 0 bytes` — everything is loose objects, never packed
or pushed anywhere. Consistent with a repository that has never talked to a
remote.

**Classification: beta-blocking, high.** Not critical only because it does not
stop a tester mid-session the way D1 does; it stops the company. Filed as a child
task of ABO-3.

**Owner note.** *Where* the code goes is not mine to decide unilaterally — a
bare repo on the NAS, or a private GitHub remote, are different custody and cost
answers, and this document names our unpatched weaknesses in plain language, so
it should not be the thing that motivates pushing to a host nobody has agreed on.
I am proposing, not committing. Two constraints I will hold either way:

- **Verified by clone, not by push.** A push that nobody has cloned from is the
  same guess Litestream was. Acceptance is `git clone <somewhere else> && uv run
  pytest` passing from the clone.
- **`.env` must not go with it.** `DATA_KEY` in a repository, even a private one,
  re-merges the two failure domains D6 is about. Secrets stay Paperclip secrets.

**Cost:** a bare repo on the NAS is $0 and needs no third party. A private GitHub
remote is $0 on the free tier. This is a decision about custody, not a purchase.
