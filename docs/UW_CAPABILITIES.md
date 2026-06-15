# Unusual Whales API — Capabilities Map for LOGIQ

Generated 2026-06-15 by the `ws-uw-explore` workstream.

Sources of truth:
- **OpenAPI spec** — `https://api.unusualwhales.com/api/openapi` (YAML, **186 GET paths** across 21 tag groups). Authoritative for what *exists*.
- **Skill whitelist** — `https://unusualwhales.com/skill.md` (a curated anti-hallucination subset, not the full surface).
- **Live probes** — 29 endpoints hit once each with our Basic-tier key (read at runtime from the sibling main-tree `.env`; the value is never printed, logged, or committed).

Our tier: **Basic API** — REST only (no websockets), ~120 req/min, ~60K/day, ~90-day lookback.

Legend for **Tier?**: `LIVE 200` = probed and returned 200 on our key · `LIVE GATED` = probed, returned 401/403/422 tier error · `spec` = present in OpenAPI, not probed (status inferred). **Have?**: `YES` = already ingested by `app/ingestion/unusual_whales.py`.

---

## 1. Full endpoint catalog (by group)

### Gex / Greeks (11)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/stock/{ticker}/gex-levels` | GEX levels (key price levels of dealer gamma) | LIVE 200 | no |
| `/api/stock/{ticker}/greek-exposure` | Aggregate greek (gamma/delta/vanna/charm) exposure | LIVE 200 | no |
| `/api/stock/{ticker}/greek-exposure/expiry` | Greek exposure by expiry | spec | no |
| `/api/stock/{ticker}/greek-exposure/strike` | Greek exposure by strike | spec | no |
| `/api/stock/{ticker}/greek-exposure/strike-expiry` | Greek exposure by strike & expiry | spec | no |
| `/api/stock/{ticker}/greek-flow` | Intraday greek flow | spec | no |
| `/api/stock/{ticker}/greek-flow/{expiry}` | Greek flow by expiry | spec | no |
| `/api/stock/{ticker}/spot-exposures` | Spot GEX per 1 min | spec | no |
| `/api/stock/{ticker}/spot-exposures/strike` | Spot GEX by strike | spec | no |
| `/api/stock/{ticker}/spot-exposures/expiry-strike` | Spot GEX by strike & expiry | spec | no |
| `/api/stock/{ticker}/spot-exposures/{expiry}/strike` | (Deprecated) | spec | no |

Note: we ingest `/api/stock/{ticker}/greeks` (raw per-strike greeks) but **not** the aggregated `greek-exposure` / `gex-levels` series.

### Stock — flow, OI, IV, fundamentals, max-pain (37)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/stock/{ticker}/greeks` | Per-strike greeks | LIVE 200 (prior) | **YES** |
| `/api/stock/{ticker}/max-pain` | Max pain strike per expiry | LIVE 200 | no |
| `/api/stock/{ticker}/iv-rank` | IV rank / percentile | LIVE 200 | no |
| `/api/stock/{ticker}/oi-change` | Largest OI changes by contract | LIVE 200 | no |
| `/api/stock/{ticker}/nope` | NOPE (net options pricing effect) | LIVE 200 | no |
| `/api/stock/{ticker}/stock-volume-price-levels` | Off/lit volume by price level | LIVE 200 | no |
| `/api/stock/{ticker}/volatility/term-structure` | IV term structure | LIVE 200 | no |
| `/api/stock/{ticker}/interpolated-iv` | Interpolated IV surface | spec | no |
| `/api/stock/{ticker}/historical-risk-reversal-skew` | Risk-reversal skew history | spec | no |
| `/api/stock/{ticker}/atm-chains` | ATM chains | spec | no |
| `/api/stock/{ticker}/flow-alerts` | Per-ticker flow alerts | spec | no |
| `/api/stock/{ticker}/flow-recent` | Recent flows (per ticker) | spec | no |
| `/api/stock/{ticker}/flow-per-expiry` | Flow by expiry | spec | no |
| `/api/stock/{ticker}/flow-per-strike` | Flow by strike | spec | no |
| `/api/stock/{ticker}/flow-per-strike-intraday` | Intraday flow by strike | spec | no |
| `/api/stock/{ticker}/net-prem-ticks` | Call/put net & vol ticks | spec | no |
| `/api/stock/{ticker}/options-volume` | Options volume summary | spec | no |
| `/api/stock/{ticker}/oi-per-expiry` | OI per expiry | spec | no |
| `/api/stock/{ticker}/oi-per-strike` | OI per strike | spec | no |
| `/api/stock/{ticker}/option-chains` | Full option chain | spec | no |
| `/api/stock/{ticker}/option-contracts` | Option contract list | spec | no |
| `/api/stock/{ticker}/option/stock-price-levels` | Option-implied price levels | spec | no |
| `/api/stock/{ticker}/option/volume-oi-expiry` | Vol & OI per expiry | spec | no |
| `/api/stock/{ticker}/expiry-breakdown` | Expiry breakdown | spec | no |
| `/api/stock/{ticker}/ohlc/{candle_size}` | OHLC candles | spec | no |
| `/api/stock/{ticker}/stock-state` | Live quote/state | spec | no |
| `/api/stock/{ticker}/info` | Ticker info | spec | no |
| `/api/stock/{ticker}/ownership` | Ownership | spec | no |
| `/api/stock/{ticker}/insider-buy-sells` | Insider buy/sell totals | spec | no |
| `/api/stock/{ticker}/earnings` | Earnings history | spec | no |
| `/api/stock/{ticker}/income-statements` | Income statements | spec | no |
| `/api/stock/{ticker}/balance-sheets` | Balance sheets | spec | no |
| `/api/stock/{ticker}/cash-flows` | Cash flow statements | spec | no |
| `/api/stock/{ticker}/financials` | Full financials | spec | no |
| `/api/stock/{ticker}/fundamental-breakdown` | Fundamental breakdown | spec | no |
| `/api/stock/{ticker}/volatility/realized` | Realized vol | spec | no |
| `/api/stock/{ticker}/volatility/stats` | Vol stats | spec | no |
| `/api/stock/{ticker}/technical-indicator/{function}` | RSI/MACD/BBANDS/etc | spec | no |
| `/api/stock/{sector}/tickers` | Companies in sector | spec | no |

### Market — tide, OI change, sector ETFs, calendars (12)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/market/market-tide` | Market-wide net premium/volume tide | LIVE 200 (prior) | **YES** |
| `/api/market/oi-change` | Market-wide largest OI changes | LIVE 200 | no |
| `/api/market/{ticker}/etf-tide` | ETF-level tide | LIVE 200 | no |
| `/api/market/economic-calendar` | Economic events calendar | LIVE 200 | no |
| `/api/market/{sector}/sector-tide` | Sector net-premium tide | spec | no |
| `/api/market/sector-etfs` | Sector ETF snapshot | spec | no |
| `/api/market/total-options-volume` | Total market options volume | spec | no |
| `/api/market/top-net-impact` | Top net-impact tickers | spec | no |
| `/api/market/insider-buy-sells` | Market insider buy/sell totals | spec | no |
| `/api/market/correlations` | Cross-asset correlations | spec | no |
| `/api/market/fda-calendar` | FDA event calendar | spec | no |
| `/api/market/movers` | Top movers | **LIVE GATED (Advanced)** | no |
| `/api/net-flow/expiry` | Net options flow by expiry | LIVE 200 | no |

### Option trades (5)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/option-trades/flow-alerts` | Unusual flow alerts (whale trades) | LIVE 200 (prior) | **YES** |
| `/api/option-trades/flow-alerts/{id}` | One flow alert | spec | no |
| `/api/option-trades/optionable-tickers` | Optionable universe | spec | no |
| `/api/option-trades/exchange-breakdown/{date}` | Exchange/trade-code breakdown | spec | no |
| `/api/option-trades/full-tape/{date}` | Full option tape | **LIVE GATED (Advanced)** | no |

### Option contract (6)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/option-contract/{id}/flow` | Per-contract flow | spec | no |
| `/api/option-contract/{id}/historic` | Per-contract history | spec | no |
| `/api/option-contract/{id}/intraday` | Per-contract intraday | spec | no |
| `/api/option-contract/{id}/volume-profile` | Per-contract volume profile | spec | no |

### Darkpool / Lit flow (4)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/darkpool/recent` | Recent dark-pool prints (market-wide) | LIVE 200 (prior) | **YES** |
| `/api/darkpool/{ticker}` | Per-ticker dark-pool prints | LIVE 200 | no |
| `/api/lit-flow/recent` | Recent lit (on-exchange) trades | spec | no |
| `/api/lit-flow/{ticker}` | Per-ticker lit trades | spec | no |

### Screener (3)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/screener/analysts` | Analyst rating changes (up/downgrades, targets) | LIVE 200 | no |
| `/api/screener/option-contracts` | Hottest chains | spec | no |
| `/api/screener/stocks` | Stock screener | LIVE 200 | no |

### Alerts (2)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/alerts` | Your triggered custom alerts | LIVE 200 | no |
| `/api/alerts/configuration` | Alert configs | spec | no |

### Short data (7)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/shorts/{ticker}/data` | Short data summary | LIVE 200 | no |
| `/api/short_screener` | Short screener | spec | no |
| `/api/shorts/{ticker}/ftds` | Failures to deliver | spec | no |
| `/api/shorts/{ticker}/interest-float/v2` | Short interest & float | spec | no |
| `/api/shorts/{ticker}/interest-float` | (Deprecated v1) | spec | no |
| `/api/shorts/{ticker}/volume-and-ratio` | Short volume & ratio | spec | no |
| `/api/shorts/{ticker}/volumes-by-exchange` | Short volume by exchange | spec | no |

### Congress / Politicians (13)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/congress/recent-trades` | Recent congressional trades | LIVE 200 (prior) | **YES** |
| `/api/congress/congress-trader` | Reports by trader | spec | no |
| `/api/congress/late-reports` | Late disclosures | spec | no |
| `/api/congress/politicians` | Politicians with trade data | spec | no |
| `/api/congress/unusual-trades` | Unusual congressional trades | **LIVE GATED (premium)** | no |
| `/api/congress/unusual-trades/by-tickers` | Unusual by ticker | spec (premium) | no |
| `/api/congress/unusual-trades/chart-data` | Unusual chart data | spec (premium) | no |
| `/api/congress/unusual-trades/stats` | Unusual aggregate stats | spec (premium) | no |
| `/api/politician-portfolios/recent_trades` | Politician trades | spec | no |
| `/api/politician-portfolios/people` | Politicians list | spec | no |
| `/api/politician-portfolios/disclosures` | Annual disclosures | spec | no |
| `/api/politician-portfolios/holders/{ticker}` | Holders by ticker | spec | no |
| `/api/politician-portfolios/{politician_id}` | Politician portfolio | spec | no |

### Insiders (4)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/insider/transactions` | Insider transactions (market-wide) | LIVE 200 (prior) | **YES** |
| `/api/insider/{ticker}` | Per-ticker insiders | spec | no |
| `/api/insider/{ticker}/ticker-flow` | Ticker insider flow | spec | no |
| `/api/insider/{sector}/sector-flow` | Sector insider flow | spec | no |

### Institutions / 13F (7)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/institutions` | List of institutions | LIVE 200 | no |
| `/api/institutions/latest_filings` | Latest 13F filings | spec | no |
| `/api/institution/{name}/holdings` | Institution holdings | spec | no |
| `/api/institution/{name}/activity/v2` | Institutional activity | spec | no |
| `/api/institution/{name}/sectors` | Sector exposure | spec | no |
| `/api/institution/{ticker}/ownership` | Institutional ownership | spec | no |
| `/api/institution/{name}/activity` | (Deprecated) | spec | no |

### ETFs (5)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/etfs/{ticker}/in-outflow` | ETF inflow/outflow | LIVE 200 | no |
| `/api/etfs/{ticker}/holdings` | ETF holdings | spec | no |
| `/api/etfs/{ticker}/exposure` | ETF exposure | spec | no |
| `/api/etfs/{ticker}/weights` | Sector/country weights | spec | no |
| `/api/etfs/{ticker}/info` | ETF info | spec | no |

### Earnings / Companies (8)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/earnings/afterhours` | Afterhours earnings | LIVE 200 | no |
| `/api/earnings/premarket` | Premarket earnings | spec | no |
| `/api/earnings/{ticker}` | Historical ticker earnings | spec | no |
| `/api/companies/{ticker}/earnings-estimates` | Forward earnings estimates | spec | no |
| `/api/companies/{ticker}/profile` | Company profile | spec | no |
| `/api/companies/{ticker}/dividends` | Dividends | spec | no |
| `/api/companies/{ticker}/splits` | Splits | spec | no |
| `/api/companies/{ticker}/transcripts/{quarter}` | Earnings call transcript | spec | no |

### News (1)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/news/headlines` | News headlines (tagged tickers/sentiment) | LIVE 200 | no |

### Volatility (6)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/volatility/vix-term-structure` | VIX term structure | spec | no |
| `/api/volatility/anomaly/top` | Top vol anomalies | spec | no |
| `/api/volatility/character/top` | Top vol character | spec | no |
| `/api/stock/{ticker}/volatility/anomaly` | Vol anomaly score | spec | no |
| `/api/stock/{ticker}/volatility/character` | Vol character | spec | no |
| `/api/stock/{ticker}/volatility/variance-risk-premium` | Variance risk premium | spec | no |

### Seasonality (4)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/seasonality/market` | Market seasonality | LIVE 200 | no |
| `/api/seasonality/{ticker}/monthly` | Avg return per month | spec | no |
| `/api/seasonality/{ticker}/year-month` | Price change per month/year | spec | no |
| `/api/seasonality/{month}/performers` | Month performers | spec | no |

### Predictions (prediction markets) (9)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/predictions/whales` | Prediction-market whales | LIVE 200 | no |
| `/api/predictions/smart-money` | Prediction smart money | spec | no |
| `/api/predictions/unusual` | Unusual prediction markets | spec | no |
| `/api/predictions/insiders` | Prediction-market insiders | spec | no |
| `/api/predictions/market/{asset_id}` (+liquidity/positions) | Market detail | spec | no |
| `/api/predictions/user/{user_id}` / `search-users` | Users | spec | no |

### Intel / Calendar / Movers (5)
| Endpoint | Returns | Tier? | Have? |
|---|---|---|---|
| `/api/calendar/ipo` | IPO calendar | spec | no |
| `/api/companies/listings` | Active/delisted securities | spec | no |
| `/api/analytics/window` / `/api/analytics/sliding` | Windowed analytics | spec | no |
| `/api/market/movers` | Top movers | **LIVE GATED (Advanced)** | no |

### Group flow (2)
| `/api/group-flow/{flow_group}/greek-flow` (+`/{expiry}`) | Sector/group greek flow | spec | no |

### Crypto / Forex / Commodities / Digital currencies (10)
| `/api/crypto/whales/recent`, `/api/crypto/{pair}/state`, `/api/crypto/{pair}/ohlc/{candle_size}`, `/api/crypto/whale-transactions` | Crypto | spec | no |
| `/api/forex/rate`, `/api/forex/history`, `/api/forex/intraday` | FX | spec | no |
| `/api/commodities/{name}` | Commodity series | spec | no |
| `/api/economy/{indicator}` | Economic indicator series | spec | no |
| `/api/digital-currencies/history` / `/intraday` | Digital currency series | spec | no |

### Private markets (9) — **premium-gated**
`/api/private-markets/companies`, `/companies/{npm_ticker}` (+funding/investors/management/pricing), `/investors`, `/search` — all return **`Missing access for private markets` (premium)**. LIVE GATED. Not usable on Basic.

### Misc
| `/api/stock-directory/ticker-exchanges` | Ticker→exchange map | spec | no |

### Websocket (14) — **NOT usable on Basic**
`/api/socket`, `/api/socket/{flow_alerts,gex,market_tide,net_flow,option_trades,price,news,lit_trades,off_lit_trades,trading_halts,custom_alerts,contract_screener,interval_flow}`. The doc-describing GET returns 200, but the streaming socket itself requires a tier with websocket entitlement; our Basic tier is REST-only. Treat as unavailable.

---

## 2. Tier-access summary (from real probes)

**29 endpoints probed live** (one call each, key redacted). Result:

- **200 on Basic (25):** max-pain, iv-rank, oi-change (stock & market), volatility/term-structure, etfs/in-outflow, etf-tide, screener/analysts, screener/stocks, economic-calendar, earnings/afterhours, news/headlines, greek-exposure, gex-levels, net-flow/expiry, stock-volume-price-levels, darkpool/{ticker}, alerts, institutions, seasonality/market, shorts/data, predictions/whales, nope, socket/flow_alerts (doc only), and the 6 we already ingest.
- **GATED (4):**
  - `/api/private-markets/*` → 422 "Missing access for private markets" (premium)
  - `/api/option-trades/full-tape/{date}` → 422 "available with the API Advanced subscription"
  - `/api/market/movers` → 403 `advanced_tier_required`
  - `/api/congress/unusual-trades` (and its `by-tickers/chart-data/stats` siblings) → 422 "Missing access for unusual trades" (premium)

**Conclusion:** the Basic tier exposes essentially the entire analytical surface — flow, GEX/greeks, OI, IV/term structure, max-pain, dark pool, ETF flows, sector/ETF tide, analyst ratings, calendars, shorts, institutions, news. The only gaps are: **full raw option tape, top-movers screener, private markets, and the "unusual" congress sub-product** (all upsell/Advanced), plus **all websocket streaming** (REST-only tier). Most "spec"-marked rows above are very likely 200 too (they share the same tier surface as their probed siblings) but were left unprobed to respect the rate budget.

---

## 3. Recommended additions (prioritized)

We currently capture the *six smart-money basics*. The highest-value gaps for a contrarian RIA — where the Idea Desk needs a non-consensus edge and the Backtest needs point-in-time series — are the **options-positioning** and **flows/calendar** layers. All recommendations below are LIVE-200 on our tier.

### TOP 5 to implement next (ranked)

**1. Max Pain + OI Change (per watchlist ticker)** — `GET /api/stock/{ticker}/max-pain`, `GET /api/stock/{ticker}/oi-change`
- *What:* the max-pain strike per expiry, and the contracts with the largest open-interest changes.
- *Why:* Max pain is a classic contrarian magnet — when price sits far from max pain into a big expiry, there's mean-reversion pressure dealers help create. OI-change surfaces fresh positioning (new conviction vs. closing) the day it happens — exactly the "is the crowd just starting or capitulating?" question the Idea Desk asks. Both give clean point-in-time daily rows for Backtest.
- *Effort:* **Low.** Two new `uw_*` tables + two ingester functions cloned from `ingest_greeks` (same per-ticker watchlist loop, same hash/upsert). ~1–2 hrs.

**2. Net Options Flow per ticker + Net-Flow by expiry** — `GET /api/stock/{ticker}/net-prem-ticks`, `GET /api/net-flow/expiry`
- *What:* call/put net-premium & net-volume ticks per ticker, and market-wide net flow bucketed by expiry.
- *Why:* This is the single best directional read on smart-money option positioning per name — it's the per-ticker analog of the market-tide we already love. A name where price is flat but net call premium is quietly stacking is a textbook non-consensus setup. Expiry bucketing tells you if it's a gamma-squeeze-style 0DTE push vs. a real LEAP thesis. Strong Backtest signal.
- *Effort:* **Low-Medium.** One `uw_net_flow` table (ticker + bucketed nets) + ingester. ~2 hrs.

**3. Analyst Rating Changes** — `GET /api/screener/analysts`
- *What:* up/downgrades, initiations, and price-target revisions across the market.
- *Why:* Contrarian gold: the Idea Desk can flag names where flow/dark-pool is bullish *into* a downgrade (or vice versa) — the disagreement is the edge. It's also a discrete, dated event stream that backtests cleanly (event-study around rating changes). We have no analyst data today.
- *Effort:* **Low.** One `uw_analyst_ratings` table + one market-wide ingester (no watchlist loop). ~1 hr.

**4. ETF In/Outflow + Sector / ETF Tide** — `GET /api/etfs/{ticker}/in-outflow`, `GET /api/market/{sector}/sector-tide`, `GET /api/market/{ticker}/etf-tide`
- *What:* daily creation/redemption flows for ETFs, plus sector-level and ETF-level net-premium tide.
- *Why:* Rotation detection. Money leaving XLK and into XLE before price confirms is the macro/contrarian rotation signal the dashboard's sector views want. Sector tide is the sector analog of the market-tide we already chart — natural extension of an existing UI surface.
- *Effort:* **Medium.** One `uw_etf_flow` table + one `uw_sector_tide` table; needs a small ETF/sector list to iterate. ~2–3 hrs.

**5. Economic + FDA Calendar** — `GET /api/market/economic-calendar`, `GET /api/market/fda-calendar`
- *What:* upcoming macro releases and FDA decision dates, forward-dated.
- *Why:* The Idea Desk should know *what catalyst is coming* before suggesting a position; FDA dates pair directly with our existing biotech/openFDA streams to pre-stage event ideas. Forward calendar also lets Backtest annotate which signals fired ahead of a known catalyst.
- *Effort:* **Low.** One `uw_calendar` table with a `kind` column (econ/fda) + one ingester. ~1–1.5 hrs.

### Honorable mentions (next wave, not top-5)
- **IV Rank / IV term structure** (`/iv-rank`, `/volatility/term-structure`) — cheap-vs-rich options context for any idea; great for "options are mispriced for this catalyst" framing. Low effort.
- **GEX levels / aggregate greek-exposure** (`/gex-levels`, `/greek-exposure`) — dealer-positioning support/resistance levels; richer than the raw per-strike greeks we already store. Medium effort.
- **Per-ticker dark pool** (`/darkpool/{ticker}`) — we have market-wide dark pool; per-ticker lets the ticker dossier show off-exchange accumulation for a specific name. Trivial extension of the existing darkpool ingester.
- **Short data** (`/shorts/{ticker}/data`, `interest-float/v2`) — short-squeeze / crowded-short detection; overlaps somewhat with what we infer elsewhere. Low-medium effort.
- **Institutional 13F** (`/institutions/latest_filings`, `/institution/{name}/holdings`) — we ingest 13D/G via SEC already, but UW's pre-parsed 13F holdings + activity would be cleaner than our SEC parse. Medium effort, some overlap.
- **News headlines** (`/news/headlines`) — overlaps heavily with our existing RSS/news streams; low marginal value, skip unless we want UW's ticker/sentiment tagging.

### Do not pursue (tier-gated on Basic)
`market/movers` (Advanced), `option-trades/full-tape` (Advanced), `congress/unusual-trades*` (premium), `private-markets/*` (premium), all `/api/socket/*` streaming (REST-only tier).

---

## 4. Recommendation

Implement **#1 (max-pain + OI change)** first — it is the lowest-effort, highest-signal addition, reuses the exact `ingest_greeks` watchlist pattern, and directly feeds both the Idea Desk (contrarian max-pain pull) and Backtest (clean daily series). Then #3 (analyst ratings — trivial, opens a whole new contrarian-disagreement signal) and #2 (per-ticker net flow — the per-name smart-money read). #4 and #5 round out rotation + catalyst awareness.

*Probe methodology note: tier status for the 29 probed endpoints is empirical (one live GET each, 2026-06-15). All other rows are marked `spec` and inferred from the OpenAPI; they share the same tier surface as their probed siblings and are expected to be 200 on Basic, but were not individually confirmed to respect the rate budget.*
