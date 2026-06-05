"""Orchestrates a full ingestion pass across all source types."""
from __future__ import annotations

import logging

from app.database import session_scope
from app.ingestion import (
    biotech, bluesky, community, congress, dilution, events8k, fda, form4,
    form144, freshrss, govcontracts, institutional, kalshi, patent, prediction,
    rss, sec, twitter, youtube,
)

logger = logging.getLogger(__name__)


def run_all_ingestion() -> dict[str, int]:
    """Run every ingester once. Returns per-source-type counts of new items.

    RSS uses the FreshRSS backbone when configured, otherwise direct feedparser.
    Each ingester manages its own failures so one bad source can't abort the run.
    """
    counts: dict[str, int] = {}
    with session_scope() as session:
        if freshrss.is_configured():
            counts["freshrss"] = freshrss.ingest_all(session)
        else:
            counts["rss"] = rss.ingest_all(session)
        counts["youtube"] = youtube.ingest_all(session)
        counts["twitter"] = twitter.ingest_all(session)
        counts["sec"] = sec.ingest_all(session)
        counts["form4"] = form4.ingest_all(session)
        counts["institutional"] = institutional.ingest_all(session)
        counts["dilution"] = dilution.ingest_all(session)
        counts["biotech"] = biotech.ingest_all(session)
        counts["form8k"] = events8k.ingest_all(session)
        counts["form144"] = form144.ingest_all(session)
        counts["govcontract"] = govcontracts.ingest_all(session)
        counts["fdarecall"] = fda.ingest_all(session)
        counts["bluesky"] = bluesky.ingest_all(session)
        counts["patent"] = patent.ingest_all(session)
        counts["congress"] = congress.ingest_all(session)
        counts["prediction"] = prediction.ingest_all(session)
        counts["kalshi"] = kalshi.ingest_all(session)
        counts["community"] = community.ingest_all(session)
    logger.info("Ingestion complete: %s", counts)
    return counts
