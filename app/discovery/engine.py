"""Orchestrate per-industry discovery: measure, retire, discover, score, refill."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.discovery import finder, recycler, scorer
from app.ingestion.common import normalize_url
from app.models import Industry, Source, SourceStatus, SourceType

logger = logging.getLogger(__name__)

_VALID_TYPES = {t.value for t in SourceType}


def _active_sources(session, industry: Industry) -> list[Source]:
    return [
        s
        for s in industry.sources
        if s.status == SourceStatus.active.value
    ]


def _exists(session, url: str, name: str, type_: str) -> bool:
    norm = normalize_url(url)
    if norm:
        if session.scalar(select(Source.id).where(Source.url == norm)):
            return True
    return bool(
        session.scalar(
            select(Source.id).where(Source.name == name, Source.type == type_)
        )
    )


def discover_for_industry(session, industry: Industry, limit: int | None = None) -> dict:
    """Fill an industry toward its target count with freshly vetted candidates."""
    active = _active_sources(session, industry)
    need = industry.target_source_count - len(active)
    if limit is not None:
        need = min(need, limit)
    if need <= 0:
        return {"industry": industry.name, "added": 0, "need": 0}

    existing_names = [s.name for s in active]
    candidates = finder.find_candidates(industry.name, existing_names)
    added = 0
    for cand in candidates:
        if added >= need:
            break
        type_ = str(cand.get("type", "")).lower()
        if type_ not in _VALID_TYPES:
            continue
        url = normalize_url(cand.get("url", ""))
        name = str(cand.get("name", "")).strip()
        if not name or _exists(session, cand.get("url", ""), name, type_):
            continue
        verdict = scorer.score_candidate(cand)
        if not verdict["keep"]:
            continue
        source = Source(
            name=name,
            type=type_,
            url=url,
            handle=str(cand.get("handle", "")),
            tags=industry.name,
            tier=verdict["tier"],
            vetting_score=verdict["score"],
            discovered_by="perplexity",
            status=SourceStatus.active.value,
            active=True,
            last_scored=datetime.now(timezone.utc),
        )
        source.industries.append(industry)
        session.add(source)
        added += 1
    return {"industry": industry.name, "added": added, "need": need}


def run_discovery(session, max_active_industries: int | None = None) -> dict:
    """Full cycle across active industries: yields -> retire -> discover."""
    recycler.update_signal_yields(session)
    retired = recycler.retire_underperformers(session)

    industries = (
        session.execute(select(Industry).where(Industry.active.is_(True)))
        .scalars()
        .all()
    )
    if max_active_industries is not None:
        industries = industries[:max_active_industries]

    results = [discover_for_industry(session, ind) for ind in industries]
    summary = {
        "retired": retired,
        "added_total": sum(r["added"] for r in results),
        "per_industry": results,
    }
    logger.info("Discovery cycle: %s", summary)
    return summary
