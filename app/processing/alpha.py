"""Non-consensus alpha scoring for signals.

Computes a 0-12 score from four components (0-3 each):
  timeliness   — how fresh the signal is
  originality  — how primary the source type is
  exclusivity  — how many other signals share the same entities
  contrarian   — whether the signal opposes the prevailing ticker direction

Scores are computed at request time, not stored, using a 60-second module-level
cache to avoid repeating the full-batch computation across rapid successive API
calls.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Signal

logger = logging.getLogger(__name__)

# Module-level cache: key -> (timestamp, result)
_cache: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 60  # seconds

# Source type → originality score
_ORIGINALITY: dict[str, int] = {
    "arxiv": 3, "patent": 3, "sec": 3, "form4": 3,
    "hackernews": 2, "forum": 2,
    "rss": 1, "youtube": 0, "twitter": 0, "reddit": 0,
}

_LABEL_MAP = ["Early", "Primary source", "Exclusive", "Contrarian"]


def _parse_entities(json_str: str) -> tuple[set[str], set[str]]:
    try:
        ents = json.loads(json_str or "{}")
    except json.JSONDecodeError:
        return set(), set()
    tickers = {str(t).upper() for t in ents.get("tickers", [])}
    techs   = {str(t).lower() for t in ents.get("technologies", [])}
    return tickers, techs


def _as_utc(dt: datetime | None) -> datetime | None:
    """SQLite stores naive datetimes; treat them as UTC for safe arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _timeliness(sig: Signal) -> int:
    created = _as_utc(sig.created_at)
    if not created:
        return 0
    age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    if age_h < 6:    return 3
    if age_h < 24:   return 2
    if age_h < 168:  return 1
    return 0


def _originality(sig: Signal) -> int:
    src_type = ""
    if sig.item and sig.item.source:
        src_type = sig.item.source.type or ""
    return _ORIGINALITY.get(src_type, 1)


def _score_signal(
    sig: Signal,
    all_entities: list[tuple[int, set[str], set[str]]],
    ticker_scores: dict[str, float],
    consensus: dict[str, float],
) -> dict:
    t_score = _timeliness(sig)
    o_score = _originality(sig)

    # Exclusivity: count signals sharing a ticker OR technology
    my_tickers, my_techs = _parse_entities(sig.entities_json)
    overlap = sum(
        1 for sid, ot, ott in all_entities
        if sid != sig.id and (my_tickers & ot or (my_techs & ott))
    )
    if overlap == 0:   e_score = 3
    elif overlap <= 2: e_score = 2
    elif overlap <= 5: e_score = 1
    else:              e_score = 0

    # Contrarian: signal direction vs. the *market consensus* for the ticker.
    # Prefer the real consensus vector (analyst+short+social); fall back to the
    # signal-flow score only when no consensus data exists for the ticker.
    c_score = 0
    for ticker in my_tickers:
        prev = consensus.get(ticker)
        if prev is None:
            prev = ticker_scores.get(ticker, 0.0)
        opposes = (prev > 0 and sig.direction == "bearish") or (
            prev < 0 and sig.direction == "bullish"
        )
        if abs(prev) > 0.4 and opposes:
            c_score = max(c_score, 3)
        elif abs(prev) > 0.15 and opposes:
            c_score = max(c_score, 2)
        elif abs(prev) > 0.1 and sig.direction == "neutral":
            c_score = max(c_score, 1)

    components = [t_score, o_score, e_score, c_score]
    total = sum(components)
    dominant_idx = components.index(max(components)) if total > 0 else 0
    label = _LABEL_MAP[dominant_idx]

    return {
        "total": total,
        "timeliness": t_score,
        "originality": o_score,
        "exclusivity": e_score,
        "contrarian": c_score,
        "label": label,
    }


def score_signals(
    signals: list[Signal],
    session,
    *,
    cache_key: str = "default",
) -> list[tuple[Signal, dict]]:
    """Score a list of signals. Returns list of (signal, score_dict) pairs.

    Builds the 7-day context window once and caches it for 60 seconds to avoid
    redundant DB round-trips across rapid successive calls.
    """
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        all_entities, ticker_scores, consensus = cached[1]
    else:
        # Build 7-day entity index
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        recent = session.execute(
            select(Signal).where(Signal.created_at >= cutoff)
        ).scalars().all()
        all_entities = [
            (s.id, *_parse_entities(s.entities_json)) for s in recent
        ]
        # Signal-flow score (fallback when no consensus data exists)
        ticker_buckets: dict[str, list] = {}
        for s in recent:
            tickers, _ = _parse_entities(s.entities_json)
            for t in tickers:
                ticker_buckets.setdefault(t, []).append(s.direction)
        ticker_scores: dict[str, float] = {}
        for sym, dirs in ticker_buckets.items():
            bull = dirs.count("bullish")
            bear = dirs.count("bearish")
            total = len(dirs)
            ticker_scores[sym] = (bull - bear) / total if total else 0.0
        # Real consensus vector from the Ticker table (analyst+short+social)
        from app.models import Ticker
        consensus = {
            t.symbol: t.consensus_score
            for t in session.execute(
                select(Ticker).where(Ticker.consensus_score.isnot(None))
            ).scalars().all()
        }
        _cache[cache_key] = (now, (all_entities, ticker_scores, consensus))

    return [
        (sig, _score_signal(sig, all_entities, ticker_scores, consensus))
        for sig in signals
    ]
