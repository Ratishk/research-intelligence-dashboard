"""Direct RSS/Atom ingestion via feedparser.

This is the default path and works with zero external infrastructure. When a
FreshRSS instance is configured (see ``freshrss.py``) the scheduler can use it
as a scalable backbone instead, but direct ingestion keeps the project runnable
out of the box.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import mktime

import feedparser

from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

# feedparser pulls full text from these keys in priority order.
_CONTENT_KEYS = ("content", "summary_detail", "summary", "description")
MAX_ITEMS_PER_FEED = 25


def _entry_text(entry) -> str:
    if entry.get("content"):
        try:
            return entry["content"][0].get("value", "")
        except (KeyError, IndexError, TypeError):
            pass
    for key in ("summary", "description"):
        if entry.get(key):
            return entry[key]
    return ""


def _entry_published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            try:
                return datetime.fromtimestamp(mktime(entry[key]), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def ingest_source(session, source: Source) -> int:
    """Fetch one RSS source's feed and upsert new entries. Returns count added."""
    if not source.url:
        return 0
    parsed = feedparser.parse(source.url)
    if parsed.bozo and not parsed.entries:
        logger.warning("RSS parse failed for %s: %s", source.url, parsed.bozo_exception)
        return 0

    added = 0
    for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
        link = entry.get("link", "")
        if not link:
            continue
        item = upsert_item(
            session,
            source=source,
            title=entry.get("title", ""),
            url=link,
            content=_entry_text(entry),
            author=entry.get("author", ""),
            media_type="article",
            published_at=_entry_published(entry),
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    """Ingest every active RSS/forum/arxiv source via direct feedparser."""
    feed_types = {
        SourceType.rss.value, SourceType.forum.value, SourceType.arxiv.value,
    }
    sources = [
        s
        for s in session.query(Source).filter(Source.active.is_(True)).all()
        if s.type in feed_types and s.url
    ]
    total = 0
    for source in sources:
        try:
            total += ingest_source(session, source)
        except Exception:  # one bad feed shouldn't kill the batch
            logger.exception("RSS ingest error for %s", source.name)
    return total
