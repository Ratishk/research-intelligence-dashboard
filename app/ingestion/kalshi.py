"""Prediction-market ingestion via Kalshi's public market-data API (no auth).

Kalshi is a CFTC-regulated US exchange with dedicated economic series (Fed
decisions, CPI, GDP, recession odds, index ranges) — a cleaner forecast source
than Polymarket for market theses. Only *trading* needs auth; reading market
data is free.

We pull open events in finance/econ/tech categories and store each market's
implied probability as a prediction Item tagged ``author="Kalshi"`` so the
cross-market validator can compare it against Polymarket.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.elections.kalshi.com/trade-api/v2/events"
_HEADERS = {"User-Agent": "research-dashboard/0.1", "Accept": "application/json"}
_TIMEOUT = 25
MAX_MARKETS = 40

# Kalshi categories worth keeping for an investment dashboard.
_KEEP_CATEGORIES = {
    "Financials", "Companies", "Economics", "Science and Technology",
}


def _price(market: dict) -> float | None:
    for key in ("last_price_dollars", "yes_bid_dollars", "previous_price_dollars"):
        v = market.get(key)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def ingest_all(session) -> int:
    source = (
        session.query(Source)
        .filter(
            Source.type == SourceType.prediction.value,
            Source.active.is_(True),
            Source.handle == "kalshi",
        )
        .first()
    )
    if source is None:
        return 0

    try:
        resp = requests.get(
            _ENDPOINT,
            params={"limit": 200, "status": "open", "with_nested_markets": "true"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        events = resp.json().get("events", [])
    except Exception:
        logger.info("Kalshi fetch failed")
        return 0

    added = 0
    for ev in events:
        if ev.get("category") not in _KEEP_CATEGORIES:
            continue
        ev_title = ev.get("title", "")
        for m in ev.get("markets", []):
            prob_raw = _price(m)
            if prob_raw is None:
                continue
            prob = round(prob_raw * 100)
            # Build a readable question: prefer the market's own title.
            q = m.get("title") or ev_title
            sub = m.get("yes_sub_title", "")
            if sub and sub.lower() not in q.lower():
                q = f"{q} ({sub})"
            title = f"{prob}% Yes — {q}"

            close = m.get("close_time") or ev.get("close_time")
            published = None
            if close:
                try:
                    published = datetime.fromisoformat(close.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass

            ticker = m.get("ticker", "")
            url = f"https://kalshi.com/markets/{ev.get('series_ticker','')}" if ev.get("series_ticker") else "https://kalshi.com"
            item = upsert_item(
                session,
                source=source,
                title=title[:300],
                url=url + (f"#{ticker}" if ticker else ""),
                content=f"{q}\nImplied: {prob}% Yes (Kalshi, {ev.get('category')})",
                author="Kalshi",
                media_type="prediction",
                published_at=published,
            )
            if item is not None:
                item.processed = True
                added += 1
            if added >= MAX_MARKETS:
                return added
    return added
