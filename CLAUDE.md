# CLAUDE.md — Research Intelligence Dashboard

## Working principles
Adapted from Andrej Karpathy's observations on LLM coding pitfalls
(github.com/multica-ai/andrej-karpathy-skills):

1. **Think before coding.** State assumptions explicitly; if uncertain, ask. Surface
   tradeoffs rather than deciding silently.
2. **Simplicity first.** The minimum code that solves the problem. Nothing speculative —
   no unasked-for abstractions, flags, or flexibility.
3. **Surgical changes.** Touch only what you must. Match the surrounding style. Clean up
   only the mess your change creates.
4. **Goal-driven execution.** Define success criteria up front, then loop until verified.
   Verification here = run the real surface (curl the endpoint, screenshot the page), not
   "the code looks right."

## What this is
An always-on research engine that aggregates **16 free data streams** into investment
signals and surfaces the **non-consensus** ones. FastAPI + SQLAlchemy + SQLite +
APScheduler backend; vanilla JS + Chart.js frontend (no build step). Run: `python3 run.py`
(port 8000). Seed: `python3 seed_sources.py`. Flagship = the **Daily Findings** tab
(two-pass DeepResearch over everything → the day's top 1–5 actionable, non-consensus ideas).

## Architecture (where things live)
- `app/ingestion/*.py` — one module per source (rss, youtube, twitter, sec/form4,
  institutional 13D-G, dilution, congress, kalshi, prediction/polymarket, patent,
  biotech, community). Each exposes `ingest_all(session)`; `runner.py` calls them all.
- `app/processing/*.py` — classifier (Haiku filter → Sonnet signal), alpha (0–12
  non-consensus score), consensus (crowd-belief vector), volume, macro, news_tone,
  attention, knowledge (DeepResearch 2-pass), investor_lens, tickers.
- `app/main.py` — all FastAPI routes + the heavy aggregators (`_smart_money_data`,
  `_conviction_packet`, `_ticker_dossier`). `models.py` = schema. `migrate.py` = idempotent
  ALTER TABLE on startup (SQLite create_all won't add columns to existing tables).
- `frontend/` — `index.html` (sidebar + views), `app.js` (one render fn per tab, a tab
  router, ⌘K palette), `styles.css` (design tokens), `charts.js`.

## Conventions & traps (learned the hard way — honor these)
- **SQLite stores datetimes naive.** Always coerce to UTC before arithmetic/compare
  (`dt.replace(tzinfo=timezone.utc)`). This bug recurs — see `alpha._as_utc`, `main._as_aware`.
- **`upsert_item` must flush-then-catch IntegrityError.** A duplicate `content_hash`
  otherwise rolls back the whole ingestion batch (zero items saved).
- **New ingester?** Clone `form4.py`/`kalshi.py`. Add the `SourceType` enum value, seed
  sources in `seed_data.py` (+ wire `seed_sources.py`), add the `runner.py` call. Sources
  sharing a URL collide across types — give synthetic sources empty url (seeder makes a
  unique `internal://` key) and put the key in `handle`.
- **Rate-limited free sources degrade gracefully** (try/except → return []/None, never
  crash the pipeline). yfinance quoteSummary, StockTwits, FRED, GDELT, Google Patents,
  Quiver all throttle this host. For prices/volume use the **Yahoo v8 chart** endpoint
  (`query1.finance.yahoo.com/v8/finance/chart/`) — it is NOT throttled, unlike yfinance.
- **Don't over-pull / re-compute.** External data is stored on the `Ticker` table (6h
  cron) and reused. Heavy aggregators are TTL-cached: `score_signals` 60s, `_smart_money_data`
  90s, `get_macro` 1h, attention/tone 1–6h. Reuse these, don't add parallel fetches.
- **LLM JSON:** use `claude.extract_json`; give Sonnet enough `max_tokens` (a truncated
  response = unparseable JSON). Multi-step (extract → verify) beats single-shot for accuracy.

## Verify before claiming done
- API: `curl -s localhost:8000/api/<route>` and inspect the JSON.
- UI: playwright is available — screenshot the page (`/tmp/*.png`), read it back.
- Never report success on a feature you haven't actually run.

## Secrets
API keys live in `.env` (gitignored). Never echo them, commit them, or print them.
