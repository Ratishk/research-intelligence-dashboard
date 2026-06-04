"""Orchestrates a full ingestion pass across all source types."""
from __future__ import annotations

import logging

from app.database import session_scope
from app.ingestion import community, freshrss, rss, sec, twitter, youtube

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
        counts["community"] = community.ingest_all(session)
    logger.info("Ingestion complete: %s", counts)
    return counts
