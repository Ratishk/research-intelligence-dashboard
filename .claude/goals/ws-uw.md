# GOAL: Goal 2 — Unusual Whales daily data into the Research Intelligence Dashboard
status: done
created: 2026-06-15T16:00:11Z
updated: 2026-06-15T16:04:00Z
branch: build/ws-uw

## Context
Add an Unusual Whales (UW) REST ingester to the main dashboard tree
(`/Users/ratishkorrapati/research-intelligence-dashboard`, which holds the gitignored
`.env` with `UW_API_KEY` and the canonical `research.db`). This replaces the yfinance
volume-ratio hack as the options-flow source. Basic tier = REST only, ~120 req/min,
~60K/day, ~90-day lookback. Endpoints are verified against skill.md
(https://unusualwhales.com/skill.md) and the OpenAPI (https://api.unusualwhales.com/api/openapi).
Follows the existing ingester pattern (form144.py, common.py, runner.py).

## Acceptance criteria  (each MUST be objectively checkable)
- [x] (1) `python -c "import app.ingestion.unusual_whales"` imports clean (exit 0).
      => prints IMPORT_OK; test_import_clean PASSED.
- [x] (2) Smoke test calls ONE light endpoint live (market-tide), ingests >=1 row into
      research.db, asserts pulled_at AND raw_json are populated, and asserts the API key
      never appears in captured logs.
      => test_live_market_tide_ingest_and_secret_redaction PASSED; 32 rows in uw_market_tide,
         pulled_at + raw_json populated; key-leak grep over app/ tests/ .claude = NO_LEAK.
- [x] (3) Idempotency: running the smoke ingest twice does not duplicate rows.
      => test_idempotency_market_tide PASSED (count unchanged on 2nd run);
         test_upsert_offline_dedup PASSED (first=True, second=False).
- [x] (4) `GET /api/unusual-whales` returns 200 with >=1 row, read from research.db
      (FastAPI TestClient, not a live passthrough).
      => test_api_route_returns_rows PASSED (200, count>=1, feeds.market_tide non-empty).
- [x] (5) `ruff check` and `python -m py_compile` clean on all new/edited files.
      => ruff "All checks passed!" on unusual_whales.py, models.py, migrate.py, runner.py,
         test_unusual_whales.py; py_compile COMPILE_OK. (4 pre-existing ruff errors in
         user-modified main.py regions left untouched per the "don't touch user edits" rule.)
- [x] (6) Every endpoint used exists in the OpenAPI whitelist (asserted in code/test).
      => All 6 paths confirmed present in https://api.unusualwhales.com/api/openapi and in
         skill.md; uw.WHITELIST asserted == used set in test_endpoints_whitelisted;
         _get() refuses non-whitelisted paths (test_non_whitelisted_get_refused).

## Definition of done
All six criteria green; smoke test passes against the live market-tide endpoint and
re-runs idempotently; the UW_API_KEY is never hardcoded/printed/logged/committed; the
GET /api/unusual-whales route serves persisted uw_* rows; uw_* tables are created via
both ORM models and migrate.py; the daily-job hook is DOCUMENTED (not executed); work
isolated on branch build/ws-uw with pre-existing uncommitted edits left untouched.

## Out of scope / hard stops
- NO Azure Container Apps Job creation/alteration and NO deploy (document the hook only).
- NO writing/printing/logging/committing the UW_API_KEY.
- NO `git add -A` / commit; leave the user's pre-existing modified files in the tree.
- NO websockets (Basic tier = REST only). NO inventing endpoints outside the whitelist.
- Keep live test calls tiny (one endpoint) to respect rate limits.

## Endpoints used (all verified in OpenAPI + skill.md whitelist)
- /api/market/market-tide          -> uw_market_tide   (key: timestamp)
- /api/option-trades/flow-alerts    -> uw_flow_alerts   (key: created_at+option_chain+ticker)
- /api/darkpool/recent              -> uw_darkpool      (key: tracking_id | executed_at+ticker)
- /api/congress/recent-trades       -> uw_congress      (key: politician_id+ticker+transaction_date+txn_type+filed_at_date)
- /api/insider/transactions         -> uw_insider       (key: ticker+owner_name+transaction_date+transaction_code+amount)
- /api/stock/{ticker}/greeks        -> uw_greeks        (key: ticker+date+expiry+strike)

## Daily-job hook (DOCUMENTED — NOT executed; staged for human approval)
HARD STOP honored: no Azure Container Apps Job created/altered, no deploy.

The UW refresh is already wired into the existing in-process scheduler via the
ingestion runner: `app/ingestion/runner.py::run_all_ingestion()` now calls
`unusual_whales.ingest_all(session)`, and that runner is invoked by
`app/scheduler.py::job_ingest()` which is registered on an hourly interval
(`scheduler.add_job(job_ingest, "interval", hours=1, id="ingest", ...)`). So UW
data already refreshes hourly with every other source — no extra wiring needed
for that cadence.

For a DEDICATED `0 10 * * * UTC` (daily 10:00 UTC) run as the prompt specifies,
the exact staged step (for human approval, NOT applied) is to add ONE line in
`app/scheduler.py::build_scheduler()` alongside the existing cron jobs:

    # daily UW refresh at 10:00 UTC (mirrors job_daily_findings cron style)
    scheduler.add_job(job_uw_refresh, "cron", hour=10, minute=0, id="uw_refresh")

backed by a small job fn (same shape as the others):

    def job_uw_refresh() -> None:
        from app.ingestion import unusual_whales
        with session_scope() as session:
            n = unusual_whales.ingest_all(session)
            logger.info("UW refresh: +%d rows", n)

This is left unimplemented pending approval because (a) UW already refreshes via
the hourly job_ingest, so a 2nd daily cron is optional, and (b) adding scheduler
jobs is a behavior change the operator should sign off on. No Azure job touched.

## Iteration log
### 2026-06-15 — iteration 1 (build → lint → test → green)
- Grounded: WebFetch skill.md (6 endpoints whitelisted) + downloaded OpenAPI YAML
  (759KB) and parsed exact schemas for all 6 paths + natural-key fields.
- Built: app/ingestion/unusual_whales.py (typed, shared Session, env-only auth via
  os.getenv, whitelist guard, 429/5xx backoff honoring Retry-After, no bare except,
  no key in logs). Added 6 ORM models + 6 CREATE TABLE IF NOT EXISTS migrations +
  GET /api/unusual-whales route (reads persisted rows) + runner registration.
- Lint: ruff "All checks passed!" on my files; py_compile clean. (4 ruff errors
  remain in pre-existing user edits in main.py — intentionally NOT modified.)
- Test: `python3 -m pytest tests/test_unusual_whales.py -v` => 7 passed in 0.99s.
  Live market-tide call returned 32 rows into research.db; idempotent on re-run;
  key-leak grep => NO_LEAK.
- Result: all 6 criteria green → status: done. No hard stop hit.
