# Secret hygiene

Status as of 2026-09-24. Owner: CTO.

## The committed USDA key (ABO-7)

`backend/macro_engine.py` set a real USDA FoodData Central key as the default
value of `os.getenv("USDA_API_KEY", ...)`. It was committed in the initial POC
commit and survived into `HEAD`.

**Fixed in the working tree.** The default is gone; the key is now read lazily
via `macro_engine.usda_api_key()` and comes only from the environment.

**Not fixed.** Two things remain, in this order:

1. The key is still live and still in git history (9 commits).
2. api.data.gov has no self-service revocation, so a new key does not disable
   the old one. Getting the old one disabled means emailing them.

### Exposure assessment

The repo has **no git remote** and has never been pushed. It lives on the
founder's Mac mini on a NAS-mounted volume. The realistic exposure is anyone
with filesystem access to that machine.

The key itself is low-value: free signup, no billing attached, read-only access
to a public government nutrition database, 1,000 requests/hour. Worst case if
someone burns it, `lookup_ingredient_macros` returns `status: "skipped"` with
zero macros until the rate limit resets. That is a degradation, not data loss
and not an outage.

**Conclusion: not beta-blocking. It is push-blocking.** The moment this repo
gets a remote — GitHub, a Hetzner bare repo, anything — the exposure changes
from "the founder's laptop" to "wherever that remote is." Both steps below must
complete before the first `git push`.

### Step 1 — Rotate

Needs an inbox; api.data.gov mails the key rather than returning it.

1. Request a new key at <https://fdc.nal.usda.gov/api-key-signup>.
2. Do **not** paste it into a comment, a task, a doc, or a commit. File it as a
   Paperclip secret (`POST /api/agents/me/secret-proposals`) and put it in
   `.env` as `USDA_API_KEY=` on the Mac mini.
3. Restart the backend. Verify:

   ```bash
   uv run python -c "
   from backend import macro_engine
   import aboyeur                      # loads .env
   print(macro_engine.lookup_ingredient_macros('asparagus'))"
   ```

   Expect `'status': 'success'` with non-zero macros. `'status': 'skipped'`
   means the env var did not reach the process; `'failed'` means the key was
   rejected.

   Note the disk cache (`data/usda_cache.json`) will short-circuit an
   already-looked-up ingredient, so test with one you have not used.

4. Ask api.data.gov to disable the old key. There is no self-service path;
   contact them through <https://api.data.gov/contact/>. Until they do, the
   old key stays valid no matter what the repo says.

### Step 2 — Scrub history

**Deliberately deferred.** With no remote, rewriting all 24 commits on the only
copy of the repo costs more than it buys. Do it as the last step before the
first push, not before.

Rollback path: the bundle in step 1 below restores the pre-rewrite repo
exactly. Do not skip it.

```bash
cd /Volumes/General/Coding/Python/Aboyeur

# 1. Rollback path first. Keep this file until the rewrite is verified.
git bundle create ~/aboyeur-prescrub-$(date +%Y%m%d).bundle --all
git status --short          # must be clean of anything you care about

# 2. filter-repo is not installed; it is the right tool here (filter-branch
#    is deprecated and mangles tags).
uv tool install git-filter-repo

# 3. Replace the literal with a placeholder across all history.
#    Put the old key in the expressions file; delete the file afterwards.
printf 'literal:PUT_OLD_KEY_HERE==>REDACTED_ROTATED_KEY\n' > /tmp/abo7-expr
git filter-repo --replace-text /tmp/abo7-expr --force
rm -f /tmp/abo7-expr

# 4. Verify nothing survives.
git log --oneline -S'PUT_OLD_KEY_HERE' --all | cat   # must print nothing
git grep -l 'PUT_OLD_KEY_HERE' $(git rev-list --all) | cat   # must print nothing
```

`filter-repo` rewrites every commit SHA and drops the origin remote by design.
Since there is no remote and no second clone, there is nothing to reconcile —
which is exactly why doing it *before* the first push is cheap and doing it
after is not.

## What else was checked

- `.env` is gitignored (`.gitignore:1`) and is not in history.
- Scanned `HEAD` for other `key|secret|token|password = "<20+ chars>"` literals
  across `*.py`, `*.yml`, `*.yaml`, `*.toml`, `*.js`, `*.ts`. The USDA key was
  the only hit.
- `aboyeur/config.py` reads every other credential (`LLM_API_KEY`,
  `STRIPE_*`, `RESEND_API_KEY`, `SENTRY_DSN`) from the environment with no
  default. The POC-era `backend/` module was the outlier.

## The bug the key was hiding

Worth recording, because it would have bitten whoever removed the default
naively.

`backend/main.py` imports `macro_engine` on line 27. `aboyeur/__init__` — which
calls `load_dotenv()` — is not imported until line 33. A module-level
`os.getenv("USDA_API_KEY")` therefore resolves to `""` under
`uvicorn backend.main:app`, which is how the Mac mini beta runs. Docker was
unaffected because compose passes `env_file: .env` into the real environment.

So dropping the default without changing anything else would have silently
turned every macro lookup into `status: "skipped"` on the beta only, while
tests and Docker stayed green. Reading the key lazily at call time avoids the
import-order dependency entirely.
