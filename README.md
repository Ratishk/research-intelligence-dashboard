# Research Intelligence Dashboard

An always-on research engine that monitors **every industry** — YouTube, X/Twitter,
blogs, newsletters, forums, SEC filings, and community signals — then classifies each
item into investment signals, ticker watchlists, and trends. A multi-tab web dashboard
replaces the manual scan, and a daily email digest keeps the original workflow.

## What it does

```
Sources (16 industries, 10+ vetted each)
   ├─ RSS / newsletters ──► FreshRSS backbone (optional) or direct feedparser
   ├─ X / Twitter ────────► Grok live X search (batched, replaces Twitter API)
   ├─ YouTube ────────────► Data API v3 + transcript extraction
   └─ SEC / HN / Reddit ──► free public APIs
        │
        ▼
   Items DB ──► Haiku relevance filter ──► Sonnet signal classifier ──► Signals
        │                                                                  │
        ▼                                                                  ▼
   Discovery engine (Perplexity/Grok find → Claude score 0–12 →    Watchlists, Trends,
   retire low-yield → refill toward 30/industry)                   Research briefs, Digest
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY, XAI_API_KEY, YOUTUBE_API_KEY at minimum
python seed_sources.py        # seed 16 industries + verified sources + 5 watchlists
python run.py                 # scheduler + dashboard at http://localhost:8000
```

Optional FreshRSS backbone for scaling RSS:

```bash
docker compose up -d          # FreshRSS at :8080; set FRESHRSS_* in .env to use it
```

## Cost tiers

One `COST_TIER` env var (`lean` | `standard` | `max`) controls the Haiku→Sonnet
relevance threshold, X polling cadence, and discovery frequency:

| Tier | Coverage | Est. monthly |
|------|----------|--------------|
| lean | 6–8 industries | ~$90 |
| standard | all 16 | ~$240 |
| max | hourly polling, daily discovery | ~$465 |

Sonnet classification is the dominant cost; the Haiku pre-filter gates it to the
~25–30% of items that matter. xAI free credits can zero out the Grok lines.

## Tabs

Feed · YouTube · Twitter/X · Blogs & Forums · Signals · Watchlists · Trends ·
Industries (live source count, discover more) · Sources (registry + vetting scores).

## Layout

```
app/
  ingestion/   freshrss, youtube, twitter (Grok), sec, community, rss, runner
  processing/  classifier, insights, research, tickers, digest
  discovery/   finder, scorer, recycler, engine
  llm/         claude, grok, perplexity
  main.py      FastAPI routes        scheduler.py  APScheduler jobs
  models.py    SQLAlchemy schema     seed_data.py  verified source list
frontend/      index.html, app.js, charts.js, styles.css
seed_sources.py · run.py · docker-compose.yml
```

Source selection, vetting rubric, and per-industry lists are documented in the plan.
Not investment advice.
