"""Shared ingestion helpers: URL normalization, dedupe hashing, item upsert."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Source

# Tracking params we strip so the same article from two referrers dedupes.
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref")


def normalize_url(url: str) -> str:
    """Lowercase host, drop fragments and tracking query params, strip trailing /."""
    if not url:
        return ""
    try:
        parts = urlparse(url.strip())
    except ValueError:
        return url.strip()

    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    query = parts.query
    if query:
        kept = [
            kv
            for kv in query.split("&")
            if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PREFIXES)
        ]
        query = "&".join(sorted(kept))

    path = parts.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", query, ""))


def content_hash(url: str, fallback_text: str = "") -> str:
    """Stable dedupe key. Uses normalized URL when present, else text digest."""
    basis = normalize_url(url) or fallback_text.strip()
    return hashlib.sha256(basis.encode("utf-8", "ignore")).hexdigest()


def upsert_item(
    session: Session,
    *,
    source: Source,
    title: str,
    url: str,
    content: str = "",
    author: str = "",
    media_type: str = "article",
    published_at: datetime | None = None,
) -> Item | None:
    """Insert an item if its content_hash is new. Returns the Item or None if dup.

    The unique constraint on content_hash also guards against races; we check
    first to avoid a flush exception on the common duplicate path.
    """
    digest = content_hash(url, f"{source.id}:{title}")
    existing = session.scalar(select(Item.id).where(Item.content_hash == digest))
    if existing:
        return None

    item = Item(
        source_id=source.id,
        title=(title or "").strip()[:600],
        url=url.strip()[:800],
        content_hash=digest,
        content=(content or "").strip(),
        author=(author or "").strip()[:200],
        media_type=media_type,
        published_at=published_at,
    )
    session.add(item)
    source.last_crawled = datetime.now(timezone.utc)
    return item
