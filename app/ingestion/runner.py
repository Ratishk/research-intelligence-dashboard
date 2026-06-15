"""Orchestrates a full ingestion pass across all source types."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.database import session_scope
from app.ingestion import (
    biotech, bluesky, community, congress, dilution, events8k, fda, form4,
    form144, freshrss, govcontracts, institutional, kalshi, openfda, patent,
    prediction, rss, sec, twitter, unusual_whales, youtube,
)

logger = logging.getLogger(__name__)

# Bounded pool: enough to overlap the many network-bound ingesters without
# spawning a thread per source type. SEC ingesters share a process-wide token
# bucket (app.ingestion.sec_throttle) so parallelism stays within EDGAR's 10/s.
_MAX_WORKERS = 8


def _run_one(key: str, ingest_fn) -> tuple[str, int]:
    """Run one ingester in its OWN DB session (threads must not share a Session).

    Each ingester already isolates its own failures, but we still wrap the whole
    unit so a thread-level error (e.g. a session problem) can't abort the pass —
    it just yields a 0 count for that source type.
    """
    try:
        with session_scope() as session:
            return key, ingest_fn(session)
    except Exception:
        logger.exception("Ingestion failed for %s", key)
        return key, 0


def run_all_ingestion() -> dict[str, int]:
    """Run every ingester once, in parallel. Returns per-source-type counts.

    RSS uses the FreshRSS backbone when configured, otherwise direct feedparser.
    Each ingester runs in its own thread with its own DB session (SQLite sessions
    are not thread-safe to share); check_same_thread is False on the engine so the
    connection pool can be used across threads. Each ingester manages its own
    failures so one bad source can't abort the run.
    """
    jobs: list[tuple[str, object]] = []
    if freshrss.is_configured():
        jobs.append(("freshrss", freshrss.ingest_all))
    else:
        jobs.append(("rss", rss.ingest_all))
    jobs += [
        ("youtube", youtube.ingest_all),
        ("twitter", twitter.ingest_all),
        ("sec", sec.ingest_all),
        ("form4", form4.ingest_all),
        ("institutional", institutional.ingest_all),
        ("dilution", dilution.ingest_all),
        ("biotech", biotech.ingest_all),
        ("form8k", events8k.ingest_all),
        ("form144", form144.ingest_all),
        ("govcontract", govcontracts.ingest_all),
        ("fdarecall", fda.ingest_all),
        ("openfda", openfda.ingest_all),
        ("bluesky", bluesky.ingest_all),
        ("patent", patent.ingest_all),
        ("congress", congress.ingest_all),
        ("prediction", prediction.ingest_all),
        ("kalshi", kalshi.ingest_all),
        ("unusual_whales", unusual_whales.ingest_all),
        ("community", community.ingest_all),
    ]

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="ingest") as pool:
        futs = {pool.submit(_run_one, key, fn): key for key, fn in jobs}
        for fut in as_completed(futs):
            key, n = fut.result()
            counts[key] = n
    logger.info("Ingestion complete: %s", counts)
    return counts
