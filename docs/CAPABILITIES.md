# Research Intelligence Dashboard — Capabilities Audit

> A machine-readable map of everything this program does: data sources, processing,
> scoring, APIs, automation, and explicit non-goals. Intended to be fed to another
> tool/LLM to brainstorm features or **honest** (non-overfit) predictive ideas.
> Generated from the codebase on 2026-06-07.

## 1. What it is
An always-on, **mostly-free** investment research aggregator focused on **non-consensus
signals** — surfacing what insiders, politicians, hedge funds, and primary filings are
doing *before* it's consensus. It aggregates, classifies, scores, and tracks; it does
**not** make price predictions or buy/sell calls (see §10).

**Stack:** FastAPI + SQLAlchemy + SQLite + APScheduler (backend); vanilla JS + Chart.js
(frontend, no build step). LLM = Anthropic Claude (Haiku for cheap filtering, Sonnet for
classification/synthesis). Cost-tiered (~$25/mo target on the "budget"/"lean" tier).

## 2. Data sources (all free unless noted)
Each runs as an ingester (`app/ingestion/`) on a schedule, producing **Items** → **Signals**.

| Source | Module | What it pulls |
|---|---|---|
| SEC 8-K / filings | `sec.py`, `events8k.py` | Material corporate events |
| Insider Form 4 | `form4.py` | Insider buys/sells (EDGAR) |
| Insider Form 144 | `form144.py` | Insider intent-to-sell |
| Institutional 13D/13G | `institutional.py` | Activist / >5% stakes |
| Hedge-fund 13F | `processing/funds.py` | 400 funds' quarterly holdings + cross-fund consensus |
| Insider cluster buys | `processing/openinsider.py` | Multiple insiders buying same name (OpenInsider, via Scrapling) |
| Congress trades | `congress.py` | Politician STOCK Act disclosures |
| Dilution | `dilution.py` | Shelf/offering/S-1/S-3 filings |
| FTD | `processing/ftd.py` | SEC fails-to-deliver |
| Short volume | `processing/short_volume.py` | FINRA daily short volume % |
| Unusual volume | `processing/volume.py` | Volume vs 20-day average |
| Gov contracts | `govcontracts.py` | USASpending federal awards |
| FDA recalls | `fda.py`, `biotech.py` | openFDA recalls, clinical trials |
| Patents | `patent.py` | Patent grants |
| Prediction markets | `kalshi.py`, `prediction.py` | Kalshi / event odds |
| Macro | `processing/macro.py` | FRED economic series |
| News tone | `processing/news_tone.py` | GDELT sentiment |
| Social | `twitter.py`, `bluesky.py`, `community.py` | X, Bluesky, forums/Reddit/HN |
| Video / research | `youtube.py`, `arxiv` | Transcripts, papers |
| RSS / FreshRSS | `rss.py`, `freshrss.py` | News feeds |

**Signal types:** commercial_deployment, pilot, funding, bottleneck, displacement, none.
**Directions:** bullish / bearish / neutral.

## 3. Intelligence / processing (`app/processing/`)
- **classifier.py** — Claude pipeline: Haiku relevance pre-filter → Sonnet classifies each
  item into a Signal (type, direction, confidence, entities). Hard daily cap for cost control.
- **alpha.py** — **Alpha score (0–12)** = non-consensus value. Four 0–3 components:
  *timeliness* (freshness), *originality* (how primary the source), *exclusivity* (how few
  other signals share the entities), *contrarian* (opposes the ticker's prevailing direction).
- **consensus.py** — **Crowd-belief score** per ticker, blended from analyst ratings (0.35),
  short interest (0.20), social (0.20), FINRA short-volume (0.15), and signal flow (0.20);
  normalized over present components. Produces a buy/hold/sell-style consensus label.
- **funds.py** — 13F holdings, quarter-over-quarter changes (new/added/trimmed/exited), and
  **cross-fund consensus** (which names the 400 funds collectively hold, by # of funds).
- **smart-money** (in `main.py`) — **divergence**: smart-money flow (insider + politician +
  institutional buys/sells) vs the crowd consensus → highest-conviction non-consensus setups.
- **theses.py** — **Thesis tracker**: every new signal is auto-classified as *confirming* or
  *contradicting* your active theses (long/short/watch).
- **research.py** — **Daily Findings**: a two-pass DeepResearch (extract → verify) over
  everything tracked, distilled to the day's 1–5 most actionable non-consensus ideas.
- **rag.py** — **Ask**: free local FTS5 retrieval over the signal corpus; only the final
  grounded, cited answer spends tokens.
- **insights.py** — theme-shift detection / shift alerts (a theme's net sentiment flips).
- **investor_lens.py**, **knowledge.py**, **attention.py**, **news_tone.py**, **digest.py**,
  **tickers.py** — investor-style framing, attention scoring, tone, email digest, watchlist
  enrichment (price/consensus/short%/RVOL).

## 4. Scoring summary (all rule-based, transparent)
- **Alpha 0–12** — how *non-consensus & primary* a signal is.
- **Consensus −1..+1** — how bullish/bearish the *crowd* is (to find divergence).
- **Smart-money flow −1..+1** — net insider/politician/institutional buying.
- **Fund consensus (count)** — how many of 400 funds hold a name.
- No machine-learned weights; everything is inspectable.

## 5. API endpoints (FastAPI)
Intelligence: `/api/conviction` (+`/synthesize`), `/api/findings/{today,history,generate}`,
`/api/ask`, `/api/home`, `/api/brief` (+`/synthesize`), `/api/digest/{preview,send}`.
Smart money: `/api/smart-money`, `/api/funds`, `/api/funds/{cik}`, `/api/fund-consensus`,
`/api/insider-clusters`, `/api/investor-lens`, `/api/short-volume`, `/api/ftd`,
`/api/unusual-volume`.
Markets/macro: `/api/macro`, `/api/pulse`, `/api/composites`, `/api/trends`,
`/api/theme-shifts`, `/api/shift-alerts`, `/api/predictions`, `/api/consensus`.
Signals/data: `/api/signals` (+`/{id}/research`), `/api/feed`, `/api/ticker/{symbol}` (dossier),
`/api/watchlists` (+CRUD), `/api/industries` (+discover), `/api/sources` (+CRUD), `/api/theses`
(+CRUD, `/evaluate`), `/api/ingest` (+`/status`), `/api/config`.

## 6. Frontend tabs
`Daily Findings` (flagship), `Ask`, `Home`, `Theses`, `Guide`, **`Smart Money`** (subtabs:
Flow & divergence, Recent trades, Hedge funds 13F, **Fund consensus**, **Insider clusters**),
`Forecasts`, `Investor Lens`, `Macro & Pulse`, `Signals`, `Theme Shifts`, `Trends`, `Feed`,
`Watchlists`, `Industries`, `Sources`. Plus a click-anywhere **Ticker Dossier** modal.

## 7. Automation (APScheduler, UTC)
ingest (1h), classify (1h), refresh tickers (6h), discovery (tier-based), shift alerts (4h),
daily findings (11:00 ≈ 7am ET), evaluate theses (6h), **fund consensus warm (09:07 ≈ 5am ET)**,
**fund consensus filing-window refresh (every 3h, only Feb/May/Aug/Nov 13F windows)**.

## 8. Data model (SQLite)
`industries, sources, items, signals, tickers, watchlist_items, digests, theses,
thesis_evidence, daily_findings, shift_alerts`. Signals carry: type, direction, confidence,
entities_json (tickers/companies/technologies), alpha components, timestamps.

## 9. Cost / LLM
Tiers (`COST_TIER`): budget (~$25/mo) / lean / standard. Levers: Haiku heuristic pre-filter,
hard daily classify cap (e.g., 250/day on budget), Sonnet only for classification/synthesis,
free local retrieval for Ask. Models: Haiku 4.5, Sonnet 4.6.

## 10. Explicit NON-goals (important for any "prediction" ideas)
This system is deliberately **descriptive, not predictive**:
- ❌ No price targets, no return forecasts from black-box ML, no buy/sell recommendations.
- ❌ No sentiment→price regressions or overfit models.
- ✅ It surfaces *what primary actors are doing*, scores how non-consensus it is, and
  *tracks whether theses play out*. Any predictive extension should stay **transparent and
  empirical** (e.g., historical base rates / hit rates of a signal), not opaque.

## 11. Good places to extend (open questions for a brainstorming tool)
- We have rich *point-in-time* signals but limited **forward-outcome tracking**. The highest-
  integrity "predictive" add is **empirical base rates**: store signals + measure forward
  N-day returns to report honest hit rates per signal type (e.g., "insider cluster-buys →
  +X% median 1M, n=…"). No model, just history.
- We compute many independent signals but don't yet have a single **cross-signal agreement**
  view (where insider + 13F + congress + low crowd-consensus all align on one ticker).
- 13F data supports **accumulation vs distribution** momentum (added vs trimmed) that isn't
  surfaced yet.
- Mega-cap dominance in 400-fund consensus suggests a "hidden overlap" filter (exclude
  obvious mega-caps to spotlight quiet smart-money agreement).
