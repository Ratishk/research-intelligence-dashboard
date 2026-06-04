"""X/Twitter ingestion via Grok live search (batched).

Instead of the Twitter API ($200/mo Basic tier), we use Grok's server-side X
search. Accounts are batched (config.tier().x_accounts_per_query) into single
queries to keep call volume — and cost — low.
"""
from __future__ import annotations

import logging

from app.config import config
from app.ingestion.common import upsert_item
from app.llm import grok
from app.models import Source, SourceType

logger = logging.getLogger(__name__)


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def ingest_all(session) -> int:
    """Batch-search all active X sources and upsert returned posts."""
    if not grok.is_configured():
        return 0
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.twitter.value)
        .all()
    )
    if not sources:
        return 0

    # Map handle -> source for attribution of returned posts.
    by_handle: dict[str, Source] = {}
    for s in sources:
        handle = (s.handle or s.url).split("/")[-1].lstrip("@").lower()
        if handle:
            by_handle[handle] = s

    batch_size = config.tier().x_accounts_per_query
    added = 0
    for batch in _chunks(list(by_handle.keys()), batch_size):
        topics = sorted(
            {
                t.strip()
                for h in batch
                for t in (by_handle[h].tags or "").split(",")
                if t.strip()
            }
        )[:6]
        try:
            results = grok.search_x_accounts(batch, topics)
        except Exception:
            logger.exception("Grok X search failed for batch %s", batch)
            continue
        for post in results:
            handle = str(post.get("handle", "")).lstrip("@").lower()
            source = by_handle.get(handle)
            if source is None or not post.get("text"):
                continue
            item = upsert_item(
                session,
                source=source,
                title=post["text"][:200],
                url=post.get("url", "") or f"https://x.com/{handle}",
                content=post["text"],
                author=f"@{handle}",
                media_type="tweet",
            )
            if item is not None:
                added += 1
    return added
