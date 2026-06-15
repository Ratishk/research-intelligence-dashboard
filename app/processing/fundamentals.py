"""Company fundamentals from SEC EDGAR (companyfacts XBRL API).

yfinance quoteSummary hard-throttles this host, and the DB only tracks ~37
tickers, so the Investor Lens dossier had no balance-sheet data for off-watchlist
names. The SEC companyfacts API is free, un-throttled, and authoritative, so we
pull the most-recent annual (10-K) figures straight from there.

We reuse form4's cached ticker->CIK map and SEC User-Agent. resolve_symbol also
fuzzy-matches a company name (e.g. "harmonic" -> "HLIT") so the lens accepts a
name as well as a symbol.
"""
from __future__ import annotations

import logging
import time
from datetime import date

import requests

from app.ingestion.form4 import _HEADERS, _TIMEOUT, _TICKERS_URL, _load_cik_map

logger = logging.getLogger(__name__)

_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# Module-level TTL cache: ticker -> (timestamp, result). companyfacts changes
# only on new filings, so a long TTL is fine.
_cache: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = 24 * 3600  # seconds

# Cached company-name index for fuzzy symbol resolution: ticker -> title.
_name_index: dict[str, str] | None = None

# Revenue is reported under several us-gaap concepts depending on the filer;
# try them in order of preference.
_REVENUE_CONCEPTS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
]
_DEBT_CONCEPTS = ["LongTermDebtNoncurrent", "LongTermDebt"]


def _load_name_index() -> dict[str, str]:
    """ticker (upper) -> company title, loaded once from company_tickers.json."""
    global _name_index
    if _name_index is not None:
        return _name_index
    try:
        resp = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        _name_index = {v["ticker"].upper(): str(v["title"]) for v in data.values()}
    except Exception:
        logger.exception("Failed to load SEC ticker->name index")
        _name_index = {}
    return _name_index


def resolve_symbol(query: str) -> str | None:
    """Resolve a ticker OR company name to a ticker symbol.

    "HLIT" -> "HLIT"; "harmonic" -> "HLIT". Falls back to fuzzy name matching
    (case-insensitive substring / token overlap, preferring the shortest ticker).
    """
    if not query:
        return None
    q = query.strip().upper()
    if not q:
        return None
    cik_map = _load_cik_map()
    if q in cik_map:
        return q

    names = _load_name_index()
    if not names:
        return None
    ql = query.strip().lower()
    q_tokens = set(ql.split())
    best: tuple[int, int, str] | None = None  # (score, -len(ticker), ticker)
    for ticker, title in names.items():
        title_l = title.lower()
        title_tokens = set(title_l.split())
        if ql and ql in title_l:
            score = 3
        elif q_tokens and q_tokens <= title_tokens:
            score = 2
        elif q_tokens & title_tokens:
            score = 1
        else:
            continue
        cand = (score, -len(ticker), ticker)
        if best is None or cand > best:
            best = cand
    return best[2] if best else None


def _full_year(pt: dict) -> bool:
    """True if a duration datapoint spans ~a full fiscal year (not a quarter).

    Many concepts tag quarterly values with fp=FY (e.g. frame CY2013Q4), so the
    only reliable annual filter is the start..end span. Balance-sheet items are
    instant (no start) and always pass.
    """
    start, end = pt.get("start"), pt.get("end")
    if not start:
        return True  # instant (point-in-time) value
    try:
        span = (date.fromisoformat(end) - date.fromisoformat(start)).days
    except (TypeError, ValueError):
        return False
    return span >= 330  # ~11+ months → full year


def _annual_points(units: dict) -> list[dict]:
    """Return annual (10-K, full fiscal year) datapoints from a concept's units.

    Picks USD (or first available unit), keeps form 10-K full-year points, and
    sorts most-recent fiscal year first (break ties by period end, then filing).
    """
    series = units.get("USD") or next(iter(units.values()), [])
    annual = [
        pt for pt in series
        if pt.get("form") == "10-K" and _full_year(pt)
        and (pt.get("fy") is not None or pt.get("end") is not None)
    ]
    annual.sort(key=lambda p: (p.get("end") or "", p.get("fy") or 0, p.get("filed") or ""), reverse=True)
    return annual


def _latest_and_prior(facts: dict, concepts: list[str]) -> tuple[float | None, float | None, int | None]:
    """Latest and prior-year annual value across the given concepts.

    Concepts are alternative tags for the same line item; we pick the one with
    the most recent annual datapoint (a filer may switch tags over time, leaving
    an older tag with stale full-year values). Returns (latest, prior, fiscal_year).
    """
    best_points: list[dict] | None = None
    for concept in concepts:
        node = facts.get(concept)
        if not node:
            continue
        points = _annual_points(node.get("units", {}))
        if not points:
            continue
        if best_points is None or (points[0].get("end") or "") > (best_points[0].get("end") or ""):
            best_points = points
    if not best_points:
        return None, None, None
    latest = best_points[0]
    fy = latest.get("fy")
    latest_end = latest.get("end")
    prior = None
    for pt in best_points[1:]:
        if pt.get("end") != latest_end:
            prior = pt.get("val")
            break
    return latest.get("val"), prior, fy


def _latest_end(facts: dict, concepts: list[str]) -> str | None:
    """Most-recent annual period-end (ISO date) across the given concepts — the
    'as of' date for the figures, so the UI can show how fresh the filing is."""
    best: str | None = None
    for concept in concepts:
        node = facts.get(concept)
        if not node:
            continue
        points = _annual_points(node.get("units", {}))
        if points and (best is None or (points[0].get("end") or "") > best):
            best = points[0].get("end")
    return best


def _div(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return round(num / den, 4)


def get_fundamentals(ticker: str) -> dict | None:
    """Most-recent annual fundamentals for a ticker from SEC EDGAR companyfacts.

    Returns a dict of headline figures + computed ratios, or None if the ticker
    has no CIK or the fetch fails. Degrades to a partial dict (None fields) when
    individual concepts are missing — never raises.
    """
    if not ticker:
        return None
    sym = ticker.strip().upper()
    cached = _cache.get(sym)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]

    result: dict | None = None
    try:
        cik = _load_cik_map().get(sym)
        if cik is None:
            _cache[sym] = (time.time(), None)
            return None
        resp = requests.get(_FACTS_URL.format(cik=cik), headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        gaap = (data.get("facts") or {}).get("us-gaap", {})
        dei = (data.get("facts") or {}).get("dei", {})

        revenue, revenue_prior, fy = _latest_and_prior(gaap, _REVENUE_CONCEPTS)
        net_income, _, ni_fy = _latest_and_prior(gaap, ["NetIncomeLoss"])
        gross_profit, _, _ = _latest_and_prior(gaap, ["GrossProfit"])
        operating_income, _, _ = _latest_and_prior(gaap, ["OperatingIncomeLoss"])
        total_assets, _, _ = _latest_and_prior(gaap, ["Assets"])
        total_liabilities, _, _ = _latest_and_prior(gaap, ["Liabilities"])
        shareholders_equity, _, _ = _latest_and_prior(gaap, ["StockholdersEquity"])
        cash, _, _ = _latest_and_prior(gaap, ["CashAndCashEquivalentsAtCarryingValue"])
        debt, _, _ = _latest_and_prior(gaap, _DEBT_CONCEPTS)
        shares, _, _ = _latest_and_prior(gaap, ["CommonStockSharesOutstanding"])
        if shares is None:
            shares, _, _ = _latest_and_prior(dei, ["EntityCommonStockSharesOutstanding"])

        result = {
            "fiscal_year": fy or ni_fy,
            "revenue": revenue,
            "revenue_yoy_growth": _div(
                (revenue - revenue_prior) if revenue is not None and revenue_prior is not None else None,
                revenue_prior,
            ),
            "net_income": net_income,
            "net_margin": _div(net_income, revenue),
            "gross_margin": _div(gross_profit, revenue),
            "operating_margin": _div(operating_income, revenue),
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "shareholders_equity": shareholders_equity,
            "cash": cash,
            "debt_to_equity": _div(debt, shareholders_equity),
            "shares_outstanding": shares,
            "period_end": _latest_end(gaap, _REVENUE_CONCEPTS),
            "source": "SEC EDGAR companyfacts",
        }
    except Exception:
        logger.warning("companyfacts fetch failed for %s", sym, exc_info=True)
        result = None

    _cache[sym] = (time.time(), result)
    return result
