"""Congressional trade ingestion via Quiver Quant's public live feed.

Politicians' disclosed stock trades are a well-known non-consensus signal —
they often trade ahead of legislation, hearings, and contract awards. Quiver's
``beta/live/congresstrading`` endpoint serves recent House + Senate trades
without auth (it throttles unauthenticated clients, so we degrade gracefully).

Each trade becomes a signal:
  Purchase → bullish, Sale → bearish.
The ``ExcessReturn`` field (trade vs SPY) is folded into the summary as a
track-record marker when present.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.ingestion.common import upsert_item
from app.models import Direction, Signal, SignalType, Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.quiverquant.com/beta/live/congresstrading"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
_TIMEOUT = 25
MAX_TRADES = 60  # per ingestion pass

_BUY_WORDS = ("purchase", "buy")
_SELL_WORDS = ("sale", "sell")


def _direction(txn: str) -> str:
    t = (txn or "").lower()
    if any(w in t for w in _BUY_WORDS):
        return Direction.bullish.value
    if any(w in t for w in _SELL_WORDS):
        return Direction.bearish.value
    return Direction.neutral.value


def _fetch() -> list[dict]:
    try:
        resp = requests.get(_ENDPOINT, headers=_HEADERS, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.info("Quiver congress feed unavailable (HTTP %s)", resp.status_code)
            return []
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception:
        logger.info("Quiver congress feed fetch failed (throttled or offline)")
        return []


def ingest_all(session) -> int:
    """Pull recent congressional trades into signals. Returns count added."""
    source = (
        session.query(Source)
        .filter(Source.type == SourceType.congress.value, Source.active.is_(True))
        .first()
    )
    if source is None:
        return 0  # not seeded

    trades = _fetch()
    if not trades:
        return 0

    added = 0
    for tr in trades[:MAX_TRADES]:
        ticker = (tr.get("Ticker") or "").strip().upper()
        if not ticker or ticker in ("--", "N/A"):
            continue
        rep = tr.get("Representative", "Unknown")
        party = tr.get("Party", "")
        house = tr.get("House", "")
        txn = tr.get("Transaction", "")
        rng = tr.get("Range", "")
        tdate = tr.get("TransactionDate") or tr.get("ReportDate") or ""
        direction = _direction(txn)

        # Track-record marker: how this politician's trade did vs SPY.
        excess = tr.get("ExcessReturn")
        perf = ""
        try:
            if excess is not None and excess != "":
                ev = float(excess)
                perf = f" · {ev:+.1f}% vs SPY"
        except (TypeError, ValueError):
            pass

        chamber = "Sen." if str(house).lower().startswith("sen") else "Rep."
        verb = "buys" if direction == Direction.bullish.value else \
               "sells" if direction == Direction.bearish.value else "trades"
        summary = f"{chamber} {rep} ({party}) {verb} {ticker} ({rng}){perf}"

        published = None
        if tdate:
            try:
                published = datetime.fromisoformat(tdate).replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        # Stable URL/dedup key per (rep, ticker, date, txn).
        key = f"congress://{rep}/{ticker}/{tdate}/{txn}".replace(" ", "_")
        item = upsert_item(
            session,
            source=source,
            title=summary[:300],
            url=key,
            content=summary,
            author=f"{rep} ({party}, {house})",
            media_type="congress",
            published_at=published,
        )
        if item is None:
            continue

        signal = Signal(
            item_id=item.id,
            signal_type=SignalType.funding.value,
            confidence=0.7,
            direction=direction,
            summary=summary,
            entities_json=f'{{"tickers": ["{ticker}"], "companies": [], "technologies": []}}',
            speculative=False,
        )
        session.add(signal)
        item.processed = True
        added += 1
    return added
