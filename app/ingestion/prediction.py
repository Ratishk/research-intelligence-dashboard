"""Prediction-market ingestion via Polymarket's public Gamma API.

Prediction markets are crowd-sourced probability forecasts of future events —
the literal "what will happen" layer. We pull active markets, keep the ones that
look finance/markets/tech/macro relevant, and store each as an Item carrying the
current implied probability. These are forecasts, not classified signals, so we
surface them in a dedicated view rather than the signal pipeline.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://gamma-api.polymarket.com/markets"
_HEADERS = {"User-Agent": "research-dashboard/0.1"}
_TIMEOUT = 25
MAX_MARKETS = 40

# Keep markets whose question looks economically relevant.
_RELEVANT = re.compile(
    r"\b(stock|shares?|nasdaq|s&p|dow|fed|rate|inflation|recession|gdp|ipo|earnings|"
    r"market cap|trillion|billion|\$[0-9]|AAPL|NVDA|TSLA|MSFT|GOOG|AMZN|META|bitcoin|"
    r"crypto|ethereum|oil|gold|tariff|chip|semiconductor|AI |OpenAI|Nvidia|Tesla|"
    r"Apple|valuation|acquire|merger|bankruptcy|default)\b",
    re.IGNORECASE,
)
# Drop obvious sports/entertainment noise.
_NOISE = re.compile(
    r"\b(match|vs\.?|atp|wta|nba|nfl|mlb|soccer|goal|album|movie|oscar|grammy|"
    r"super bowl|world cup|handicap|round of)\b",
    re.IGNORECASE,
)


def _parse_prices(market: dict) -> tuple[list[str], list[float]]:
    try:
        outcomes = json.loads(market.get("outcomes") or "[]")
        prices = [float(p) for p in json.loads(market.get("outcomePrices") or "[]")]
    except (json.JSONDecodeError, TypeError, ValueError):
        return [], []
    return outcomes, prices


def ingest_all(session) -> int:
    source = (
        session.query(Source)
        .filter(
            Source.type == SourceType.prediction.value,
            Source.active.is_(True),
            Source.handle == "polymarket",
        )
        .first()
    )
    if source is None:
        return 0

    try:
        resp = requests.get(
            _ENDPOINT,
            params={"limit": 200, "active": "true", "closed": "false",
                    "order": "volume", "ascending": "false"},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        markets = resp.json()
    except Exception:
        logger.info("Polymarket fetch failed")
        return 0
    if not isinstance(markets, list):
        return 0

    added = 0
    for m in markets:
        q = m.get("question", "")
        if not q or _NOISE.search(q) or not _RELEVANT.search(q):
            continue
        outcomes, prices = _parse_prices(m)
        if not prices:
            continue
        # Lead probability = highest-priced outcome.
        top_idx = max(range(len(prices)), key=lambda i: prices[i])
        prob = round(prices[top_idx] * 100)
        lead = outcomes[top_idx] if top_idx < len(outcomes) else "Yes"
        vol = m.get("volume")
        try:
            vol_str = f" · ${float(vol):,.0f} vol" if vol else ""
        except (TypeError, ValueError):
            vol_str = ""
        title = f"{prob}% {lead} — {q}"

        published = None
        end = m.get("endDate")
        if end:
            try:
                published = datetime.fromisoformat(end.replace("Z", "+00:00"))
            except ValueError:
                pass

        slug = m.get("slug", "")
        url = f"https://polymarket.com/event/{slug}" if slug else f"https://polymarket.com/market/{m.get('id','')}"
        content = f"{q}\nImplied: {prob}% {lead}{vol_str}\nOutcomes: {dict(zip(outcomes, prices))}"

        item = upsert_item(
            session,
            source=source,
            title=title[:300],
            url=url,
            content=content,
            author="Polymarket",
            media_type="prediction",
            published_at=published,
        )
        if item is not None:
            item.processed = True  # forecasts skip the signal classifier
            added += 1
        if added >= MAX_MARKETS:
            break
    return added
