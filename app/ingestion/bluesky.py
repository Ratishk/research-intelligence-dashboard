"""Bluesky ingestion via the free, keyless public AppView API.

No authentication is required for public data. ``app.bsky.feed.getAuthorFeed``
on ``public.api.bsky.app`` returns an account's recent posts. We reuse the
"tweet" media_type so Bluesky posts surface in the existing Twitter/X feed tab,
serving as a free, no-auth replacement for the paid X/Twitter feed.

Web URL for a post: https://bsky.app/profile/{handle}/post/{rkey}
where rkey is the last path segment of the post AT-URI
(at://{did}/app.bsky.feed.post/{rkey}).
"""
from __future__ import annotations

import logging
from datetime import datetime

import requests

from app.ingestion.common import upsert_item
from app.models import Source

logger = logging.getLogger(__name__)

_ENDPOINT = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
_TIMEOUT = 20
_LIMIT = 15


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ingest_source(session, source: Source) -> int:
    """Fetch an author's recent Bluesky posts and upsert them."""
    handle = (source.handle or source.url or "").split("/")[-1].lstrip("@").strip()
    if not handle:
        return 0

    params = {
        "actor": handle,
        "limit": _LIMIT,
        "filter": "posts_no_replies",
    }
    try:
        resp = requests.get(_ENDPOINT, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        feed = resp.json().get("feed", [])
    except Exception:
        logger.exception("Bluesky getAuthorFeed failed for %s", handle)
        return 0

    added = 0
    for entry in feed:
        # Skip reposts: a repost carries a "reason" of type repost.
        if entry.get("reason"):
            continue
        post = entry.get("post") or {}
        record = post.get("record") or {}
        text = record.get("text", "")
        uri = post.get("uri", "")
        if not text or not uri:
            continue

        rkey = uri.split("/")[-1]
        url = f"https://bsky.app/profile/{handle}/post/{rkey}"
        item = upsert_item(
            session,
            source=source,
            title=text[:200],
            url=url,
            content=text,
            author=f"@{handle}",
            media_type="tweet",
            published_at=_parse_dt(record.get("createdAt")),
        )
        if item is not None:
            added += 1
    return added


def ingest_all(session) -> int:
    """Ingest all active Bluesky sources."""
    sources = (
        session.query(Source)
        .filter(Source.active.is_(True), Source.type == "bluesky")
        .all()
    )
    added = 0
    for source in sources:
        added += ingest_source(session, source)
    logger.info("Bluesky ingested %d new posts", added)
    return added
