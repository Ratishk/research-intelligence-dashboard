"""Two-stage classification: Haiku relevance pre-filter -> Sonnet signal extract.

Stage 1 (cheap): Haiku scores each item's investment relevance 0-1. Only items
at/above the tier threshold proceed to stage 2.

Stage 2 (expensive): Sonnet extracts a structured Signal — type, direction,
confidence, summary, and entities (companies/tickers/technologies). Signals from
Tier 2 sources are flagged ``speculative``.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from app.config import config
from app.llm import claude
from app.models import Direction, Industry, Item, Signal, SignalType, Source

logger = logging.getLogger(__name__)

_RELEVANCE_SYSTEM = (
    "You are a triage filter for an investment research pipeline tracking frontier "
    "technology and industrial sectors (semiconductors, AI, energy, biotech, "
    "defense, materials, macro, and more). Given a content item, rate how likely "
    "it contains a market-relevant signal — a concrete development affecting a "
    "company, technology, or sector. Respond with ONLY a JSON object: "
    '{"relevance": <float 0..1>}. High = specific deployments, deals, results, '
    "funding, supply-chain shifts. Low = generic commentary, opinion, or noise."
)

_SIGNAL_SYSTEM = (
    "You are a senior analyst extracting investment signals from research content "
    "across all industries. Classify the item into exactly one signal_type:\n"
    "- commercial_deployment: a company is buying/installing/shipping/selling a product\n"
    "- pilot: lab result, demo, patent, prototype, or government-funded project\n"
    "- funding: new capital, grants, M&A, or state support\n"
    "- bottleneck: supply-chain, capacity, materials, or logistics constraint\n"
    "- displacement: evidence one approach/company is replacing an incumbent\n"
    "- none: no actionable signal\n\n"
    "Return ONLY JSON with keys: signal_type, confidence (0..1), direction "
    "(bullish|bearish|neutral), summary (one sentence), entities (object with "
    "arrays companies, tickers, technologies). Tickers must be valid exchange "
    "symbols you are confident about; otherwise leave the array empty."
)

_VALID_TYPES = {t.value for t in SignalType}
_VALID_DIRECTIONS = {d.value for d in Direction}

# Cached lowercase set of tracked entities for the free heuristic pre-filter.
_terms_cache: tuple[float, set[str]] | None = None
_TERMS_TTL = 1800  # 30 min

# Always-relevant macro/market keywords so important non-ticker news survives
# the pre-filter even when it names no watched company.
_KEYWORDS = {
    "fed", "fomc", "inflation", "cpi", "rate cut", "rate hike", "recession",
    "tariff", "earnings", "ipo", "acquisition", "merger", "bankruptcy",
    "semiconductor", "chip", "ai ", "artificial intelligence", "euv",
    "lithography", "gpu", "data center", "nuclear", "battery", "biotech",
    "fda", "clinical", "defense", "quantum",
}


def _tracked_terms(session) -> set[str]:
    """Lowercase tickers + company names + keywords we care about."""
    global _terms_cache
    import time
    if _terms_cache and (time.monotonic() - _terms_cache[0]) < _TERMS_TTL:
        return _terms_cache[1]
    from app.models import Ticker, WatchlistItem
    terms: set[str] = set(_KEYWORDS)
    for row in session.execute(select(WatchlistItem.ticker_symbol)).all():
        if row[0]:
            terms.add(row[0].lower())
    for t in session.execute(select(Ticker)).scalars().all():
        if t.name:
            # first token of the company name (e.g. "NVIDIA" from "NVIDIA Corp")
            terms.add(t.name.split()[0].lower())
    _terms_cache = (time.monotonic(), terms)
    return terms


def _mentions_tracked(item: Item, terms: set[str]) -> bool:
    blob = f"{item.title} {item.content[:1500]}".lower()
    return any(term in blob for term in terms)


def score_relevance(item: Item) -> float:
    # Shorter input than before (title + 600 chars) — barely affects the
    # relevance judgment but cuts Haiku token cost ~5×.
    text = f"Title: {item.title}\n\n{item.content[:600]}"
    raw = claude.complete(
        system=_RELEVANCE_SYSTEM, user=text, model=config.HAIKU_MODEL, max_tokens=64
    )
    data = claude.extract_json(raw)
    try:
        return max(0.0, min(1.0, float(data.get("relevance", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def _resolve_industry(session, source: Source) -> Industry | None:
    """Attribute a signal to the source's primary (first) industry."""
    if source.industries:
        return source.industries[0]
    return None


def classify_item(session, item: Item) -> Signal | None:
    source = item.source
    text = f"Title: {item.title}\nSource: {source.name}\n\n{item.content[:6000]}"
    raw = claude.complete(
        system=_SIGNAL_SYSTEM, user=text, model=config.SONNET_MODEL, max_tokens=512
    )
    data = claude.extract_json(raw)
    if not data:
        return None

    signal_type = data.get("signal_type", "none")
    if signal_type not in _VALID_TYPES:
        signal_type = "none"
    if signal_type == "none":
        return None

    direction = data.get("direction", "neutral")
    if direction not in _VALID_DIRECTIONS:
        direction = "neutral"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.5:
        return None

    industry = _resolve_industry(session, source)
    signal = Signal(
        item_id=item.id,
        industry_id=industry.id if industry else None,
        signal_type=signal_type,
        confidence=confidence,
        direction=direction,
        summary=str(data.get("summary", ""))[:1000],
        entities_json=json.dumps(data.get("entities", {})),
        speculative=(source.tier >= 2),
    )
    session.add(signal)
    return signal


# Daily LLM-classification counter for the cost cap (resets each UTC day).
_classify_count: dict[str, int] = {"date": "", "n": 0}


def _llm_budget_left(cap: int) -> int:
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _classify_count["date"] != today:
        _classify_count.update(date=today, n=0)
    return max(0, cap - _classify_count["n"])


def run_classification(session, batch_limit: int = 200) -> dict[str, int]:
    """Process unprocessed items: heuristic pre-filter (free) → Haiku relevance →
    Sonnet signal extraction, bounded by a daily LLM-call cap to control cost."""
    tier = config.tier()
    threshold = tier.sonnet_relevance_threshold
    items = (
        session.execute(
            select(Item).where(Item.processed.is_(False)).limit(batch_limit)
        )
        .scalars()
        .all()
    )
    terms = _tracked_terms(session) if tier.prefilter else None
    budget = _llm_budget_left(tier.classify_cap_per_day)
    stats = {"scored": 0, "classified": 0, "signals": 0, "skipped": 0}
    for item in items:
        # Free heuristic pre-filter: items naming no tracked entity never hit the LLM.
        if terms is not None and not _mentions_tracked(item, terms):
            item.relevance = 0.0
            item.processed = True
            stats["skipped"] += 1
            continue
        if budget <= 0:
            break  # daily cost cap reached — leave the rest for the next run/day
        relevance = score_relevance(item)
        item.relevance = relevance
        item.processed = True
        budget -= 1
        _classify_count["n"] += 1
        stats["scored"] += 1
        if relevance >= threshold:
            stats["classified"] += 1
            signal = classify_item(session, item)
            if signal is not None:
                stats["signals"] += 1
    logger.info("Classification: %s (threshold=%.2f, tier=%s)", stats, threshold, config.COST_TIER)
    return stats
