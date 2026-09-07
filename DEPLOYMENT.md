# Deploying Aboyeur

Runbook for putting the real app (backend + frontend) on a public URL for
investors, per the approved plan's Phase 4. You (Jose) need to drive this —
it needs your cloud account, your domain, and your Tailscale admin console,
none of which I have access to. This doc is the exact sequence; where a step
needs a decision only you can make, it says so.

**Decision already made:** backend runs on a cloud VM joined to your
Tailscale tailnet, so `LLM_BASE_URL` keeps pointing at the DGX Spark
(`http://100.70.6.1:4000/v1`) unchanged — no LLM-side code changes.

**Decision made while writing this** (revises the plan file's Phase 4
slightly, now that auth is actually built): serve the frontend's static
build from the **same domain** as the backend API, not a separate Vercel
domain. The session cookie is `SameSite=Lax`, and every frontend fetch call
uses relative paths (`/api/...`, `/auth/...`) — both assume same-origin.
Splitting frontend and backend across two domains would break login
(cookie wouldn't be sent cross-site) and need CORS wired up for no real
benefit at this scale. One Caddy instance serving both is simpler and
matches what the Vite dev proxy already does locally.

## 0. What you need before starting

- A cloud VM provider account (Hetzner, DigitalOcean, Linode, Vultr — any
  works, this doesn't need anything provider-specific; a $6–12/mo box with
  1–2GB RAM is plenty for a POC with a handful of users).
- A domain (or subdomain) you can point at the VM's IP — Caddy needs this
  for automatic HTTPS.
- Your Tailscale admin console access, to authorize the new VM on your
  tailnet.

## 1. Provision the VM

Ubuntu 24.04 LTS, smallest tier. SSH in as root (or a sudo user).

```bash
ssh root@<vm-ip>
apt update && apt upgrade -y
adduser aboyeur --disabled-password --gecos ""
usermod -aG sudo aboyeur
su - aboyeur
```

## 2. Join the Tailscale tailnet

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

That prints a URL — open it, authorize the new device in your tailnet from
the browser. Then verify it can actually reach the DGX Spark before doing
anything else (no point deploying if the LLM is unreachable):

```bash
tailscale ping 100.70.6.1
curl -s http://100.70.6.1:4000/v1/models   # should return JSON, not hang
```

## 3. Copy the recipe corpus + database

**This is the step most likely to get skipped and silently break things.**
`get_recipe` reads the recipe `.md` file straight off disk by the path
stored in the database — search results and chat both work fine without
this (they only need the DB), but the moment anyone asks to actually see a
recipe or plan a menu (which fetches full recipe text), it 404s.

The database's `recipes.path` column holds absolute paths like
`/Volumes/General/Cookbooks/Recipes/<book>/<recipe>.md` — your Mac's SMB
mount path. Two ways to handle it; **use the first one** — it preserves
every cached USDA/nutrition lookup already in the database (real API calls
you don't want to redo) instead of re-indexing from scratch:

**Preserve the exact path** (recommended): recreate that same absolute
path on the VM and rsync the corpus into it verbatim, then copy the
database as-is — no re-indexing, no re-computation, nothing changes for the
app.

```bash
# On the VM:
sudo mkdir -p /Volumes/General/Cookbooks
sudo chown aboyeur:aboyeur /Volumes/General/Cookbooks

# From your Mac (299MB, ~75k files — rsync, not scp, for the resume support):
rsync -avz --progress /Volumes/General/Cookbooks/Recipes/ aboyeur@<vm-ip>:/Volumes/General/Cookbooks/Recipes/

# Database (45MB):
rsync -avz --progress ~/.local/share/aboyeur/aboyeur.db aboyeur@<vm-ip>:/opt/aboyeur-data/aboyeur.db
```

(Alternative if you'd rather not have a `/Volumes/...` path on a Linux box:
copy the corpus anywhere you like, then re-run `aboyeur index --rebuild`
with `RECIPES_DIR` pointing there — but that also re-triggers USDA lookups
and LLM ingredient parsing for anything not already cached, which costs
real time and API calls for no benefit. Only do this if the exact-path
option is truly not workable.)

## 4. Install the app

```bash
sudo apt install -y git
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

sudo mkdir -p /opt/aboyeur && sudo chown aboyeur:aboyeur /opt/aboyeur
cd /opt/aboyeur
git clone <your-repo-url> .          # or rsync the working tree over directly
uv sync                               # installs everything from pyproject.toml,
                                       # including fastapi/uvicorn/sqlalchemy
```

### `.env` on the VM

```bash
cat > /opt/aboyeur/.env <<'EOF'
RECIPES_DIR=/Volumes/General/Cookbooks/Recipes
DB_PATH=/opt/aboyeur-data/aboyeur.db
LLM_BASE_URL=http://100.70.6.1:4000/v1
LLM_MODEL=hard
EMBEDDING_MODEL=nomic-embed-text
USDA_API_KEY=<copy from your local .env>
JWT_SECRET=<generate a NEW one for prod — don't reuse the local dev one>
LLM_SETTINGS_KEY=<generate a NEW one for prod — see below>
EOF
```

Generate a fresh JWT secret:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Generate the LLM-settings encryption key:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`LLM_BASE_URL` / `LLM_MODEL` are now only a *fallback* — every web account
sets its own endpoint, model and API key in Settings, and those are what the
agent runs with. The env pair still covers the CLI and the OpenWebUI shim,
which have no logged-in user of their own.

Leave `COOKIE_SECURE` unset here. It defaults to true, which is what you want
behind Caddy's HTTPS; it exists only so local `http://localhost` dev can hold a
session (Safari refuses Secure cookies over plain http).

`LLM_SETTINGS_KEY` is what those per-user keys are encrypted with in the DB.
Set it explicitly: with it unset the key is derived from `JWT_SECRET`, so
rotating `JWT_SECRET` would silently make every stored LLM key undecryptable.
Rotating `LLM_SETTINGS_KEY` itself has the same effect by design — stored keys
are orphaned and each user re-enters theirs in Settings (the server treats an
undecryptable row as "not configured" rather than erroring).

### Run the migration once

```bash
cd /opt/aboyeur
uv run python -m aboyeur.migrate
```

Idempotent and safe. It now runs two migrations in order: single-household →
multi-tenant, then multi-tenant → practitioner (per-person tables re-keyed
from `user_id` to `client_id`, with each account's existing household folded
into its first client, named after the primary member).

Run it with the app **stopped**. The client step takes a `BEGIN IMMEDIATE`
write lock precisely because two processes starting at once — uvicorn's
`--reload` calls `migrate()` on every restart — would otherwise interleave
their table renames into a half-migrated database.

**Before going live**, decide what to do with the `investor1@example.com` /
`testpass123` test account that's sitting in that copied database — it's
real, working credentials to a real login on whatever you deploy. Either
delete it or change its password:

```bash
uv run python -c "
from aboyeur.config import Config
from aboyeur.db import connect
conn = connect(Config.load().db_path)
conn.execute(\"DELETE FROM users WHERE email = 'investor1@example.com'\")
conn.commit()
"
```

### Provision real investor accounts

```bash
cd /opt/aboyeur
uv run aboyeur create-user investor@theirfund.com
# prompts for a password (hidden input) — hand it to them out of band,
# e.g. a phone call or a password manager share, not email/Slack
```

## 5. systemd service for the backend

```bash
sudo tee /etc/systemd/system/aboyeur-backend.service <<'EOF'
[Unit]
Description=Aboyeur backend
After=network.target tailscaled.service

[Service]
Type=simple
User=aboyeur
WorkingDirectory=/opt/aboyeur
EnvironmentFile=/opt/aboyeur/.env
ExecStart=/opt/aboyeur/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now aboyeur-backend
sudo systemctl status aboyeur-backend   # should say "active (running)"
curl -s http://127.0.0.1:8000/auth/me   # {"detail":"not logged in"} = alive
```

Bound to `127.0.0.1`, not `0.0.0.0` — only Caddy (below) talks to it
directly; nothing external hits port 8000.

This intentionally does **not** use the repo's `docker-compose.yml` — that
file's `chromadb`/`watcher` services are unrelated to this deployment and
are left alone, per your earlier call not to touch that dead-code path.

## 6. Build and place the frontend

From your Mac (or anywhere with Node — doesn't need to be the VM):

```bash
cd frontend
npm install
npm run build        # produces frontend/dist/, a static site
rsync -avz --progress dist/ aboyeur@<vm-ip>:/opt/aboyeur/frontend/dist/
```

## 7. Caddy — TLS + reverse proxy + static frontend, one config

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Point your domain's DNS A record at the VM's IP first — Caddy's automatic
HTTPS needs that to resolve before it can get a certificate.

```bash
sudo tee /etc/caddy/Caddyfile <<'EOF'
your-domain.com {
    handle /api/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /auth/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /profile/* {
        reverse_proxy 127.0.0.1:8000
    }
    handle /chat {
        reverse_proxy 127.0.0.1:8000
    }

    handle {
        root * /opt/aboyeur/frontend/dist
        try_files {path} /index.html
        file_server
    }
}
EOF

sudo systemctl reload caddy
```

`try_files {path} /index.html` is what makes client-side routing work —
without it, a direct visit or refresh on `/app/library` 404s instead of
loading the React app and letting the router take over.

## 8. Verify

```bash
curl -sI https://your-domain.com                      # 200, valid cert
curl -s https://your-domain.com/auth/me                # {"detail":"not logged in"}
```

Then in a real browser: log in with an investor account, send a chat
message, browse the library, run through onboarding. Same checks I ran
locally — see the smoke scripts at `frontend/e2e-smoke.mjs` and
`frontend/e2e-onboarding.mjs` if you want to re-run them against the live
URL (edit the `http://localhost:5173` base to your domain first).

## Updating the deployed app later

```bash
# Backend:
cd /opt/aboyeur && git pull && uv sync
sudo systemctl restart aboyeur-backend

# Frontend:
# (rebuild locally, rsync dist/ over again — step 6)
```

## Known limits at this scale

- SQLite is fine for a handful of concurrent investor accounts; revisit
  only if this grows into a real multi-tenant product with real traffic.
- One VM, one backend process — if it crashes, systemd restarts it
  (`Restart=on-failure`), but there's no redundancy. Acceptable for a demo,
  not for anything you'd stake a sales call on without a heads-up check
  beforehand.
- The DGX Spark is a single home machine behind Tailscale — if it's off or
  your home internet is down, chat stops working regardless of how solid
  the cloud side is. Worth a quick health check before any live demo:
  `tailscale ping 100.70.6.1` from the VM.
