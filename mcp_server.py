"""MCP stdio server exposing the research-intelligence-dashboard's data as tools.

This is a thin, read-only proxy over the dashboard's REST API (FastAPI on
:8000). It does not touch the dashboard's database or scheduler — it only makes
HTTP GET requests against the live API and reshapes the responses into compact,
LLM-friendly dicts.

Run it (stdio transport):

    python mcp_server.py

The base URL is taken from the ``DASHBOARD_API_URL`` env var and defaults to
``http://127.0.0.1:8000``. Every tool returns a JSON-serialisable dict and, on
any failure (dashboard offline, timeout, HTTP error), returns
``{"error": "..."}`` with a human-readable message rather than raising — so the
calling LLM always gets a clean, interpretable result.

Server name: ``research-dashboard``.
"""
from __future__ import annotations

import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

# --- Configuration -----------------------------------------------------------

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
BASE_URL = os.environ.get("DASHBOARD_API_URL", DEFAULT_BASE_URL).rstrip("/")
HTTP_TIMEOUT = 15  # seconds; all calls share this budget

mcp = FastMCP("research-dashboard")


# --- HTTP helper -------------------------------------------------------------


def _offline_message() -> str:
    """Human-readable hint for when the dashboard API can't be reached."""
    return (
        f"could not reach the research dashboard at {BASE_URL} — is it running? "
        f"start it with `python run.py` in the dashboard repo, or set "
        f"DASHBOARD_API_URL to the correct base URL"
    )


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET ``{BASE_URL}{path}`` and return the parsed JSON body.

    Returns the decoded JSON on success. On any error (connection refused,
    timeout, non-2xx status, malformed JSON) returns a dict shaped as
    ``{"error": "...", "__error__": True}`` so callers can detect and pass
    failures straight through to the LLM.
    """
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
    except requests.exceptions.Timeout:
        return {"error": f"request to {url} timed out after {HTTP_TIMEOUT}s", "__error__": True}
    except requests.exceptions.ConnectionError:
        return {"error": _offline_message(), "__error__": True, "__offline__": True}
    except requests.exceptions.RequestException as exc:  # pragma: no cover - defensive
        return {"error": f"request to {url} failed: {exc}", "__error__": True}

    if resp.status_code == 404:
        return {"error": "not_found", "__error__": True, "__status__": 404}
    if resp.status_code >= 400:
        return {
            "error": f"dashboard returned HTTP {resp.status_code} for {path}",
            "__error__": True,
            "__status__": resp.status_code,
        }

    try:
        return resp.json()
    except ValueError:
        return {"error": f"dashboard returned non-JSON body for {path}", "__error__": True}


def _is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("__error__") is True


def _clean_error(payload: dict[str, Any]) -> dict[str, str]:
    """Strip internal sentinel keys, leaving only the public ``error`` field."""
    return {"error": payload.get("error", "unknown error")}


def _drop_empty_lists(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with empty-list values removed."""
    return {k: v for k, v in data.items() if not (isinstance(v, list) and len(v) == 0)}


# --- Tools -------------------------------------------------------------------


@mcp.tool()
def get_signals(limit: int = 25, direction: str = "") -> dict[str, Any]:
    """Get the most recent investment signals from the research dashboard.

    Signals are classified, deduplicated market events drawn from SEC filings,
    congressional/insider/institutional trades, news, and thematic research.
    Each signal carries a direction (bullish/bearish/neutral), a confidence
    score, a short summary, and the tickers it concerns.

    Args:
        limit: Maximum number of signals to return (default 25). The newest
            signals come first.
        direction: Optional filter applied client-side. Pass "bullish",
            "bearish", or "neutral" to keep only signals with that direction.
            Empty string (default) returns all directions.

    Returns:
        ``{"signals": [...], "count": n}`` where each signal is
        ``{id, signal_type, direction, confidence, summary, tickers, industry,
        url, created_at}``. On failure: ``{"error": "..."}``.
    """
    data = _get("/api/signals", params={"limit": max(1, limit)})
    if _is_error(data):
        return _clean_error(data)
    if not isinstance(data, list):
        return {"error": "unexpected /api/signals response shape"}

    wanted = direction.strip().lower()
    signals: list[dict[str, Any]] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if wanted and str(raw.get("direction", "")).lower() != wanted:
            continue
        entities = raw.get("entities") or {}
        tickers = entities.get("tickers", []) if isinstance(entities, dict) else []
        signals.append(
            {
                "id": raw.get("id"),
                "signal_type": raw.get("signal_type"),
                "direction": raw.get("direction"),
                "confidence": raw.get("confidence"),
                "summary": raw.get("summary"),
                "tickers": tickers,
                "industry": raw.get("industry"),
                "url": raw.get("url"),
                "created_at": raw.get("created_at"),
            }
        )

    return {"signals": signals, "count": len(signals)}


@mcp.tool()
def get_conviction() -> dict[str, Any]:
    """Get the dashboard's conviction evidence packet.

    This is the aggregated, cross-source "what to pay attention to right now"
    bundle: the macro regime and indicators, non-consensus signals, smart-money
    divergences (where insiders/politicians/institutions disagree with the
    market), recent insider/politician/institutional trades, unusual-volume
    names, rising themes, and prediction conflicts. Use it for a single
    high-level read of current market conviction.

    Returns:
        The conviction packet as a dict (e.g. keys ``macro_regime``,
        ``smart_money_divergent``, ``insider_trades``, ``politician_trades``,
        ``institutional_stakes``, ``unusual_volume``, ``rising_themes``,
        ``prediction_conflicts``). Empty sections are omitted. On failure:
        ``{"error": "..."}``.
    """
    data = _get("/api/conviction")
    if _is_error(data):
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/conviction response shape"}
    return _drop_empty_lists(data)


@mcp.tool()
def get_smart_money(days: int = 14) -> dict[str, Any]:
    """Get recent smart-money activity: politician, insider, and institutional trades.

    Aggregates the "smart money" flow over a lookback window: congressional
    (politician) trades, corporate insider (Form 4) trades, institutional
    (13F) stake changes, a net flow summary, and "divergent" names where smart
    money is moving against the broader consensus.

    Args:
        days: Lookback window in days (default 14).

    Returns:
        ``{"politician_trades": [...], "insider_trades": [...],
        "institutional_trades": [...], "flow": {...}, "divergent": [...]}``.
        On failure: ``{"error": "..."}``.
    """
    data = _get("/api/smart-money", params={"days": max(1, days)})
    if _is_error(data):
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/smart-money response shape"}
    return data


@mcp.tool()
def get_ticker_dossier(symbol: str) -> dict[str, Any]:
    """Get a full per-ticker dossier: price, fundamentals, flow, and signals.

    Pulls everything the dashboard knows about one ticker into a single view:
    latest price and move, 52-week range, relative volume, short interest,
    analyst consensus, fundamentals, smart-money flow (insider/congress/
    institutional buy-vs-sell), recent classified signals, and any tracked
    theses or portfolio holding info.

    Args:
        symbol: Stock ticker symbol, e.g. "NVDA" (case-insensitive; it is
            upper-cased before the lookup).

    Returns:
        The ticker dossier as a dict. On failure: ``{"error": "..."}``.
    """
    sym = symbol.strip().upper()
    if not sym:
        return {"error": "symbol is required"}
    data = _get(f"/api/ticker/{sym}")
    if _is_error(data):
        if data.get("__status__") == 404:
            return {"error": f"no dossier found for ticker {sym}"}
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/ticker response shape"}
    return data


@mcp.tool()
def get_macro() -> dict[str, Any]:
    """Get the current macro snapshot: indicators, outlook, and regime.

    Returns key macro indicators (inflation, jobs, credit spreads, etc.) with
    their latest values, previous values, and direction, plus a composite
    outlook score/label and the current market regime classification.

    Returns:
        ``{"indicators": [...], "outlook": {...}, "regime": ..., "updated": ...}``.
        On failure: ``{"error": "..."}``.
    """
    data = _get("/api/macro")
    if _is_error(data):
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/macro response shape"}
    return data


@mcp.tool()
def get_material_events(days: int = 14) -> dict[str, Any]:
    """Get recent 8-K material corporate events for tracked tickers.

    Surfaces SEC Form 8-K filings (material events: earnings results,
    M&A, leadership changes, restructuring, etc.) classified by item code and
    direction, over a lookback window.

    Args:
        days: Lookback window in days (default 14).

    Returns:
        ``{"events": [{ticker, item_codes, label, direction, summary, url,
        filed}], "by_item": {"2.02": n, ...}}``. If the events endpoint is not
        yet deployed on the running dashboard, returns
        ``{"error": "events endpoint not available yet — restart the dashboard"}``.
        On other failures: ``{"error": "..."}``.
    """
    data = _get("/api/events8k", params={"days": max(1, days)})
    if _is_error(data):
        if data.get("__status__") == 404:
            return {"error": "events endpoint not available yet — restart the dashboard"}
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/events8k response shape"}
    return data


@mcp.tool()
def get_form144(days: int = 30) -> dict[str, Any]:
    """Get recent Form 144 filings: planned (intended) insider stock sales.

    Form 144 is filed when an insider intends to sell restricted/control
    stock — a forward-looking complement to Form 4 (executed sales). Each item
    names the seller, issuer, ticker, share count, approximate dollar value,
    and approximate intended sale date.

    Args:
        days: Lookback window in days (default 30).

    Returns:
        ``{"items": [{filed, person, issuer, ticker, shares, value_usd,
        approx_sale_date, url}], "count": n}``. If the Form 144 endpoint is not
        yet deployed on the running dashboard, returns
        ``{"error": "form144 endpoint not available yet — restart the dashboard"}``.
        On other failures: ``{"error": "..."}``.
    """
    data = _get("/api/form144", params={"days": max(1, days)})
    if _is_error(data):
        if data.get("__status__") == 404:
            return {"error": "form144 endpoint not available yet — restart the dashboard"}
        return _clean_error(data)
    if not isinstance(data, dict):
        return {"error": "unexpected /api/form144 response shape"}
    return data


if __name__ == "__main__":
    mcp.run(transport="stdio")
