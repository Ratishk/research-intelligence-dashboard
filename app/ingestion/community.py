"""Community signal ingestion: Hacker News (Algolia) + Reddit.

High-velocity, lower-authority sources. HN uses the free Algolia API; Reddit
uses PRAW when credentials are present, else falls back to public JSON.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from app.config import config
from app.ingestion.common import upsert_item
from app.models import Source, SourceType

logger = logging.getLogger(__name__)

_HN_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
_TIMEOUT = 20
HN_MIN_POINTS = 50
MAX_HITS = 20


def _ingest_hn(session, source: Source) -> int:
    """Query HN for recent high-score stories matching the source's keywords."""
    query = source.handle or source.tags or source.name
    try:
        resp = requests.get(
            _HN_ENDPOINT,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"points>{HN_MIN_POINTS}",
                "hitsPerPage": MAX_HITS,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except (requests.RequestException, ValueError):
        logger.exception("HN search failed for %s", source.name)
        return 0

    added = 0
    for hit in hits:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        published = None
        if hit.get("created_at_i"):
            published = datetime.fromtimestamp(hit["created_at_i"], tz=timezone.utc)
        item = upsert_item(
            session,
            source=source,
            title=hit.get("title", ""),
            url=url,
            content=hit.get("story_text") or hit.get("title", ""),
            author=hit.get("author", ""),
            media_type="thread",
            published_at=published,
        )
        if item is not None:
            added += 1
    return added


def _reddit_client():
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        return None
    try:
        import praw

        return praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
            check_for_async=False,
        )
    except Exception:
        logger.exception("Reddit client init failed")
        return None


def _ingest_reddit(session, source: Source) -> int:
    subreddit = (source.handle or source.url).rstrip("/").split("/")[-1].replace("r/", "")
    reddit = _reddit_client()
    added = 0
    if reddit is not None:
        try:
            for post in reddit.subreddit(subreddit).hot(limit=MAX_HITS):
                if post.stickied:
                    continue
                item = upsert_item(
                    session,
                    source=source,
                    title=post.title,
                    url=f"https://reddit.com{post.permalink}",
                    content=post.selftext or post.title,
                    author=str(post.author),
                    media_type="thread",
                    published_at=datetime.fromtimestamp(post.created_utc, tz=timezone.utc),
                )
                if item is not None:
                    added += 1
            return added
        except Exception:
            logger.exception("PRAW fetch failed for r/%s", subreddit)

    # Fallback: public JSON listing (no auth).
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/hot.json",
            headers={"User-Agent": config.REDDIT_USER_AGENT},
            params={"limit": MAX_HITS},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        children = resp.json().get("data", {}).get("children", [])
    except (requests.RequestException, ValueError):
        logger.exception("Reddit JSON fallback failed for r/%s", subreddit)
        return added
    for child in children:
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        item = upsert_item(
            session,
            source=source,
            title=d.get("title", ""),
            url=f"https://reddit.com{d.get('permalink', '')}",
            content=d.get("selftext") or d.get("title", ""),
            author=d.get("author", ""),
            media_type="thread",
            published_at=datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc),
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    total = 0
    for source in (
        session.query(Source).filter(Source.active.is_(True)).all()
    ):
        try:
            if source.type == SourceType.hackernews.value:
                total += _ingest_hn(session, source)
            elif source.type == SourceType.reddit.value:
                total += _ingest_reddit(session, source)
        except Exception:
            logger.exception("Community ingest error for %s", source.name)
    return total
