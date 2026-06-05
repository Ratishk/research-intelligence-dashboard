"""X / Twitter ingestion via the official X API v2 (Bearer / app-only auth).

Replaces the deprecated Grok live-search path. Accounts are batched into a
single ``from:`` recent-search query (the API allows OR-ing authors), and the
``author_id`` expansion maps each returned tweet back to its handle for
attribution. Recent search covers the last ~7 days.

Requires X_BEARER_TOKEN (Basic tier or above — recent search is not on the free
tier). Batching keeps call volume low to respect rate limits.
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from app.config import config
from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.twitter.com/2/tweets/search/recent"
_TIMEOUT = 25
_MAX_QUERY_LEN = 480  # API cap is 512; leave headroom for operators
MAX_RESULTS = 100     # per request (10..100)


def is_configured() -> bool:
    return bool(config.X_BEARER_TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.X_BEARER_TOKEN}"}


def _batch_handles(handles: list[str]) -> list[list[str]]:
    """Group handles so each OR-ed query stays under the length cap."""
    batches, cur, cur_len = [], [], 0
    for h in handles:
        piece = len(f"from:{h} OR ")
        if cur and cur_len + piece > _MAX_QUERY_LEN:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(h)
        cur_len += piece
    if cur:
        batches.append(cur)
    return batches


def _search(handles: list[str]) -> tuple[list[dict], dict[str, str]]:
    """Return (tweets, author_id->username) for a batch of handles."""
    query = "(" + " OR ".join(f"from:{h}" for h in handles) + ") -is:retweet -is:reply"
    params = {
        "query": query,
        "max_results": MAX_RESULTS,
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    try:
        resp = requests.get(_ENDPOINT, headers=_headers(), params=params, timeout=_TIMEOUT)
        if resp.status_code == 429:
            logger.warning("X API rate limited; skipping remaining batches")
            return [], {}
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("X API search failed for batch %s", handles[:3])
        return [], {}
    tweets = data.get("data", [])
    users = {
        u["id"]: u["username"]
        for u in data.get("includes", {}).get("users", [])
    }
    return tweets, users


def ingest_all(session) -> int:
    """Batch-search all active X sources and upsert returned posts."""
    if not is_configured():
        logger.info("X_BEARER_TOKEN not set; skipping Twitter ingestion")
        return 0

    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == SourceType.twitter.value)
        .all()
    )
    if not sources:
        return 0

    # Map lowercased handle -> source for attribution.
    by_handle: dict[str, Source] = {}
    for s in sources:
        handle = (s.handle or s.url).split("/")[-1].lstrip("@").lower()
        if handle:
            by_handle[handle] = s
    if not by_handle:
        return 0

    added = 0
    for batch in _batch_handles(list(by_handle.keys())):
        tweets, users = _search(batch)
        if not tweets:
            continue
        for tw in tweets:
            handle = users.get(tw.get("author_id"), "").lower()
            source = by_handle.get(handle)
            text = tw.get("text", "")
            if source is None or not text:
                continue
            published = None
            if tw.get("created_at"):
                try:
                    published = datetime.fromisoformat(tw["created_at"].replace("Z", "+00:00"))
                except ValueError:
                    pass
            tweet_id = tw.get("id", "")
            url = f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else f"https://x.com/{handle}"
            item = upsert_item(
                session,
                source=source,
                title=text[:200],
                url=url,
                content=text,
                author=f"@{handle}",
                media_type="tweet",
                published_at=published,
            )
            if item is not None:
                added += 1
    logger.info("X/Twitter ingested %d new tweets", added)
    return added
