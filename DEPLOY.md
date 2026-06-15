# Deploying the Research Intelligence Dashboard to Azure

This document describes how to deploy the dashboard (FastAPI + in-process APScheduler,
`python run.py` on port 8000, SQLite `research.db`) to **Azure Container Apps** so that
LogiqGPT can consume it in production. It mirrors the proven LogiqGPT deploy pattern
(ACR cloud build → Container Apps, secret-splitting, local-disk SQLite seeded from an
Azure Files share).

> **Nothing here deploys automatically.** `azure-deploy-dashboard.sh` is a **dry run by
> default** — it prints the plan and never calls `az`. A real deploy requires the
> explicit `--confirm` flag **and** human approval of the printed plan.

---

## 1. Artifacts in this repo

| File | Purpose |
|------|---------|
| `Dockerfile` | `python:3.12-slim`; installs `requirements.txt`; `EXPOSE 8000`; `CMD ["python","run.py","--host","0.0.0.0","--port","8000"]`; `HEALTHCHECK` hits `/api/config`. |
| `.dockerignore` | Excludes `.venv`, `.git`, `research.db`, `__pycache__`, `.env`, etc. — keeps the ACR build context small and never ships local state/secrets. |
| `azure-deploy-dashboard.sh` | Dry-run-by-default deploy script (ACR build, storage, Container Apps, secret-splitting). `--confirm` to actually run. |
| `DEPLOY.md` | This file. |

---

## 2. Prerequisites (only needed for the real `--confirm` run)

- **Azure CLI** installed and logged in: `brew install azure-cli && az login`
- A populated **`.env`** in the repo root (gitignored, **never committed**) with the
  ingestion / LLM keys — see `.env.example` for the full list (`ANTHROPIC_API_KEY`,
  `YOUTUBE_API_KEY`, `XAI_API_KEY`, `PERPLEXITY_API_KEY`, `GOOGLE_API_KEY`,
  `REDDIT_CLIENT_ID/SECRET`, `X_BEARER_TOKEN`, `SMTP_USER/PASS`, `COST_TIER`, …).
- **No local Docker required** — the image is built inside Azure Container Registry via
  `az acr build`.

---

## 3. Critical gotchas (do not skip)

### SQLite on Azure Files (SMB) breaks the WAL — run SQLite on LOCAL disk
This is the exact issue LogiqGPT hit. SQLite's write-ahead log (WAL) relies on byte-range
locks that **do not work over SMB** (Azure Files), causing lock stalls / "database is
locked" / corruption. The fix (mirrored from LogiqGPT):

- The Azure Files share is mounted at **`/app/data`** (persistent master copy).
- SQLite runs on **`/app/local`** (container-local disk) via
  `DATABASE_URL=sqlite:////app/local/research.db`.
- `run.py` should **seed `/app/local` from `/app/data` at startup** and (optionally) copy
  the DB back to the share periodically / on shutdown for durability.
  - The deploy script sets `DASHBOARD_SHARE_DATA_DIR=/app/data` and
    `DASHBOARD_LOCAL_DATA_DIR=/app/local` to support this. **TODO (app-side, separate
    change):** wire the startup seed-from-share / persist-to-share in `run.py`. Until that
    is added, the SQLite DB is ephemeral per-revision (acceptable for a stateless,
    always-recomputing cache; not acceptable if you need durable history).

### In-process APScheduler → min-replicas = 1, NO scale-to-zero
`run.py` starts the scheduler **inside the web process** (`build_scheduler().start()`).
If Container Apps scales to zero (or runs >1 replica), the scheduler thread dies (or runs
duplicated). The deploy script therefore pins **`--min-replicas 1 --max-replicas 1`**.
Do not enable scale-to-zero or multiple replicas without first moving the scheduler to a
separate Container Apps **Job** (cron) — otherwise ingestion silently stops.

### Secrets are split out of `.env`
Any key matching `*_API_KEY | *_SECRET | *_TOKEN | *PASSWORD | *_PASS | *_KEY` is stored
as a **Container Apps secret** and referenced via `secretref:` in the env vars — values
are never passed as plain env values and are never echoed by the script (dry-run shows
`<redacted>`). Non-secret keys (`COST_TIER`, `SMTP_HOST`, …) are passed as plain env vars.

---

## 4. The env vars LogiqGPT needs to point at this deployment

LogiqGPT's `services/dashboard_client.py` **already reads** these (no LogiqGPT code change
needed beyond setting them):

| Env var | Meaning |
|---------|---------|
| `DASHBOARD_API_URL` | Base URL of the deployed dashboard, e.g. `https://research-dashboard.<region>.azurecontainerapps.io`. Defaults to `http://127.0.0.1:8000` when unset. |
| `DASHBOARD_API_KEY` | Shared key sent by LogiqGPT as the `X-API-Key` header. The deploy script sets this as a Container Apps secret (auto-generated via `openssl rand -hex 24` if not in `.env`). |

> **Auth follow-up (out of scope for this prep):** the dashboard does **not yet enforce**
> the `X-API-Key` header server-side — it currently serves all `/api/*` routes
> unauthenticated. `DASHBOARD_API_KEY` is wired as a secret here so that adding a FastAPI
> dependency that checks `request.headers["X-API-Key"]` against `os.getenv("DASHBOARD_API_KEY")`
> is a single-file change later. **Until that is added, restrict ingress** (e.g. Container
> Apps IP restrictions, or front it with Easy Auth / a private endpoint) so the API is not
> openly reachable.

---

## 5. Exact human deploy steps

```bash
cd /path/to/research-intelligence-dashboard

# 1. DRY RUN — prints the full plan, calls no az, changes nothing. Review it.
./azure-deploy-dashboard.sh

# 2. Get human approval of the printed plan (resource names, region, replica count,
#    which keys are split into secrets).

# 3. REAL DEPLOY — only after approval. Creates ACR, storage, Container Apps env + app.
./azure-deploy-dashboard.sh --confirm
```

On success the `--confirm` run prints the deployed `https://…azurecontainerapps.io` URL
and the `DASHBOARD_API_URL` / `DASHBOARD_API_KEY` values to set in LogiqGPT.

To read the generated API key later:
```bash
az containerapp secret show -n research-dashboard -g research-dashboard-rg \
  --secret-name dashboard-api-key --query value -o tsv
```

---

## 6. Staged LogiqGPT change (separate repo — do NOT edit it from here)

After the dashboard is deployed, point LogiqGPT at it. This is a **one-line config
change in the LogiqGPT repo** (its `dashboard_client.py` already reads the env):

```bash
# In the LogiqGPT deployment (its .env / Container Apps env-vars), set:
DASHBOARD_API_URL=https://research-dashboard.<region>.azurecontainerapps.io
DASHBOARD_API_KEY=<value of the dashboard-api-key secret above>
```

Then redeploy LogiqGPT (its `./update-azure.sh`). No LogiqGPT source change is required.
