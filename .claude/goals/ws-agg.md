# GOAL: Make Research Intelligence Dashboard aggregation optimal (Goal 4 / AUDIT §3.6)
status: done
created: 2026-06-15
updated: 2026-06-15
branch: build/ws-uw

## Context
The aggregation pipeline runs all ingesters sequentially in one DB session, has an N+1 in
watchlist ticker refresh, cold-rebuilds the 400-fund 13F consensus on every restart, and the
scheduler lacks max_instances/coalesce on most jobs. This goal parallelizes ingestion, fixes
the N+1, persists hot caches to disk, hardens the scheduler, adds a shared SEC token bucket,
uses last_crawled to dedup SEC walks, and drops the orphaned signal_fts table. Work lives in
app/ingestion, app/processing, app/scheduler.py, app/migrate.py, app/cache.py (new),
app/ingestion/sec_throttle.py (new). Branch build/ws-uw (has research.db + .env for benchmarking).

## Acceptance criteria
- [x] (1) `python -c "import app.main"` and `python -c "import run"` succeed
- [x] (2) tests/test_aggregation.py::test_scheduler_hardening_all_jobs — all 9 jobs max_instances=1 + coalesce=True
- [x] (3) Ingestion parallel (ThreadPoolExecutor, per-thread session); test_parallel_ingestion_isolation_and_keys (all keys + failure isolation + >1 thread). Benchmark below.
- [x] (4) refresh_watchlist_tickers parallel (thread pool + shared limiter) + batched signal_flow_scores; test_signal_flow_scores_batched + test_refresh_watchlist_parallel
- [x] (5) cold-start disk cache: test_funds_consensus_cold_start_from_disk + test_macro_cold_start_from_disk + test_disk_cache_roundtrip
- [x] (6) signal_fts absent after run_migrations: test_signal_fts_dropped (verified live: dropped from research.db)
- [x] (7) full suite: 15 passed

## Benchmark (one full run_all_ingestion pass against real research.db + .env)
- BEFORE (sequential, one session): SEQUENTIAL_TIME_SEC=355.5
- AFTER  (ThreadPoolExecutor, 8 workers, per-thread sessions): PARALLEL_TIME_SEC=159.7
- Speedup ~2.2x wall-clock. (After-run shows fewer new items due to dedup + the new
  last_crawled SEC cursor bounding submission walks — expected; the wall-clock figure is
  the valid comparison since SEC requests are gated by the shared ~9 req/s token bucket.)

## Definition of done
All criteria green, no `except Exception: pass`, per-ingester try/except preserved, UW ingester still
works, no secret printed/committed, committed on build/ws-uw with a clear message.

## Out of scope / hard stops
No Azure deploy. No committing .env/secrets. Do not hammer SEC (token bucket must gate). Optional
main.py router split only if everything else green with iterations to spare — else defer.

## Iteration log
- 2026-06-15: Implemented all 7 priority items. Parallelized runner (per-thread sessions,
  8 workers). Fixed tickers N+1 (batched signal_flow_scores + thread pool + shared limiter).
  Added app/cache.py disk cache; wired funds-consensus + macro cold-start load/save. Scheduler:
  max_instances=1 + coalesce=True on all 9 jobs. Added app/ingestion/sec_throttle.py shared
  token bucket; wired into 6 SEC ingesters + funds. last_crawled cursor bounds SEC walks
  (new_since_cursor/stamp_crawled). Dropped signal_fts in migrate.py. Added tests/test_aggregation.py
  (8 tests). Full suite 15 passed. Benchmark 355.5s -> 159.7s. Optional main.py split DEFERRED
  (item 8): 2334-line file, route-breakage risk outweighs benefit; left app intact.
